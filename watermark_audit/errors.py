"""Typed exception carrying a precise JSON-path field reference."""

from __future__ import annotations


class ValidationError(Exception):
    """Raised when a case document fails structural validation.

    ``field`` is a JSON-pointer-like path (``catalog.versions[1].copies[2]``)
    so that the caller can locate the offending field exactly.
    """

    def __init__(self, field: str, message: str) -> None:
        self.field = field
        self.message = message
        super().__init__(f"{field}: {message}")
