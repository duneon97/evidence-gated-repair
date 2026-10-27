#!/usr/bin/env python3
"""Evidence-gated repair agent (single-issue MVP).

The agent is not trusted. Every claim it makes has to survive execution:

  1. issue text -> query expansion -> ripgrep candidates (searched code, not the whole world)
  2. the model writes a regression test; we run it on the BASELINE
       - it must fail with a real assertion/exception, not a collection or import error
       - if it passes -> CANNOT_REPRODUCE, the issue is not reproduced
  3. the test file is frozen (sha256 recorded, and every baseline file under tests/ must stay
     byte-identical, so a patch cannot "fix" the bug by editing, deleting or skipping tests)
  4. up to --max-attempts patches: the model proposes full-file contents, we apply them in a
     disposable copy and run the frozen test plus the whole suite inside the sandbox
  5. the gate says VERIFIED only when the frozen test passes, the suite has no new failures,
     and both hashes are unchanged. Anything else is NEEDS_REVIEW with the evidence attached.

Output: one `receipt.json` plus a one-line verdict on stdout. No UI, no server.

Usage:
    python -m agent.repair --repo fixtures/tinybug --issue fixtures/tinybug/ISSUE.md \
        --out receipt.json --runner docker

Secrets are read from --env-file (default /data/.env) and are never logged or written
into the receipt: only the variable name and a boolean are recorded.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

SCHEMA_VERSION = 1
MAX_TOKENS = 8000
HTTP_TIMEOUT = 150
MAX_CONTEXT_CHARS = 7000
IGNORE_DIRS = {".git", "__pycache__", ".pytest_cache", ".venv", "venv", "node_modules", ".mypy_cache"}

VERIFIED = "VERIFIED"
NEEDS_REVIEW = "NEEDS_REVIEW"
CANNOT_REPRODUCE = "CANNOT_REPRODUCE"


# --------------------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------------------
def now() -> float:
    return time.time()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def tail(text: str, limit: int = 2500) -> str:
    text = text or ""
    return text if len(text) <= limit else "…" + text[-limit:]


def load_env(path: str) -> dict[str, str]:
    values: dict[str, str] = {}
    p = Path(path)
    if not p.exists():
        return values
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        values[key.strip()] = val.strip().strip('"').strip("'")
    return values


def repo_files(root: Path) -> dict[str, str]:
    """Map of relative path -> sha256 for every test file that the patch must not touch."""
    out: dict[str, str] = {}
    for path in root.rglob("*.py"):
        if any(part in IGNORE_DIRS for part in path.parts):
            continue
        rel = path.relative_to(root).as_posix()
        if rel.startswith("tests/") or "/tests/" in rel:
            out[rel] = sha256_bytes(path.read_bytes())
    return out


# --------------------------------------------------------------------------------------
# model client: HF router, two tokens, no key ever printed
# --------------------------------------------------------------------------------------
class ModelError(RuntimeError):
    pass


class ModelClient:
    def __init__(self, base_url: str, model: str, tokens: list[tuple[str, str]], receipt: dict):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.tokens = [t for t in tokens if t[1]]
        self.receipt = receipt
        if not self.tokens:
            raise ModelError("no token available in env file")

    def _post(self, payload: dict, token: str, timeout: int) -> tuple[int, dict]:
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError:
                parsed = {"raw": body[:400]}
            return exc.code, parsed
        except Exception as exc:  # noqa: BLE001 - network errors are data, not crashes
            return 0, {"raw": f"{type(exc).__name__}: {exc}"}

    def chat(self, messages: list[dict], *, max_tokens: int = MAX_TOKENS, temperature: float = 0.2,
             label: str = "chat") -> str:
        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        last_error = "no attempt made"
        for round_no in range(1, 4):
            for key_name, token in self.tokens:
                started = now()
                status, body = self._post(payload, token, HTTP_TIMEOUT)
                elapsed = round(now() - started, 2)
                usage = body.get("usage") or {}
                message = ((body.get("choices") or [{}])[0].get("message") or {})
                content = (message.get("content") or "").strip()
                self.receipt["model_calls"].append(
                    {
                        "label": label,
                        "round": round_no,
                        "key_env": key_name,
                        "key_present": bool(token),
                        "http_status": status,
                        "elapsed_s": elapsed,
                        "prompt_tokens": usage.get("prompt_tokens"),
                        "completion_tokens": usage.get("completion_tokens"),
                    }
                )
                if status == 200 and content:
                    return content
                if status == 200 and not content:
                    # reasoning ate the whole budget: retry with a bigger one, same token
                    payload["max_tokens"] = min(payload["max_tokens"] * 3, 32000)
                    last_error = "empty content (reasoning consumed max_tokens)"
                    continue
                last_error = f"HTTP {status}: {json.dumps(body)[:200]}"
                if status in (401, 403):
                    continue  # try the other token
                if status in (429, 500, 502, 503, 504) or status == 0:
                    continue
                raise ModelError(last_error)
            time.sleep(min(2 ** round_no, 8))
        raise ModelError(f"all tokens failed: {last_error}")


def parse_json_reply(text: str) -> dict:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in reply")
    return json.loads(text[start : end + 1])


def ask_json(client: ModelClient, messages: list[dict], label: str, retries: int = 1) -> dict:
    prompt = list(messages)
    last: Exception | None = None
    for attempt in range(retries + 1):
        reply = client.chat(prompt, label=f"{label}{'' if attempt == 0 else f'+repair{attempt}'}")
        try:
            return parse_json_reply(reply)
        except (ValueError, json.JSONDecodeError) as exc:
            last = exc
            prompt = prompt + [
                {"role": "assistant", "content": reply[:2000]},
                {"role": "user", "content": "That was not valid JSON. Reply again with only the JSON object."},
            ]
    raise ModelError(f"{label}: model never returned valid JSON ({last})")


# --------------------------------------------------------------------------------------
# search: ripgrep, no index, no daemon
# --------------------------------------------------------------------------------------
def rg_available() -> bool:
    return shutil.which("rg") is not None


def search_repo(root: Path, queries: list[str], max_files: int = 8) -> dict[str, str]:
    """Return {relative path: snippet} for the queries, best-effort, never fatal."""
    found: dict[str, str] = {}
    if not queries:
        return found
    for query in queries:
        if not query.strip():
            continue
        if rg_available():
            cmd = ["rg", "-n", "-i", "--no-heading", "-m", "6", "-F", query, str(root)]
        else:
            cmd = ["grep", "-rni", "-m", "6", "-F", query, str(root)]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            continue
        for line in proc.stdout.splitlines():
            path_part, _, _rest = line.partition(":")
            try:
                rel = str(Path(path_part).resolve().relative_to(root.resolve()))
            except (ValueError, OSError):
                continue
            if any(part in IGNORE_DIRS for part in rel.split("/")):
                continue
            found.setdefault(rel, "")
            if len(found[rel]) < 1200:
                found[rel] += line.replace(str(root) + "/", "") + "\n"
            if len(found) >= max_files:
                return found
    return found


def build_context(files: dict[str, str], budget: int = MAX_CONTEXT_CHARS) -> str:
    chunks: list[str] = []
    used = 0
    for path, snippet in files.items():
        block = f"### {path}\n{snippet.strip()[:1500]}\n"
        if used + len(block) > budget:
            break
        chunks.append(block)
        used += len(block)
    return "\n".join(chunks)


# --------------------------------------------------------------------------------------
# sandbox runner: docker with no network, or a local fallback for pure-gate testing
# --------------------------------------------------------------------------------------
class Runner:
    def __init__(self, kind: str, image: str, receipt: dict):
        self.kind = kind
        self.image = image
        self.receipt = receipt

    def run(self, work: Path, args: list[str], timeout: int, label: str) -> dict:
        if self.kind == "docker":
            cmd = [
                "docker", "run", "--rm", "--network", "none",
                "--cpus", "1", "--memory", "1g", "--pids-limit", "256",
                "-v", f"{work}:/work", "-w", "/work",
                self.image, *args,
            ]
        else:
            cmd = args
        started = now()
        timed_out = False
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout,
                cwd=None if self.kind == "docker" else str(work),
            )
            stdout, stderr, code = proc.stdout, proc.stderr, proc.returncode
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            stdout = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
            stderr = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
            code = 124
        record = {
            "label": label,
            "command": " ".join(cmd),
            "exit_code": code,
            "timed_out": timed_out,
            "elapsed_s": round(now() - started, 2),
            "stdout_tail": tail(stdout),
            "stderr_tail": tail(stderr),
        }
        self.receipt["commands"].append(record)
        return {"exit_code": code, "stdout": stdout, "stderr": stderr, "timed_out": timed_out}


FAILED_RE = re.compile(r"^(?:FAILED|ERROR)\s+(\S+)", re.M)


def failed_ids(stdout: str) -> set[str]:
    return {m.group(1).split("::")[0] if "::" not in m.group(1) else m.group(1)
            for m in FAILED_RE.finditer(stdout)}


def genuine_reproduction(output: str) -> tuple[bool, str]:
    """A regression test must fail because the bug is there, not because the file is broken."""
    text = output or ""
    for marker, reason in (
        ("ERROR collecting", "collection_error"),
        ("ModuleNotFoundError", "import_error"),
        ("ImportError", "import_error"),
        ("SyntaxError", "syntax_error"),
        ("no tests ran", "no_tests_ran"),
    ):
        if marker in text:
            return False, reason
    if "FAILED" not in text:
        return False, "test_did_not_fail"
    if "AssertionError" in text or re.search(r"\bassert\b", text):
        return True, "assertion_failure"
    if "Error" in text:
        return True, "exception_failure"
    return False, "unclear_failure"


# --------------------------------------------------------------------------------------
# prompts
# --------------------------------------------------------------------------------------
SYSTEM = (
    "You are a repair agent. You have no memory and no shell. You answer with JSON only. "
    "You never claim a fix works; execution evidence decides that. "
    "Python 3.13 and pytest only. No network access at test time."
)

QUERY_PROMPT = """Issue:
{issue}

