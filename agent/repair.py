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
     and test hashes are unchanged after execution. Anything else is NEEDS_REVIEW.

Output: one `receipt.json` plus a one-line verdict on stdout. No UI, no server.

Usage:
    python -m agent.repair --repo fixtures/tinybug --issue fixtures/tinybug/ISSUE.md \
        --out receipt.json --runner docker

Secrets are read from the environment or --env-file (default .env), never logged or written
into the receipt: only the variable name and a boolean are recorded.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from agent.mock import MockModelClient, SCENARIOS

SCHEMA_VERSION = 2
MAX_TOKENS = 8000
HTTP_TIMEOUT = 150
MAX_CONTEXT_CHARS = 7000
IGNORE_DIRS = {".git", "__pycache__", ".pytest_cache", ".venv", "venv", "node_modules", ".mypy_cache"}
DEFAULT_MODEL = "meta-llama/Llama-3.3-70B-Instruct"
HF_BASE_URL = "https://router.huggingface.co/v1"

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
# model client: real HF and native Meta endpoints, or an explicit offline mock
# --------------------------------------------------------------------------------------
class ModelError(RuntimeError):
    pass


class ModelClient:
    def __init__(self, base_url: str, model: str, tokens: list[tuple[str, str]], receipt: dict,
                 provider: str = "hf"):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.tokens = [t for t in tokens if t[1]]
        self.receipt = receipt
        self.provider = provider
        if not self.tokens:
            raise ModelError("no provider credential configured")

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
        except Exception:  # noqa: BLE001 - do not expose credential-bearing URLs/errors
            return 0, {}

    def chat(self, messages: list[dict], *, max_tokens: int = MAX_TOKENS, temperature: float = 0.2,
             label: str = "chat") -> str:
        # Meta's native API uses max_completion_tokens and completion_message,
        # unlike the HF router's OpenAI-compatible request/response schema.
        budget_key = "max_completion_tokens" if self.provider == "llama" else "max_tokens"
        payload = {
            "model": self.model,
            "messages": messages,
            budget_key: max_tokens,
            "temperature": temperature,
        }
        last_error = "no attempt made"
        for round_no in range(1, 4):
            for key_name, token in self.tokens:
                started = now()
                status, body = self._post(payload, token, HTTP_TIMEOUT)
                elapsed = round(now() - started, 2)
                usage = body.get("usage") or {}
                message = (body.get("completion_message")
                           or ((body.get("choices") or [{}])[0].get("message") or {}))
                content = message.get("content") or ""
                if isinstance(content, dict):
                    content = content.get("text", "")
                content = content.strip() if isinstance(content, str) else ""
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
                    payload[budget_key] = min(payload[budget_key] * 3, 32000)
                    last_error = "empty content (reasoning consumed max_tokens)"
                    continue
                # Provider error bodies can echo credentials or private endpoint details.
                last_error = f"HTTP {status}"
                if status in (401, 403):
                    continue  # try the other token
                if status in (429, 500, 502, 503, 504) or status == 0:
                    continue
                raise ModelError(last_error)
            time.sleep(min(2 ** round_no, 8))
        raise ModelError(f"all tokens failed: {last_error}")


