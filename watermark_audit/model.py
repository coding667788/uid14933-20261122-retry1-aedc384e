"""Case document model with strict, field-precise validation.

Input document (UTF-8 JSON) layout::

    {
      "case_id": "...",
      "catalog": {
        "pinned_version": "v1",
        "versions": [
          {"version": "v1", "released_at": "...", "note": "...",
           "copies": [{"copy_id", "batch", "recipient", "codeword", "missing"}
                      ...],
           "custody": [{"copy_id", "stages": [{"stage", "recipient"} ...]}
                       ...]}
        ]
      },
      "fragments": [
        {"fragment_id", "batch_candidates", "candidate_copies",
         "event_stage", "observations": [
            {"position", "symbol", "erased", "confidence", "cost"} ...
         ]}
      ],
      "same_source_pairs": [["f1", "f2"], ...],
      "different_source_pairs": [["f3", "f4"], ...],
      "per_fragment_cost_cap": int,
      "total_budget": int,
      "batch_recipient_caps": {"batch": int, ...}
    }
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .errors import ValidationError

# Documented capacity envelope of the review procedure.
MAX_FRAGMENTS = 28
MAX_CANDIDATES_PER_FRAGMENT = 5
MAX_OBSERVED_POSITIONS = 80

_KNOWN_TOP_LEVEL = {
    "case_id",
    "default_event_stage",
    "catalog",
    "fragments",
    "same_source_pairs",
    "different_source_pairs",
    "per_fragment_cost_cap",
    "total_budget",
    "batch_recipient_caps",
}
_KNOWN_CATALOG = {"pinned_version", "versions"}
_KNOWN_VERSION = {"version", "released_at", "note", "copies", "custody"}
_KNOWN_COPY = {"copy_id", "batch", "recipient", "codeword", "missing"}
_KNOWN_STAGE = {"stage", "recipient"}
_KNOWN_CUSTODY = {"copy_id", "stages"}
_KNOWN_FRAGMENT = {
    "fragment_id",
    "batch_candidates",
    "candidate_copies",
    "event_stage",
    "observations",
}
_KNOWN_OBS = {"position", "symbol", "erased", "confidence", "cost"}
_CONFIDENCE_LEVELS = {"high", "low"}


def _require_type(obj: Any, field: str, types: tuple[type, ...], name: str) -> Any:
    if not isinstance(obj, types):
        expect = " or ".join(t.__name__ for t in types)
        raise ValidationError(field, f"expected {expect}, got {type(obj).__name__}")
    return obj


def _check_known(obj: dict[str, Any], known: set[str], field: str) -> None:
    unknown = sorted(set(obj) - known)
    if unknown:
        raise ValidationError(
            f"{field}.{unknown[0]}", "unknown field (not part of the case schema)"
        )


def _is_symbol(value: Any) -> bool:
    # Bools are ints in Python; reject them explicitly so True never silently
    # behaves as symbol 1.
    if isinstance(value, bool):
        return False
    return isinstance(value, (str, int))


@dataclass(frozen=True)
class Observation:
    position: int
    symbol: Any  # str or int; None only for erased observations
    erased: bool
    confidence: str
    cost: int  # integer penalty for a low-confidence disagreement


@dataclass(frozen=True)
class Copy:
    copy_id: str
    batch: str
    recipient: str
    codeword: dict[int, Any]
    missing: frozenset[int]


@dataclass(frozen=True)
class StageHolding:
    stage: str
    recipient: str


@dataclass(frozen=True)
class CustodyRecord:
    copy_id: str
    stages: tuple[StageHolding, ...]

    def holder(self, stage: str) -> str | None:
        for holding in self.stages:
            if holding.stage == stage:
                return holding.recipient
        return None


@dataclass(frozen=True)
class CatalogVersion:
    version: str
    released_at: str
    note: str
    copies: dict[str, Copy]
    custody: dict[str, CustodyRecord]


@dataclass(frozen=True)
class Fragment:
    fragment_id: str
    batch_candidates: tuple[str, ...]
    candidate_copies: tuple[str, ...]
    event_stage: str | None
    observations: tuple[Observation, ...]

    def per_fragment_raw_cost(self) -> int:
        """Sum of costs of the fragment's own low-confidence observations."""
        return sum(obs.cost for obs in self.observations if not obs.erased and obs.confidence == "low")


