from pathlib import Path

path = Path(__file__).with_name("engine.py")
text = path.read_text(encoding="utf-8")
changes = []

# #6 - Completed-trade logging.
# Tracking only: records visible executions and finalises a trade result after
# a tracked position is no longer open. Does not place/cancel/modify orders.

# Add a dedicated completed-trades file.
if "TRADE_RESULTS_PATH" not in text:
    marker = 'LOG_PATH = ROOT / "execution_log.jsonl"\n'
    if marker not in text:
        raise SystemExit("LOG_PATH target not found; refusing partial patch")
    text = text.replace(marker, marker + 'TRADE_RESULTS_PATH = ROOT / "trade_results.jsonl"\n', 1)
    changes.append("trade-results path")

# Persist any executions visible to the IBKR session/current day so later runs
# can still reconstruct the exit even if reqExecutions no longer returns it.
if "def capture_visible_executions(" not in text:
    marker = '''def make_asx_contract(symbol):
    return Stock(symbol, "SMART", CONFIG["currency"], primaryExchange="ASX")
'''
    helper = marker + '''\n\ndef capture_visible_executions(ib, state):
    journal = state.setdefault("execution_fills", {})
    try:
        fills = list(ib.fills())
        if not fills:
            fills = list(ib.reqExecutions())
    except Exception as exc:
        log_event("EXECUTION_CAPTURE_ERROR", {"error": str(exc)})
        return 0

    added = 0
    for fill in fills:
        try:
            ex = fill.execution
            symbol = str(fill.contract.symbol or "").strip().upper()
            exec_id = str(getattr(ex, "execId", "") or "").strip()
            if not symbol or not exec_id or exec_id in journal:
                continue
            when = getattr(ex, "time", None)
            journal[exec_id] = {
                "symbol": symbol,
                "side": str(getattr(ex, "side", "") or "").upper(),
                "shares": float(getattr(ex, "shares", 0) or 0),
                "price": float(getattr(ex, "price", 0) or 0),
                "time": when.isoformat() if hasattr(when, "isoformat") else str(when or ""),
                "order_id": int(getattr(ex, "orderId", 0) or 0),
                "perm_id": int(getattr(ex, "permId", 0) or 0),
            }
            added += 1
        except Exception:
            continue
    if added:
        save_state(state)
        log_event("EXECUTIONS_CAPTURED", {"new_fills": added})
    return added

\ndef _parse_execution_time(raw):
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None

\ndef _weighted_fill(fills):
    qty = sum(float(x.get("shares") or 0) for x in fills)
    if qty <= 0:
        return None, 0.0
    value = sum(float(x.get("shares") or 0) * float(x.get("price") or 0) for x in fills)
    return value / qty, qty

\ndef append_trade_result(record):
    with TRADE_RESULTS_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\\n")

\ndef finalize_closed_trades(state, active_signals):
    tracking = state.setdefault("position_tracking", {})
    journal = list((state.get("execution_fills") or {}).values())
    completed = state.setdefault("completed_trades", {})
    active_by_symbol = {
        str(s.get("symbol") or "").strip().upper(): s
        for s in active_signals if s.get("symbol")
    }
    written = 0

    for symbol, rec in list(tracking.items()):
        if rec.get("status") != "CLOSED_PENDING_LOG":
            continue
        trade_id = str(rec.get("trade_id") or f"{symbol}|{rec.get('holding_start_date') or rec.get('first_seen_date')}")
        if trade_id in completed:
            rec["status"] = "CLOSED_LOGGED"
            continue

        start_raw = rec.get("fill_timestamp")
        start_dt = _parse_execution_time(start_raw)
        if start_dt is None:
            try:
                d = datetime.strptime(
                    str(rec.get("holding_start_date") or rec.get("first_seen_date")), "%Y-%m-%d"
                ).date()
                start_dt = datetime(d.year, d.month, d.day, tzinfo=ZoneInfo("Australia/Sydney"))
            except Exception:
                start_dt = None

        sell_fills = []
        for x in journal:
            if str(x.get("symbol") or "").upper() != symbol:
                continue
            if str(x.get("side") or "").upper() not in {"SLD", "SELL"}:
                continue
            dt = _parse_execution_time(x.get("time"))
            if start_dt is not None and dt is not None and dt < start_dt:
                continue
            sell_fills.append(x)

        exit_price, exit_qty = _weighted_fill(sell_fills)
        if exit_price is None or exit_qty <= 0:
            log_event("TRADE_LOG_PENDING", {
                "symbol": symbol,
                "reason": "position closed but no captured SELL execution is available yet",
            })
            continue

        sig = active_by_symbol.get(symbol) or {}
        entry_price = float(rec.get("avg_cost") or sig.get("entry_price") or 0)
        qty = min(abs(float(rec.get("quantity") or exit_qty)), exit_qty)
        target = float(rec.get("target") or sig.get("profit_target") or 0)
        stop = float(rec.get("stop") or sig.get("stop_loss") or 0)
        strategy = str(rec.get("strategy") or sig.get("strategy") or "").upper()
        signal_date = rec.get("signal_date") or sig.get("signal_date")

        exit_times = [d for d in (_parse_execution_time(x.get("time")) for x in sell_fills) if d is not None]
        exit_dt = max(exit_times) if exit_times else None
        exit_date = None
        if exit_dt is not None:
            exit_date = str(exit_dt.astimezone(ZoneInfo("Australia/Sydney")).date())

        start_date_raw = rec.get("holding_start_date") or rec.get("first_seen_date")
        holding_days = None
        if start_date_raw and exit_date:
            try:
                holding_days = trading_days_between_dates(
                    datetime.strptime(str(start_date_raw), "%Y-%m-%d").date(),
                    datetime.strptime(exit_date, "%Y-%m-%d").date(),
                )
            except Exception:
                holding_days = None

        pnl = (exit_price - entry_price) * qty if entry_price > 0 else None
        return_pct = ((exit_price / entry_price) - 1.0) * 100.0 if entry_price > 0 else None
        risk_per_share = entry_price - stop if entry_price > 0 and stop > 0 and entry_price > stop else None
        initial_risk = risk_per_share * qty if risk_per_share else None
        r_multiple = pnl / initial_risk if pnl is not None and initial_risk and initial_risk > 0 else None

        if rec.get("time_exit_requested") or rec.get("time_exit_due"):
            exit_reason = "TIME_EXIT"
        elif target > 0 and exit_price >= target * 0.995:
            exit_reason = "TARGET"
        elif stop > 0 and exit_price <= stop * 1.005:
            exit_reason = "STOP"
        else:
            exit_reason = "OTHER"

        record = {
            "timestamp": now_iso(),
            "trade_id": trade_id,
            "symbol": symbol,
            "strategy": strategy,
            "signal_date": signal_date,
            "entry_date": start_date_raw,
            "entry_price": entry_price,
            "exit_date": exit_date,
            "exit_price": exit_price,
            "quantity": qty,
            "target": target or None,
            "stop": stop or None,
            "holding_trading_days": holding_days,
            "pnl_aud": pnl,
            "return_pct": return_pct,
            "r_multiple": r_multiple,
            "exit_reason": exit_reason,
        }
        append_trade_result(record)
        log_event("TRADE_COMPLETED", record)
        completed[trade_id] = record
        rec["status"] = "CLOSED_LOGGED"
        rec["closed_trade_id"] = trade_id
        written += 1

    if written:
        save_state(state)
    return written
'''
    if marker not in text:
        raise SystemExit("completed-trade helper insertion target not found; refusing partial patch")
    text = text.replace(marker, helper, 1)
    changes.append("execution journal and completed-trade logger")

