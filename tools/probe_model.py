#!/usr/bin/env python3
"""Day-1 kill-gate probe for the evidence-gated repair agent.

Checks, against a live OpenAI-compatible endpoint:
  1. model id resolves and answers (3 samples -> latency, tokens, cost)
  2. tool calling works and returns sane arguments
  3. JSON-schema structured output works (fallback if tools are flaky)

Never prints API keys. Usage:
  python3 probe_model.py --provider openrouter --model meta/muse-spark-open
  python3 probe_model.py --provider hf --model meta-models/Muse-Spark-Open-816B-A42B
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request

ENV_FILE = "/root/.hermes/.env"
PROVIDERS = {
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "key_env": "OPENROUTER_API_KEY",
    },
    "hf": {
        "base_url": "https://router.huggingface.co/v1",
        "key_env": "HF_TOKEN",
    },
}
# USD per 1M tokens (input, output) from the public catalogs, for cost maths only.
PRICES = {
    "meta/muse-spark-open": (1.25, 4.25),
    "meta-models/Muse-Spark-Open-816B-A42B": (1.25, 4.25),
    "meta/muse-glimmer-30b": (0.30, 1.20),
    "meta-models/Muse-Glimmer-30B": (0.30, 1.20),
}


def load_env(path: str = ENV_FILE) -> None:
    """Load KEY=VALUE pairs from a dotenv file without echoing values."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def post(url: str, key: str, payload: dict, timeout: int = 180) -> tuple[dict, float, int]:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        return {"_http_error": exc.code, "_body": raw[:800]}, time.monotonic() - started, exc.code
    elapsed = time.monotonic() - started
    try:
        return json.loads(raw), elapsed, status
    except json.JSONDecodeError:
        return {"_body": raw[:800]}, elapsed, status


def _is_empty(data: dict) -> bool:
    """True when a 200 response carries neither text nor a tool call.

    Muse Spark bills reasoning tokens as completion tokens; if max_tokens is too small the
    whole budget goes to reasoning and the message comes back empty. That is a retryable
    model-side failure, not a verdict about the model.
    """
    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    return not (msg.get("content") or "").strip() and not msg.get("tool_calls")


def call_with_empty_retry(
    base: str, key: str, payload: dict, timeout: int = 180, attempts: int = 2
) -> tuple[dict, float, int, int]:
    """POST, and retry once with a larger output budget when the reply is empty."""
    data, elapsed, status = post(f"{base}/chat/completions", key, payload, timeout=timeout)
    tries = 1
    while status == 200 and _is_empty(data) and tries < attempts:
        bigger = dict(payload)
        bigger["max_tokens"] = min(int(payload.get("max_tokens", 2000)) * 3, 32000)
        more, spent, status = post(f"{base}/chat/completions", key, bigger, timeout=timeout)
        data = more
        elapsed += spent
        tries += 1
    return data, elapsed, status, tries

def cost_usd(model: str, usage: dict) -> float | None:
    price = PRICES.get(model)
    if not price or not usage:
        return None
    pin, pout = price
    return (usage.get("prompt_tokens", 0) * pin + usage.get("completion_tokens", 0) * pout) / 1_000_000


def reasoning_block(mode: str) -> dict:
    """Reasoning control for OpenRouter; ignored by plain HF router."""
    if mode == "off":
        return {"reasoning": {"enabled": False}}
    if mode == "low":
        return {"reasoning": {"effort": "low"}}
    return {}


def test_latency(base: str, key: str, model: str, runs: int, reasoning: str = "default") -> dict:
    results = []
    for i in range(runs):
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": "Answer with one short sentence."},
                {"role": "user", "content": f"Run {i + 1}: name the pytest flag that stops after the first failure."},
            ],
            "max_tokens": 2000,
            "temperature": 0,
            **reasoning_block(reasoning),
        }
        data, elapsed, status, tries = call_with_empty_retry(base, key, payload)
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        results.append(
            {
                "status": status,
                "attempts": tries,
                "elapsed_s": round(elapsed, 3),
                "usage": data.get("usage"),
                "cost_usd": cost_usd(model, data.get("usage")),
                "text": (msg.get("content") or data.get("_body", ""))[:200],
                "provider_reported": data.get("provider"),
            }
        )
    ok = [r for r in results if r["status"] == 200 and r["text"]]
    lat = [r["elapsed_s"] for r in ok]
    return {
        "runs": results,
        "ok_runs": len(ok),
        "latency_median_s": round(statistics.median(lat), 3) if lat else None,
        "latency_min_s": min(lat) if lat else None,
        "latency_max_s": max(lat) if lat else None,
        "cost_total_usd": round(sum(r["cost_usd"] or 0 for r in results), 6),
    }


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "write_regression_test",
            "description": "Write a pytest regression test that reproduces the reported bug on the current baseline.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path relative to repo root, e.g. tests/test_bug_42.py"},
                    "test_name": {"type": "string"},
                    "source": {"type": "string", "description": "Full pytest file content"},
                },
                "required": ["path", "test_name", "source"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_pytest",
            "description": "Run pytest in the sandbox and return exit code plus captured output.",
            "parameters": {
                "type": "object",
                "properties": {
                    "node_ids": {"type": "array", "items": {"type": "string"}},
                    "timeout_s": {"type": "integer"},
                },
                "required": ["node_ids"],
            },
        },
    },
]

ISSUE = (
    "Repo: tinycalc (Python 3.13, pytest). Bug report: `tinycalc.parse_duration('90s')` "
    "returns 1 instead of 90; suffixes other than 's' raise KeyError. Expected: "
    "parse_duration accepts s/m/h and returns int seconds, raising ValueError on unknown suffix."
)


