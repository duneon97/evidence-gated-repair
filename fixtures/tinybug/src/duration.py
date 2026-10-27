"""Duration parsing helpers for the tinybug fixture.

Supported formats are documented in README.md.
"""

from __future__ import annotations

import re

_PATTERN = re.compile(r"^(?:(?P<hours>\d+)h)$")


def parse_duration(text: str) -> int:
    """Parse a duration such as ``2h`` and return whole seconds."""
    if not isinstance(text, str):
        raise TypeError("text must be str")
    match = _PATTERN.match(text.strip())
    if not match:
        raise ValueError(f"invalid duration: {text!r}")
    hours = int(match.group("hours") or 0)
    return hours * 3600
