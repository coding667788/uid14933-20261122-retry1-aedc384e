"""Command line entry point.

Usage::

    python -m watermark_audit CASE.json [OUTPUT.json]

The report is UTF-8 JSON with deterministic key ordering and no timestamps, so
re-running the same case document byte-for-byte reproduces the same report.
Parse failures print the precise offending field to stderr and exit with code 2.
"""

from __future__ import annotations

import json
import sys

from .errors import ValidationError
from .model import parse_case
from .report import build_report


def _load_case(path: str):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        raise ValidationError(path, f"cannot read case file: {exc}") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValidationError(
            f"{path}:{exc.lineno}:{exc.colno}",
            f"invalid JSON syntax: {exc.msg}",
        ) from exc


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) < 1 or len(args) > 2 or args[0] in {"-h", "--help"}:
        print(
            "usage: python -m watermark_audit CASE.json [OUTPUT.json]",
            file=sys.stderr,
        )
        return 2
    try:
        raw = _load_case(args[0])
        case = parse_case(raw)
        report = build_report(case)
    except ValidationError as exc:
        print(f"validation error at {exc.field}: {exc.message}", file=sys.stderr)
        return 2

    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if len(args) == 2:
        with open(args[1], "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
    else:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        sys.stdout.write(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
