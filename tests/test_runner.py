from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from agent.repair import Runner


def test_local_runs_in_work_directory_without_provider_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_TOKEN_MAIN", "test-only-placeholder")
    monkeypatch.setenv("LLAMA_API_KEY", "test-only-placeholder")
    receipt = {"commands": []}
    result = Runner("local", "", receipt).run(tmp_path, [
        "python", "-c",
        "import os, json; print(json.dumps([os.getcwd(), "
        "'HF_TOKEN_MAIN' in os.environ, 'LLAMA_API_KEY' in os.environ]))",
    ], 10, "environment")
    assert result["exit_code"] == 0
    assert json.loads(result["stdout"]) == [str(tmp_path), False, False]
    assert receipt["commands"][0]["label"] == "environment"


def test_local_timeout_keeps_logs_and_fails_closed(tmp_path):
    receipt = {"commands": []}
    result = Runner("local", "", receipt).run(
        tmp_path, ["python", "-c", "import time; print('started', flush=True); time.sleep(30)"],
        0.2, "timeout",
    )
    assert result["timed_out"] is True
    assert result["exit_code"] == 124
    assert "started" in result["stdout"]
    assert receipt["commands"][0]["timed_out"] is True


def test_missing_docker_is_a_failed_command_not_a_crash(tmp_path, monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(subprocess, "Popen", missing)
    receipt = {"commands": []}
    result = Runner("docker", "repairgate-sandbox:py313", receipt).run(
        tmp_path, ["python", "-m", "pytest"], 10, "missing_docker",
    )
    assert result["exit_code"] == 127
    command = receipt["commands"][0]["command"]
    for flag in ("--network none", "--cpus 1", "--memory 1g", "--pids-limit 256"):
        assert flag in command


def test_unknown_runner_is_rejected():
    with pytest.raises(ValueError):
        Runner("typo", "", {"commands": []})


def test_docker_timeout_attempts_container_cleanup_even_if_daemon_stalls(tmp_path, monkeypatch):
    class Process:
        pid = 12345
        calls = 0

        def communicate(self, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired("docker", timeout)
            return "partial log", ""

    cleanup = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: Process())
    monkeypatch.setattr("agent.repair.os.killpg", lambda *a: None)

    def stalled_cleanup(command, **kwargs):
        cleanup.append(command)
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", stalled_cleanup)
    receipt = {"commands": []}
    result = Runner("docker", "repairgate-sandbox:py313", receipt).run(
        tmp_path, ["python", "-m", "pytest"], 1, "docker_timeout",
    )
    assert result["exit_code"] == 124
    assert result["timed_out"] is True
    assert cleanup[0][:3] == ["docker", "rm", "-f"]
    assert cleanup[0][3].startswith("repairgate-")
    assert result["stdout"] == "partial log"


def test_docker_image_contract_when_available():
    if not shutil.which("docker"):
        pytest.skip("Docker executable is unavailable")
    try:
        ready = subprocess.run(["docker", "info"], capture_output=True, timeout=10)
        if ready.returncode:
            pytest.skip("Docker daemon is unavailable")
        image = subprocess.run(
            ["docker", "image", "inspect", "repairgate-sandbox:py313"],
            capture_output=True, timeout=10,
        )
        if image.returncode:
            pytest.skip("Build the sandbox image to run its integration contract")
    except (OSError, subprocess.TimeoutExpired):
        pytest.skip("Docker is unavailable or unresponsive")
    receipt = {"commands": []}
    smoke = Path(__file__).resolve().parents[1] / "sandbox/smoke"
    result = Runner("docker", "repairgate-sandbox:py313", receipt).run(
        smoke, ["python", "-m", "pytest", "-q", "-o", "cache_dir=/tmp/pytest-cache"],
        30, "sandbox_contract",
    )
    assert result["exit_code"] == 0, result["stdout"] + result["stderr"]
    assert "4 passed" in result["stdout"]
