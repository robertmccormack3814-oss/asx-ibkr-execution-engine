from pathlib import Path

path = Path(__file__).with_name("engine.py")
text = path.read_text(encoding="utf-8")
changes = []

# #5 - Filled-position management: persistent first-seen date + strategy-specific
# maximum holding-period monitoring. In dry-run this is observation-only.

# Ensure ASX timezone helpers exist before using them.
if "from zoneinfo import ZoneInfo" not in text:
    text = text.replace(
        "from datetime import datetime, timezone",
        "from datetime import datetime, timezone, timedelta\nfrom zoneinfo import ZoneInfo",
        1,
    )
    changes.append("ASX timezone imports")

if "def trading_days_between_dates(" not in text:
    marker = '''def signal_key(sig):
    return f"{sig.get('symbol')}|{sig.get('signal_date')}|{sig.get('strategy')}|{sig.get('entry_price')}"
'''
    helper = marker + '''\n\ndef trading_days_between_dates(start_date, end_date):
    if start_date > end_date:
        return 0
    return sum(
        1 for i in range(1, (end_date - start_date).days + 1)
        if (start_date + timedelta(days=i)).weekday() < 5
    )
'''
    if marker not in text:
        raise SystemExit("signal_key insertion target not found; refusing partial patch.")
    text = text.replace(marker, helper, 1)
    changes.append("trading-day holding helper")

if "def manage_filled_positions(" not in text:
    marker = '''def make_asx_contract(symbol):
    return Stock(symbol, "SMART", CONFIG["currency"], primaryExchange="ASX")
'''
    helper = marker + '''\n\ndef manage_filled_positions(ib, state, active_signals):
    """Track filled positions and identify strategy-specific time exits.

    The first run that observes a filled symbol records first_seen_date in the
    ASX timezone. Subsequent restarts reuse that persisted date. This avoids
    using signal_date as a substitute for fill date.
    """
    today = datetime.now(ZoneInfo("Australia/Sydney")).date()
    tracking = state.setdefault("position_tracking", {})
    active_by_symbol = {}
    for sig in active_signals:
        symbol = str(sig.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        current = active_by_symbol.get(symbol)
        if current is None or str(sig.get("signal_date") or "") > str(current.get("signal_date") or ""):
            active_by_symbol[symbol] = sig

    open_positions = {
        str(p.contract.symbol or "").strip().upper(): p
        for p in ib.positions()
        if p.position != 0 and p.contract.symbol
    }

    # Remove tracking only after a position is no longer open. Keep a closed
    # snapshot for later trade-result logging.
    for symbol in list(tracking):
        if symbol not in open_positions and tracking[symbol].get("status") == "OPEN":
            tracking[symbol]["status"] = "CLOSED_PENDING_LOG"
            tracking[symbol]["last_seen_open_date"] = str(today)

    results = []
    limits = CONFIG.get("max_holding_days_by_strategy", {})
    for symbol, position in open_positions.items():
        sig = active_by_symbol.get(symbol)
        strategy = str((sig or {}).get("strategy") or tracking.get(symbol, {}).get("strategy") or "").upper()
        signal_date = (sig or {}).get("signal_date") or tracking.get(symbol, {}).get("signal_date")

        rec = tracking.get(symbol)
        if not rec or rec.get("status") != "OPEN":
            rec = {
                "status": "OPEN",
                "first_seen_date": str(today),
                "first_seen_timestamp": now_iso(),
                "quantity": float(position.position),
                "avg_cost": float(position.avgCost or 0),
                "strategy": strategy,
                "signal_date": signal_date,
            }
            tracking[symbol] = rec
        else:
            rec["quantity"] = float(position.position)
            rec["avg_cost"] = float(position.avgCost or 0)
            if strategy:
                rec["strategy"] = strategy
            if signal_date:
                rec["signal_date"] = signal_date

        try:
            first_seen = datetime.strptime(rec["first_seen_date"], "%Y-%m-%d").date()
        except Exception:
            first_seen = today
            rec["first_seen_date"] = str(today)

        held_days = trading_days_between_dates(first_seen, today)
        max_days = int(limits.get(rec.get("strategy"), 0) or 0)
        due = bool(max_days and held_days >= max_days)
        payload = {
            "symbol": symbol,
            "strategy": rec.get("strategy"),
            "quantity": float(position.position),
            "avg_cost": float(position.avgCost or 0),
            "first_seen_date": rec.get("first_seen_date"),
            "holding_trading_days": held_days,
            "max_holding_trading_days": max_days or None,
            "time_exit_due": due,
        }
        log_event("POSITION_STATUS", payload)
        if due:
            event_type = "TIME_EXIT_DRY_RUN" if CONFIG.get("dry_run") else "TIME_EXIT_DUE"
            log_event(event_type, payload)
        results.append(payload)

    save_state(state)
    return results
'''
    if marker not in text:
        raise SystemExit("Filled-position helper insertion target not found; refusing partial patch.")
    text = text.replace(marker, helper, 1)
    changes.append("filled-position holding manager")

if "manage_filled_positions(ib, state, active)" not in text:
    anchors = [
        '''        reconcile_ibkr_state(ib, state, active)\n''',
        '''        expire_stale_unfilled_entries(ib, active, open_syms)\n'''
    ]
    inserted = False
    for anchor in anchors:
        if anchor in text:
            text = text.replace(anchor, anchor + '''        manage_filled_positions(ib, state, active)\n''', 1)
            inserted = True
            changes.append("filled-position startup scan")
            break
    if not inserted:
        raise SystemExit("Filled-position startup insertion target not found; refusing partial patch.")

path.write_text(text, encoding="utf-8")
if changes:
    print("engine.py patched successfully: " + ", ".join(changes))
else:
    print("engine.py already contains filled-position holding management")
