from pathlib import Path

path = Path(__file__).with_name("engine.py")
text = path.read_text(encoding="utf-8")
changes = []

# #3 - Automatic expiry of stale, UNFILLED entry brackets.
# This patch deliberately never expires a symbol that is already a filled position.

# 1) Give future brackets an IBKR orderRef containing the signal key so restart
# reconciliation can identify which signal created the bracket.
if "orderRef=signal_key(sig)" not in text:
    old_parent = '''    parent = LimitOrder(
        "BUY", qty, prices["entry"], orderId=parent_id, transmit=False,
        tif=CONFIG["time_in_force"], account=CONFIG["ibkr"]["account"]
    )
'''
    new_parent = '''    parent = LimitOrder(
        "BUY", qty, prices["entry"], orderId=parent_id, transmit=False,
        tif=CONFIG["time_in_force"], account=CONFIG["ibkr"]["account"],
        orderRef=signal_key(sig)
    )
'''
    if old_parent in text:
        text = text.replace(old_parent, new_parent, 1)
        changes.append("signal orderRef on parent")

    old_take = '''    take = LimitOrder(
        "SELL", qty, prices["target"], orderId=take_id, parentId=parent_id,
        transmit=False, tif="GTC", account=CONFIG["ibkr"]["account"]
    )
'''
    new_take = '''    take = LimitOrder(
        "SELL", qty, prices["target"], orderId=take_id, parentId=parent_id,
        transmit=False, tif="GTC", account=CONFIG["ibkr"]["account"],
        orderRef=signal_key(sig)
    )
'''
    if old_take in text:
        text = text.replace(old_take, new_take, 1)
        changes.append("signal orderRef on target")

    old_stop = '''    protective = StopOrder(
        "SELL", qty, prices["stop"], orderId=stop_id, parentId=parent_id,
        transmit=True, tif="GTC", account=CONFIG["ibkr"]["account"]
    )
'''
    new_stop = '''    protective = StopOrder(
        "SELL", qty, prices["stop"], orderId=stop_id, parentId=parent_id,
        transmit=True, tif="GTC", account=CONFIG["ibkr"]["account"],
        orderRef=signal_key(sig)
    )
'''
    if old_stop in text:
        text = text.replace(old_stop, new_stop, 1)
        changes.append("signal orderRef on stop")

# 2) Add helper that finds and (paper mode only) expires stale unfilled brackets.
if "def expire_stale_unfilled_entries(" not in text:
    marker = '''def make_asx_contract(symbol):
    return Stock(symbol, "SMART", CONFIG["currency"], primaryExchange="ASX")
'''
    helper = marker + '''\n\ndef expire_stale_unfilled_entries(ib, active_signals, open_syms):
    """Expire stale unfilled entry brackets without touching filled positions.

    In dry-run mode this is observation-only and logs ENTRY_EXPIRY_DRY_RUN.
    In paper execution mode it cancels only the matching parent BUY and its
    attached children. A filled symbol is always excluded from expiry.
    """
    max_age = int(CONFIG.get("entry_expiry_trading_days", 2))
    working_statuses = {"PendingSubmit", "PreSubmitted", "Submitted", "PendingCancel"}
    trades = [
        t for t in ib.reqAllOpenOrders()
        if t.orderStatus.status in working_statuses
    ]

    active_by_symbol = {}
    for sig in active_signals:
        symbol = str(sig.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        current = active_by_symbol.get(symbol)
        if current is None or str(sig.get("signal_date") or "") > str(current.get("signal_date") or ""):
            active_by_symbol[symbol] = sig

    events = []
    for parent_trade in trades:
        symbol = str(parent_trade.contract.symbol or "").strip().upper()
        order = parent_trade.order

        # Only parent BUY entries can expire here. Protective children and any
        # symbol that has already filled are explicitly excluded.
        if not symbol or symbol in open_syms:
            continue
        if str(order.action).upper() != "BUY" or int(order.parentId or 0) != 0:
            continue

        sig = None
        ref = str(getattr(order, "orderRef", "") or "")
        if ref:
            for candidate in active_signals:
                if signal_key(candidate) == ref:
                    sig = candidate
                    break
        if sig is None:
            sig = active_by_symbol.get(symbol)
        if sig is None:
            log_event("ENTRY_EXPIRY_UNTRACKED", {
                "symbol": symbol,
                "order_id": order.orderId,
                "reason": "working parent BUY has no matching active signal",
            })
            continue

        age_days = signal_age_trading_days(sig)
        if age_days is None or age_days <= max_age:
            continue

        bracket = [
            t for t in trades
            if str(t.contract.symbol or "").strip().upper() == symbol
            and (
                t.order.orderId == order.orderId
                or int(t.order.parentId or 0) == int(order.orderId)
            )
        ]
        payload = {
            "symbol": symbol,
            "signal": signal_key(sig),
            "signal_date": sig.get("signal_date"),
            "signal_age_trading_days": age_days,
            "entry_expiry_trading_days": max_age,
            "parent_order_id": order.orderId,
            "bracket_order_ids": [t.order.orderId for t in bracket],
        }

        if CONFIG.get("dry_run"):
            log_event("ENTRY_EXPIRY_DRY_RUN", payload)
            events.append({**payload, "action": "WOULD_CANCEL"})
            continue

        if not CONFIG.get("paper_only"):
            raise RuntimeError("Refusing automatic entry expiry outside paper_only mode")

        for trade in sorted(bracket, key=lambda t: 0 if t.order.parentId else 1):
            if trade.orderStatus.status in working_statuses:
                ib.cancelOrder(trade.order)
        ib.sleep(1)
        log_event("ENTRY_EXPIRED", payload)
        events.append({**payload, "action": "CANCELLED"})

    return events
'''
    if marker not in text:
        raise SystemExit("Entry-expiry helper insertion target not found; refusing partial patch.")
    text = text.replace(marker, helper, 1)
    changes.append("stale unfilled-entry expiry helper")

# 3) Run expiry scan after positions/working orders are known and before new entries.
if "expire_stale_unfilled_entries(ib, active, open_syms)" not in text:
    anchors = [
        '''        occupied_syms = open_syms | working_syms
        risk_pct = len(occupied_syms) * CONFIG["risk_per_trade_pct"]
''',
        '''        working_syms = working_order_symbols(ib)
        risk_pct = open_risk_pct(ib, equity)
'''
    ]
    inserted = False
    for anchor in anchors:
        if anchor in text:
            replacement = anchor + '''        expire_stale_unfilled_entries(ib, active, open_syms)
'''
            text = text.replace(anchor, replacement, 1)
            inserted = True
            changes.append("entry-expiry scan")
            break
    if not inserted:
        raise SystemExit("Entry-expiry scan insertion target not found; refusing partial patch.")

path.write_text(text, encoding="utf-8")
if changes:
    print("engine.py patched successfully: " + ", ".join(changes))
else:
    print("engine.py already contains stale unfilled-entry expiry safeguards")
