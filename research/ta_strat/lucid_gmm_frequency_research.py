"""Sequential (never stacked) trade-frequency test for the NQ GMM state."""
from __future__ import annotations

from datetime import date

import lucid_causal_rebuild as L
import lucid_eval_scalper_research as E
import lucid_gmm_confluence_research as G


def main() -> int:
    days = L.load_days("nq")
    bars = G._bars(days)
    x = G._features(bars)
    signals = G.walkforward_signals(
        bars, x, transition_threshold=-1.0, volume_threshold=-999.0
    )
    rows = []
    for pullback in (0.10, 0.15):
        for start in (30, 60):
            for maximum in (1, 2, 3):
                cfg = G.GConfig(
                    pullback, 0.25, "long", 13, start, 300, 13, maximum
                )
                trades = G.generate(days, bars, signals, cfg)
                train = L.basic_stats(L.size_trades(
                    L._slice(trades, None, L.TRAIN_END), 500.0
                ))
                valid = L.basic_stats(L.size_trades(
                    L._slice(trades, date(2022, 1, 1), L.VALID_END), 500.0
                ))
                rows.append((
                    E.development_score(train, valid),
                    cfg, trades, train, valid,
                ))
    rows.sort(key=lambda row: row[0], reverse=True)
    print("FINALISTS SELECTED ON TRAIN + VALIDATION ONLY")
    for score, cfg, trades, train, valid in rows:
        test = L.basic_stats(L.size_trades(
            L._slice(trades, date(2024, 1, 1), None), 500.0
        ))
        print(f"\n{cfg.label} score {score:.3f}")
        for name, result in (("train", train), ("valid", valid), ("TEST", test)):
            print(
                f"  {name:<5} n{result['n']:5} PF{result['pf']:.2f} "
                f"net{result['net']:+9.0f} avg{result['avg']:+6.1f} "
                f"DD{result['maxdd']:8.0f}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