def create_client(args: argparse.Namespace, receipt: dict) -> ModelClient | MockModelClient:
    """CLI overrides process environment, which overrides nonempty dotenv settings."""
    env = {k: v for k, v in load_env(args.env_file).items() if v}
    env.update({k: v for k, v in os.environ.items() if v})
    provider = args.provider or env.get("MODEL_PROVIDER", "hf")
    if provider not in {"hf", "llama", "mock"}:
        raise ModelError("MODEL_PROVIDER must be hf, llama, or mock")
    receipt["model"] = {"provider": provider, "mode": "mock" if provider == "mock" else "live"}
    if provider == "mock":
        receipt["model"].update({"id": "fixture-mock-v1", "scenario": args.mock_scenario})
        return MockModelClient(args.mock_scenario, receipt)
    if args.mock_scenario != "good_patch":
        raise ModelError("--mock-scenario requires --provider mock")
    if provider == "hf":
        model = args.model or env.get("MODEL_ID", DEFAULT_MODEL)
        base_url = args.base_url or env.get("HF_BASE_URL", HF_BASE_URL)
        tokens = [(key, env.get(key, "")) for key in ("HF_TOKEN_MAIN", "HF_TOKEN_FALLBACK")]
    else:
        model = args.model or env.get("MODEL_ID")
        base_url = args.base_url or env.get("LLAMA_API_BASE")
        tokens = [("LLAMA_API_KEY", env.get("LLAMA_API_KEY", ""))]
        if not model or not base_url:
            raise ModelError("llama requires MODEL_ID and LLAMA_API_BASE (or CLI overrides)")
    # Never put configurable endpoint URLs in receipts.
    receipt["model"]["id"] = model
    if not base_url.startswith("https://"):
        raise ModelError("live provider base URL must use HTTPS")
    if not any(token for _, token in tokens):
        required = "HF_TOKEN_MAIN / HF_TOKEN_FALLBACK" if provider == "hf" else "LLAMA_API_KEY"
        raise ModelError(f"no credential configured: set {required}; live mode never uses mock")
    return ModelClient(base_url, model, tokens, receipt, provider=provider)


def parse_json_reply(text: str) -> dict:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in reply")
    return json.loads(text[start : end + 1])


def ask_json(client: ModelClient | MockModelClient, messages: list[dict],
             label: str, retries: int = 1) -> dict:
    prompt = list(messages)
    last: Exception | None = None
    for attempt in range(retries + 1):
        reply = client.chat(prompt, label=f"{label}{'' if attempt == 0 else f'+repair{attempt}'}")
        try:
            parsed = parse_json_reply(reply)
            if not isinstance(parsed, dict):
                raise ValueError("reply must be an object")
            return parsed
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
        if kind not in {"docker", "local"}:
            raise ValueError("runner must be docker or local")
        self.kind = kind
        self.image = image
        self.receipt = receipt

    def run(self, work: Path, args: list[str], timeout: int, label: str) -> dict:
        container_name = f"repairgate-{uuid.uuid4().hex}"
        if self.kind == "docker":
            cmd = [
                "docker", "run", "--rm", "--name", container_name, "--network", "none",
                "--cpus", "1", "--memory", "1g", "--pids-limit", "256",
                "-v", f"{work}:/work", "-w", "/work",
                self.image, *args,
            ]
        else:
            # Use the interpreter containing the installed pytest, not an arbitrary `python`.
            cmd = [sys.executable if args[0] == "python" else args[0], *args[1:]]
        env = None
        if self.kind == "local":
            # Hygiene only, NOT a sandbox: code can still read host files and use the network.
            env = {
                "PATH": os.environ.get("PATH", os.defpath),
                # Preserve user-site Python dependencies installed by `pip --user`.
                "HOME": str(Path.home()), "LANG": "C.UTF-8",
                "PYTHONDONTWRITEBYTECODE": "1", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            }
        started = now()
        timed_out = False
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                cwd=None if self.kind == "docker" else str(work),
                env=env, start_new_session=True,
            )
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
                code = proc.returncode
            except subprocess.TimeoutExpired:
                timed_out = True
                os.killpg(proc.pid, signal.SIGKILL)
                if self.kind == "docker":
                    # Killing the Docker CLI alone does not stop the test container.
                    try:
                        subprocess.run(["docker", "rm", "-f", container_name],
                                       capture_output=True, timeout=20)
                    except (OSError, subprocess.TimeoutExpired):
                        pass  # still return a failed/timeout receipt, never VERIFIED
                stdout, stderr = proc.communicate()
                code = 124
        except OSError:
            stdout, stderr, code = "", "runner could not start; check executable/daemon availability", 127
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
        return {n for n in names if n in IGNORE_DIRS
                or n == ".env" or (n.startswith(".env.") and n != ".env.example")}

    shutil.copytree(src, dst, ignore=ignore)