def test_tool_calling(base: str, key: str, model: str, reasoning: str = "default") -> dict:
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a repair agent. First reproduce the bug: create a pytest regression "
                    "test and run it on the baseline. Prefer calling tools over prose."
                ),
            },
            {"role": "user", "content": ISSUE},
        ],
        "tools": TOOLS,
        "tool_choice": "auto",
        "max_tokens": 8000,
        "temperature": 0,
        **reasoning_block(reasoning),
    }
    data, elapsed, status, tries = call_with_empty_retry(base, key, payload)
    if status != 200:
        return {"ok": False, "status": status, "elapsed_s": round(elapsed, 3), "error": data.get("_body", data)}
    msg = (data.get("choices") or [{}])[0].get("message") or {}
    calls = msg.get("tool_calls") or []
    parsed = []
    for call in calls:
        fn = call.get("function") or {}
        args_raw = fn.get("arguments") or "{}"
        try:
            args = json.loads(args_raw)
            valid_json = True
        except json.JSONDecodeError:
            args = args_raw
            valid_json = False
        parsed.append(
            {
                "name": fn.get("name"),
                "valid_json": valid_json,
                "arg_keys": sorted(args) if isinstance(args, dict) else None,
                "args_preview": {k: (str(v)[:120]) for k, v in args.items()} if isinstance(args, dict) else None,
            }
        )
    return {
        "ok": bool(parsed) and all(p["valid_json"] for p in parsed),
        "attempts": tries,
        "status": status,
        "elapsed_s": round(elapsed, 3),
        "finish_reason": (data.get("choices") or [{}])[0].get("finish_reason"),
        "tool_calls": parsed,
        "usage": data.get("usage"),
        "cost_usd": cost_usd(model, data.get("usage")),
        "prose": (msg.get("content") or "")[:300],
    }


SCHEMA = {
    "name": "repair_verdict",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "state": {"type": "string", "enum": ["VERIFIED", "CANNOT_REPRODUCE", "NEEDS_REVIEW"]},
            "reproduced_on_baseline": {"type": "boolean"},
            "evidence": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "check": {"type": "string"},
                        "exit_code": {"type": "integer"},
                    },
                    "required": ["check", "exit_code"],
                    "additionalProperties": False,
                },
            },
            "reason": {"type": "string"},
        },
        "required": ["state", "reproduced_on_baseline", "evidence", "reason"],
        "additionalProperties": False,
    },
}


def test_structured(base: str, key: str, model: str, reasoning: str = "default") -> dict:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "Return only the JSON object that matches the schema."},
            {
                "role": "user",
                "content": (
                    "Baseline run of tests/test_bug_42.py exited 1 (assert 1 == 90). Candidate run exited 0. "
                    "Existing suite exited 0. Test file hash unchanged. Give the verdict."
                ),
            },
        ],
        "response_format": {"type": "json_schema", "json_schema": SCHEMA},
        "max_tokens": 8000,
        "temperature": 0,
        **reasoning_block(reasoning),
    }
    data, elapsed, status, tries = call_with_empty_retry(base, key, payload)
    if status != 200:
        return {"ok": False, "status": status, "elapsed_s": round(elapsed, 3), "error": data.get("_body", data)}
    msg = (data.get("choices") or [{}])[0].get("message") or {}
    content = msg.get("content") or ""
    try:
        parsed = json.loads(content)
        ok = parsed.get("state") == "VERIFIED"
    except json.JSONDecodeError:
        parsed, ok = None, False
    return {
        "ok": ok,
        "attempts": tries,
        "status": status,
        "elapsed_s": round(elapsed, 3),
        "parsed": parsed,
        "raw": content[:300],
        "usage": data.get("usage"),
        "cost_usd": cost_usd(model, data.get("usage")),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", choices=sorted(PROVIDERS), default="openrouter")
    ap.add_argument("--model", required=True)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--reasoning", choices=["default", "off", "low"], default="default")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    load_env()
    cfg = PROVIDERS[args.provider]
    key = os.environ.get(cfg["key_env"], "")
    report: dict = {
        "provider": args.provider,
        "base_url": cfg["base_url"],
        "model": args.model,
        "key_env": cfg["key_env"],
        "key_present": bool(key),
    }
    if not key:
        report["verdict"] = "NO_KEY"
        print(json.dumps(report, indent=2))
        return 2

    report["reasoning_mode"] = args.reasoning
    report["latency"] = test_latency(cfg["base_url"], key, args.model, args.runs, args.reasoning)
    report["tool_calling"] = test_tool_calling(cfg["base_url"], key, args.model, args.reasoning)
    report["structured_output"] = test_structured(cfg["base_url"], key, args.model, args.reasoning)
    reasoning_tokens = [
        (r.get("usage") or {}).get("completion_tokens_details", {}).get("reasoning_tokens", 0)
        for r in report["latency"]["runs"]
        if r.get("usage")
    ]
    report["reasoning_tokens_per_run"] = reasoning_tokens

    report["latency"]["attempts_per_run"] = [r.get("attempts") for r in report["latency"]["runs"]]
    lat_ok = report["latency"]["ok_runs"] == args.runs
    report["verdict"] = (
        "PASS"
        if lat_ok and report["tool_calling"]["ok"]
        else ("DEGRADED" if lat_ok else "FAIL")
    )
    text = json.dumps(report, indent=2)
    print(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
