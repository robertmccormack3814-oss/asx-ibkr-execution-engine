from pathlib import Path

path = Path(__file__).with_name("engine.py")
text = path.read_text(encoding="utf-8")
changes = []

# #5 finalization: anchor holding periods to an actual IBKR fill timestamp when
# it is available, otherwise use the earliest reliable local RECONCILE record.
# This patch is tracking-only; it does not place, cancel, or modify orders.

if "def earliest_local_position_date(" not in text:
    marker = '''def trading_days_between_dates(start_date, end_date):
    if start_date > end_date:
        return 0
    return sum(
        1 for i in range(1, (end_date - start_date).days + 1)
        if (start_date + timedelta(days=i)).weekday() < 5
    )
'''
    helper = marker + '''\n\ndef earliest_local_position_date(symbol):
    """Earliest ASX-local date where our log recorded symbol as an open position."""
    symbol = str(symbol or "").strip().upper()
    if not symbol or not LOG_PATH.exists():
        return None
    earliest = None
    try:
        with LOG_PATH.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if row.get("type") != "RECONCILE":
                    continue
                positions = row.get("positions") or {}
                if symbol not in positions:
                    continue
                raw_ts = str(row.get("timestamp") or "")
                try:
                    dt = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
                    local_date = dt.astimezone(ZoneInfo("Australia/Sydney")).date()
                except Exception:
                    continue
                if earliest is None or local_date < earliest:
                    earliest = local_date
    except Exception:
        return None
    return earliest

\ndef current_session_buy_fill(ib, symbol):
    """Return earliest visible BUY execution for symbol from the current IBKR session/day."""
    symbol = str(symbol or "").strip().upper()
    matches = []
    try:
        fills = list(ib.fills())
        if not fills:
            fills = list(ib.reqExecutions())
    except Exception:
        fills = []
    for fill in fills:
        try:
            if str(fill.contract.symbol or "").strip().upper() != symbol:
                continue
            side = str(fill.execution.side or "").upper()
            if side not in {"BOT", "BUY"}:
                continue
            when = fill.execution.time
            if when is None:
                continue
            matches.append(when)
        except Exception:
            continue
    if not matches:
        return None
    return min(matches)
'''
    if marker not in text:
        raise SystemExit("holding helper target not found; refusing partial patch")
    text = text.replace(marker, helper, 1)
    changes.append("fill/local-history date helpers")

old_new_record = '''        if not rec or rec.get("status") != "OPEN":
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
'''
new_new_record = '''        if not rec or rec.get("status") != "OPEN":
            fill_dt = current_session_buy_fill(ib, symbol)
            historical_date = earliest_local_position_date(symbol)
            if fill_dt is not None:
                if fill_dt.tzinfo is None:
                    fill_dt = fill_dt.replace(tzinfo=timezone.utc)
                start_date = fill_dt.astimezone(ZoneInfo("Australia/Sydney")).date()
                start_source = "IBKR_EXECUTION"
                fill_timestamp = fill_dt.isoformat()
            elif historical_date is not None:
                start_date = historical_date
                start_source = "LOCAL_RECONCILE_HISTORY"
                fill_timestamp = None
            else:
                start_date = today
                start_source = "FIRST_OBSERVED_FALLBACK"
                fill_timestamp = None

            rec = {
                "status": "OPEN",
                "first_seen_date": str(start_date),
                "holding_start_date": str(start_date),
                "holding_start_source": start_source,
                "fill_timestamp": fill_timestamp,
                "first_seen_timestamp": now_iso(),
                "quantity": float(position.position),
                "avg_cost": float(position.avgCost or 0),
                "strategy": strategy,
                "signal_date": signal_date,
            }
            tracking[symbol] = rec
'''
if old_new_record in text:
    text = text.replace(old_new_record, new_new_record, 1)
    changes.append("actual fill/history holding start")

# Upgrade already-tracked positions such as AVH without moving the clock later.
old_else = '''        else:
            rec["quantity"] = float(position.position)
            rec["avg_cost"] = float(position.avgCost or 0)
            if strategy:
                rec["strategy"] = strategy
            if signal_date:
                rec["signal_date"] = signal_date

        try:
            first_seen = datetime.strptime(rec["first_seen_date"], "%Y-%m-%d").date()
'''
new_else = '''        else:
            rec["quantity"] = float(position.position)
            rec["avg_cost"] = float(position.avgCost or 0)
            if strategy:
                rec["strategy"] = strategy
            if signal_date:
                rec["signal_date"] = signal_date

            existing_date = None
            try:
                existing_date = datetime.strptime(
                    str(rec.get("holding_start_date") or rec.get("first_seen_date")), "%Y-%m-%d"
                ).date()
            except Exception:
                pass
            fill_dt = current_session_buy_fill(ib, symbol)
            historical_date = earliest_local_position_date(symbol)
            candidates = [d for d in [existing_date, historical_date] if d is not None]
            if fill_dt is not None:
                if fill_dt.tzinfo is None:
                    fill_dt = fill_dt.replace(tzinfo=timezone.utc)
                fill_date = fill_dt.astimezone(ZoneInfo("Australia/Sydney")).date()
                candidates.append(fill_date)
                rec["fill_timestamp"] = fill_dt.isoformat()
                rec["holding_start_source"] = "IBKR_EXECUTION"
            elif historical_date is not None and rec.get("holding_start_source") != "IBKR_EXECUTION":
                rec["holding_start_source"] = "LOCAL_RECONCILE_HISTORY"
            if candidates:
                start_date = min(candidates)
                rec["holding_start_date"] = str(start_date)
                rec["first_seen_date"] = str(start_date)

        try:
            first_seen = datetime.strptime(
                str(rec.get("holding_start_date") or rec["first_seen_date"]), "%Y-%m-%d"
            ).date()
'''
if old_else in text:
    text = text.replace(old_else, new_else, 1)
    changes.append("existing position start-date upgrade")

old_payload = '''            "first_seen_date": rec.get("first_seen_date"),
            "holding_trading_days": held_days,
'''
new_payload = '''            "first_seen_date": rec.get("first_seen_date"),
            "holding_start_date": rec.get("holding_start_date") or rec.get("first_seen_date"),
            "holding_start_source": rec.get("holding_start_source"),
            "fill_timestamp": rec.get("fill_timestamp"),
            "holding_trading_days": held_days,
'''
if old_payload in text and '"holding_start_source": rec.get("holding_start_source")' not in text:
    text = text.replace(old_payload, new_payload, 1)
    changes.append("position start-source logging")

path.write_text(text, encoding="utf-8")
if changes:
    print("engine.py patched successfully: " + ", ".join(changes))
else:
    print("engine.py already contains finalized filled-position start-date tracking")
