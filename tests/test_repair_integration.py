from __future__ import annotations

import argparse
import json
import urllib.request
from pathlib import Path

import pytest

from agent.demo import main as demo_main
from agent.mock import MockModelClient, TEST_PATH
from agent.repair import Runner, copy_repo, main, run_issue, safe_join

ROOT = Path(__file__).resolve().parents[1]


def options(tmp_path, **kwargs):
    defaults = dict(
        repo=str(ROOT / "fixtures/tinybug"), issue=str(ROOT / "fixtures/tinybug/ISSUE.md"),
        provider="mock", mock_scenario="good_patch", model=None, base_url=None,
        env_file=str(tmp_path / "missing.env"), runner="local", image="repairgate-sandbox:py313",
        work_dir=str(tmp_path / "work"), max_attempts=2, timeout=20,
    )
    return argparse.Namespace(**(defaults | kwargs))


@pytest.mark.parametrize(("scenario", "verdict", "command_count"), [
    ("good_patch", "VERIFIED", 4),
    ("bad_patch", "NEEDS_REVIEW", 2),
    ("cannot_reproduce", "CANNOT_REPRODUCE", 1),
])
def test_offline_scenarios_execute_real_gate(tmp_path, monkeypatch, scenario, verdict, command_count):
    def no_network(*args, **kwargs):
        pytest.fail("offline mock must not make an HTTP request")

    monkeypatch.setattr(urllib.request, "urlopen", no_network)
    issue = "ISSUE_ALREADY_SUPPORTED.md" if scenario == "cannot_reproduce" else "ISSUE.md"
    receipt = run_issue(options(
        tmp_path, mock_scenario=scenario, issue=str(ROOT / "fixtures/tinybug" / issue),
    ))
    assert receipt["verdict"] == verdict
    assert receipt["model"]["mode"] == "mock"
    assert receipt["runner"]["sandbox"] is False
    assert receipt["runner"]["network"] == "host (NOT a sandbox)"
    assert receipt["test_sha256_frozen"] == receipt["test_sha256_after"]
    assert len(receipt["commands"]) == command_count
    assert receipt["repo"]["commit"]
    if scenario == "good_patch":
        assert receipt["commands"][0]["exit_code"] == 1
        assert receipt["commands"][-1]["exit_code"] == 0
        assert "6 passed" in receipt["commands"][-1]["stdout_tail"]
        assert receipt["attempts"][0]["files"] == ["src/duration.py"]
    elif scenario == "bad_patch":
        assert len(receipt["attempts"]) == 2
        assert all("frozen test" in attempt["rejected"] for attempt in receipt["attempts"])
        assert all(attempt["gate"] is None for attempt in receipt["attempts"])
    else:
        assert receipt["commands"][0]["exit_code"] == 0
        assert receipt["attempts"] == []


@pytest.mark.parametrize("suite_code", [1, 2, 3, 5, 124])
def test_gate_never_verifies_failed_suite_without_failure_ids(tmp_path, monkeypatch, suite_code):
    original = Runner.run

    def run(self, work, args, timeout, label):
        if label.endswith("_suite") and label.startswith("candidate"):
            return {"exit_code": suite_code, "stdout": "", "stderr": "suite did not pass",
                    "timed_out": suite_code == 124}
        return original(self, work, args, timeout, label)

    monkeypatch.setattr(Runner, "run", run)
    receipt = run_issue(options(tmp_path, max_attempts=1))
    assert receipt["verdict"] == "NEEDS_REVIEW"
    assert receipt["attempts"][0]["gate"]["suite_exit_code"] == suite_code


@pytest.mark.parametrize("path", [
    "tests/test_duration.py", "src/../" + TEST_PATH, "tests/../" + TEST_PATH,
    "../work-neighbor/stolen.py", "conftest.py", "pytest.ini",
])
def test_test_edits_aliases_configuration_and_path_escapes_are_blocked(tmp_path, monkeypatch, path):
    original = MockModelClient.chat

    def chat(self, messages, *, label, **kwargs):
        reply = json.loads(original(self, messages, label=label, **kwargs))
        if label.startswith("patch_attempt_"):
            reply["files"] = [{"path": path, "content": "assert True\n"}]
        return json.dumps(reply)

    monkeypatch.setattr(MockModelClient, "chat", chat)
    receipt = run_issue(options(tmp_path, max_attempts=1))
    assert receipt["verdict"] == "NEEDS_REVIEW"
    assert receipt["attempts"][0]["rejected"]
    assert receipt["attempts"][0]["gate"] is None