Available files (paths only):
{paths}

Reply with JSON: {{"queries": ["search string", ...]}} — at most 4 exact strings that appear
in the code related to this issue (function names, error messages, identifiers)."""

TEST_PROMPT = """Issue:
{issue}

Code found by search:
{context}

Write ONE pytest regression test that fails on the current code because of this exact bug and
will pass once it is fixed. Rules: stdlib + pytest only, no network, no sleeps, no fixtures from
conftest, deterministic, and it must assert the behaviour described in the issue.

Reply with JSON: {{"path": "tests/test_regression_<short_name>.py", "content": "<full file>"}}"""

PATCH_PROMPT = """Issue:
{issue}

Search context:
{context}

Frozen regression test (you must NOT modify or delete this file):
{test_content}

Previous attempt evidence:
{evidence}

Return the smallest fix as complete file contents. Do not modify any existing file under tests/.
Reply with JSON: {{"summary": "...", "files": [{{"path": "src/...", "content": "<full file>"}}]}}"""


# --------------------------------------------------------------------------------------
# main flow
# --------------------------------------------------------------------------------------
def copy_repo(src: Path, dst: Path) -> None:
    def ignore(_dir: str, names: list[str]) -> set[str]:
        return {n for n in names if n in IGNORE_DIRS}

    shutil.copytree(src, dst, ignore=ignore)


def safe_join(root: Path, rel: str) -> Path:
    target = (root / rel).resolve()
    if not str(target).startswith(str(root.resolve())):
        raise ValueError(f"path escapes the work copy: {rel}")
    return target


def run_issue(args: argparse.Namespace) -> dict:
    repo = Path(args.repo).resolve()
    issue = Path(args.issue).read_text(encoding="utf-8")
    started = now()
    receipt: dict = {
        "schema_version": SCHEMA_VERSION,
        "verdict": None,
        "reason": None,
        "issue_sha256": sha256_text(issue),
        "repo": {"path": str(repo), "commit": git_commit(repo)},
        "model": {"id": args.model, "base_url": args.base_url},
        "runner": {"kind": args.runner, "image": args.image, "network": "none"},
        "test_file": None,
        "test_sha256_frozen": None,
        "test_sha256_after": None,
        "attempts": [],
        "model_calls": [],
        "commands": [],
        "limits": {"max_attempts": args.max_attempts, "test_timeout_s": args.timeout},
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
    }

    env = load_env(args.env_file)
    tokens = [("HF_TOKEN_MAIN", env.get("HF_TOKEN_MAIN", "")),
              ("HF_TOKEN_FALLBACK", env.get("HF_TOKEN_FALLBACK", ""))]
    if not any(t[1] for t in tokens):
        raise SystemExit(f"no HF token found in {args.env_file} (expected HF_TOKEN_MAIN)")
    client = ModelClient(args.base_url, args.model, tokens, receipt)
    runner = Runner(args.runner, args.image, receipt)

    work_root = Path(args.work_dir or (repo.parent / f".repair-work-{int(started)}"))
    if work_root.exists():
        shutil.rmtree(work_root)
    work_root.mkdir(parents=True)
    baseline = work_root / "baseline"
    copy_repo(repo, baseline)
    baseline_tests = repo_files(baseline)

    # 1. search
    paths = sorted(p.relative_to(baseline).as_posix() for p in baseline.rglob("*.py")
                   if not any(part in IGNORE_DIRS for part in p.parts))
    query_reply = ask_json(client, [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": QUERY_PROMPT.format(issue=issue[:4000], paths="\n".join(paths[:200]))},
    ], label="queries")
    queries = [q for q in (query_reply.get("queries") or []) if isinstance(q, str)][:4]
    hits = search_repo(baseline, queries)
    context = build_context(hits)
    receipt["search"] = {"queries": queries, "files": sorted(hits)}
    if not hits:
        context = "no search hits; use the file list:\n" + "\n".join(paths[:60])

    # 2. regression test on the baseline
    test_reply = ask_json(client, [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": TEST_PROMPT.format(issue=issue[:4000], context=context)},
    ], label="write_test")
    test_path = str(test_reply.get("path") or "").strip()
    test_content = str(test_reply.get("content") or "")
    if not test_path.startswith("tests/") or not test_path.endswith(".py") or not test_content:
        return finish(receipt, CANNOT_REPRODUCE, "model did not return a usable regression test")
    receipt["test_file"] = test_path
    target = safe_join(baseline, test_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(test_content, encoding="utf-8")
    frozen_sha = sha256_bytes(target.read_bytes())
    receipt["test_sha256_frozen"] = frozen_sha

    baseline_run = runner.run(baseline, ["python", "-m", "pytest", "-q", "--tb=short", test_path],
                              args.timeout, "baseline_regression_test")
    genuine, why = genuine_reproduction(baseline_run["stdout"] + baseline_run["stderr"])
    receipt["baseline"] = {"genuine": genuine, "reason": why}
    if not genuine:
        return finish(receipt, CANNOT_REPRODUCE, f"regression test did not reproduce the bug ({why})")

    # baseline suite state, so we can tell new failures from pre-existing ones
    suite_base = runner.run(baseline, ["python", "-m", "pytest", "-q", "--tb=no", "-rf"],
                            args.timeout, "baseline_suite")
    baseline_failures = failed_ids(suite_base["stdout"])
    receipt["baseline"]["suite_failures"] = sorted(baseline_failures)

    # 3. patch loop
    evidence = "none yet"
    for attempt in range(1, args.max_attempts + 1):
        patch_reply = ask_json(client, [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": PATCH_PROMPT.format(
                issue=issue[:3000], context=context, test_content=test_content[:3000], evidence=evidence)},
        ], label=f"patch_attempt_{attempt}")
        files = patch_reply.get("files") or []
        attempt_rec: dict = {"n": attempt, "summary": patch_reply.get("summary"), "files": [],
                             "rejected": None, "gate": None}

        candidate = work_root / f"candidate_{attempt}"
        copy_repo(baseline, candidate)
        rejected = None
        for item in files:
            rel = str(item.get("path") or "")
            content = item.get("content")
            if not rel or not isinstance(content, str):
                rejected = "malformed file entry"
                break
            if rel == test_path or rel in baseline_tests:
                rejected = f"patch tried to modify a frozen test: {rel}"
                break
            dest = safe_join(candidate, rel)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")
            attempt_rec["files"].append(rel)
        if not files:
            rejected = "patch returned no files"
        if rejected is None:
            touched = {p: sha256_bytes((candidate / p).read_bytes()) for p in baseline_tests}
            changed = [p for p, h in touched.items() if baseline_tests[p] != h]
            if changed:
                rejected = f"patch changed frozen test files: {changed}"

        if rejected:
            attempt_rec["rejected"] = rejected
            receipt["attempts"].append(attempt_rec)
            evidence = f"{rejected}. Previous attempt was refused before execution."
            continue

        frozen_now = sha256_bytes(safe_join(candidate, test_path).read_bytes())
        if frozen_now != frozen_sha:
            attempt_rec["rejected"] = "frozen test hash changed"
            receipt["attempts"].append(attempt_rec)
            evidence = "the regression test file changed; it is frozen, do not touch it."
            continue

        test_run = runner.run(candidate, ["python", "-m", "pytest", "-q", "--tb=short", test_path],
                              args.timeout, f"candidate_{attempt}_frozen_test")
        suite_run = runner.run(candidate, ["python", "-m", "pytest", "-q", "--tb=no", "-rf"],
                               args.timeout, f"candidate_{attempt}_suite")
        cand_failures = failed_ids(suite_run["stdout"])
        new_failures = sorted(cand_failures - baseline_failures)
        gate = {
            "frozen_test_passed": test_run["exit_code"] == 0,
            "frozen_exit_code": test_run["exit_code"],
            "new_failures": new_failures,
            "suite_exit_code": suite_run["exit_code"],
        }
        attempt_rec["gate"] = gate
        receipt["attempts"].append(attempt_rec)
        receipt["test_sha256_after"] = frozen_now

        if gate["frozen_test_passed"] and not new_failures:
            receipt["candidate_dir"] = str(candidate)
            receipt["diff"] = git_diff(baseline, candidate)
            return finish(receipt, VERIFIED, "frozen test passes and the suite has no new failures")

        if not gate["frozen_test_passed"]:
            evidence = ("the frozen test still failed:\n"
                        + tail(test_run["stdout"] + test_run["stderr"], 1800))
        else:
            evidence = ("the frozen test passed but the suite regressed in: "
                        + ", ".join(new_failures[:5]))

    return finish(receipt, NEEDS_REVIEW, "no attempt satisfied the gate; inspect the receipt")


def git_commit(root: Path) -> str | None:
    try:
        proc = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=20)
        return proc.stdout.strip() or None
    except Exception:  # noqa: BLE001
        return None


def git_diff(a: Path, b: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", "diff", "--no-index", "--stat", "--", str(a), str(b)],
            capture_output=True, text=True, timeout=60,
        )
        return proc.stdout.strip()[:4000]
    except Exception:  # noqa: BLE001
        return ""


def finish(receipt: dict, verdict: str, reason: str) -> dict:
    receipt["verdict"] = verdict
    receipt["reason"] = reason
    receipt["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evidence-gated repair agent (one issue, one repo).")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--issue", required=True)
    parser.add_argument("--out", default="receipt.json")
    parser.add_argument("--runner", choices=("docker", "local"), default="docker")
    parser.add_argument("--image", default="repairgate-sandbox:py313")
    parser.add_argument("--model", default="meta-models/Muse-Spark-Open-816B-A42B")
    parser.add_argument("--base-url", default="https://router.huggingface.co/v1")
    parser.add_argument("--env-file", default="/data/.env")
    parser.add_argument("--max-attempts", type=int, default=2)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--work-dir", default=None)
    args = parser.parse_args(argv)

    receipt = run_issue(args)
    Path(args.out).write_text(json.dumps(receipt, indent=2, ensure_ascii=False), encoding="utf-8")
    model_calls = len(receipt.get("model_calls") or [])
    print(f"{receipt['verdict']}: {receipt['reason']} "
          f"(model_calls={model_calls}, attempts={len(receipt.get('attempts') or [])}, "
          f"receipt={args.out})")
    return 0 if receipt["verdict"] == VERIFIED else 1


if __name__ == "__main__":
    sys.exit(main())
