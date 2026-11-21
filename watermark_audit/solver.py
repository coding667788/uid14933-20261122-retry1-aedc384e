"""Constraint solving over fragment-to-copy interpretations.

Search model
------------
Fragments joined by ``same_source_pairs`` form one equivalence group and must
be assigned a single copy. ``different_source_pairs`` forbid two groups from
sharing a copy.  Every assignment must additionally satisfy:

* erased observations may only land on positions the copy registers as
  allowed-missing (an erasure is never a symbol match and never carries cost);
* high-confidence non-erased observations must equal the registered symbol;
* low-confidence disagreements are charged the observation's integer cost,
  but a repeated observation of the same ``(position, wrong symbol)`` error on
  one copy is charged only once, at the smallest reported cost;
* each fragment's own raw low-confidence cost stays under the per-fragment cap;
* the deduplicated total stays under the case budget;
* at every fragment's event stage the copy must have a registered holder, and
  per batch the number of distinct responsible recipients stays under the cap.

The optimizer minimizes lexicographically:
1. number of distinct copies involved;
2. total deduplicated difference cost;
3. the tuple of source copy ids ordered by fragment id (deterministic tiebreak).

Because a later group can re-report an already charged error at a lower cost,
the running total is not monotone along a path.  All pruning therefore uses a
sound lower bound derived from the globally smallest cost each error key could
ever reach on a copy, while exact budget and cost checks happen at leaves.
"""

from __future__ import annotations

from dataclasses import dataclass

from .model import Case, CatalogVersion, Copy, Fragment


@dataclass(frozen=True)
class LowMismatch:
    position: int
    observed: object
    expected: object
    cost: int


@dataclass(frozen=True)
class GroupStatic:
    group_id: int
    members: tuple[str, ...]  # fragment ids, sorted
    candidates: tuple[str, ...]  # copy ids, sorted lexicographically
    # feasible[copy_id][fragment_id] = (mismatches, erased positions)
    feasible: dict[str, dict[str, tuple[tuple[LowMismatch, ...], tuple[int, ...]]]]


class _DSU:
    def __init__(self, ids: list[str]) -> None:
        self.parent = {i: i for i in ids}

    def find(self, x: str) -> str:
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            # Attach the lexicographically larger root under the smaller one so
            # group ordering is deterministic across runs.
            if ra < rb:
                self.parent[rb] = ra
            else:
                self.parent[ra] = rb

