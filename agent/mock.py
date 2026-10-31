"""Offline, fixture-specific responses, never a fallback for a real provider."""

from __future__ import annotations

import json

SCENARIOS = ("good_patch", "bad_patch", "cannot_reproduce")
TEST_PATH = "tests/test_regression_duration.py"
REGRESSION_TEST = '''from src.duration import parse_duration


def test_documented_duration_formats():
    assert parse_duration("90m") == 5400
    assert parse_duration("1h30m") == 5400
    assert parse_duration("2H") == 7200
'''
PASSING_TEST = '''from src.duration import parse_duration


def test_already_supported_hours():
    assert parse_duration("2h") == 7200
'''
FIXED_SOURCE = '''"""Duration parsing helpers for the tinybug fixture."""

import re

_PATTERN = re.compile(r"(?:(?P<hours>\\d+)h)?(?:(?P<minutes>\\d+)m)?", re.I)


def parse_duration(text: str) -> int:
    if not isinstance(text, str):
        raise TypeError("text must be str")
    match = _PATTERN.fullmatch(text.strip())
    if not match or not any(match.groupdict().values()):
        raise ValueError(f"invalid duration: {text!r}")
    return int(match.group("hours") or 0) * 3600 + int(match.group("minutes") or 0) * 60
'''


class MockModelClient:
    def __init__(self, scenario: str, receipt: dict):
        if scenario not in SCENARIOS:
            raise ValueError("unknown mock scenario")
        self.scenario = scenario
        self.receipt = receipt

    def chat(self, messages: list[dict], *, label: str = "chat", **kwargs) -> str:
        if label == "queries":
            reply = {"queries": ["parse_duration", "invalid duration"]}
        elif label == "write_test":
            reply = {
                "path": TEST_PATH,
                "content": PASSING_TEST if self.scenario == "cannot_reproduce" else REGRESSION_TEST,
            }
        elif label.startswith("patch_attempt_"):
            bad = self.scenario == "bad_patch"
            reply = {
                "summary": "Weaken the frozen test" if bad else "Support documented duration formats",
                "files": [{
                    "path": TEST_PATH if bad else "src/duration.py",
                    "content": PASSING_TEST if bad else FIXED_SOURCE,
                }],
            }
        else:
            raise ValueError(f"unsupported mock call: {label}")
        self.receipt["model_calls"].append({
            "label": label, "mock": True, "scenario": self.scenario,
            "prompt_tokens": 0, "completion_tokens": 0,
        })
        return json.dumps(reply)
