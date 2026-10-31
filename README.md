# evidence-gated-repair

**Don't trust the patch. Inspect the evidence.**

A Meta-model-powered Python repair agent that refuses to claim an unproven fix.
It generates a regression test **before** patching, reproduces the failure, freezes
the test with SHA-256, then checks the candidate against that test and the full suite.
Editing the test is not a fix, and the gate blocks it before execution.

Built for the **Meta Global AI Developer Hackathon**, deadline **2026-11-05**.
The intended impact is fewer false-success repairs and less reviewer time spent
reconstructing what an agent actually tested, not a claim of autonomous correctness.

## Setup and offline demo (no Docker or API credentials)

Python 3.10+ on Linux/macOS; the Docker image uses Python 3.13.

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 -m pytest -q
python3 -m agent.demo
```

The demo uses **deterministic mock model responses but real subprocess pytest runs**.
It makes no inference calls. The local runner is **NOT a sandbox**: use it only
with trusted fixtures/code. The demo prints this verdict table and the paths to
three JSON receipts:

```text
SCENARIO             EXPECTED           ACTUAL             RESULT
good_patch           VERIFIED           VERIFIED           PASS
bad_patch            NEEDS_REVIEW       NEEDS_REVIEW       PASS
cannot_reproduce     CANNOT_REPRODUCE   CANNOT_REPRODUCE   PASS
```

- `good_patch`: reproduce the `90m` bug, freeze the test, fix the parser, pass all six tests.
- `bad_patch`: propose weakening the frozen test instead; reject both attempts before execution.
- `cannot_reproduce`: test an already-supported `2h` report; baseline passes, so stop without patching.

Each invocation preserves its receipts and work copies in a new directory under
`receipts/demo/`. Use `--out-dir` to choose another location. The demo exits 0 only
if **all three expected verdicts** match, including the two intentional refusals.
The individual repair CLI exits 0 only for `VERIFIED`, otherwise 1.

See the [three-minute demo script and shot list](docs/DEMO_SCRIPT.md).

## Architecture

```text
                         HOST: model credentials stay here
 Issue + repository --> Meta Llama / Muse Spark Open (real HTTPS inference)
                              |
                         search queries --> ripgrep (grep fallback)
                              |
                         regression test
                              v
                       baseline pytest run
                         |             |
                      fails         passes/invalid
                         |             +--> CANNOT_REPRODUCE + receipt
                   freeze SHA-256
                         |
                   model proposes patch (at most 2 attempts)
                         |
                   path / test / config edit checks
                         |             |
                       allowed       rejected --> NEEDS_REVIEW after attempts
                         v
           Docker runner (default): non-root, no network, resource limits
           OR local subprocess (explicit opt-in, NOT a sandbox)
                         |
               frozen test + full suite + hash evidence
                         |
                   deterministic gate
                         +--> VERIFIED / NEEDS_REVIEW + receipt

 Offline demo replaces ONLY model responses with fixture-specific mock JSON.
 Repository copies, test execution, hashes, rejection logic, and receipts are real.
```

## How Meta models are used

Meta models drive all three reasoning steps: **code-search query expansion,
regression-test generation, and repair proposals**. The deterministic gate, not
the model, assigns the verdict. No real provider silently falls back to mock.

| Provider | Configuration | Wire format |
| --- | --- | --- |
| `hf` (default) | `HF_TOKEN_MAIN`, optional `HF_TOKEN_FALLBACK`; optional `HF_BASE_URL` | HF router `/chat/completions`, OpenAI-compatible |
| `llama` | `LLAMA_API_KEY`, `LLAMA_API_BASE`, `MODEL_ID` | Native Meta Llama API `/chat/completions`, `max_completion_tokens` and `completion_message` |
| `mock` | Explicit `--provider mock`; no credentials | Deterministic fixture-only JSON, no HTTP |

The default HF model is **`meta-llama/Llama-3.3-70B-Instruct`**.
Other router-supported `meta-llama/Llama-*` models can be selected with `MODEL_ID`
or `--model`. The existing **`meta-models/Muse-Spark-Open-816B-A42B`** path remains
supported. Model availability, gated access, and quotas depend on your provider account.

Configuration precedence: **CLI > nonempty process environment > nonempty `.env`
settings > defaults**. `MODEL_PROVIDER` selects the provider when `--provider`
is omitted; `MODEL_ID` selects the model. Empty template entries use defaults.
For direct Meta access, set the API root including its version prefix in
`LLAMA_API_BASE` and an exact model ID available to your account. URLs must use HTTPS.
`--base-url` overrides the selected provider's API root.

```bash
cp .env.example .env
chmod 600 .env
# Edit .env privately: set HF_TOKEN_MAIN. Never paste credentials into commands/logs.
docker build -t repairgate-sandbox:py313 sandbox/

