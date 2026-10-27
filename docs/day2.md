# Day 2 — the vertical slice runs end to end (2026-10-27)

One issue, one repo, one sandbox, one receipt. Everything below was executed on the InstaBox
machine (Python 3.13.5, Docker 29.8.2) with the secrets read from `/data/.env` (mode 600).
Receipt: [`docs/evidence/receipt-duration.json`](evidence/receipt-duration.json).

## Sandbox contract

`sandbox/smoke/test_sandbox_contract.py` runs inside `repairgate-sandbox:py313` with
`--network none`: **4 passed**. It asserts the test process is not root (uid 10001), cannot open a
TCP connection, has no `HF_TOKEN*`/`SSH_*` variables, and cannot see `/data/.env`.

Control run in the same image without `--network none`: `net-reachable-when-allowed` — so the
network block comes from the flag, not from a broken image.

Gate unit tests run inside the container with the network off: **14 passed**
(`tests/test_gate.py`).

## The repair run

`fixtures/tinybug` — a 21-line duration parser whose regex only accepts whole hours. `README.md`
documents `90m` and `1h30m`; the existing 5 tests all pass on the baseline, so the bug is real and
uncovered.

| Step | Command | Exit |
| --- | --- | --- |
| baseline regression test | `python -m pytest -q --tb=short tests/test_regression_duration_minutes.py` | 1 (assertion failure) |
| baseline suite | `python -m pytest -q --tb=no -rf` | 1 (only the new regression test) |
| candidate: frozen test | same test path, after the patch | 0 |
| candidate: suite | full suite, after the patch | 0 |

Verdict: **VERIFIED**, one attempt, no retries.

- search queries the model chose: `parse_duration`, `invalid duration`, `ValueError`, `TypeError`
- search hits: `ISSUE.md`, `README.md`, `src/duration.py`, `tests/test_duration.py`
- regression test it wrote: `tests/test_regression_duration_minutes.py`
- frozen hash `dba73128d0ab…` before and after the patch — unchanged
- patch: `src/duration.py` only, 1 file changed, +11 −15 (also handles uppercase suffixes)
- model calls: 4 (queries, write_test, patch — one patch call was retried, and the run used
  `HF_TOKEN_FALLBACK` at least once, which is the rotation working as designed)
- cost: 3,843 prompt + 14,306 completion tokens ≈ **$0.066** for one verified fix

## What the gate refused to accept

The gate never ran a patch that touched a test. Implementation, in order:

1. a patch entry naming the frozen test path, or any file under `tests/` that existed on the
   baseline, is rejected **before execution**;
2. every baseline test file is re-hashed after the patch is applied, and any difference rejects
   the attempt;
3. the frozen test file is hashed again before running, and a changed hash rejects the attempt;
4. a baseline failure that is a collection error, import error or syntax error returns
   `CANNOT_REPRODUCE` instead of counting as reproduction;
5. `VERIFIED` needs the frozen test to exit 0 **and** the suite to produce no failure that was not
   already failing on the baseline.

## Honest limitations

- One fixture, one bug, one model, one attempt. This is a vertical slice, not an evaluation.
- The "bad patch blocked by the gate" scene still needs a scripted, repeatable version; today it is
  covered by unit tests and by rules 1–3 above, not by a recorded live run.
- The patch is applied as complete file contents, so very large files would blow the context
  budget; diffs are a later optimisation.
- No Zoekt. `ripgrep` over a small repo is fast enough that an index would be decoration.
- `python -m pytest` runs the whole suite twice per attempt. Fine for a small repo, too slow for a
  large one.
