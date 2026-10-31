# Three-minute demo: don't trust the patch

Meta Global AI Developer Hackathon, deadline 2026-11-05.
Target runtime: **3:00**. Prepare the environment and terminal before recording.

## Preparation (off-camera)

1. Install `requirements.txt` in a virtual environment and run `python3 -m pytest -q`.
2. Open README's architecture diagram, `fixtures/tinybug/ISSUE.md`, and
   `agent/repair.py` at the frozen-test rejection and final gate checks.
3. Run `python3 -m agent.demo` once. Know where the printed receipt paths are.
   Each repeat creates a fresh run directory; no cleanup is necessary.
4. Set a readable terminal font and hide unrelated tabs, environment files, and
   account details. Never record keys, shell environment dumps, or private URLs.
5. If demonstrating live inference, privately configure credentials, build the
   Docker image, and pre-record a real run using the README's HF Llama command.
   Label that clip with its actual model/provider. Do not impersonate a live run
   with the offline demo. If live access is unavailable, say so and use only the
   clearly labeled historical Muse receipt as prior evidence.

## Timed script and shot list

| Time | Shot | Narration / action |
| --- | --- | --- |
| 0:00–0:20 | README title and issue's `90m` example | “A green agent run can hide a bad fix: change the assertion and everything passes. Evidence-gated repair makes the agent prove its claim, or refuse it. This is about reviewer trust, not more patch volume.” |
| 0:20–0:40 | Text architecture diagram | “A Meta model chooses search queries, writes the regression test first, and proposes the patch. The host freezes the test and checks execution evidence. Docker is the default execution boundary. The model never gets to award itself VERIFIED.” |
| 0:40–1:00 | Provider table, then an actual live receipt/clip if available | “HF defaults to Meta Llama, with Muse Spark Open also supported. Native Meta API access uses private environment configuration. Real mode sends HTTPS requests and never falls back to fake answers. This receipt's provider, model, HTTP status, and usage distinguish live inference from mock.” If only historical evidence is available: “This is the recorded Muse run, not a new Llama validation.” |
| 1:00–1:20 | Terminal: `python3 -m agent.demo` | “For repeatability, the next three scenarios are explicitly offline mock model responses. Everything after generation, including pytest, hashing, the gate, and receipts, actually executes. Local mode is NOT a sandbox; these are trusted fixtures.” |
| 1:20–1:45 | Good-patch receipt: `commands`, hashes, candidate gate | “On baseline, minutes raise an error. The regression fails. Freeze its SHA-256. Change only the parser. Now the same test and all six tests pass. The frozen hash matches: VERIFIED.” |
| 1:45–2:10 | Bad-patch receipt: `attempts[].rejected`; only baseline commands | “Now the model tries to weaken the regression test instead of fixing the parser. Both proposals are rejected before any candidate execution. No greenwash: NEEDS_REVIEW. You can see the exact forbidden path in the receipt.” |
| 2:10–2:30 | Cannot-reproduce receipt: baseline exit 0, empty attempts | “The third report says an already-supported hours format fails. The proposed test passes on baseline. There is no demonstrated bug and no patch: CANNOT_REPRODUCE.” |
| 2:30–2:45 | Final three-row verdict table and receipt paths | “Three different outcomes, one repeatable command, inspectable evidence for every one. The demo itself succeeds only when all three expected outcomes match, including both refusals.” |
| 2:45–3:00 | README limitations and closing title | “This is a narrow Python prototype, not a security proof or an accuracy benchmark. A weak test can still miss a bug. The contribution is an honest boundary: don't trust the patch, inspect the evidence.” |

## Receipt close-ups

Open only the three `.receipt.json` paths printed by the **same** demo run.
Expand these fields in an editor:

- Good: `model.mode: mock`, `runner.sandbox: false`, baseline exit 1, candidate
  exits 0, equal `test_sha256_frozen` / `test_sha256_after`, suite log `6 passed`.
- Bad: two `attempts[].rejected` entries naming a frozen test, `gate: null`,
  no candidate commands, final `NEEDS_REVIEW`.
- Cannot reproduce: baseline exit 0, `baseline.genuine: false`, no attempts,
  final `CANNOT_REPRODUCE`.

Do not claim the mock run measures Meta-model quality, API latency, billing cost,
or Docker isolation. The existing historical live receipt and optional fresh
live recording are separate evidence.
