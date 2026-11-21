"""Build the deterministic, JSON-serialisable review report."""

from __future__ import annotations

from .model import Case, CatalogVersion
from .solver import Solver

SCHEMA_VERSION = "watermark-audit/1"
WITNESS_LIMIT = 8
VERDICT_LABELS = {
    "inevitable": "必然相关",
    "possible": "可能相关",
    "impossible": "不可能相关",
}


def _version_summary(case: Case) -> list[dict]:
    return [
        {
            "version": version.version,
            "released_at": version.released_at,
            "note": version.note,
            "copy_count": len(version.copies),
            "pinned": version.version == case.pinned_version,
        }
        for version in sorted(case.catalogs.values(), key=lambda v: v.version)
    ]


def _pinned_view(catalog: CatalogVersion) -> dict:
    return {
        "version": catalog.version,
        "released_at": catalog.released_at,
        "note": catalog.note,
        "copies": [
            {
                "copy_id": copy.copy_id,
                "batch": copy.batch,
                "registered_recipient": copy.recipient,
                "codeword": {str(pos): sym for pos, sym in sorted(copy.codeword.items())},
                "allowed_missing": sorted(copy.missing),
                "custody_chain": [
                    {"stage": h.stage, "recipient": h.recipient}
                    for h in catalog.custody[copy.copy_id].stages
                ],
            }
            for copy in sorted(catalog.copies.values(), key=lambda c: c.copy_id)
        ],
    }


def _constraints_echo(case: Case) -> dict:
    return {
        "per_fragment_cost_cap": case.per_fragment_cost_cap,
        "total_budget": case.total_budget,
        "batch_recipient_caps": dict(sorted(case.batch_recipient_caps.items())),
        "same_source_pairs": [list(p) for p in case.same_source_pairs],
        "different_source_pairs": [list(p) for p in case.different_source_pairs],
        "default_event_stage": case.default_event_stage,
        "fragment_count": len(case.fragments),
        "observed_position_count": sum(
            len(f.observations) for f in case.fragments
        ),
    }


def _serialise_witness(leaf: dict, solver: Solver) -> dict:
    return {
        "tie_break_tuple": list(leaf["assignment_tuple"]),
        "distinct_copies": leaf["distinct_copies"],
        "total_cost": leaf["total_cost"],
        "responsible_recipients": sorted(leaf["holders"]),
        "responsible_by_batch": _holders_by_batch(leaf, solver),
    }


def _holders_by_batch(leaf: dict, solver: Solver) -> dict[str, list[str]]:
    by_batch: dict[str, set[str]] = {}
    for gid, copy_id in leaf["assignment"].items():
        group = solver.groups[gid]
        batch = solver.catalog.copies[copy_id].batch
        for fid in group.members:
            stage = solver.fragments[fid].event_stage
            holder = solver.catalog.custody[copy_id].holder(stage)
            by_batch.setdefault(batch, set()).add(holder)
    return {batch: sorted(holders) for batch, holders in sorted(by_batch.items())}


def _feasible_report(case: Case, solver: Solver) -> dict:
    leaf = solver.find_optimal()
    assert leaf is not None  # guarded by the caller
    replay = solver.replay_assignment(leaf)
    audit = solver.audit_verdicts(witness_limit=WITNESS_LIMIT)

    responsible_by_batch = _holders_by_batch(leaf, solver)
    responsible = sorted(leaf["holders"])

    verdicts = [
        {
            "recipient": recipient,
            "verdict": audit["verdicts"][recipient],
            "verdict_label": VERDICT_LABELS[audit["verdicts"][recipient]],
        }
        for recipient in solver.recipients
    ]

    return {
        "schema_version": SCHEMA_VERSION,
        "case_id": case.case_id,
        "status": "feasible",
        "catalog": {
            "pinned_version": case.pinned_version,
            "versions": _version_summary(case),
            "pinned_view": _pinned_view(case.catalog),
        },
        "constraints": _constraints_echo(case),
        "report": {
            "optimization_order": [
                "min distinct copies involved",
                "min total deduplicated difference cost",
                "lexicographic tie-break on source copy ids ordered by fragment id",
            ],
            "distinct_copies": leaf["distinct_copies"],
            "total_cost": leaf["total_cost"],
            "copies_involved": sorted(leaf["copy_errors"].keys()),
            "tie_break_tuple": replay["tie_break_tuple"],
            "responsible_recipients": responsible,
            "responsible_by_batch": responsible_by_batch,
            "fragments": replay["assignment"],
        },
        "audit": {
            "unique_attribution": audit["unique_attribution"],
            "unique_recipient": audit["unique_recipient"],
            "exhaustive_witnesses": audit["exhaustive_leaves"],
            "witnesses_examined": audit["leaves_examined"],
            "recipient_verdicts": verdicts,
            "witnesses": [
                _serialise_witness(witness, solver) for witness in audit["witnesses"]
            ],
            "note": (
                "唯一归属仅在每种可行解释都指向同一接收人时给出；否则 witness 列表"
                "至少保留两份完整兼容见证（若可行解释超过上限，则为带代表性的样本）。"
            ),
        },
    }


def _infeasible_report(case: Case, solver: Solver) -> dict:
    conflict = solver.conflict_core()
    return {
        "schema_version": SCHEMA_VERSION,
        "case_id": case.case_id,
        "status": "infeasible",
        "catalog": {
            "pinned_version": case.pinned_version,
            "versions": _version_summary(case),
            "pinned_view": _pinned_view(case.catalog),
        },
        "constraints": _constraints_echo(case),
        "report": None,
        "audit": None,
        "conflict": {
            "procedure": "按片段 ID 降序逐一试删；删除后剩余集合恢复可行则保留，"
            "否则永久删除；最终保留集即极小冲突片集合。",
            "conflict_fragments": conflict["conflict_fragments"],
            "deletion_trace": conflict["deletion_trace"],
        },
    }


def build_report(case: Case) -> dict:
    solver = Solver(case)
    if solver.is_feasible():
        return _feasible_report(case, solver)
    return _infeasible_report(case, solver)
