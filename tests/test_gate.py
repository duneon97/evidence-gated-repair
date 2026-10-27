"""Gate logic tests. No network, no docker: pure functions only.

Run: python -m pytest tests/test_gate.py -q
"""

from __future__ import annotations

import json

import pytest

from agent.repair import (
    MAX_CONTEXT_CHARS,
    build_context,
    failed_ids,
    genuine_reproduction,
    parse_json_reply,
    safe_join,
    sha256_text,
)
from pathlib import Path


class TestGenuineReproduction:
    def test_assertion_failure_counts(self) -> None:
        out = "FAILED tests/test_regression_x.py::test_it - AssertionError: assert 1 == 90\n1 failed"
        ok, why = genuine_reproduction(out)
        assert ok is True
        assert why == "assertion_failure"

    def test_collection_error_does_not_count(self) -> None:
        out = "ERROR collecting tests/test_regression_x.py\nModuleNotFoundError: No module named 'nope'"
        ok, why = genuine_reproduction(out)
        assert ok is False
        assert why in {"collection_error", "import_error"}

    def test_passing_test_does_not_count(self) -> None:
        out = "1 passed in 0.01s"
        ok, why = genuine_reproduction(out)
        assert ok is False
        assert why == "test_did_not_fail"

    def test_no_tests_ran_does_not_count(self) -> None:
        out = "no tests ran in 0.01s\nERROR: file or directory not found"
        ok, why = genuine_reproduction(out)
        assert ok is False


class TestFailedIds:
    def test_parses_failed_and_error_lines(self) -> None:
        out = (
            "FAILED tests/test_a.py::test_one - assert 1\n"
            "ERROR tests/test_b.py::test_two - boom\n"
            "3 passed, 1 failed\n"
        )
        assert failed_ids(out) == {"tests/test_a.py::test_one", "tests/test_b.py::test_two"}

    def test_empty_output(self) -> None:
        assert failed_ids("") == set()


class TestSafeJoin:
    def test_rejects_traversal(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            safe_join(tmp_path, "../../etc/passwd")

    def test_allows_nested_file(self, tmp_path: Path) -> None:
        assert safe_join(tmp_path, "src/x.py") == tmp_path.resolve() / "src/x.py"


class TestJsonReply:
    def test_plain_object(self) -> None:
        assert parse_json_reply('{"a": 1}') == {"a": 1}

    def test_fenced_object(self) -> None:
        assert parse_json_reply('```json\n{"a": 1}\n```') == {"a": 1}

    def test_prose_around_object(self) -> None:
        assert parse_json_reply('Here you go: {"a": 1} hope it helps') == {"a": 1}

    def test_no_object_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_json_reply("no json here")


class TestContextBudget:
    def test_context_is_bounded(self) -> None:
        files = {f"src/f{i}.py": "x" * 2000 for i in range(20)}
        assert len(build_context(files)) <= MAX_CONTEXT_CHARS + 200

    def test_hash_is_stable(self) -> None:
        assert sha256_text("abc") == sha256_text("abc")
        assert sha256_text("abc") != sha256_text("abd")
        assert json.dumps({"k": sha256_text("abc")})  # importable smoke
