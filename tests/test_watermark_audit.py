"""unittest suite on entirely fictional watermark-review data.

The cases cover every acceptance trap called out in the task:

* nearest-distance alone misidentifies the source;
* custody stages are confused with sample batches;
* erasures are not matches;
* two fragments share one error while the budget stays satisfiable;
* too many low-confidence observations jointly veto an interpretation;
* a corrected catalog version never rewrites historical conclusions.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from watermark_audit.__main__ import main
from watermark_audit.errors import ValidationError
from watermark_audit.model import parse_case
from watermark_audit.report import build_report
from watermark_audit.solver import Solver

from tests.brute import brute_force

ROOT = Path(__file__).resolve().parents[1]


def copy(copy_id, batch, recipient, codeword, missing=None, custody=None):
    stages = custody if custody is not None else [
        {"stage": "capture", "recipient": recipient},
        {"stage": "storage", "recipient": recipient},
    ]
    return {
        "copy_id": copy_id,
        "batch": batch,
        "recipient": recipient,
        "codeword": {str(pos): sym for pos, sym in sorted(codeword.items())},
        "missing": sorted(missing or []),
        "custody": [{"copy_id": copy_id, "stages": stages}],
    }


def obs(position, symbol=None, erased=False, confidence="low", cost=0):
    data = {"position": position, "erased": erased, "confidence": confidence}
    if erased:
        # Erased observations normally omit the symbol; tests that want to prove
        # validation rejects a symbol on an erasure pass one explicitly.
        if symbol is not None:
            data["symbol"] = symbol
    else:
        data["symbol"] = symbol
        if confidence == "low":
            data["cost"] = cost
    return data


def fragment(fid, batches, candidates, observations, stage="capture"):
    return {
        "fragment_id": fid,
        "batch_candidates": batches,
        "candidate_copies": candidates,
        "event_stage": stage,
        "observations": observations,
    }


def _clean_copy(copy_dict):
    return {k: v for k, v in copy_dict.items() if k != "custody"}


def _custody(copy_dicts):
    return [
        {"copy_id": c["copy_id"], "stages": c["custody"][0]["stages"]}
        for c in copy_dicts
    ]


def version(version_id, released_at, note, copies):
    return {
        "version": version_id,
        "released_at": released_at,
        "note": note,
        "copies": [_clean_copy(c) for c in copies],
        "custody": _custody(copies),
    }


def make_case(
    copies,
    fragments,
    same_pairs=None,
    diff_pairs=None,
    per_fragment_cap=None,
    total_budget=None,
    batch_caps=None,
    extra_versions=None,
    pinned="v1",
    case_id="case-1",
    default_stage=None,
):
    catalog = {
        "pinned_version": pinned,
        "versions": [
            {
                "version": "v1",
                "released_at": "2026-01-01",
                "note": "发行登记",
                "copies": [_clean_copy(c) for c in copies],
                "custody": _custody(copies),
            }
        ]
        + list(extra_versions or []),
    }
    doc = {
        "case_id": case_id,
        "catalog": catalog,
        "fragments": fragments,
        "same_source_pairs": same_pairs or [],
        "different_source_pairs": diff_pairs or [],
    }
    if default_stage is not None:
        doc["default_event_stage"] = default_stage
    if per_fragment_cap is not None:
        doc["per_fragment_cost_cap"] = per_fragment_cap
    if total_budget is not None:
        doc["total_budget"] = total_budget
    if batch_caps is not None:
        doc["batch_recipient_caps"] = batch_caps
    return doc


class NearestDistanceTrapTest(unittest.TestCase):
    """Nearest-distance picks the wrong copy; the optimizer must not."""

    def setUp(self):
        self.copies = [
            # c1: one low-confidence disagreement (cost 5), everything else fits.
            copy("c1", "B", "R1", {0: "a", 1: "b", 2: "c", 3: "d"}),
            # c2: perfect match on three symbols but an unrecoverable high conflict.
            copy("c2", "B", "R2", {0: "a", 1: "b", 2: "X", 3: "d"}),
        ]
        self.fragments = [
            fragment(
                "f1",
                ["B"],
                ["c1", "c2"],
                [
                    obs(0, "a", confidence="high"),
                    obs(1, "b", confidence="high"),
                    obs(2, "c", confidence="high"),
                    obs(3, "z", confidence="low", cost=5),
                ],
            )
        ]

    def test_high_conflict_rules_out_nearest(self):
        case = parse_case(make_case(self.copies, self.fragments))
        report = build_report(case)
        self.assertEqual(report["status"], "feasible")
        self.assertEqual(report["report"]["tie_break_tuple"], ["c1"])
        self.assertEqual(report["report"]["total_cost"], 5)
        detail = report["report"]["fragments"][0]
        self.assertEqual(detail["copy_id"], "c1")
        self.assertEqual(detail["responsible_recipient"], "R1")

    def test_matches_brute_force(self):
        case = parse_case(make_case(self.copies, self.fragments))
        brute = brute_force(case)
        self.assertTrue(brute.feasible)
        solver = Solver(case)
        leaf = solver.find_optimal()
        self.assertEqual(leaf["assignment_tuple"], brute.optimal["assignment_tuple"])
        self.assertEqual(leaf["total_cost"], brute.optimal["total_cost"])
        self.assertEqual(leaf["distinct_copies"], brute.optimal["distinct_copies"])


class CustodyBatchConfusionTest(unittest.TestCase):
    """The responsible party comes from the custody stage, not the batch label."""

    def setUp(self):
        self.copies = [
            copy(
                "c1",
                "batch-alpha",
                "R-original",
                {0: "a", 1: "b"},
                custody=[
                    {"stage": "capture", "recipient": "R-capture"},
                    {"stage": "storage", "recipient": "R-storage"},
                    {"stage": "court", "recipient": "R-court"},
                ],
            )
        ]
        self.fragments = [
            fragment(
                "f1",
                ["batch-alpha"],
                ["c1"],
                [obs(0, "a", confidence="high"), obs(1, "b", confidence="high")],
                stage="storage",
            )
        ]

    def test_attribution_uses_event_stage_holder(self):
        case = parse_case(make_case(self.copies, self.fragments))
        report = build_report(case)
        detail = report["report"]["fragments"][0]
        self.assertEqual(detail["event_stage"], "storage")
        self.assertEqual(detail["responsible_recipient"], "R-storage")
        self.assertEqual(
            report["report"]["responsible_recipients"], ["R-storage"]
        )
        # The batch id and registered recipient must not be mistaken for the holder.
        self.assertNotIn("batch-alpha", report["report"]["responsible_recipients"])
        self.assertNotIn("R-original", report["report"]["responsible_recipients"])

    def test_stage_merge_split_is_versioned(self):
        # New catalog version splits storage into two stages, correcting the chain.
        v2_copies = [
            copy(
                "c1",
                "batch-alpha",
                "R-original",
                {0: "a", 1: "b"},
                custody=[
                    {"stage": "capture", "recipient": "R-capture"},
                    {"stage": "storage-in", "recipient": "R-storage"},
                    {"stage": "storage-out", "recipient": "R-vault"},
                    {"stage": "court", "recipient": "R-court"},
                ],
            )
        ]
        v2 = version("v2", "2026-06-01", "保管补录：拆分存储阶段", v2_copies)
        doc = make_case(self.copies, self.fragments, extra_versions=[v2], pinned="v1")
        case = parse_case(doc)
        report = build_report(case)
        # The historical conclusion still renders the pinned v1 view.
        self.assertEqual(report["catalog"]["pinned_version"], "v1")
        pinned = report["catalog"]["pinned_view"]
        self.assertEqual(
            [h["stage"] for h in pinned["copies"][0]["custody_chain"]],
            ["capture", "storage", "court"],
        )
        self.assertEqual(
            report["report"]["fragments"][0]["responsible_recipient"], "R-storage"
        )
        versions = {v["version"]: v for v in report["catalog"]["versions"]}
        self.assertTrue(versions["v2"]["pinned"] is False)


class ErasureNotMatchTest(unittest.TestCase):
    def setUp(self):
        self.copies = [
            # c1 allows position 2 to be missing; c2 does not.
            copy("c1", "B", "R1", {0: "a", 1: "b", 2: "c"}, missing=[2]),
            copy("c2", "B", "R2", {0: "a", 1: "b", 2: "c"}),
        ]

    def test_erasure_requires_allowed_missing(self):
        fragments = [
            fragment(
                "f1",
                ["B"],
                ["c1", "c2"],
                [
                    obs(0, "a", confidence="high"),
                    obs(1, "b", confidence="high"),
                    obs(2, erased=True),
                ],
            )
        ]
        case = parse_case(make_case(self.copies, fragments))
        report = build_report(case)
        self.assertEqual(report["report"]["tie_break_tuple"], ["c1"])
        self.assertEqual(report["report"]["total_cost"], 0)
        self.assertEqual(
            report["report"]["fragments"][0]["erased_positions"], [2]
        )
        # c2 is impossible because its registered position 2 cannot be erased.
        verdicts = {v["recipient"]: v["verdict"] for v in report["audit"]["recipient_verdicts"]}
        self.assertEqual(verdicts["R2"], "impossible")

    def test_erased_observation_rejects_symbol_and_cost(self):
        bad = make_case(
            self.copies,
            [
                fragment(
                    "f1",
                    ["B"],
                    ["c1"],
                    [obs(0, "a", confidence="high"), obs(2, symbol="c", erased=True)],
                )
            ],
        )
        with self.assertRaises(ValidationError) as ctx:
            parse_case(bad)
        self.assertIn("symbol", ctx.exception.field)

    def test_erasure_on_present_position_blocks_copy(self):
        fragments = [
            fragment(
                "f1",
                ["B"],
                ["c2"],
                [
                    obs(0, "a", confidence="high"),
                    obs(1, "b", confidence="high"),
                    obs(2, erased=True),
                ],
            )
        ]
        case = parse_case(make_case(self.copies, fragments))
        solver = Solver(case)
        self.assertFalse(solver.is_feasible())
        report = build_report(case)
        self.assertEqual(report["status"], "infeasible")
        self.assertIn("f1", report["conflict"]["conflict_fragments"])


class SharedErrorBudgetTest(unittest.TestCase):
    """Two fragments share one observed error; it is charged once, not twice."""

    def setUp(self):
        self.copies = [
            copy("c1", "B", "R1", {0: "a", 1: "b", 2: "c"}),
            copy("c2", "B", "R2", {0: "a", 1: "b", 2: "d"}),
            # c3 genuinely registers d at position 2, so an observed d is a
            # perfect match there and the only error-free alternative.
            copy("c3", "B", "R3", {0: "a", 1: "b", 2: "d"}),
        ]
        self.fragments = [
            fragment(
                "f1",
                ["B"],
                ["c1", "c2", "c3"],
                [
                    obs(0, "a", confidence="high"),
                    obs(1, "b", confidence="high"),
                    obs(2, "d", confidence="low", cost=7),
                ],
            ),
            fragment(
                "f2",
                ["B"],
                ["c1", "c2", "c3"],
                [
                    obs(0, "a", confidence="high"),
                    obs(2, "d", confidence="low", cost=7),
                ],
            ),
        ]

    def test_same_source_charges_once(self):
        # Restrict both fragments to c1: the observed d is one logical error
        # (position 2, symbol d) seen twice, charged once at cost 7.
        fragments = [
            fragment(
                "f1", ["B"], ["c1"],
                [
                    obs(0, "a", confidence="high"),
                    obs(1, "b", confidence="high"),
                    obs(2, "d", confidence="low", cost=7),
                ],
            ),
            fragment(
                "f2", ["B"], ["c1"],
                [
                    obs(0, "a", confidence="high"),
                    obs(2, "d", confidence="low", cost=7),
                ],
            ),
        ]
        doc = make_case(
            self.copies,
            fragments,
            same_pairs=[["f1", "f2"]],
            total_budget=7,
        )
        case = parse_case(doc)
        report = build_report(case)
        self.assertEqual(report["status"], "feasible")
        self.assertEqual(report["report"]["total_cost"], 7)
        self.assertEqual(report["report"]["distinct_copies"], 1)
        self.assertEqual(report["report"]["tie_break_tuple"], ["c1", "c1"])
        # Exactly one fragment carries the charge; the other shows charged=false.
        charged = [
            f for f in report["report"]["fragments"]
            if any(m["charged"] for m in f["mismatches"])
        ]
        self.assertEqual(len(charged), 1)
        self.assertEqual(charged[0]["fragment_id"], "f1")

    def test_double_charge_would_break_budget(self):
        # Two distinct copies both register c at position 2; forced onto
        # different copies, the observed d becomes two independent charged
        # errors (7 + 7), so the shared budget 7 fails.
        copies = [
            copy("c1", "B", "R1", {0: "a", 1: "b", 2: "c"}),
            copy("c4", "B", "R4", {0: "a", 1: "b", 2: "c"}),
        ]
        fragments = [
            fragment(
                "f1", ["B"], ["c1", "c4"],
                [
                    obs(0, "a", confidence="high"),
                    obs(1, "b", confidence="high"),
                    obs(2, "d", confidence="low", cost=7),
                ],
            ),
            fragment(
                "f2", ["B"], ["c1", "c4"],
                [
                    obs(0, "a", confidence="high"),
                    obs(2, "d", confidence="low", cost=7),
                ],
            ),
        ]
        doc = make_case(
            copies,
            fragments,
            diff_pairs=[["f1", "f2"]],
            total_budget=7,
        )
        case = parse_case(doc)
        report = build_report(case)
        self.assertEqual(report["status"], "infeasible")

    def test_cheapest_reported_cost_wins(self):
        fragments = [
            fragment(
                "f1",
                ["B"],
                ["c1"],
                [
                    obs(0, "a", confidence="high"),
                    obs(2, "d", confidence="low", cost=9),
                ],
            ),
            fragment(
                "f2",
                ["B"],
                ["c1"],
                [
                    obs(1, "b", confidence="high"),
                    obs(2, "d", confidence="low", cost=4),
                ],
            ),
        ]
        case = parse_case(make_case(self.copies, fragments, same_pairs=[["f1", "f2"]]))
        report = build_report(case)
        self.assertEqual(report["report"]["total_cost"], 4)

    def test_matches_brute_force(self):
        case = parse_case(
            make_case(self.copies, self.fragments, same_pairs=[["f1", "f2"]],
                      total_budget=7)
        )
        brute = brute_force(case)
        leaf = Solver(case).find_optimal()
        self.assertTrue(brute.feasible)
        self.assertEqual(leaf["assignment_tuple"], brute.optimal["assignment_tuple"])
        self.assertEqual(leaf["total_cost"], brute.optimal["total_cost"])


class LowConfidenceJointVetoTest(unittest.TestCase):
    """Many cheap low-confidence disagreements jointly exhaust the caps."""

    def setUp(self):
        self.copies = [
            copy("c1", "B", "R1", {i: "x" for i in range(5)}),
            copy("c2", "B", "R2", {i: "y" for i in range(5)}),
        ]

    def _fragments(self):
        return [
            fragment(
                "f1",
                ["B"],
                ["c1", "c2"],
                [obs(i, "z", confidence="low", cost=2) for i in range(5)],
            )
        ]

    def test_per_fragment_cap_vetoes(self):
        case = parse_case(make_case(self.copies, self._fragments(), per_fragment_cap=8))
        solver = Solver(case)
        # c2 also mismatches on every position (x/y vs z), so no copy survives.
        self.assertFalse(solver.is_feasible())

    def test_total_budget_vetoes_at_leaf(self):
        case = parse_case(make_case(self.copies, self._fragments(), total_budget=8))
        report = build_report(case)
        self.assertEqual(report["status"], "infeasible")
        core = report["conflict"]["conflict_fragments"]
        self.assertEqual(core, ["f1"])

    def test_budget_just_above_passes(self):
        case = parse_case(make_case(self.copies, self._fragments(), total_budget=10))
        report = build_report(case)
        self.assertEqual(report["status"], "feasible")
        self.assertEqual(report["report"]["total_cost"], 10)


class VersionCorrectionHistoryTest(unittest.TestCase):
    """A corrected catalog never rewrites the pinned historical conclusion."""

    def _copies_v1(self):
        return [
            copy(
                "c1",
                "B",
                "R-wrong",
                {0: "a", 1: "b"},
                custody=[
                    {"stage": "capture", "recipient": "R-wrong"},
                    {"stage": "storage", "recipient": "R-wrong"},
                ],
            )
        ]

    def _copies_v2(self):
        return [
            copy(
                "c1",
                "B",
                "R-wrong",
                {0: "a", 1: "b"},
                custody=[
                    {"stage": "capture", "recipient": "R-correct"},
                    {"stage": "storage", "recipient": "R-correct"},
                ],
            )
        ]

    def test_pinned_v1_keeps_historical_holder(self):
        fragments = [
            fragment(
                "f1",
                ["B"],
                ["c1"],
                [obs(0, "a", confidence="high"), obs(1, "b", confidence="high")],
                stage="storage",
            )
        ]
        v2 = version("v2", "2026-09-01", "纠正接收人登记", self._copies_v2())
        doc = make_case(self._copies_v1(), fragments, extra_versions=[v2], pinned="v1")
        report = build_report(parse_case(doc))
        self.assertEqual(
            report["report"]["fragments"][0]["responsible_recipient"], "R-wrong"
        )
        self.assertEqual(report["catalog"]["pinned_view"]["version"], "v1")

        # Re-running pinned against v2 yields the corrected holder.
        doc["catalog"]["pinned_version"] = "v2"
        report_v2 = build_report(parse_case(doc))
        self.assertEqual(
            report_v2["report"]["fragments"][0]["responsible_recipient"], "R-correct"
        )


class BatchRecipientCapTest(unittest.TestCase):
    def test_cap_forces_separate_batches_attribution(self):
        copies = [
            copy(
                "c1",
                "B",
                "R1",
                {0: "a"},
                custody=[{"stage": "capture", "recipient": "R1"}],
            ),
            copy(
                "c2",
                "B",
                "R2",
                {0: "a"},
                custody=[{"stage": "capture", "recipient": "R2"}],
            ),
        ]
        fragments = [
            fragment("f1", ["B"], ["c1", "c2"], [obs(0, "a", confidence="high")]),
            fragment("f2", ["B"], ["c1", "c2"], [obs(0, "a", confidence="high")]),
        ]
        doc = make_case(copies, fragments, diff_pairs=[["f1", "f2"]], batch_caps={"B": 1})
        case = parse_case(doc)
        # Two distinct recipients needed for one batch, cap 1 -> impossible.
        self.assertFalse(Solver(case).is_feasible())

        # Raising the cap restores feasibility.
        doc["batch_recipient_caps"] = {"B": 2}
        report = build_report(parse_case(doc))
        self.assertEqual(report["status"], "feasible")
        self.assertEqual(sorted(report["report"]["responsible_recipients"]), ["R1", "R2"])


class SameSourceAlignmentTest(unittest.TestCase):
    def test_high_contradiction_breaks_same_source(self):
        copies = [
            copy("c1", "B", "R1", {0: "a", 1: "b"}),
            copy("c2", "B", "R2", {0: "a", 1: "c"}),
        ]
        fragments = [
            fragment("f1", ["B"], ["c1", "c2"], [obs(0, "a", confidence="high"),
                                                  obs(1, "b", confidence="high")]),
            fragment("f2", ["B"], ["c1", "c2"], [obs(1, "c", confidence="high")]),
        ]
        case = parse_case(make_case(copies, fragments, same_pairs=[["f1", "f2"]]))
        # Same copy required, but f1 needs symbol b and f2 needs c at position 1.
        self.assertFalse(Solver(case).is_feasible())

    def test_independent_observations_not_merged(self):
        # Different wrong symbols at the same position are two distinct errors.
        copies = [copy("c1", "B", "R1", {0: "a", 1: "b"})]
        fragments = [
            fragment("f1", ["B"], ["c1"], [obs(1, "p", confidence="low", cost=3)]),
            fragment("f2", ["B"], ["c1"], [obs(1, "q", confidence="low", cost=4)]),
        ]
        case = parse_case(make_case(copies, fragments, same_pairs=[["f1", "f2"]]))
        report = build_report(case)
        self.assertEqual(report["report"]["total_cost"], 7)


class AuditVerdictTest(unittest.TestCase):
    def test_non_unique_requires_two_witnesses(self):
        copies = [
            copy("c1", "B", "R1", {0: "a"},
                 custody=[{"stage": "capture", "recipient": "R1"}]),
            copy("c2", "B", "R2", {0: "a"},
                 custody=[{"stage": "capture", "recipient": "R2"}]),
        ]
        fragments = [
            fragment("f1", ["B"], ["c1", "c2"], [obs(0, "a", confidence="high")])
        ]
        report = build_report(parse_case(make_case(copies, fragments)))
        audit = report["audit"]
        self.assertFalse(audit["unique_attribution"])
        self.assertIsNone(audit["unique_recipient"])
        self.assertGreaterEqual(len(audit["witnesses"]), 2)
        holders = {tuple(w["responsible_recipients"]) for w in audit["witnesses"]}
        self.assertIn(("R1",), holders)
        self.assertIn(("R2",), holders)
        verdicts = {v["recipient"]: v["verdict"] for v in audit["recipient_verdicts"]}
        self.assertEqual(verdicts["R1"], "possible")
        self.assertEqual(verdicts["R2"], "possible")

    def test_unique_attribution_when_all_roads_lead_same(self):
        copies = [
            copy("c1", "B", "R1", {0: "a"},
                 custody=[{"stage": "capture", "recipient": "R1"}]),
            copy("c2", "B", "R1", {0: "a"},
                 custody=[{"stage": "capture", "recipient": "R1"}]),
        ]
        fragments = [
            fragment("f1", ["B"], ["c1", "c2"], [obs(0, "a", confidence="high")])
        ]
        report = build_report(parse_case(make_case(copies, fragments)))
        self.assertTrue(report["audit"]["unique_attribution"])
        self.assertEqual(report["audit"]["unique_recipient"], "R1")

    def test_conflict_trace_descending_id_order(self):
        copies = [copy("c1", "B", "R1", {0: "a", 1: "b"})]
        # Each fragment contradicts c1 on its own position.  Tried in descending
        # order, f2 is removed first (the f1-only remainder is still infeasible);
        # f1 is then indispensable because deleting it leaves an empty feasible set.
        fragments = [
            fragment("f1", ["B"], ["c1"], [obs(0, "z", confidence="high")]),
            fragment("f2", ["B"], ["c1"], [obs(1, "z", confidence="high")]),
        ]
        report = build_report(parse_case(make_case(copies, fragments)))
        self.assertEqual(report["status"], "infeasible")
        trace = report["conflict"]["deletion_trace"]
        self.assertEqual([step["trial_fragment"] for step in trace], ["f2", "f1"])
        self.assertEqual(trace[0]["action"], "removed")
        self.assertEqual(trace[1]["action"], "kept")
        self.assertEqual(report["conflict"]["conflict_fragments"], ["f1"])

    def test_conflict_core_keeps_jointly_contradicting_pair(self):
        # Two fragments are only contradictory together: each alone is feasible.
        # A same-source group with a high/high symbol clash behaves exactly so.
        copies = [
            copy("c1", "B", "R1", {0: "a", 1: "b"}),
            copy("c2", "B", "R2", {0: "a", 1: "c"}),
        ]
        fragments = [
            fragment("f1", ["B"], ["c1", "c2"],
                     [obs(0, "a", confidence="high"), obs(1, "b", confidence="high")]),
            fragment("f2", ["B"], ["c1", "c2"],
                     [obs(1, "c", confidence="high")]),
        ]
        doc = make_case(copies, fragments, same_pairs=[["f1", "f2"]])
        report = build_report(parse_case(doc))
        self.assertEqual(report["status"], "infeasible")
        self.assertEqual(report["conflict"]["conflict_fragments"], ["f1", "f2"])
        self.assertTrue(
            all(step["action"] == "kept"
                for step in report["conflict"]["deletion_trace"])
        )

    def test_conflict_core_drops_redundant_fragment(self):
        # Two fragments carry the very same contradiction; one alone suffices,
        # so the descending procedure removes the redundant lower-priority one.
        copies = [copy("c1", "B", "R1", {0: "a"})]
        fragments = [
            fragment("f1", ["B"], ["c1"], [obs(0, "z", confidence="high")]),
            fragment("f2", ["B"], ["c1"], [obs(0, "z", confidence="high")]),
        ]
        report = build_report(parse_case(make_case(copies, fragments)))
        self.assertEqual(report["conflict"]["conflict_fragments"], ["f1"])
        trace = {step["trial_fragment"]: step for step in report["conflict"]["deletion_trace"]}
        self.assertEqual(trace["f2"]["action"], "removed")
        self.assertEqual(trace["f1"]["action"], "kept")


class ValidationTest(unittest.TestCase):
    def _base(self):
        return make_case(
            [copy("c1", "B", "R1", {0: "a"})],
            [fragment("f1", ["B"], ["c1"], [obs(0, "a", confidence="high")])],
        )

    def test_unknown_field_points_to_exact_path(self):
        doc = self._base()
        doc["fragments"][0]["bogus"] = 1
        with self.assertRaises(ValidationError) as ctx:
            parse_case(doc)
        self.assertEqual(ctx.exception.field, "$.fragments[0].bogus")

    def test_bad_type_field(self):
        doc = self._base()
        doc["total_budget"] = "lots"
        with self.assertRaises(ValidationError) as ctx:
            parse_case(doc)
        self.assertEqual(ctx.exception.field, "$.total_budget")

    def test_missing_stage_without_default(self):
        doc = self._base()
        del doc["fragments"][0]["event_stage"]
        with self.assertRaises(ValidationError) as ctx:
            parse_case(doc)
        self.assertIn("event_stage", ctx.exception.field)

    def test_default_event_stage_works(self):
        doc = self._base()
        del doc["fragments"][0]["event_stage"]
        doc["default_event_stage"] = "capture"
        case = parse_case(doc)
        self.assertEqual(case.fragments[0].event_stage, "capture")

    def test_pinned_version_must_exist(self):
        doc = self._base()
        doc["catalog"]["pinned_version"] = "v9"
        with self.assertRaises(ValidationError) as ctx:
            parse_case(doc)
        self.assertEqual(ctx.exception.field, "$.catalog.pinned_version")

    def test_candidate_not_in_batch_candidates(self):
        doc = self._base()
        # c1 physically belongs to batch B but the fragment only declares OTHER.
        doc["fragments"][0]["candidate_copies"] = ["c1"]
        doc["fragments"][0]["batch_candidates"] = ["OTHER"]
        # OTHER must resolve to a real batch in the pinned catalog.
        doc["catalog"]["versions"][0]["copies"].append(
            _clean_copy(copy("c9", "OTHER", "R9", {0: "a"}))
        )
        doc["catalog"]["versions"][0]["custody"].append(
            {"copy_id": "c9",
             "stages": [{"stage": "capture", "recipient": "R9"}]}
        )
        with self.assertRaises(ValidationError) as ctx:
            parse_case(doc)
        self.assertIn("candidate_copies[0]", ctx.exception.field)

    def test_duplicate_position_rejected(self):
        doc = self._base()
        doc["fragments"][0]["observations"].append(obs(0, "a", confidence="high"))
        with self.assertRaises(ValidationError) as ctx:
            parse_case(doc)
        self.assertIn("position", ctx.exception.field)

    def test_boolean_symbol_rejected(self):
        doc = self._base()
        doc["fragments"][0]["observations"] = [obs(0, True, confidence="high")]
        with self.assertRaises(ValidationError) as ctx:
            parse_case(doc)
        self.assertIn("symbol", ctx.exception.field)


class DeterminismAndCliTest(unittest.TestCase):
    def _doc(self):
        copies = [
            copy("c1", "B", "R1", {0: "a", 1: "b"},
                 custody=[{"stage": "capture", "recipient": "R1"}]),
            copy("c2", "B", "R2", {0: "a", 1: "b"},
                 custody=[{"stage": "capture", "recipient": "R2"}]),
        ]
        fragments = [
            fragment("f1", ["B"], ["c1", "c2"],
                     [obs(0, "a", confidence="high"), obs(1, "b", confidence="high")])
        ]
        return make_case(copies, fragments)

    def test_reproducible_bytes(self):
        case = parse_case(self._doc())
        first = json.dumps(build_report(case), ensure_ascii=False, indent=2, sort_keys=True)
        second = json.dumps(build_report(case), ensure_ascii=False, indent=2, sort_keys=True)
        self.assertEqual(first, second)

    def test_cli_writes_file_and_fails_on_bad_case(self):
        good = ROOT / ".runtime-tmp" / "good_case.json"
        out = ROOT / ".runtime-tmp" / "report.json"
        good.parent.mkdir(exist_ok=True)
        good.write_text(json.dumps(self._doc(), ensure_ascii=False), encoding="utf-8")
        code = main([str(good), str(out)])
        self.assertEqual(code, 0)
        payload = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(payload["status"], "feasible")

        bad = ROOT / ".runtime-tmp" / "bad_case.json"
        bad.write_text("{not json", encoding="utf-8")
        err = io.StringIO()
        with redirect_stderr(err):
            code = main([str(bad)])
        self.assertEqual(code, 2)
        self.assertIn("validation error at", err.getvalue())

    def test_module_invocation(self):
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        good = ROOT / ".runtime-tmp" / "good_case.json"
        proc = subprocess.run(
            [sys.executable, "-m", "watermark_audit", str(good)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["status"], "feasible")


class RandomizedBruteCrossCheckTest(unittest.TestCase):
    """Property-style cross check over many tiny random fictional cases."""

    def test_enumerated_cases_match_brute_force(self):
        import random

        rng = random.Random(20261122)
        for trial in range(60):
            doc = self._random_case(rng, trial)
            case = parse_case(doc)
            brute = brute_force(case)
            solver = Solver(case)
            any_leaf = solver.find_any()
            self.assertEqual(brute.feasible, any_leaf is not None, f"trial {trial}")
            if not brute.feasible:
                continue
            optimal = solver.find_optimal()
            self.assertEqual(
                optimal["assignment_tuple"],
                brute.optimal["assignment_tuple"],
                f"trial {trial} tuple",
            )
            self.assertEqual(
                optimal["total_cost"], brute.optimal["total_cost"], f"trial {trial} cost"
            )
            self.assertEqual(
                optimal["distinct_copies"],
                brute.optimal["distinct_copies"],
                f"trial {trial} copies",
            )
            # Recipient verdict consistency against the full brute enumeration.
            audit = solver.audit_verdicts()
            brute_present = {r: False for r in solver.recipients}
            brute_absent = {r: False for r in solver.recipients}
            for leaf in brute.all_leaves:
                for r in solver.recipients:
                    if r in leaf["holders"]:
                        brute_present[r] = True
                    else:
                        brute_absent[r] = True
            for r in solver.recipients:
                verdict = audit["verdicts"][r]
                if not brute_present[r]:
                    self.assertEqual(verdict, "impossible", f"trial {trial} {r}")
                elif not brute_absent[r]:
                    self.assertEqual(verdict, "inevitable", f"trial {trial} {r}")
                else:
                    self.assertEqual(verdict, "possible", f"trial {trial} {r}")

    def _random_case(self, rng, trial):
        n_copies = rng.randint(2, 4)
        n_fragments = rng.randint(1, 4)
        width = rng.randint(2, 4)
        alphabet = ["a", "b", "c"]
        stages = ["capture", "storage"]
        copies = []
        for ci in range(n_copies):
            codeword = {pos: rng.choice(alphabet) for pos in range(width)}
            missing = [pos for pos in range(width) if rng.random() < 0.15]
            holder = f"R{ci % 2}"  # occasionally share holders
            custody = [
                {"stage": stage, "recipient": holder if stage == "capture" else f"H{ci}"}
                for stage in stages
            ]
            copies.append(copy(f"c{ci}", "B", f"R{ci}", codeword, missing, custody))
        fragments = []
        for fi in range(n_fragments):
            chosen = rng.sample(
                [f"c{ci}" for ci in range(n_copies)],
                k=min(n_copies, rng.randint(1, 3)),
            )
            template = copies[int(chosen[0][1:])]
            observations = []
            for pos in range(width):
                if rng.random() < 0.15 and pos in template["missing"]:
                    observations.append(obs(pos, erased=True))
                    continue
                if rng.random() < 0.25:
                    symbol = rng.choice([s for s in alphabet if s != "a"])
                    confidence = "low" if rng.random() < 0.8 else "high"
                    observations.append(
                        obs(pos, symbol, confidence=confidence,
                            cost=rng.randint(1, 5))
                    )
                else:
                    observations.append(
                        obs(pos, rng.choice(alphabet),
                            confidence=rng.choice(["high", "low"]),
                            cost=rng.randint(1, 3))
                    )
            fragments.append(
                fragment(
                    f"f{fi}", ["B"], chosen, observations, stage=rng.choice(stages)
                )
            )
        same_pairs, diff_pairs = [], []
        for i in range(n_fragments):
            for j in range(i + 1, n_fragments):
                roll = rng.random()
                if roll < 0.15:
                    same_pairs.append([f"f{i}", f"f{j}"])
                elif roll < 0.3:
                    diff_pairs.append([f"f{i}", f"f{j}"])
        return make_case(
            copies,
            fragments,
            same_pairs=same_pairs,
            diff_pairs=diff_pairs,
            total_budget=rng.choice([None, 3, 6, 100]),
            per_fragment_cap=rng.choice([None, 4, 100]),
            case_id=f"rand-{trial}",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