@dataclass(frozen=True)
class Case:
    case_id: str
    pinned_version: str
    catalogs: dict[str, CatalogVersion]
    fragments: tuple[Fragment, ...]
    same_source_pairs: tuple[tuple[str, str], ...]
    different_source_pairs: tuple[tuple[str, str], ...]
    per_fragment_cost_cap: int | None
    total_budget: int | None
    batch_recipient_caps: dict[str, int]
    default_event_stage: str | None = None

    @property
    def catalog(self) -> CatalogVersion:
        return self.catalogs[self.pinned_version]

    def fragment_by_id(self) -> dict[str, Fragment]:
        return {f.fragment_id: f for f in self.fragments}


def parse_case(doc: Any) -> Case:
    """Validate the raw decoded JSON document and build a :class:`Case`."""
    _require_type(doc, "$", (dict,), "case document")
    _check_known(doc, _KNOWN_TOP_LEVEL, "$")

    case_id = _require_type(doc.get("case_id"), "$.case_id", (str,), "case_id")
    if not case_id:
        raise ValidationError("$.case_id", "must be a non-empty string")

    catalog_raw = _require_type(doc.get("catalog"), "$.catalog", (dict,), "catalog")
    _check_known(catalog_raw, _KNOWN_CATALOG, "$.catalog")
    catalogs, pinned_version = _parse_catalog(catalog_raw)

    default_stage = None
    if "default_event_stage" in doc and doc["default_event_stage"] is not None:
        default_stage = _require_type(
            doc["default_event_stage"],
            "$.default_event_stage",
            (str,),
            "default_event_stage",
        )
        if not default_stage:
            raise ValidationError(
                "$.default_event_stage", "stage must be a non-empty string"
            )

    fragments_raw = _require_type(
        doc.get("fragments"), "$.fragments", (list,), "fragments"
    )
    if not fragments_raw:
        raise ValidationError("$.fragments", "at least one fragment is required")
    if len(fragments_raw) > MAX_FRAGMENTS:
        raise ValidationError(
            "$.fragments",
            f"at most {MAX_FRAGMENTS} fragments are supported, got {len(fragments_raw)}",
        )
    fragments = _parse_fragments(fragments_raw, catalogs[pinned_version])
    fragments = _resolve_event_stages(fragments, default_stage, catalogs[pinned_version])

    same_pairs = _parse_id_pairs(
        doc.get("same_source_pairs", []),
        "$.same_source_pairs",
        fragments,
    )
    diff_pairs = _parse_id_pairs(
        doc.get("different_source_pairs", []),
        "$.different_source_pairs",
        fragments,
    )
    pair_ids = {tuple(sorted(p)) for p in same_pairs} & {
        tuple(sorted(p)) for p in diff_pairs
    }
    if pair_ids:
        a, b = sorted(pair_ids)[0]
        raise ValidationError(
            "$.same_source_pairs",
            f"fragments {a!r} and {b!r} appear in both same- and different-source pairs",
        )

    per_fragment_cap = _parse_optional_nonneg_int(
        doc, "per_fragment_cost_cap", allow_zero=True
    )
    total_budget = _parse_optional_nonneg_int(doc, "total_budget", allow_zero=True)
    batch_caps = _parse_batch_caps(
        doc.get("batch_recipient_caps", {}), catalogs[pinned_version]
    )

    return Case(
        case_id=case_id,
        pinned_version=pinned_version,
        catalogs=catalogs,
        fragments=tuple(fragments),
        same_source_pairs=same_pairs,
        different_source_pairs=diff_pairs,
        per_fragment_cost_cap=per_fragment_cap,
        total_budget=total_budget,
        batch_recipient_caps=batch_caps,
        default_event_stage=default_stage,
    )


