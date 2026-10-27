#!/usr/bin/env python3
"""Verify project credentials without ever printing them.

Checks each HF token in /root/.hermes/.env against the HF Inference router with one
minimal chat completion, then optionally the InstaBox SSH alias.

Prints only: variable name -> HTTP status, verdict, latency. Never the value.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import urllib.error
import urllib.request

ENV_FILE = "/root/.hermes/.env"
BASE_URL = "https://router.huggingface.co/v1"
MODEL = "meta-models/Muse-Spark-Open-816B-A42B"
TOKEN_VARS = ("HF_TOKEN_MAIN", "HF_TOKEN_FALLBACK")


def load_env(path: str) -> dict[str, str]:
    values: dict[str, str] = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            values[k.strip()] = v.strip().strip('"').strip("'")
    return values


def probe_token(token: str) -> dict:
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
        "max_tokens": 800,
        "temperature": 0,
    }
    req = urllib.request.Request(
        f"{BASE_URL}/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    import time

    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = json.loads(resp.read().decode())
            status = resp.status
    except urllib.error.HTTPError as exc:
        return {"status": exc.code, "ok": False, "elapsed_s": round(time.monotonic() - started, 3),
                "reason": exc.read().decode(errors="replace")[:200]}
    except Exception as exc:  # noqa: BLE001 - network failure must not kill the report
        return {"status": None, "ok": False, "elapsed_s": round(time.monotonic() - started, 3),
                "reason": type(exc).__name__}
    msg = (body.get("choices") or [{}])[0].get("message") or {}
    text = (msg.get("content") or "").strip()
    return {
        "status": status,
        "ok": status == 200 and bool(text),
        "elapsed_s": round(time.monotonic() - started, 3),
        "reply_words": len(text.split()),
        "model_echo": body.get("model"),
    }


def check_ssh(alias: str) -> dict:
    """alias may be a plain host alias or a full command such as 'ssh -i KEY user@host'."""
    remote = (
        "uname -srm; python3 -V; docker --version 2>/dev/null || echo 'docker: absent'; "
        "nproc; free -g | awk '/^Mem:/{print \"mem_gib:\", $2}'; "
        "df -h /data 2>/dev/null | tail -1 || echo '/data: absent'; "
        "test -f /data/.env && echo 'data_env: present' || echo 'data_env: absent'"
    )
    base = shlex.split(alias)
    if base and base[0] != "ssh":
        base = ["ssh", *base]
    cmd = [base[0], "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", *base[1:], remote]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=90,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": "timeout"}
    except FileNotFoundError:
        return {"ok": False, "reason": "ssh client missing"}
    if proc.returncode != 0:
        return {"ok": False, "returncode": proc.returncode, "reason": proc.stderr.strip()[:200]}
    return {"ok": True, "facts": proc.stdout.strip().splitlines()}


def main() -> int:
    env = load_env(ENV_FILE)
    report: dict = {"env_file": ENV_FILE}
    for name in TOKEN_VARS:
        if name not in env or not env[name]:
            report[name] = {"present": False}
            continue
        result = probe_token(env[name])
        result["present"] = True
        report[name] = result

    alias = env.get("SSH_ALIAS_INSTABOX", "").strip()
    report["instabox"] = {"alias_configured": bool(alias)}
    if alias:
        report["instabox"].update(check_ssh(alias))

    print(json.dumps(report, indent=2))
    tokens_ok = all(report.get(n, {}).get("ok") for n in TOKEN_VARS)
    return 0 if tokens_ok and report["instabox"].get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
