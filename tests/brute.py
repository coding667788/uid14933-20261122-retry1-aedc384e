"""Independent brute-force enumeration used only for cross-checking.

This module deliberately re-derives feasibility and optimization from scratch
with the plain English rules of the procedure, using no code from
``watermark_audit.solver``.  It is exponentially expensive and is only applied
to the small synthetic cases in the test suite.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass

from watermark_audit.model import Case


@dataclass
class BruteResult:
    feasible: bool
    optimal: dict | None
    all_leaves: list[dict]


def brute_force(case: Case) -> BruteResult:
    catalog = case.catalog
    fragments = sorted(case.fragments, key=lambda f: f.fragment_id)
    by_id = {f.fragment_id: f for f in fragments}

    # Union-find for same-source groups, implemented independently.
    parent = {f.fragment_id: f.fragment_id for f in fragments}

    def root(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in case.same_source_pairs:
        ra, rb = root(a), root(b)
        parent[rb] = ra

    group_of: dict[str, str] = {f.fragment_id: root(f.fragment_id) for f in fragments}
    group_members: dict[str, list[str]] = {}
    for fid in group_of:
        group_members.setdefault(group_of[fid], []).append(fid)
    group_ids = sorted(group_members)

    diff_roots = {(group_of[a], group_of[b]) for a, b in case.different_source_pairs}
    if any(a == b for a, b in diff_roots):
        return BruteResult(False, None, [])

    # Candidate lists per group: intersection, with static hard checks.
    def copy_passes(fid: str, copy_id: str):
        fragment = by_id[fid]
        copy = catalog.copies[copy_id]
        record = catalog.custody[copy_id]
        if record.holder(fragment.event_stage) is None:
            return False, []
        mismatches = []
        for obs in fragment.observations:
            if obs.erased:
                if obs.position not in copy.missing:
                    return False, []
                continue
            if obs.position not in copy.codeword or obs.position in copy.missing:
                return False, []
            expected = copy.codeword[obs.position]
            if obs.symbol == expected:
                continue
            if obs.confidence == "high":
                return False, []
            mismatches.append((obs.position, obs.symbol, obs.cost))
        if case.per_fragment_cost_cap is not None:
            if sum(cost for _, _, cost in mismatches) > case.per_fragment_cost_cap:
                return False, []
        return True, mismatches

    options: dict[str, list[str]] = {}
    group_mismatch: dict[tuple[str, str], dict[str, list]] = {}
    for gid in group_ids:
        members = sorted(group_members[gid])
        common = set(by_id[members[0]].candidate_copies)
        for fid in members[1:]:
            common &= set(by_id[fid].candidate_copies)
        viable = []
        for copy_id in sorted(common):
            ok_all = True
            per_fragment = {}
            for fid in members:
                ok, mismatches = copy_passes(fid, copy_id)
                if not ok:
                    ok_all = False
                    break
                per_fragment[fid] = mismatches
            if ok_all:
                viable.append(copy_id)
                group_mismatch[(gid, copy_id)] = per_fragment
        if not viable:
            return BruteResult(False, None, [])
        options[gid] = viable

    leaves: list[dict] = []
    for combo in itertools.product(*(options[gid] for gid in group_ids)):
        group_copy = dict(zip(group_ids, combo))
        # Different-source restriction.
        violated = False
        for a, b in diff_roots:
            if group_copy[a] == group_copy[b]:
                violated = True
                break
        if violated:
            continue

        # Deduplicated unified error model per copy.
        charged: dict[str, dict[tuple[int, object], int]] = {}
        for gid, copy_id in group_copy.items():
            per_copy = charged.setdefault(copy_id, {})
            for mismatches in group_mismatch[(gid, copy_id)].values():
                for position, observed, cost in mismatches:
                    key = (position, observed)
                    if key not in per_copy or cost < per_copy[key]:
                        per_copy[key] = cost
        total = sum(sum(errors.values()) for errors in charged.values())
        if case.total_budget is not None and total > case.total_budget:
            continue

        # Batch recipient caps evaluated at each fragment's event stage.
        batch_holders: dict[str, set[str]] = {}
        for gid, copy_id in group_copy.items():
            batch = catalog.copies[copy_id].batch
            for fid in group_members[gid]:
                stage = by_id[fid].event_stage
                holder = catalog.custody[copy_id].holder(stage)
                batch_holders.setdefault(batch, set()).add(holder)
        cap_violated = any(
            len(holders) > case.batch_recipient_caps[batch]
            for batch, holders in batch_holders.items()
            if batch in case.batch_recipient_caps
        )
        if cap_violated:
            continue

        fragment_tuple = tuple(
            group_copy[group_of[fid]]
            for fid in sorted(by_id)
        )
        holders = {h for hs in batch_holders.values() for h in hs}
        leaves.append(
            {
                "assignment_tuple": fragment_tuple,
                "distinct_copies": len(charged),
                "total_cost": total,
                "holders": holders,
            }
        )

    if not leaves:
        return BruteResult(False, None, [])
    leaves.sort(
        key=lambda leaf: (
            leaf["distinct_copies"],
            leaf["total_cost"],
            leaf["assignment_tuple"],
        )
    )
    return BruteResult(True, leaves[0], leaves)