def _resolve_event_stages(
    fragments: list[Fragment],
    default_stage: str | None,
    catalog: CatalogVersion,
) -> list[Fragment]:
    known_stages = {
        holding.stage
        for record in catalog.custody.values()
        for holding in record.stages
    }
    resolved: list[Fragment] = []
    for index, fragment in enumerate(fragments):
        fpath = f"$.fragments[{index}].event_stage"
        stage = fragment.event_stage or default_stage
        if stage is None:
            raise ValidationError(
                fpath,
                "fragment has no event_stage and no top-level "
                "default_event_stage is defined; attribution requires a stage",
            )
        if stage not in known_stages:
            raise ValidationError(
                fpath,
                f"stage {stage!r} is not registered in any custody record of "
                "the pinned catalog",
            )
        resolved.append(
            Fragment(
                fragment_id=fragment.fragment_id,
                batch_candidates=fragment.batch_candidates,
                candidate_copies=fragment.candidate_copies,
                event_stage=stage,
                observations=fragment.observations,
            )
        )
    return resolved


def _parse_optional_nonneg_int(
    doc: dict[str, Any], key: str, allow_zero: bool
) -> int | None:
    if key not in doc or doc[key] is None:
        return None
    value = doc[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"$.{key}", "expected a non-negative integer")
    if value < 0 or (value == 0 and not allow_zero):
        raise ValidationError(f"$.{key}", "must be a non-negative integer")
    return value


def _parse_batch_caps(raw: Any, catalog: CatalogVersion) -> dict[str, int]:
    path = "$.batch_recipient_caps"
    _require_type(raw, path, (dict,), "batch_recipient_caps")
    known_batches = {copy.batch for copy in catalog.copies.values()}
    caps: dict[str, int] = {}
    for batch, cap in raw.items():
        if not isinstance(batch, str):
            raise ValidationError(path, "batch keys must be strings")
        if batch not in known_batches:
            raise ValidationError(
                f"{path}.{batch}", "no copy of this batch exists in the pinned catalog"
            )
        if isinstance(cap, bool) or not isinstance(cap, int) or cap <= 0:
            raise ValidationError(
                f"{path}.{batch}", "cap must be a positive integer"
            )
        caps[batch] = cap
    return caps


def _parse_catalog(
    catalog_raw: dict[str, Any],
) -> tuple[dict[str, CatalogVersion], str]:
    pinned = _require_type(
        catalog_raw.get("pinned_version"),
        "$.catalog.pinned_version",
        (str,),
        "pinned_version",
    )
    versions_raw = _require_type(
        catalog_raw.get("versions"), "$.catalog.versions", (list,), "versions"
    )
    if not versions_raw:
        raise ValidationError("$.catalog.versions", "at least one version is required")

    catalogs: dict[str, CatalogVersion] = {}
    for vi, version_raw in enumerate(versions_raw):
        vpath = f"$.catalog.versions[{vi}]"
        _require_type(version_raw, vpath, (dict,), "catalog version")
        _check_known(version_raw, _KNOWN_VERSION, vpath)
        version_id = _require_type(
            version_raw.get("version"), f"{vpath}.version", (str,), "version"
        )
        if not version_id:
            raise ValidationError(f"{vpath}.version", "must be a non-empty string")
        if version_id in catalogs:
            raise ValidationError(f"{vpath}.version", f"duplicate version {version_id!r}")

        released_at = version_raw.get("released_at", "")
        _require_type(released_at, f"{vpath}.released_at", (str,), "released_at")
        note = version_raw.get("note", "")
        _require_type(note, f"{vpath}.note", (str,), "note")

        copies = _parse_copies(
            _require_type(
                version_raw.get("copies"), f"{vpath}.copies", (list,), "copies"
            ),
            vpath,
        )
        custody = _parse_custody(
            _require_type(
                version_raw.get("custody", []),
                f"{vpath}.custody",
                (list,),
                "custody",
            ),
            copies,
            vpath,
        )
        catalogs[version_id] = CatalogVersion(
            version=version_id,
            released_at=released_at,
            note=note,
            copies=copies,
            custody=custody,
        )

    if pinned not in catalogs:
        raise ValidationError(
            "$.catalog.pinned_version",
            f"pinned version {pinned!r} is not present in versions",
        )
    return catalogs, pinned