# Store plan data with each open position so trade finalisation is independent
# of whether the scanner still lists the signal after the trade closes.
old = '''            if strategy:
                rec["strategy"] = strategy
            if signal_date:
                rec["signal_date"] = signal_date
'''
new = '''            if strategy:
                rec["strategy"] = strategy
            if signal_date:
                rec["signal_date"] = signal_date
            if sig:
                rec["target"] = float(sig.get("profit_target") or rec.get("target") or 0)
                rec["stop"] = float(sig.get("stop_loss") or rec.get("stop") or 0)
                rec["planned_entry"] = float(sig.get("entry_price") or rec.get("planned_entry") or 0)
                rec["trade_id"] = rec.get("trade_id") or signal_key(sig)
'''
if old in text and 'rec["planned_entry"]' not in text:
    text = text.replace(old, new, 1)
    changes.append("persist position trade plan")

# Also populate plan data when the position record is first created.
old_new = '''                "strategy": strategy,
                "signal_date": signal_date,
            }
'''
new_new = '''                "strategy": strategy,
                "signal_date": signal_date,
                "target": float((sig or {}).get("profit_target") or 0),
                "stop": float((sig or {}).get("stop_loss") or 0),
                "planned_entry": float((sig or {}).get("entry_price") or 0),
                "trade_id": signal_key(sig) if sig else None,
            }
'''
if old_new in text and '"planned_entry": float((sig or {}).get("entry_price")' not in text:
    text = text.replace(old_new, new_new, 1)
    changes.append("seed position trade plan")

# Capture fills, run position management, then finalise any position that has
# transitioned to CLOSED_PENDING_LOG in the same startup cycle.
old_call = '''        manage_filled_positions(ib, state, active)
'''
new_call = '''        capture_visible_executions(ib, state)
        manage_filled_positions(ib, state, active)
        finalize_closed_trades(state, active)
'''
if old_call in text and "finalize_closed_trades(state, active)" not in text:
    text = text.replace(old_call, new_call, 1)
    changes.append("startup completed-trade scan")

path.write_text(text, encoding="utf-8")
if changes:
    print("engine.py patched successfully: " + ", ".join(changes))
else:
    print("engine.py already contains completed-trade logging")
