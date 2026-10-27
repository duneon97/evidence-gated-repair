"""Contract test for the sandbox image.

Run inside the container, never on the host:

    docker run --rm --network none -v "$PWD/sandbox/smoke:/work" -w /work \
        repairgate-sandbox:py313 python -m pytest -q

If any of these fail, the runner is not safe enough to execute a candidate patch:
a patch must not be able to reach the network, must not run as root, and must not
see the agent's environment.
"""

from __future__ import annotations

import os
import socket

import pytest


def test_runs_unprivileged() -> None:
    assert os.geteuid() != 0, "the test process must not be root"


def test_network_is_blocked() -> None:
    with pytest.raises(OSError):
        socket.create_connection(("1.1.1.1", 443), timeout=3)


def test_no_agent_secrets_in_env() -> None:
    leaked = [k for k in os.environ if k.startswith("HF_TOKEN") or k.startswith("SSH_")]
    assert leaked == [], f"secrets leaked into the sandbox: {leaked}"


def test_filesystem_is_limited_to_mount() -> None:
    # /data/.env lives on the machine hosting the container, never inside it.
    assert not os.path.exists("/data/.env")