def safe_join(root: Path, rel: str) -> Path:
    if not rel or Path(rel).is_absolute():
        raise ValueError("path must be nonempty and relative")
    target = (root / rel).resolve()
    if not target.is_relative_to(root.resolve()):
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
        "model": {},
        "runner": {
            "kind": args.runner, "image": args.image if args.runner == "docker" else None,
            "sandbox": args.runner == "docker",
            "network": "none" if args.runner == "docker" else "host (NOT a sandbox)",
        },
        "test_file": None,
        "test_sha256_frozen": None,
        "test_sha256_after": None,
        "attempts": [],
        "model_calls": [],
        "commands": [],
        "limits": {"max_attempts": args.max_attempts, "test_timeout_s": args.timeout},
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
    }

    try:
        return execute_issue(args, receipt, repo, issue)
    except ModelError as exc:
        return finish(receipt, NEEDS_REVIEW, str(exc))
    except (OSError, ValueError) as exc:
        return finish(receipt, NEEDS_REVIEW, f"invalid workspace or model response ({type(exc).__name__})")


def execute_issue(args: argparse.Namespace, receipt: dict, repo: Path, issue: str) -> dict:
    client = create_client(args, receipt)
    runner = Runner(args.runner, args.image, receipt)

    if args.work_dir:
        work_root = Path(args.work_dir).resolve()
        if work_root.is_relative_to(repo):
            raise ValueError("work directory must be outside the target repository")
        # Never erase an existing work directory, even if it looks generated.
        work_root.mkdir(parents=True, exist_ok=False)
    else:
        work_root = Path(tempfile.mkdtemp(prefix=".repair-work-", dir=repo.parent))
        work_root.chmod(0o755)  # the Docker non-root user must be able to traverse the mount
    receipt["work_dir"] = str(work_root)
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
    if not isinstance(query_reply.get("queries"), list):
        raise ModelError("queries: expected a list of search strings")
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
    test_path = target.relative_to(baseline).as_posix()
    if not test_path.startswith("tests/") or target.exists():
        return finish(receipt, CANNOT_REPRODUCE, "regression test must be a new file under tests/")
    receipt["test_file"] = test_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(test_content, encoding="utf-8")
    frozen_sha = sha256_bytes(target.read_bytes())
    receipt["test_sha256_frozen"] = frozen_sha
    receipt["test_sha256_after"] = frozen_sha

    baseline_run = runner.run(baseline, ["python", "-m", "pytest", "-q", "--tb=short", test_path],
                              args.timeout, "baseline_regression_test")
    genuine, why = genuine_reproduction(baseline_run["stdout"] + baseline_run["stderr"])
    if (baseline_run["timed_out"] or baseline_run["exit_code"] not in (0, 1, 2, 5)
            or "No module named pytest" in baseline_run["stderr"]):
        return finish(receipt, NEEDS_REVIEW, "baseline runner failed or timed out; no reproduction evidence")
    genuine = genuine and baseline_run["exit_code"] == 1
    receipt["baseline"] = {"genuine": genuine, "reason": why}
    if not genuine:
        return finish(receipt, CANNOT_REPRODUCE, f"regression test did not reproduce the bug ({why})")

    # baseline suite state, so we can tell new failures from pre-existing ones
    suite_base = runner.run(baseline, ["python", "-m", "pytest", "-q", "--tb=no", "-rf"],
                            args.timeout, "baseline_suite")
    if suite_base["timed_out"] or suite_base["exit_code"] not in (0, 1):
        return finish(receipt, NEEDS_REVIEW, "baseline suite could not complete")
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
        if not isinstance(files, list):
            raise ModelError("patch: expected a list of files")
        attempt_rec: dict = {"n": attempt, "summary": patch_reply.get("summary"), "files": [],
                             "rejected": None, "gate": None}

        candidate = work_root / f"candidate_{attempt}"
        copy_repo(baseline, candidate)
        rejected = None
        for item in files:
            if not isinstance(item, dict):
                rejected = "malformed file entry"
                break
            rel = str(item.get("path") or "")
            content = item.get("content")
            if not rel or not isinstance(content, str):
                rejected = "malformed file entry"
                break
            try:
                dest = safe_join(candidate, rel)
            except ValueError:
                rejected = "patch path escapes the work copy"
                break
            rel = dest.relative_to(candidate).as_posix()
            if rel == test_path or rel in baseline_tests or "tests" in Path(rel).parts:
                rejected = f"patch tried to modify a frozen test: {rel}"
                break
            if dest.name in {"conftest.py", "pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini"}:
                rejected = f"patch tried to modify test execution configuration: {rel}"
                break
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
        # The candidate can execute arbitrary Python, so a pre-run hash alone is insufficient.
        try:
            frozen_after = sha256_bytes(safe_join(candidate, test_path).read_bytes())
        except (OSError, ValueError):
            frozen_after = None
        after_tests = repo_files(candidate)
        gate = {
            "frozen_test_passed": test_run["exit_code"] == 0 and not test_run["timed_out"],
            "frozen_exit_code": test_run["exit_code"],
            "frozen_hash_unchanged": frozen_after == frozen_sha,
            "baseline_tests_unchanged": all(after_tests.get(p) == h for p, h in baseline_tests.items()),
            "new_failures": new_failures,
            "suite_exit_code": suite_run["exit_code"],
            "suite_completed": not suite_run["timed_out"] and suite_run["exit_code"] in (0, 1),
        }
        attempt_rec["gate"] = gate
        receipt["attempts"].append(attempt_rec)
        receipt["test_sha256_after"] = frozen_after

        if (gate["frozen_test_passed"] and gate["frozen_hash_unchanged"]
                and gate["baseline_tests_unchanged"] and gate["suite_completed"]
                and suite_run["exit_code"] == 0 and not new_failures):
            receipt["candidate_dir"] = str(candidate)
            receipt["diff"] = git_diff(baseline, candidate)
            return finish(receipt, VERIFIED, "frozen test and full suite pass; test hashes unchanged")

        if not gate["frozen_hash_unchanged"] or not gate["baseline_tests_unchanged"]:
            attempt_rec["rejected"] = "test hashes changed during execution"
            evidence = "The candidate modified or deleted a test during execution; this is forbidden."
        elif not gate["frozen_test_passed"]:
            evidence = ("the frozen test still failed:\n"
                        + tail(test_run["stdout"] + test_run["stderr"], 1800))
        else:
            evidence = ("the frozen test passed but the full suite did not pass:\n"
                        + tail(suite_run["stdout"] + suite_run["stderr"], 1800))

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
    parser.add_argument("--runner", choices=("docker", "local"), default="docker",
                        help="docker (default) or local subprocess, NOT a sandbox")
    parser.add_argument("--image", default="repairgate-sandbox:py313")
    parser.add_argument("--provider", choices=("hf", "llama", "mock"), default=None,
                        help="MODEL_PROVIDER env, otherwise hf; mock is offline and explicit")
    parser.add_argument("--mock-scenario", choices=SCENARIOS, default="good_patch")
    parser.add_argument("--model", default=None, help="MODEL_ID env, otherwise Meta Llama on HF")
    parser.add_argument("--base-url", default=None, help="HF_BASE_URL or LLAMA_API_BASE env")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--max-attempts", type=int, choices=(1, 2), default=2)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--work-dir", default=None)
    args = parser.parse_args(argv)

    if args.runner == "local":
        print("WARNING: --runner local is NOT a sandbox. Trusted code only; host/network access remains.",
              file=sys.stderr)
    receipt = run_issue(args)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(receipt, indent=2, ensure_ascii=False), encoding="utf-8")
    model_calls = len(receipt.get("model_calls") or [])
    print(f"{receipt['verdict']}: {receipt['reason']} "
          f"(model_calls={model_calls}, attempts={len(receipt.get('attempts') or [])}, "
          f"receipt={args.out})")
    return 0 if receipt["verdict"] == VERIFIED else 1


if __name__ == "__main__":
    sys.exit(main())