class Solver:
    def __init__(self, case: Case, active_fragments: set[str] | None = None) -> None:
        self.case = case
        self.catalog: CatalogVersion = case.catalog
        if active_fragments is None:
            active = {f.fragment_id for f in case.fragments}
        else:
            active = set(active_fragments)
        self.active = active
        self.fragments: dict[str, Fragment] = {
            f.fragment_id: f for f in case.fragments if f.fragment_id in active
        }
        self.groups = self._build_groups()
        self.group_by_fragment: dict[str, int] = {}
        for g in self.groups:
            for fid in g.members:
                self.group_by_fragment[fid] = g.group_id
        self.diff_group_pairs = self._build_diff_pairs()
        self._all_recipients = self._collect_recipients()
        # global_min_cost[copy][(position, observed)] = smallest cost at which
        # any group that could use this copy ever reports that error key.
        self.global_min_cost = self._build_global_min_cost()

    # ------------------------------------------------------------------ setup
    def _build_groups(self) -> list[GroupStatic]:
        dsu = _DSU(list(self.fragments))
        for a, b in self.case.same_source_pairs:
            if a in self.fragments and b in self.fragments:
                dsu.union(a, b)

        buckets: dict[str, list[str]] = {}
        for fid in sorted(self.fragments):
            buckets.setdefault(dsu.find(fid), []).append(fid)

        groups: list[GroupStatic] = []
        ordered = sorted(buckets.values(), key=lambda members: members[0])
        for index, members_list in enumerate(ordered):
            members = tuple(sorted(members_list))
            candidate_sets = [
                set(self.fragments[fid].candidate_copies) for fid in members
            ]
            intersection = sorted(set.intersection(*candidate_sets))
            feasible: dict[
                str, dict[str, tuple[tuple[LowMismatch, ...], tuple[int, ...]]]
            ] = {}
            for copy_id in intersection:
                per_fragment = self._static_evaluate(
                    members, self.catalog.copies[copy_id]
                )
                if per_fragment is not None:
                    feasible[copy_id] = per_fragment
            groups.append(
                GroupStatic(
                    group_id=index,
                    members=members,
                    candidates=tuple(intersection),
                    feasible=feasible,
                )
            )
        return groups

    def _static_evaluate(
        self, members: tuple[str, ...], copy: Copy
    ) -> dict[str, tuple[tuple[LowMismatch, ...], tuple[int, ...]]] | None:
        """Per-fragment mismatch/erasure tables, or None when the copy cannot
        host the group under the hard (non-cost-budget) constraints."""
        result: dict[str, tuple[tuple[LowMismatch, ...], tuple[int, ...]]] = {}
        for fid in members:
            fragment = self.fragments[fid]
            stage = fragment.event_stage
            if stage is None:
                return None
            if self.catalog.custody[copy.copy_id].holder(stage) is None:
                return None
            mismatches: list[LowMismatch] = []
            erased: list[int] = []
            for obs in fragment.observations:
                if obs.erased:
                    # An erasure is admissible only at an allowed-missing slot;
                    # it is neither a match nor a charged difference.
                    if obs.position not in copy.missing:
                        return None
                    erased.append(obs.position)
                    continue
                if obs.position not in copy.codeword or obs.position in copy.missing:
                    return None
                expected = copy.codeword[obs.position]
                if obs.symbol == expected:
                    continue
                if obs.confidence == "high":
                    return None  # trusted high mark disagrees: hard contradiction
                mismatches.append(
                    LowMismatch(obs.position, obs.symbol, expected, obs.cost)
                )
            cap = self.case.per_fragment_cost_cap
            fragment_raw = sum(m.cost for m in mismatches)
            if cap is not None and fragment_raw > cap:
                return None
            result[fid] = (tuple(mismatches), tuple(sorted(erased)))
        return result

    def _build_diff_pairs(self) -> set[tuple[int, int]]:
        pairs: set[tuple[int, int]] = set()
        for a, b in self.case.different_source_pairs:
            if a in self.group_by_fragment and b in self.group_by_fragment:
                ga = self.group_by_fragment[a]
                gb = self.group_by_fragment[b]
                pairs.add((min(ga, gb), max(ga, gb)))
        return pairs

    def _collect_recipients(self) -> list[str]:
        recipients: set[str] = set()
        for copy in self.catalog.copies.values():
            recipients.add(copy.recipient)
        for record in self.catalog.custody.values():
            for holding in record.stages:
                recipients.add(holding.recipient)
        return sorted(recipients)

    def _build_global_min_cost(self) -> dict[str, dict[tuple[int, object], int]]:
        minima: dict[str, dict[tuple[int, object], int]] = {}
        for group in self.groups:
            for copy_id, table in group.feasible.items():
                per_copy = minima.setdefault(copy_id, {})
                for mismatches, _ in table.values():
                    for mm in mismatches:
                        key = (mm.position, mm.observed)
                        if key not in per_copy or mm.cost < per_copy[key]:
                            per_copy[key] = mm.cost
        return minima

    @property
    def recipients(self) -> list[str]:
        return self._all_recipients

    def structurally_infeasible(self) -> bool:
        """Contradictions independent of the dynamic assignment state."""
        for g in self.groups:
            if not g.feasible:
                return True
        for ga, gb in self.diff_group_pairs:
            if ga == gb:
                return True
        return False

    # ------------------------------------------------------------- search API
    def find_optimal(self) -> dict | None:
        if self.structurally_infeasible():
            return None
        return self._search(mode="optimal")

    def find_any(self) -> dict | None:
        if self.structurally_infeasible():
            return None
        return self._search(mode="any")

    def find_with_predicate(self, predicate) -> dict | None:
        if self.structurally_infeasible():
            return None
        return self._search(mode="predicate", predicate=predicate)

    def collect_leaves(self, limit: int) -> list[dict]:
        if self.structurally_infeasible():
            return []
        return self._search(mode="collect", collect_limit=limit)

    def is_feasible(self) -> bool:
        return self.find_any() is not None

    # ----------------------------------------------------------------- search
    def _search(self, mode: str, predicate=None, collect_limit: int = 0):
        assignment: dict[int, str] = {}
        # copy_errors[copy][(position, observed)] = currently charged min cost
        copy_errors: dict[str, dict[tuple[int, object], int]] = {}
        copy_groups: dict[str, list[int]] = {}
        batch_holders: dict[str, set[str]] = {}
        total_cost = 0
        best_holder: list[dict] = []
        leaves: list[dict] = []

        fragment_order = sorted(self.fragments)

        def current_tuple() -> tuple[str, ...]:
            return tuple(
                assignment[self.group_by_fragment[fid]] for fid in fragment_order
            )

        def holders_snapshot() -> set[str]:
            snap: set[str] = set()
            for holders in batch_holders.values():
                snap.update(holders)
            return snap

        def can_assign(group: GroupStatic, copy_id: str) -> bool:
            """Pure check: does not mutate any search state."""
            for other in copy_groups.get(copy_id, ()):
                key = (min(group.group_id, other), max(group.group_id, other))
                if key in self.diff_group_pairs:
                    return False
            batch = self.catalog.copies[copy_id].batch
            cap = self.case.batch_recipient_caps.get(batch)
            if cap is None:
                return True
            added: set[str] = set()
            existing = batch_holders.get(batch, set())
            for fid in group.members:
                stage = self.fragments[fid].event_stage
                holder = self.catalog.custody[copy_id].holder(stage)
                if holder not in existing:
                    added.add(holder)
            return len(existing) + len(added) <= cap

        def apply_assignment(group: GroupStatic, copy_id: str) -> list:
            nonlocal total_cost
            log: list = []
            copy_groups.setdefault(copy_id, []).append(group.group_id)
            log.append(("group", copy_id))
            if copy_id not in copy_errors:
                copy_errors[copy_id] = {}
                log.append(("errors_new", copy_id))
            errors = copy_errors[copy_id]

            batch = self.catalog.copies[copy_id].batch
            existed = set(batch_holders.setdefault(batch, set()))
            log.append(("holders", batch, existed))
            for fid in group.members:
                stage = self.fragments[fid].event_stage
                holder = self.catalog.custody[copy_id].holder(stage)
                batch_holders[batch].add(holder)

            table = group.feasible[copy_id]
            for fid in group.members:
                mismatches, _ = table[fid]
                for mm in mismatches:
                    key = (mm.position, mm.observed)
                    if key not in errors:
                        errors[key] = mm.cost
                        total_cost += mm.cost
                        log.append(("error_add", copy_id, key))
                    elif mm.cost < errors[key]:
                        old = errors[key]
                        total_cost += mm.cost - old
                        errors[key] = mm.cost
                        log.append(("error_adjust", copy_id, key, old))
            assignment[group.group_id] = copy_id
            log.append(("assign", group.group_id))
            return log

        def rollback(log: list) -> None:
            nonlocal total_cost
            for entry in reversed(log):
                kind = entry[0]
                if kind == "assign":
                    del assignment[entry[1]]
                elif kind == "error_add":
                    _, copy_id, key = entry
                    total_cost -= copy_errors[copy_id].pop(key)
                elif kind == "error_adjust":
                    _, copy_id, key, old = entry
                    current = copy_errors[copy_id][key]
                    total_cost += old - current
                    copy_errors[copy_id][key] = old
                elif kind == "errors_new":
                    del copy_errors[entry[1]]
                elif kind == "group":
                    copy_groups[entry[1]].pop()
                    if not copy_groups[entry[1]]:
                        del copy_groups[entry[1]]
                elif kind == "holders":
                    _, batch, existed = entry
                    batch_holders[batch] = set(existed)
                    if not batch_holders[batch]:
                        del batch_holders[batch]

        def cost_lower_bound() -> int:
            """Smallest total the current error set could reach after the
            remaining groups apply their (possibly cheaper) re-reports."""
            headroom = 0
            for copy_id, errors in copy_errors.items():
                floor = self.global_min_cost.get(copy_id, {})
                for key, charged in errors.items():
                    reachable = floor.get(key, charged)
                    if reachable < charged:
                        headroom += charged - reachable
            return total_cost - headroom

        def make_leaf() -> dict:
            return {
                "assignment_tuple": current_tuple(),
                "assignment": dict(assignment),
                "distinct_copies": len(copy_groups),
                "total_cost": total_cost,
                "holders": holders_snapshot(),
                "copy_errors": {c: dict(errs) for c, errs in copy_errors.items()},
            }

        def better_than_best(leaf: dict) -> bool:
            assert best_holder
            current_best = best_holder[0]
            if (leaf["distinct_copies"], leaf["total_cost"]) < (
                current_best["distinct_copies"],
                current_best["total_cost"],
            ):
                return True
            if (leaf["distinct_copies"], leaf["total_cost"]) > (
                current_best["distinct_copies"],
                current_best["total_cost"],
            ):
                return False
            return leaf["assignment_tuple"] < current_best["assignment_tuple"]

        def dfs(done: set[int]):
            if len(done) == len(self.groups):
                leaf = make_leaf()
                if (
                    self.case.total_budget is not None
                    and leaf["total_cost"] > self.case.total_budget
                ):
                    return None
                if mode == "any":
                    return leaf
                if mode == "predicate":
                    return leaf if predicate(leaf) else None
                if mode == "collect":
                    leaves.append(leaf)
                    return "stop" if len(leaves) >= collect_limit else None
                if not best_holder or better_than_best(leaf):
                    best_holder[:] = [leaf]
                return None

            # Static MRV: the group with the fewest structurally feasible copies.
            # Dynamic restrictions (different-source, caps) are enforced when
            # each branch is actually tried, so they must not influence choice.
            remaining = [g for g in self.groups if g.group_id not in done]
            chosen = min(remaining, key=lambda g: (len(g.feasible), g.members[0]))

            for copy_id in chosen.feasible:
                if not can_assign(chosen, copy_id):
                    continue
                log = apply_assignment(chosen, copy_id)
                lb = cost_lower_bound()
                if self.case.total_budget is not None and lb > self.case.total_budget:
                    rollback(log)
                    continue
                if mode == "optimal" and best_holder:
                    current_best = best_holder[0]
                    if len(copy_groups) > current_best["distinct_copies"]:
                        rollback(log)
                        continue
                    if (
                        len(copy_groups) == current_best["distinct_copies"]
                        and lb > current_best["total_cost"]
                    ):
                        rollback(log)
                        continue
                done.add(chosen.group_id)
                outcome = dfs(done)
                done.discard(chosen.group_id)
                rollback(log)
                if mode in ("any", "predicate") and outcome is not None:
                    return outcome
                if mode == "collect" and outcome == "stop":
                    return "stop"
            return None

        outcome = dfs(set())
        if mode == "collect":
            return leaves
        if mode in ("any", "predicate"):
            return outcome
        return best_holder[0] if best_holder else None

    # --------------------------------------------------------- audit helpers
    def audit_verdicts(self, witness_limit: int = 8) -> dict:
        """Decide inevitable/possible/impossible for every registered recipient."""
        leaves = self.collect_leaves(limit=witness_limit)
        exhaustive = len(leaves) < witness_limit
        present: dict[str, bool] = {r: False for r in self._all_recipients}
        absent: dict[str, bool] = {r: False for r in self._all_recipients}
        for leaf in leaves:
            for r in self._all_recipients:
                if r in leaf["holders"]:
                    present[r] = True
                else:
                    absent[r] = True

        verdicts: dict[str, str] = {}
        for r in self._all_recipients:
            if exhaustive:
                if not leaves:
                    verdicts[r] = "impossible"
                elif not absent[r]:
                    verdicts[r] = "inevitable"
                elif not present[r]:
                    verdicts[r] = "impossible"
                else:
                    verdicts[r] = "possible"
                continue
            # Sample was capped: settle open questions with targeted searches.
            if not present[r]:
                found = self.find_with_predicate(
                    lambda leaf, r=r: r in leaf["holders"]
                )
                if found is None:
                    verdicts[r] = "impossible"
                    continue
            if not absent[r]:
                without = self.find_with_predicate(
                    lambda leaf, r=r: r not in leaf["holders"]
                )
                verdicts[r] = "inevitable" if without is None else "possible"
            else:
                verdicts[r] = "possible"

        unique_recipient = None
        unique_attribution = False
        inevitable = sorted(r for r, v in verdicts.items() if v == "inevitable")
        if len(inevitable) == 1:
            r0 = inevitable[0]
            other = self.find_with_predicate(
                lambda leaf, r=r0: leaf["holders"] != {r}
            )
            if other is None:
                unique_attribution = True
                unique_recipient = r0

        return {
            "exhaustive_leaves": exhaustive,
            "leaves_examined": len(leaves),
            "verdicts": verdicts,
            "unique_attribution": unique_attribution,
            "unique_recipient": unique_recipient,
            "witnesses": leaves,
        }

    def conflict_core(self) -> dict:
        """Minimal conflict set via descending-id trial deletions.

        Fragments are tried in descending id order.  A fragment is permanently
        removed only when deleting it restores feasibility of the remaining
        set; if the remainder is still infeasible the fragment is indispensable
        and is kept.  The surviving set is irreducible and is the
        procedure-defined answer.
        """
        all_ids = sorted(self.fragments)
        core = list(all_ids)
        trace: list[dict] = []
        for fid in sorted(all_ids, reverse=True):
            trial = [x for x in core if x != fid]
            sub = Solver(self.case, active_fragments=set(trial))
            feasible = sub.is_feasible()
            if feasible:
                action = "kept"  # deleting it would restore feasibility
            else:
                action = "removed"  # contradiction survives without it
                core = trial
            trace.append(
                {
                    "trial_fragment": fid,
                    "remaining_after_trial": list(trial),
                    "feasible_without": feasible,
                    "action": action,
                }
            )
        return {"conflict_fragments": core, "deletion_trace": trace}

    # ------------------------------------------------------- report replay
    def replay_assignment(self, leaf: dict) -> dict:
        """Expand an optimal leaf into per-fragment charged-cost details."""
        assignment_tuple = leaf["assignment_tuple"]
        fragment_order = sorted(self.fragments)
        # Each deduplicated error is charged to the lowest-id fragment that
        # witnesses it, at the minimum reported cost actually reached.
        ownership: dict[tuple[str, tuple[int, object]], str] = {}
        for fid, copy_id in zip(fragment_order, assignment_tuple):
            group = self.groups[self.group_by_fragment[fid]]
            mismatches = group.feasible[copy_id][fid][0]
            for mm in mismatches:
                key = (copy_id, (mm.position, mm.observed))
                if key not in ownership:
                    ownership[key] = fid

        per_fragment: list[dict] = []
        for fid, copy_id in zip(fragment_order, assignment_tuple):
            fragment = self.fragments[fid]
            copy = self.catalog.copies[copy_id]
            group = self.groups[self.group_by_fragment[fid]]
            mismatches, erased = group.feasible[copy_id][fid]
            detail = []
            charged_cost = 0
            raw_cost = 0
            for mm in mismatches:
                key = (copy_id, (mm.position, mm.observed))
                charged = ownership[key] == fid
                raw_cost += mm.cost
                if charged:
                    charged_cost += leaf["copy_errors"][copy_id][
                        (mm.position, mm.observed)
                    ]
                detail.append(
                    {
                        "position": mm.position,
                        "observed": mm.observed,
                        "expected": mm.expected,
                        "cost": mm.cost,
                        "charged": charged,
                    }
                )
            holder = self.catalog.custody[copy_id].holder(fragment.event_stage)
            per_fragment.append(
                {
                    "fragment_id": fid,
                    "copy_id": copy_id,
                    "batch": copy.batch,
                    "event_stage": fragment.event_stage,
                    "responsible_recipient": holder,
                    "raw_low_cost": raw_cost,
                    "charged_cost": charged_cost,
                    "mismatches": detail,
                    "erased_positions": list(erased),
                }
            )
        return {"assignment": per_fragment, "tie_break_tuple": list(assignment_tuple)}