def test_safe_join_rejects_sibling_with_shared_prefix_and_absolute_path(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    with pytest.raises(ValueError):
        safe_join(root, "../work-other/file.py")
    with pytest.raises(ValueError):
        safe_join(root, str(root / "file.py"))


@pytest.mark.parametrize("path", [TEST_PATH, "tests/test_duration.py"])
def test_execution_cannot_change_tests_after_pre_run_hash_check(tmp_path, monkeypatch, path):
    original = Runner.run

    def run(self, work, args, timeout, label):
        result = original(self, work, args, timeout, label)
        if label.startswith("candidate") and label.endswith("_suite"):
            assert result["exit_code"] == 0
            (work / path).write_text("def test_weakened():\n    assert True\n")
        return result

    monkeypatch.setattr(Runner, "run", run)
    receipt = run_issue(options(tmp_path, max_attempts=1))
    assert receipt["verdict"] == "NEEDS_REVIEW"
    assert receipt["attempts"][0]["rejected"] == "test hashes changed during execution"


@pytest.mark.parametrize("baseline_result", [
    {"exit_code": 124, "stdout": "FAILED x - AssertionError", "stderr": "", "timed_out": True},
    {"exit_code": 127, "stdout": "", "stderr": "runner unavailable", "timed_out": False},
    {"exit_code": 1, "stdout": "", "stderr": "No module named pytest", "timed_out": False},
])
def test_infrastructure_failures_are_not_reproduction_evidence(tmp_path, monkeypatch, baseline_result):
    monkeypatch.setattr(Runner, "run", lambda *a: baseline_result)
    receipt = run_issue(options(tmp_path))
    assert receipt["verdict"] == "NEEDS_REVIEW"
    assert receipt["attempts"] == []


def test_collection_error_returns_cannot_reproduce(tmp_path, monkeypatch):
    monkeypatch.setattr(Runner, "run", lambda *a: {
        "exit_code": 2, "stdout": "ERROR collecting test\nModuleNotFoundError",
        "stderr": "", "timed_out": False,
    })
    assert run_issue(options(tmp_path))["verdict"] == "CANNOT_REPRODUCE"


def test_existing_work_directory_is_not_deleted(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    marker = work / "keep.txt"
    marker.write_text("user work")
    receipt = run_issue(options(tmp_path))
    assert receipt["verdict"] == "NEEDS_REVIEW"
    assert marker.read_text() == "user work"


def test_copy_does_not_mount_dotenv_files(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    for name in (".env", ".env.production", ".env.example", "module.py"):
        (source / name).write_text("# placeholder\n")
    copy_repo(source, tmp_path / "copy")
    assert sorted(p.name for p in (tmp_path / "copy").iterdir()) == [".env.example", "module.py"]


def test_cli_defaults_to_docker_and_writes_receipt_on_missing_live_credentials(tmp_path):
    receipt_path = tmp_path / "receipt.json"
    code = main([
        "--repo", str(ROOT / "fixtures/tinybug"),
        "--issue", str(ROOT / "fixtures/tinybug/ISSUE.md"),
        "--env-file", str(tmp_path / "missing.env"), "--out", str(receipt_path),
    ])
    receipt = json.loads(receipt_path.read_text())
    assert code == 1
    assert receipt["verdict"] == "NEEDS_REVIEW"
    assert receipt["runner"]["kind"] == "docker"
    assert receipt["model"]["mode"] == "live"
    assert receipt["commands"] == []
    assert "live mode never uses mock" in receipt["reason"]


def test_demo_is_repeatable_and_checks_expected_not_only_success_verdicts(tmp_path, capsys):
    for _ in range(2):
        assert demo_main(["--out-dir", str(tmp_path)]) == 0
    output = capsys.readouterr().out
    assert "NOT a sandbox" in output
    for verdict in ("VERIFIED", "NEEDS_REVIEW", "CANNOT_REPRODUCE"):
        assert verdict in output
    assert output.count("PASS") == 6
    assert len(list(tmp_path.glob("run-*/*.receipt.json"))) == 6
