# evidence-gated-repair

**Don't trust the patch. Inspect the evidence.**

A repair agent that **refuses** to claim a fix it cannot prove. It writes a regression test
from the issue *first*, freezes it, reproduces the failure on the baseline, and only reports
`VERIFIED` when the frozen test passes on the candidate **and** the existing suite does not
regress **and** the test file hash is unchanged. Anything else is `NEEDS_REVIEW`.

Built for the Meta Global AI Developer Hackathon (Indie track). Model: **Muse Spark Open**
(`meta-models/Muse-Spark-Open-816B-A42B`) through the Hugging Face Inference router.

## Why this exists

Search → patch → run tests is the standard SWE-bench loop, and it produces green runs that are
wrong: the agent edits the assertion, skips the test, or fixes the wrong thing. A passing test
only means "covered evidence passes". The value here is the **gate that blocks false success**,
not the patch generator.

## Status values

| Status | Meaning |
| --- | --- |
| `VERIFIED` | Frozen regression test failed for the right reason on baseline, passes on candidate, existing suite passes, test hash unchanged. Receipt written. |
| `NEEDS_REVIEW` | Patch attempts exhausted, or evidence is incomplete or contradictory. Never reported as fixed. |
| `CANNOT_REPRODUCE` | The regression test does not fail on the baseline, or the failure is an import/build error rather than the reported bug. |

Every terminal status carries a `receipt.json`: baseline and candidate commit SHA, frozen test
hash, exact command, exit codes, captured logs, model id, provider and token cost.

## Scope (deliberately narrow)

- Python + `pytest` only.
- One prepared repository per run; no arbitrary GitHub clones.
- Maximum 2 repair attempts per issue.
- No multi-agent swarm, no voice/image/vision input.
- Sandbox: disposable container, `--network none`, no secrets mounted, CPU/time/output limits.
  A git worktree is **not** a sandbox.

## Quickstart

```bash
cp .env.example .env      # fill in HF_TOKEN_MAIN / HF_TOKEN_FALLBACK, chmod 600
# Day-1 kill-gate probes (no agent loop yet)
python3 tools/probe_model.py --provider hf --model meta-models/Muse-Spark-Open-816B-A42B
python3 tools/probe_tool_loop.py --provider hf --model meta-models/Muse-Spark-Open-816B-A42B --repeats 3

# build the sandbox image once, then repair one issue
docker build -t repairgate-sandbox:py313 sandbox/
python3 -m agent.repair --repo fixtures/tinybug --issue fixtures/tinybug/ISSUE.md \
    --out receipt.json --runner docker --env-file .env
```

## Status

Day 1 (2026-10-27): model access, tool calling and latency verified end to end on
`meta-models/Muse-Spark-Open-816B-A42B` via the HF router — tool calling 3/3 in a two-turn loop,
single-turn latency 1.03–1.90 s, $0.009–$0.026 per loop. See
[`docs/kill-gate.md`](docs/kill-gate.md) and [`docs/secrets.md`](docs/secrets.md). The agent loop
itself is now implemented and was run end to end.

Day 2 (2026-10-27): vertical slice works on the InstaBox machine. Fixture bug
(`fixtures/tinybug`, a duration parser that rejects `90m`) went baseline fail →
one patch → **VERIFIED**, frozen test hash unchanged, suite green, 4 model calls,
≈$0.066. Sandbox contract (non-root, no network, no secrets) and 14 gate unit tests
pass inside the container. See [`docs/day2.md`](docs/day2.md) and the
[receipt](docs/evidence/receipt-duration.json).

## License

MIT — see [LICENSE](LICENSE).