def _parse_copies(raw: list[Any], vpath: str) -> dict[str, Copy]:
    copies: dict[str, Copy] = {}
    for ci, copy_raw in enumerate(raw):
        cpath = f"{vpath}.copies[{ci}]"
        _require_type(copy_raw, cpath, (dict,), "copy")
        _check_known(copy_raw, _KNOWN_COPY, cpath)
        copy_id = _require_type(
            copy_raw.get("copy_id"), f"{cpath}.copy_id", (str,), "copy_id"
        )
        if not copy_id:
            raise ValidationError(f"{cpath}.copy_id", "must be a non-empty string")
        if copy_id in copies:
            raise ValidationError(f"{cpath}.copy_id", f"duplicate copy_id {copy_id!r}")
        batch = _require_type(
            copy_raw.get("batch"), f"{cpath}.batch", (str,), "batch"
        )
        if not batch:
            raise ValidationError(f"{cpath}.batch", "must be a non-empty string")
        recipient = _require_type(
            copy_raw.get("recipient"), f"{cpath}.recipient", (str,), "recipient"
        )
        if not recipient:
            raise ValidationError(f"{cpath}.recipient", "must be a non-empty string")

        codeword_raw = _require_type(
            copy_raw.get("codeword"), f"{cpath}.codeword", (dict,), "codeword"
        )
        codeword: dict[int, Any] = {}
        for pos_key, symbol in codeword_raw.items():
            if not isinstance(pos_key, str) or not pos_key.isdigit():
                raise ValidationError(
                    f"{cpath}.codeword.{pos_key}",
                    "codeword keys must be decimal position strings",
                )
            position = int(pos_key)
            if not _is_symbol(symbol):
                raise ValidationError(
                    f"{cpath}.codeword.{pos_key}",
                    "symbol must be a string or integer (booleans are not allowed)",
                )
            if position in codeword:
                raise ValidationError(
                    f"{cpath}.codeword.{pos_key}", "duplicate codeword position"
                )
            codeword[position] = symbol
        if not codeword:
            raise ValidationError(f"{cpath}.codeword", "codeword must not be empty")

        missing_raw = copy_raw.get("missing", [])
        _require_type(missing_raw, f"{cpath}.missing", (list,), "missing")
        missing: set[int] = set()
        for mi, position in enumerate(missing_raw):
            if isinstance(position, bool) or not isinstance(position, int) or position < 0:
                raise ValidationError(
                    f"{cpath}.missing[{mi}]", "must be a non-negative integer position"
                )
            if position in missing:
                raise ValidationError(
                    f"{cpath}.missing[{mi}]", "duplicate missing position"
                )
            missing.add(position)
        outside = sorted(missing - set(codeword))
        if outside:
            raise ValidationError(
                f"{cpath}.missing",
                f"missing position {outside[0]} is not part of the codeword",
            )
        copies[copy_id] = Copy(
            copy_id=copy_id,
            batch=batch,
            recipient=recipient,
            codeword=codeword,
            missing=frozenset(missing),
        )
    return copies


def _parse_custody(
    raw: list[Any], copies: dict[str, Copy], vpath: str
) -> dict[str, CustodyRecord]:
    custody: dict[str, CustodyRecord] = {}
    for ri, record_raw in enumerate(raw):
        rpath = f"{vpath}.custody[{ri}]"
        _require_type(record_raw, rpath, (dict,), "custody record")
        _check_known(record_raw, _KNOWN_CUSTODY, rpath)
        copy_id = _require_type(
            record_raw.get("copy_id"), f"{rpath}.copy_id", (str,), "copy_id"
        )
        if copy_id not in copies:
            raise ValidationError(
                f"{rpath}.copy_id", "no copy with this id in the same catalog version"
            )
        if copy_id in custody:
            raise ValidationError(
                f"{rpath}.copy_id", "duplicate custody record for copy"
            )
        stages_raw = _require_type(
            record_raw.get("stages"), f"{rpath}.stages", (list,), "stages"
        )
        holdings: list[StageHolding] = []
        seen_stages: set[str] = set()
        for si, stage_raw in enumerate(stages_raw):
            spath = f"{rpath}.stages[{si}]"
            _require_type(stage_raw, spath, (dict,), "stage holding")
            _check_known(stage_raw, _KNOWN_STAGE, spath)
            stage = _require_type(
                stage_raw.get("stage"), f"{spath}.stage", (str,), "stage"
            )
            if not stage:
                raise ValidationError(f"{spath}.stage", "must be a non-empty string")
            if stage in seen_stages:
                raise ValidationError(f"{spath}.stage", f"duplicate stage {stage!r}")
            seen_stages.add(stage)
            recipient = _require_type(
                stage_raw.get("recipient"),
                f"{spath}.recipient",
                (str,),
                "recipient",
            )
            if not recipient:
                raise ValidationError(
                    f"{spath}.recipient", "must be a non-empty string"
                )
            holdings.append(StageHolding(stage=stage, recipient=recipient))
        if not holdings:
            raise ValidationError(f"{rpath}.stages", "at least one stage is required")
        custody[copy_id] = CustodyRecord(copy_id=copy_id, stages=tuple(holdings))
    missing_records = sorted(set(copies) - set(custody))
    if missing_records:
        raise ValidationError(
            f"{vpath}.custody",
            f"copy {missing_records[0]!r} has no custody record",
        )
    return custody


