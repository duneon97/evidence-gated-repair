# Day-1 kill-gate — verified 2026-10-27

Question the gate answers: **can we build the agent loop on Muse Spark Open through the Hugging
Face Inference router, today, with reliable tool calling and acceptable latency?**

Verdict: **PASS**, with three hardening rules (see the bottom of this page).

All numbers below come from the two probe scripts in `tools/` run from this workspace. No value
in this document is estimated.

## Model access

Public catalogue, no token needed — `GET https://router.huggingface.co/v1/models` (134 models):

| Field | `meta-models/Muse-Spark-Open-816B-A42B` |
| --- | --- |
| Owner | `meta-models` |
| Provider | `baseten`, status `live` |
| Context length | 1,048,576 |
| `supports_tools` | `true` |
| `supports_structured_output` | `false` (see hardening rule 3) |
| Price | $1.25 / M input, $4.25 / M output |

Related ids on the same router: `meta-models/Muse-Glimmer-30B` (30B, `deepinfra` +
`featherless-ai`, tools `true`, structured `true`, $0.30 / $1.20). On the hub the Spark repo is
`gated: manual`, but router inference worked with a token.

Cross-check on a second OpenAI-compatible provider (OpenRouter): `meta/muse-spark-open` exists,
1,048,576 context, tools and structured output both listed as supported, same $1.25 / $4.25, and
the request was served by **BaseTen** and **Nebius** on different calls. This is the documented
fallback path if the router misbehaves: change `HF_BASE_URL`, keep `MODEL_ID`.

An unauthenticated `POST /v1/chat/completions` returns **HTTP 401** — a token is mandatory.

## Single-turn latency (Muse Spark Open)

| Endpoint | Prompt | n | Median | Min | Max |
| --- | --- | --- | --- | --- | --- |
| HF router (`baseten`) | 166 prompt tokens, 600 max_tokens | 2 | **1.47 s** | 1.03 s | 1.90 s |
| OpenRouter (`BaseTen` / `Nebius`) | same | 3 | 2.47 s | 2.33 s | 6.15 s |

## Tool calling

`tools/probe_model.py` sends a real repair instruction with two function schemas
(`write_regression_test`, `run_pytest`) and `tool_choice: auto`.

- `finish_reason: "tool_calls"`, valid JSON arguments, correct required keys — on both providers.
- Single tool call latency: 1.19 s on the HF router, 2.82 s through OpenRouter.
- `response_format: json_schema` (strict, `repair_verdict`) also returned a schema-valid object
  with `state: VERIFIED` — even though the router metadata advertises `supports_structured_output: false`.

## Multi-turn loop (`tools/probe_tool_loop.py`)

Turn 1: model must write the regression test. Turn 2: it is handed a real pytest baseline output
(`exit_code: 1`, `assert 1 == 90`) plus the note that the test file is frozen, and must continue.

| Run | Turn 1 | Turn 2 | Turn 1 latency | Turn 2 latency | Reasoning tokens (T2) | Cost |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | `write_regression_test` | `propose_patch` | 2.92 s | 18.09 s | 4,187 | $0.0264 |
| 2 | `write_regression_test` | `propose_patch` | 3.51 s | 2.59 s | 7 | $0.0090 |
| 3 | `write_regression_test` | `propose_patch` | 2.02 s | 9.85 s | 1,842 | $0.0139 |

**3/3 loops completed.** Every run produced a valid tool call in both turns and every patch
referenced the observed failure. Cost per two-turn loop: **$0.009 – $0.026**; at $150 of credits
that is 5,689 – 16,665 loops (median run: about 11,000), far more than a 9-day project needs.

## What broke (and the rules that follow)

1. **Reasoning cannot be disabled.** `reasoning: {enabled: false}` returns HTTP 400
   `"Reasoning is mandatory for this endpoint and cannot be disabled."` This applies to Muse Spark
   Open and Muse Glimmer 30B. Reasoning tokens are billed as output tokens, and they dominate the
   latency tail (7 → 4,187 reasoning tokens is the difference between 2.6 s and 18.1 s).
2. **A small `max_tokens` silently returns an empty message.** Reproduced twice: at
   `max_tokens: 3000` one turn spent the whole budget on reasoning and returned no content and no
   tool call, at 26 s and $0.0228; in CI at `max_tokens: 600` one sample came back empty with 375
   reasoning tokens. Rule: `max_tokens >= 8000`, retry once with 3x the budget, and treat
   "no content and no tool call" as a **retryable model failure**, never as `CANNOT_REPRODUCE`.
3. **Rate limits are real.** One turn of an early batch failed with HTTP 429 `Rate limit
   exceeded`. Rule: keep `HF_TOKEN_FALLBACK` wired from the first commit, retry 429/5xx with
   backoff, and record every retry in the receipt.
4. **Router metadata and runtime disagree on structured output.** Enforce the JSON schema in code
   (validate and re-ask once) instead of trusting `supports_structured_output`.

Timeout budget for the runner: 60 s per model call, 2 repair attempts, so worst case per issue
stays inside ~4 min of model time.

## Repository-level verification (GitHub Actions)

`.github/workflows/kill-gate.yml` (manual dispatch) replays the probes on a fresh runner using the
two repository secrets. Secrets are injected as `HF_TOKEN` and masked as `***` in the log; no value
is printed or written to the artifacts.

Run [40491351625](https://github.com/duneon97/evidence-gated-repair/actions/runs/40491351625) —
**success**:

| Step | Token | Verdict | Notes |
| --- | --- | --- | --- |
| Single-turn probe | `HF_TOKEN_MAIN` | PASS | latency median 2.27 s |
| Single-turn probe | `HF_TOKEN_FALLBACK` | PASS | latency median 1.89 s, so the fallback token is live and usable |
| Two-turn tool loop | `HF_TOKEN_MAIN` | PASS 2/2 | total $0.0138 |

The first run ([40491120215](https://github.com/duneon97/evidence-gated-repair/actions/runs/40491120215))
reported `verdict=FAIL` for the main token with `ok_runs: 1` and one `"text": ""`. That was not an
access problem: `max_tokens: 600` had been consumed by 375 reasoning tokens. The probes now retry an
empty reply once with three times the budget, which is hardening rule 2 made executable.
