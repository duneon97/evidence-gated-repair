from __future__ import annotations

import pytest

from src.duration import parse_duration


def test_hours_only() -> None:
    assert parse_duration("2h") == 7200


def test_zero_hours() -> None:
    assert parse_duration("0h") == 0


def test_unknown_unit_is_rejected() -> None:
    with pytest.raises(ValueError):
        parse_duration("2d")


def test_garbage_is_rejected() -> None:
    with pytest.raises(ValueError):
        parse_duration("banana")


def test_non_string_is_rejected() -> None:
    with pytest.raises(TypeError):
        parse_duration(5)  # type: ignore[arg-type]
