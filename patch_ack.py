from pathlib import Path

path = Path(__file__).with_name("engine.py")
text = path.read_text(encoding="utf-8")
changes = []

# #7 - Performance statistics. Read-only analytics over completed trade results.

if "PERFORMANCE_PATH" not in text:
    marker = 'TRADE_RESULTS_PATH = ROOT / "trade_results.jsonl"\n'
    if marker not in text:
        raise SystemExit("TRADE_RESULTS_PATH target not found; refusing partial patch")
    text = text.replace(marker, marker + 'PERFORMANCE_PATH = ROOT / "performance_summary.json"\n', 1)
    changes.append("performance-summary path")

if "def compute_performance_statistics(" not in text:
    marker = '''def append_trade_result(record):
    with TRADE_RESULTS_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\\n")
'''
    helper = marker + '''\n\ndef load_trade_results():
    rows = []
    if not TRADE_RESULTS_PATH.exists():
        return rows
    with TRADE_RESULTS_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                row = json.loads(line)
            except Exception:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows

\ndef _metric_block(rows):
    n = len(rows)
    if n == 0:
        return {
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate_pct": None,
            "total_pnl_aud": 0.0,
            "average_pnl_aud": None,
            "average_winner_aud": None,
            "average_loser_aud": None,
            "expectancy_aud_per_trade": None,
            "average_r": None,
            "profit_factor": None,
            "max_drawdown_aud": 0.0,
        }

    pnls = [float(r.get("pnl_aud") or 0.0) for r in rows]
    winners = [x for x in pnls if x > 0]
    losers = [x for x in pnls if x < 0]
    r_values = [
        float(r.get("r_multiple")) for r in rows
        if r.get("r_multiple") is not None
    ]

    gross_profit = sum(winners)
    gross_loss_abs = abs(sum(losers))
    profit_factor = None
    if gross_loss_abs > 0:
        profit_factor = gross_profit / gross_loss_abs
    elif gross_profit > 0:
        profit_factor = None

    equity_curve = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for pnl in pnls:
        equity_curve += pnl
        if equity_curve > peak:
            peak = equity_curve
        drawdown = peak - equity_curve
        if drawdown > max_drawdown:
            max_drawdown = drawdown

    wins = len(winners)
    losses = len(losers)
    return {
        "trades": n,
        "wins": wins,
        "losses": losses,
        "win_rate_pct": wins / n * 100.0,
        "total_pnl_aud": sum(pnls),
        "average_pnl_aud": sum(pnls) / n,
        "average_winner_aud": (sum(winners) / len(winners)) if winners else None,
        "average_loser_aud": (sum(losers) / len(losers)) if losers else None,
        "expectancy_aud_per_trade": sum(pnls) / n,
        "average_r": (sum(r_values) / len(r_values)) if r_values else None,
        "profit_factor": profit_factor,
        "max_drawdown_aud": max_drawdown,
    }

\ndef compute_performance_statistics():
    rows = load_trade_results()
    by_strategy = {}
    strategies = sorted({str(r.get("strategy") or "UNKNOWN").upper() for r in rows})
    for strategy in strategies:
        subset = [r for r in rows if str(r.get("strategy") or "UNKNOWN").upper() == strategy]
        by_strategy[strategy] = _metric_block(subset)

    summary = {
        "timestamp": now_iso(),
        "overall": _metric_block(rows),
        "by_strategy": by_strategy,
    }
    PERFORMANCE_PATH.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    log_event("PERFORMANCE_SUMMARY", summary)
    return summary
'''
    if marker not in text:
        raise SystemExit("append_trade_result target not found; refusing partial patch")
    text = text.replace(marker, helper, 1)
    changes.append("performance statistics helpers")

# Important: look specifically for the indented runtime call in main().
# The old check matched the function definition itself and skipped this insertion.
if "        compute_performance_statistics()\n" not in text:
    marker = '''        finalize_closed_trades(state, active)
'''
    if marker not in text:
        raise SystemExit("completed-trade startup target not found; refusing partial patch")
    text = text.replace(marker, marker + '''        compute_performance_statistics()
''', 1)
    changes.append("startup performance summary")

path.write_text(text, encoding="utf-8")
if changes:
    print("engine.py patched successfully: " + ", ".join(changes))
else:
    print("engine.py already contains performance statistics and startup call")
