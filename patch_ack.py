from pathlib import Path

path = Path(__file__).with_name("engine.py")
text = path.read_text(encoding="utf-8")
changes = []

# #4 - Restart/reconciliation safety.
# IBKR is treated as the source of truth for positions and working orders.

if "def reconcile_ibkr_state(" not in text:
    marker = '''def make_asx_contract(symbol):\n    return Stock(symbol, "SMART", CONFIG["currency"], primaryExchange="ASX")\n'''
    helper = marker + '''\n\ndef reconcile_ibkr_state(ib, state, active_signals):\n    working_statuses = {"PendingSubmit", "PreSubmitted", "Submitted", "PendingCancel"}\n\n    positions = {}\n    for p in ib.positions():\n        if not p.contract.symbol or p.position == 0:\n            continue\n        positions[p.contract.symbol] = {\n            "quantity": float(p.position),\n            "avg_cost": float(p.avgCost or 0),\n            "account": p.account,\n        }\n\n    working_orders = []\n    working_symbols = set()\n    for t in ib.reqAllOpenOrders():\n        if t.orderStatus.status not in working_statuses:\n            continue\n        symbol = str(t.contract.symbol or "").strip().upper()\n        if not symbol:\n            continue\n        working_symbols.add(symbol)\n        working_orders.append({\n            "symbol": symbol,\n            "order_id": int(t.order.orderId or 0),\n            "parent_id": int(t.order.parentId or 0),\n            "action": str(t.order.action or ""),\n            "order_type": str(t.order.orderType or ""),\n            "status": str(t.orderStatus.status or ""),\n            "quantity": float(t.order.totalQuantity or 0),\n            "limit_price": float(getattr(t.order, "lmtPrice", 0) or 0),\n            "stop_price": float(getattr(t.order, "auxPrice", 0) or 0),\n            "order_ref": str(getattr(t.order, "orderRef", "") or ""),\n        })\n\n    open_symbols = set(positions)\n    occupied = open_symbols | working_symbols\n\n    known_signal_keys = {signal_key(sig) for sig in active_signals}\n    active_symbols = {str(sig.get("symbol") or "").strip().upper() for sig in active_signals}\n    untracked = []\n    for symbol in sorted(occupied):\n        refs = {o["order_ref"] for o in working_orders if o["symbol"] == symbol and o["order_ref"]}\n        if refs and any(ref in known_signal_keys for ref in refs):\n            continue\n        if symbol in active_symbols:\n            continue\n        untracked.append(symbol)\n\n    snapshot = {\n        "timestamp": now_iso(),\n        "positions": positions,\n        "working_orders": working_orders,\n        "open_symbols": sorted(open_symbols),\n        "working_order_symbols": sorted(working_symbols),\n        "occupied_symbols": sorted(occupied),\n        "planned_open_risk_pct": len(occupied) * CONFIG["risk_per_trade_pct"],\n        "untracked_symbols": untracked,\n    }\n    state["ibkr_reconciliation"] = snapshot\n    save_state(state)\n    log_event("RECONCILE", snapshot)\n    return snapshot\n'''
    if marker not in text:
        raise SystemExit("Reconciliation helper insertion target not found; refusing partial patch.")
    text = text.replace(marker, helper, 1)
    changes.append("restart reconciliation helper")

if "reconcile_ibkr_state(ib, state, active)" not in text:
    anchors = [
        '''        occupied_syms = open_syms | working_syms\n        risk_pct = len(occupied_syms) * CONFIG["risk_per_trade_pct"]\n''',
        '''        working_syms = working_order_symbols(ib)\n        occupied_syms = open_syms | working_syms\n        risk_pct = len(occupied_syms) * CONFIG["risk_per_trade_pct"]\n'''
    ]
    inserted = False
    for anchor in anchors:
        if anchor in text:
            text = text.replace(anchor, anchor + '''        reconcile_ibkr_state(ib, state, active)\n''', 1)
            inserted = True
            changes.append("startup reconciliation scan")
            break
    if not inserted:
        raise SystemExit("Reconciliation call insertion target not found; refusing partial patch.")

path.write_text(text, encoding="utf-8")
if changes:
    print("engine.py patched successfully: " + ", ".join(changes))
else:
    print("engine.py already contains restart reconciliation safeguards")