# Real Meta Llama inference, Docker test execution (default runner).
python3 -m agent.repair --repo fixtures/tinybug --issue fixtures/tinybug/ISSUE.md \
    --provider hf --out receipts/llama.receipt.json

# Same real inference pipeline with the existing Muse Spark Open model.
python3 -m agent.repair --repo fixtures/tinybug --issue fixtures/tinybug/ISSUE.md \
    --provider hf --model meta-models/Muse-Spark-Open-816B-A42B \
    --out receipts/muse.receipt.json

# Optional native Meta API, after setting its three variables privately.
python3 -m agent.repair --repo fixtures/tinybug --issue fixtures/tinybug/ISSUE.md \
    --provider llama --out receipts/meta-api.receipt.json
```

Missing credentials, exhausted API retries, or unavailable Docker produce
`NEEDS_REVIEW`, not a fabricated model response or a claimed repair.
HF token rotation, retryable HTTP failures, empty-response budget increases,
and one JSON re-ask are recorded in the receipt.
The native Meta request/response adapter follows the
[official Python SDK schema](https://github.com/meta-llama/llama-api-python).

To explicitly use the local fallback for a trusted fixture:

```bash
python3 -m agent.repair --repo fixtures/tinybug --issue fixtures/tinybug/ISSUE.md \
    --provider mock --runner local --mock-scenario bad_patch \
    --out receipts/blocked.receipt.json
# Expected exit 1, verdict NEEDS_REVIEW. NOT a sandbox.
```

`--runner local` also works with real providers, but executes model-generated code
on your host. It removes provider credentials from the child environment as
hygiene only; it does **not** block host-file or network access. Docker remains
the default for real runs; no automatic downgrade occurs.

## Verdicts and receipts

| Status | Meaning |
| --- | --- |
| `VERIFIED` | Baseline reproduced a test failure; the frozen test and full candidate suite exit 0; test hashes match. |
| `NEEDS_REVIEW` | Attempts exhausted, forbidden edits, runner/API failure, or incomplete evidence. Never reported as fixed. |
| `CANNOT_REPRODUCE` | Proposed regression passes on baseline, or the test is unusable (for example, import/collection errors). |

Receipts contain the repository commit, issue hash, model/provider and live/mock
mode, runner isolation metadata, frozen-test hashes, attempt rejection reasons,
exact test commands, exit codes, timeouts, truncated logs, and available token usage.
They do **not** store API keys or configurable endpoint URLs. Billing cost is not
invented when unavailable. Candidate directories and a diff summary are preserved
for verified runs. Source repositories are copied, never patched in place.
An existing `--work-dir` is refused rather than erased.

## Tests and sandbox contract

`python3 -m pytest -q` runs the existing gate unit tests plus provider, runner,
path protection, end-to-end scenario, and repeated-demo tests. The fixture is
an independent target project, not a top-level importable test suite; its tests
run in the integration scenarios. No inference credentials are needed.

Docker integration skips cleanly if the executable, daemon, or prebuilt image
is unavailable. The four sandbox-only assertions are skipped on the host.
After building the image, run its contract explicitly:

```bash
docker run --rm --network none \
    -v "$PWD/sandbox/smoke:/work" -w /work \
    repairgate-sandbox:py313 python -m pytest -q -o cache_dir=/tmp/pytest-cache
```

The image explicitly enables `REPAIRGATE_SANDBOX_CONTRACT=1`; do not set it on
the host. Contract checks cover non-root execution, blocked network, absent
agent credentials, and absence of the known host env file.

## Evidence and limitations

- Historical real Muse Spark Open probes and the duration repair receipt are in
  [day 1](docs/kill-gate.md), [day 2](docs/day2.md), and
  [the recorded receipt](docs/evidence/receipt-duration.json). These are historical
  evidence, **not** a new live Llama run. Offline mock receipts prove the gate,
  not model quality or current provider availability.
- Python + pytest, one prepared trusted repository, at most two attempts.
  Docker dependencies must be installed at image-build time; no arbitrary clones.
- This is one small fixture, not an accuracy benchmark or measured reviewer-time study.
  A model can write an inadequate test; passing covered evidence is not a proof
  of correctness. Source code can game tests without directly editing them.
- Docker is defense in depth, not a hardened hostile-code boundary. Copies and
  candidates are writable; hashes are checked by the host. No immutable external
  test oracle or resistance to a malicious process forging pytest output is claimed.
- Non-root containers have CPU/memory/PID/time limits. Captured receipt logs are
  truncated, but execution output has no hard byte quota. Full-file proposals,
  bounded search context, and repeated full-suite runs limit scale.
- Secrets must not be in target repositories. Dotenv files are excluded from copies,
  but other sensitive files/source snippets can enter prompts or test logs.
  Review receipts before sharing; see [secret handling](docs/secrets.md).

## License

MIT, see [LICENSE](LICENSE).