def _parse_fragments(
    raw: list[Any], catalog: CatalogVersion
) -> list[Fragment]:
    fragments: list[Fragment] = []
    seen_ids: set[str] = set()
    total_positions = 0
    for fi, fragment_raw in enumerate(raw):
        fpath = f"$.fragments[{fi}]"
        _require_type(fragment_raw, fpath, (dict,), "fragment")
        _check_known(fragment_raw, _KNOWN_FRAGMENT, fpath)
        fragment_id = _require_type(
            fragment_raw.get("fragment_id"),
            f"{fpath}.fragment_id",
            (str,),
            "fragment_id",
        )
        if not fragment_id:
            raise ValidationError(f"{fpath}.fragment_id", "must be a non-empty string")
        if fragment_id in seen_ids:
            raise ValidationError(
                f"{fpath}.fragment_id", f"duplicate fragment_id {fragment_id!r}"
            )
        seen_ids.add(fragment_id)

        batches_raw = _require_type(
            fragment_raw.get("batch_candidates"),
            f"{fpath}.batch_candidates",
            (list,),
            "batch_candidates",
        )
        if not batches_raw:
            raise ValidationError(
                f"{fpath}.batch_candidates", "at least one batch candidate is required"
            )
        batches: list[str] = []
        for bi, batch in enumerate(batches_raw):
            _require_type(
                batch,
                f"{fpath}.batch_candidates[{bi}]",
                (str,),
                "batch candidate",
            )
            if not batch:
                raise ValidationError(
                    f"{fpath}.batch_candidates[{bi}]", "batch id must not be empty"
                )
            if batch not in batches:
                batches.append(batch)
        unknown_batches = sorted(
            b for b in batches if not any(c.batch == b for c in catalog.copies.values())
        )
        if unknown_batches:
            raise ValidationError(
                f"{fpath}.batch_candidates",
                f"batch {unknown_batches[0]!r} does not exist in the pinned catalog",
            )

        candidates_raw = _require_type(
            fragment_raw.get("candidate_copies"),
            f"{fpath}.candidate_copies",
            (list,),
            "candidate_copies",
        )
        if not candidates_raw:
            raise ValidationError(
                f"{fpath}.candidate_copies", "at least one candidate copy is required"
            )
        if len(candidates_raw) > MAX_CANDIDATES_PER_FRAGMENT:
            raise ValidationError(
                f"{fpath}.candidate_copies",
                f"at most {MAX_CANDIDATES_PER_FRAGMENT} candidates per fragment, "
                f"got {len(candidates_raw)}",
            )
        candidates: list[str] = []
        candidate_batches: set[str] = set()
        for ci, copy_id in enumerate(candidates_raw):
            cpath = f"{fpath}.candidate_copies[{ci}]"
            _require_type(copy_id, cpath, (str,), "candidate copy id")
            if not copy_id:
                raise ValidationError(cpath, "copy id must not be empty")
            if copy_id in candidates:
                raise ValidationError(cpath, "duplicate candidate copy")
            if copy_id not in catalog.copies:
                raise ValidationError(cpath, "no copy with this id in the pinned catalog")
            copy = catalog.copies[copy_id]
            if copy.batch not in batches:
                raise ValidationError(
                    cpath,
                    f"copy belongs to batch {copy.batch!r}, which is not among the "
                    "fragment's batch_candidates",
                )
            candidates.append(copy_id)
            candidate_batches.add(copy.batch)

        event_stage = fragment_raw.get("event_stage")
        if event_stage is not None:
            _require_type(
                event_stage, f"{fpath}.event_stage", (str,), "event_stage"
            )
            if not event_stage:
                raise ValidationError(
                    f"{fpath}.event_stage", "stage must be a non-empty string"
                )

        observations_raw = _require_type(
            fragment_raw.get("observations"),
            f"{fpath}.observations",
            (list,),
            "observations",
        )
        if not observations_raw:
            raise ValidationError(
                f"{fpath}.observations", "at least one observation is required"
            )
        observations: list[Observation] = []
        seen_positions: set[int] = set()
        for oi, obs_raw in enumerate(observations_raw):
            opath = f"{fpath}.observations[{oi}]"
            _require_type(obs_raw, opath, (dict,), "observation")
            _check_known(obs_raw, _KNOWN_OBS, opath)
            position = _require_type(
                obs_raw.get("position"), f"{opath}.position", (int,), "position"
            )
            if isinstance(obs_raw.get("position"), bool) or position < 0:
                raise ValidationError(
                    f"{opath}.position", "must be a non-negative integer"
                )
            if position in seen_positions:
                raise ValidationError(
                    f"{opath}.position",
                    "duplicate position inside fragment; observations must align "
                    "to a single original-position model",
                )
            seen_positions.add(position)

            erased = bool(obs_raw.get("erased", False))
            confidence = obs_raw.get("confidence", "low")
            if confidence not in _CONFIDENCE_LEVELS:
                raise ValidationError(
                    f"{opath}.confidence",
                    f"must be one of {sorted(_CONFIDENCE_LEVELS)}",
                )
            cost = obs_raw.get("cost", 0)
            if isinstance(cost, bool) or not isinstance(cost, int) or cost < 0:
                raise ValidationError(f"{opath}.cost", "must be a non-negative integer")

            if erased:
                if "symbol" in obs_raw and obs_raw["symbol"] is not None:
                    raise ValidationError(
                        f"{opath}.symbol",
                        "erased observations carry no symbol (use null or omit it)",
                    )
                if "cost" in obs_raw and obs_raw["cost"] != 0:
                    raise ValidationError(
                        f"{opath}.cost", "erased observations cannot carry a cost"
                    )
                if confidence != "low":
                    raise ValidationError(
                        f"{opath}.confidence",
                        "erased observations cannot carry high confidence",
                    )
                symbol = None
            else:
                if "symbol" not in obs_raw:
                    raise ValidationError(
                        f"{opath}.symbol", "non-erased observation requires a symbol"
                    )
                symbol = obs_raw["symbol"]
                if not _is_symbol(symbol):
                    raise ValidationError(
                        f"{opath}.symbol",
                        "symbol must be a string or integer (booleans are not allowed)",
                    )
            observations.append(
                Observation(
                    position=position,
                    symbol=symbol,
                    erased=erased,
                    confidence=confidence,
                    cost=cost,
                )
            )
        total_positions += len(observations)
        fragments.append(
            Fragment(
                fragment_id=fragment_id,
                batch_candidates=tuple(batches),
                candidate_copies=tuple(candidates),
                event_stage=event_stage,
                observations=tuple(observations),
            )
        )
    if total_positions > MAX_OBSERVED_POSITIONS:
        raise ValidationError(
            "$.fragments",
            f"the procedure supports at most {MAX_OBSERVED_POSITIONS} observed "
            f"positions across all fragments, got {total_positions}",
        )
    return fragments


def _parse_id_pairs(
    raw: Any, path: str, fragments: list[Fragment]
) -> tuple[tuple[str, str], ...]:
    _require_type(raw, path, (list,), "pair list")
    ids = {f.fragment_id for f in fragments}
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for pi, pair in enumerate(raw):
        ppath = f"{path}[{pi}]"
        _require_type(pair, ppath, (list,), "pair")
        if len(pair) != 2:
            raise ValidationError(ppath, "each pair must contain exactly two fragment ids")
        for member in pair:
            if not isinstance(member, str):
                raise ValidationError(ppath, "fragment ids must be strings")
        a, b = pair
        if a == b:
            raise ValidationError(ppath, "a fragment cannot be paired with itself")
        if a not in ids or b not in ids:
            raise ValidationError(ppath, "pair references an unknown fragment id")
        key = tuple(sorted((a, b)))
        if key in seen:
            raise ValidationError(ppath, "duplicate pair")
        seen.add(key)
        pairs.append((a, b))
    return tuple(pairs)
