"""Run the Reference Ladder baseline or full research suite from the CLI."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from reference_ladder.config import LadderConfig
from reference_ladder.data import BinanceMinuteLoader
from reference_ladder.research import run_research


def main() -> int:
    parser = argparse.ArgumentParser(description="BTC 1m Reference Ladder research")
    parser.add_argument("--start", default="2018-01-01")
    parser.add_argument("--end", default="")
    parser.add_argument("--baseline-only", action="store_true")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--output", default="output/reference_ladder/latest.json")
    args = parser.parse_args()

    print(f"Loading official BTCUSDT 1m data: {args.start} -> {args.end or 'latest'}", flush=True)
    frame = BinanceMinuteLoader().load(args.start, args.end, refresh=args.refresh)
    quality = frame.attrs.get("data_quality", {})
    print(f"Loaded {len(frame):,} bars: {frame.index[0]} -> {frame.index[-1]}", flush=True)
    print(f"Data quality: {quality}", flush=True)
    print("Running Reference Ladder research...", flush=True)
    report = run_research(frame, LadderConfig(), full=not args.baseline_only)

    path = ROOT / args.output
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, allow_nan=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)

    baseline = report["baseline_summary"]
    print("\nBASELINE")
    for key, value in baseline.items():
        print(f"  {key}: {value}")
    if report.get("stress_tests"):
        print("\nSTRESS TESTS")
        for row in report["stress_tests"]:
            print(
                f"  {row['period']}: recovered={row['recovered']} "
                f"deepest={row.get('deepest_floating_loss_usd')} "
                f"liquidations={row.get('liquidations')} return={row.get('total_return_pct')}%"
            )
    print(f"\nSaved: {path}")
    return 0 if report["baseline"].get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
