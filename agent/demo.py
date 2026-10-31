"""Three repeatable, offline scenarios exercising the real execution gate."""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from agent.repair import CANNOT_REPRODUCE, NEEDS_REVIEW, VERIFIED, main as repair_main

SCENARIOS = (
    ("good_patch", VERIFIED),
    ("bad_patch", NEEDS_REVIEW),
    ("cannot_reproduce", CANNOT_REPRODUCE),
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", default="receipts/demo")
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    output = Path(args.out_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    # Keep each run's evidence and work copies, never overwrite a previous demo.
    run_dir = Path(tempfile.mkdtemp(prefix="run-", dir=output))
    print("OFFLINE DEMO: fixture-specific MOCK responses, real pytest execution.")
    print("LOCAL runner is NOT a sandbox. No API calls, tokens, or Docker required.\n", flush=True)
    rows = []
    for scenario, expected in SCENARIOS:
        issue = "ISSUE_ALREADY_SUPPORTED.md" if scenario == "cannot_reproduce" else "ISSUE.md"
        receipt_path = run_dir / f"{scenario}.receipt.json"
        repair_main([
            "--repo", str(root / "fixtures/tinybug"),
            "--issue", str(root / "fixtures/tinybug" / issue),
            "--provider", "mock", "--mock-scenario", scenario,
            "--runner", "local", "--env-file", str(run_dir / "unused.env"),
            "--work-dir", str(run_dir / scenario),
            "--out", str(receipt_path),
        ])
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        rows.append((scenario, expected, receipt["verdict"], receipt_path))
    print("\nSCENARIO             EXPECTED           ACTUAL             RESULT")
    print("-" * 70)
    for scenario, expected, actual, _ in rows:
        print(f"{scenario:<20} {expected:<18} {actual:<18} {'PASS' if actual == expected else 'FAIL'}")
    print("\nReceipts (commands, exit codes, logs, hashes, and rejected edits):")
    for scenario, _, _, path in rows:
        print(f"  {scenario}: {path}")
    return 0 if all(actual == expected for _, expected, actual, _ in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
