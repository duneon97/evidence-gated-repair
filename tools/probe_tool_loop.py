#!/usr/bin/env python3
"""Kill-gate part 2: does the model hold a multi-turn tool loop?

Turn 1: model must write a regression test (tool call).
Then we feed a fake-but-realistic baseline failure as the tool result.
Turn 2: model must react to the evidence, not restate the plan. Accept either
        a patch proposal or a second tool call, and require it to mention the
        observed failure (assert 1 == 90) — that is the behaviour the agent
        loop depends on.

Repeats N times to measure flakiness. Never prints API keys.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from probe_model import ISSUE, PROVIDERS, cost_usd, load_env, post  # noqa: E402

TEST_TOOL = {
    "type": "function",
    "function": {
        "name": "write_regression_test",
        "description": "Write a pytest regression test that reproduces the bug on the current baseline.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "test_name": {"type": "string"},
                "source": {"type": "string"},
            },
            "required": ["path", "test_name", "source"],
        },
    },
}
PATCH_TOOL = {
    "type": "function",
    "function": {
        "name": "propose_patch",
        "description": "Propose a unified-diff patch for the source file under repair.",
        "parameters": {
            "type": "object",
            "properties": {
                "file": {"type": "string"},
                "diff": {"type": "string"},
                "rationale": {"type": "string"},
            },
            "required": ["file", "diff", "rationale"],
        },
    },
}

BASELINE_RESULT = (
    "command: python -m pytest tests/test_regression_parse_duration.py -q\n"
    "exit_code: 1\n"
    "output:\n"
    "    def test_parse_duration_seconds():\n"
    ">       assert tinycalc.parse_duration('90s') == 90\n"
    "E       assert 1 == 90\n"
    "1 failed in 0.04s\n"
    "note: the test file is frozen; editing, skipping or xfail-ing it is blocked by the gate."
)


def one_loop(base: str, key: str, model: str, timeout: int, max_tokens: int = 8000) -> dict:
    messages = [
        {
            "role": "system",
            "content": (
                "You are a repair agent. You must produce execution evidence, not claims. "
                "Step 1: write a regression test for the issue, then stop and wait for the runner."
            ),
        },
        {"role": "user", "content": ISSUE},
    ]
    tools = [TEST_TOOL, PATCH_TOOL]
    t1, el1, st1 = post(
        f"{base}/chat/completions",
        key,
        {"model": model, "messages": messages, "tools": tools, "max_tokens": max_tokens, "temperature": 0},
        timeout=timeout,
    )
    if st1 != 200:
        return {"ok": False, "stage": "turn1", "status": st1, "error": t1.get("_body", t1)[:300]}
    msg1 = (t1.get("choices") or [{}])[0].get("message") or {}
    calls1 = msg1.get("tool_calls") or []
    if not calls1:
        return {"ok": False, "stage": "turn1", "error": "no tool call", "prose": (msg1.get("content") or "")[:300]}

    finish1 = (t1.get("choices") or [{}])[0].get("finish_reason")
    call = calls1[0]
    call_id = call.get("id") or f"call_{uuid.uuid4().hex[:12]}"
    call["id"] = call_id
    messages.append({"role": "assistant", "content": msg1.get("content") or "", "tool_calls": [call]})
    messages.append({"role": "tool", "tool_call_id": call_id, "content": BASELINE_RESULT})
    messages.append(
        {
            "role": "user",
            "content": "The regression test failed on baseline exactly as reported. Continue: produce the fix.",
        }
    )

    t2, el2, st2 = post(
        f"{base}/chat/completions",
        key,
        {"model": model, "messages": messages, "tools": tools, "max_tokens": max_tokens, "temperature": 0},
        timeout=timeout,
    )
    if st2 != 200:
        return {"ok": False, "stage": "turn2", "status": st2, "error": t2.get("_body", t2)[:300]}
    msg2 = (t2.get("choices") or [{}])[0].get("message") or {}
    calls2 = msg2.get("tool_calls") or []
    prose2 = msg2.get("content") or ""
    # Reacting means acting on the evidence: a tool call in turn 2 counts on its own.
    # We also record whether the observed failure text made it into the response.
    mentions_failure = "1 == 90" in prose2 or "90" in json.dumps(calls2) or "assert" in prose2.lower()
    reacted = bool(calls2) or mentions_failure
    names2 = []
    for c in calls2:
        fn = c.get("function") or {}
        try:
            json.loads(fn.get("arguments") or "{}")
            names2.append(fn.get("name"))
        except json.JSONDecodeError:
            names2.append(f"{fn.get('name')}(BAD_JSON)")
    finish2 = (t2.get("choices") or [{}])[0].get("finish_reason")
    usage1, usage2 = t1.get("usage") or {}, t2.get("usage") or {}
    return {
        "ok": bool(calls2),
        "turn1_call": (call.get("function") or {}).get("name"),
        "turn2_calls": names2,
        "turn2_reacted_to_failure": reacted,
        "turn2_mentions_failure_text": mentions_failure,
        "turn1_elapsed_s": round(el1, 3),
        "turn2_elapsed_s": round(el2, 3),
        "turn1_finish_reason": finish1,
        "turn2_finish_reason": finish2,
        "turn2_completion_tokens": usage2.get("completion_tokens"),
        "turn2_reasoning_tokens": (usage2.get("completion_tokens_details") or {}).get("reasoning_tokens"),
        "turn2_empty_content": not prose2 and not calls2,
        "turn2_prose": prose2[:220],
        "cost_usd": round((cost_usd(model, usage1) or 0) + (cost_usd(model, usage2) or 0), 6),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", choices=sorted(PROVIDERS), default="hf")
    ap.add_argument("--model", required=True)
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--timeout", type=int, default=180)
    ap.add_argument("--max-tokens", type=int, default=8000)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    load_env()
    cfg = PROVIDERS[args.provider]
    key = os.environ.get(cfg["key_env"], "")
    if not key:
        print(json.dumps({"verdict": "NO_KEY", "key_env": cfg["key_env"]}))
        return 2

    runs = [
        one_loop(cfg["base_url"], key, args.model, args.timeout, args.max_tokens)
        for _ in range(args.repeats)
    ]
    ok = sum(1 for r in runs if r.get("ok"))
    report = {
        "provider": args.provider,
        "model": args.model,
        "repeats": args.repeats,
        "loops_ok": ok,
        "success_rate": f"{ok}/{args.repeats}",
        "total_cost_usd": round(sum(r.get("cost_usd") or 0 for r in runs), 6),
        "runs": runs,
        "verdict": "PASS" if ok == args.repeats else ("FLAKY" if ok else "FAIL"),
    }
    text = json.dumps(report, indent=2)
    print(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    return 0 if ok == args.repeats else 1


if __name__ == "__main__":
    sys.exit(main())
