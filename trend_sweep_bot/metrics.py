"""
metrics.py — performance statistics from a list of closed trades + an equity curve.

Pure Python (no pandas needed) so it can run anywhere. Sharpe is annualized from the per-trade
return series; monthly returns are grouped by the trade's close month.
"""
from __future__ import annotations
import math
from collections import defaultdict
from datetime import datetime, timezone


def _stdev(xs):
    n = len(xs)
    if n < 2:
        return 0.0
    m = sum(xs) / n
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))


def compute(trades, equity_curve, start_balance, bars_per_year=None):
    """trades: list of dicts with 'pnl' and 'closed_at'. equity_curve: list of (ts, equity)."""
    n = len(trades)
    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    net = sum(pnls)
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    win_rate = (len(wins) / n) if n else 0.0
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0)
    avg_trade = (net / n) if n else 0.0

    # Sharpe from per-trade returns (relative to start balance), annualized by trade frequency
    rets = [p / start_balance for p in pnls]
    sd = _stdev(rets)
    mean = (sum(rets) / len(rets)) if rets else 0.0
    if sd > 0 and n > 1:
        # annualize by the average number of trades per year
        span_days = max(1.0, (equity_curve[-1][0] - equity_curve[0][0]) / 86400.0) if len(equity_curve) > 1 else 1.0
        trades_per_year = n / (span_days / 365.0)
        sharpe = (mean / sd) * math.sqrt(max(1.0, trades_per_year))
    else:
        sharpe = 0.0

    # max drawdown from the equity curve
    peak = -float("inf")
    max_dd = 0.0
    max_dd_pct = 0.0
    for _, eq in equity_curve:
        peak = max(peak, eq)
        dd = peak - eq
        if dd > max_dd:
            max_dd = dd
            max_dd_pct = dd / peak if peak > 0 else 0.0

    # monthly returns (sum of PnL grouped by close month, as % of start balance)
    monthly = defaultdict(float)
    for t in trades:
        d = datetime.fromtimestamp(t["closed_at"], timezone.utc)
        monthly[d.strftime("%Y-%m")] += t["pnl"]
    monthly_pct = {k: round(v / start_balance * 100, 2) for k, v in sorted(monthly.items())}

    final_equity = equity_curve[-1][1] if equity_curve else start_balance
    return {
        "trades": n,
        "net_profit": round(net, 2),
        "net_profit_pct": round(net / start_balance * 100, 2),
        "final_equity": round(final_equity, 2),
        "win_rate": round(win_rate * 100, 2),
        "wins": len(wins),
        "losses": len(losses),
        "profit_factor": round(profit_factor, 3) if profit_factor != float("inf") else "inf",
        "avg_trade": round(avg_trade, 2),
        "avg_win": round(sum(wins) / len(wins), 2) if wins else 0.0,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0.0,
        "sharpe": round(sharpe, 2),
        "max_drawdown": round(max_dd, 2),
        "max_drawdown_pct": round(max_dd_pct * 100, 2),
        "monthly_returns_pct": monthly_pct,
    }


def print_report(m, title="BACKTEST RESULTS"):
    print("\n" + "=" * 60)
    print(f" {title}")
    print("=" * 60)
    rows = [
        ("Trades", m["trades"]),
        ("Net profit", f"${m['net_profit']:,.2f} ({m['net_profit_pct']:+.2f}%)"),
        ("Final equity", f"${m['final_equity']:,.2f}"),
        ("Win rate", f"{m['win_rate']:.2f}%  ({m['wins']}W / {m['losses']}L)"),
        ("Profit factor", m["profit_factor"]),
        ("Avg trade", f"${m['avg_trade']:,.2f}"),
        ("Avg win / loss", f"${m['avg_win']:,.2f} / ${m['avg_loss']:,.2f}"),
        ("Sharpe (annualized)", m["sharpe"]),
        ("Max drawdown", f"${m['max_drawdown']:,.2f} ({m['max_drawdown_pct']:.2f}%)"),
    ]
    for k, v in rows:
        print(f"  {k:<22} {v}")
    print("  Monthly returns (%):")
    for mth, pct in m["monthly_returns_pct"].items():
        print(f"    {mth}: {pct:+.2f}%")
    print("=" * 60 + "\n")
