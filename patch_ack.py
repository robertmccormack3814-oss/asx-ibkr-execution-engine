from pathlib import Path

path = Path(__file__).with_name("engine.py")
text = path.read_text(encoding="utf-8")
changes = []

# 1) Accept IBKR's normal child/trigger hold reasons for attached bracket legs.
old_ack = '''    status_ok = all(s["status"] in acceptable_statuses for s in snapshots)
    no_hold_reason = all(not s["why_held"] for s in snapshots)
    no_errors = all(
        not s["advanced_error"]
        and all(not row["error_code"] for row in s["log"])
        for s in snapshots
    )
    return structure_ok and status_ok and no_hold_reason and no_errors, snapshots
'''
new_ack = '''    status_ok = all(s["status"] in acceptable_statuses for s in snapshots)

    # IBKR normally marks attached bracket children as held until the parent
    # fills. These are expected protective states, not acknowledgement
    # failures. The parent itself should not be held.
    parent_hold_ok = not snapshots[0]["why_held"]
    child_holds_ok = all(
        not s["why_held"]
        or all(part.strip() in {"child", "trigger"} for part in s["why_held"].split(","))
        for s in snapshots[1:]
    )

    no_errors = all(
        not s["advanced_error"]
        and all(not row["error_code"] for row in s["log"])
        for s in snapshots
    )
    return structure_ok and status_ok and parent_hold_ok and child_holds_ok and no_errors, snapshots
'''
if old_ack in text:
    text = text.replace(old_ack, new_ack, 1)
    changes.append("acknowledgement")
elif "parent_hold_ok" not in text or "child_holds_ok" not in text:
    raise SystemExit("Acknowledgement patch target not found; refusing partial patch.")

# 2) Discover working IBKR orders independently of local state.json.
old_helper = '''def open_symbols(ib):
    return {p.contract.symbol for p in ib.positions() if p.position != 0}


def open_risk_pct(ib, equity):
'''
new_helper = '''def open_symbols(ib):
    return {p.contract.symbol for p in ib.positions() if p.position != 0}


def working_order_symbols(ib):
    trades = ib.reqAllOpenOrders()
    working_statuses = {"PendingSubmit", "PreSubmitted", "Submitted", "PendingCancel"}
    return {
        trade.contract.symbol
        for trade in trades
        if trade.contract.symbol and trade.orderStatus.status in working_statuses
    }


def occupied_symbols(ib):
    # A symbol consumes one risk slot if it is either a filled position or has
    # any working bracket leg. Using a set avoids double-counting once an entry
    # fills while its protective children remain working.
    return open_symbols(ib) | working_order_symbols(ib)


def open_risk_pct(ib, equity):
'''
if old_helper in text:
    text = text.replace(old_helper, new_helper, 1)
    changes.append("working-order helper")
elif "def working_order_symbols(ib):" not in text:
    raise SystemExit("Working-order helper patch target not found; refusing partial patch.")

# Add occupied_symbols if working_order_symbols is already present from a prior run.
if "def occupied_symbols(ib):" not in text:
    old = '''def open_risk_pct(ib, equity):
    return len(open_symbols(ib)) * CONFIG["risk_per_trade_pct"]
'''
    new = '''def occupied_symbols(ib):
    return open_symbols(ib) | working_order_symbols(ib)


def open_risk_pct(ib, equity):
    return len(occupied_symbols(ib)) * CONFIG["risk_per_trade_pct"]
'''
    if old not in text:
        raise SystemExit("Occupied-risk patch target not found.")
    text = text.replace(old, new, 1)
    changes.append("occupied-risk accounting")
else:
    old = '''def open_risk_pct(ib, equity):
    return len(open_symbols(ib)) * CONFIG["risk_per_trade_pct"]
'''
    new = '''def open_risk_pct(ib, equity):
    return len(occupied_symbols(ib)) * CONFIG["risk_per_trade_pct"]
'''
    if old in text:
        text = text.replace(old, new, 1)
        changes.append("occupied-risk accounting")

old_init = '''        equity = sizing_equity(actual_equity)
        open_syms = open_symbols(ib)
        risk_pct = open_risk_pct(ib, equity)
'''
new_init = '''        equity = sizing_equity(actual_equity)
        open_syms = open_symbols(ib)
        working_syms = working_order_symbols(ib)
        occupied_syms = open_syms | working_syms
        risk_pct = len(occupied_syms) * CONFIG["risk_per_trade_pct"]
'''
if old_init in text:
    text = text.replace(old_init, new_init, 1)
    changes.append("working-order scan")
elif "working_syms = working_order_symbols(ib)" in text and "occupied_syms = open_syms | working_syms" not in text:
    old = '''        working_syms = working_order_symbols(ib)
        risk_pct = open_risk_pct(ib, equity)
'''
    new = '''        working_syms = working_order_symbols(ib)
        occupied_syms = open_syms | working_syms
        risk_pct = len(occupied_syms) * CONFIG["risk_per_trade_pct"]
'''
    if old not in text:
        raise SystemExit("Occupied symbol init patch target not found.")
    text = text.replace(old, new, 1)
    changes.append("occupied-risk initialization")

old_log = '''            "open_positions": sorted(open_syms),
            "working_order_symbols": sorted(working_syms),
            "open_risk_pct": risk_pct,
'''
new_log = '''            "open_positions": sorted(open_syms),
            "working_order_symbols": sorted(working_syms),
            "occupied_symbols": sorted(occupied_syms),
            "open_risk_pct": risk_pct,
'''
if old_log in text:
    text = text.replace(old_log, new_log, 1)
    changes.append("occupied-risk account log")
elif '"working_order_symbols": sorted(working_syms)' not in text:
    old_log2 = '''            "open_positions": sorted(open_syms),
            "open_risk_pct": risk_pct,
'''
    new_log2 = '''            "open_positions": sorted(open_syms),
            "working_order_symbols": sorted(working_syms),
            "occupied_symbols": sorted(occupied_syms),
            "open_risk_pct": risk_pct,
'''
    if old_log2 in text:
        text = text.replace(old_log2, new_log2, 1)
        changes.append("account working-order log")

old_loop = '''            contract = make_asx_contract(sig["symbol"])
            ib.qualifyContracts(contract)
'''
new_loop = '''            if sig["symbol"] in working_syms:
                state["seen_signals"][key] = {
                    "status": "WORKING_ORDER_EXISTS",
                    "timestamp": now_iso(),
                }
                log_event("SKIP_WORKING_ORDER", {
                    "signal": key,
                    "symbol": sig["symbol"],
                    "reason": "working IBKR order already exists for symbol",
                })
                continue

            contract = make_asx_contract(sig["symbol"])
            ib.qualifyContracts(contract)
'''
if old_loop in text:
    text = text.replace(old_loop, new_loop, 1)
    changes.append("duplicate-order guard")
elif '"SKIP_WORKING_ORDER"' not in text:
    raise SystemExit("Duplicate-order guard patch target not found; refusing partial patch.")

old_after_submit = '''            open_syms.add(sig["symbol"])
            risk_pct += CONFIG["risk_per_trade_pct"]
'''
new_after_submit = '''            open_syms.add(sig["symbol"])
            working_syms.add(sig["symbol"])
            occupied_syms.add(sig["symbol"])
            risk_pct = len(occupied_syms) * CONFIG["risk_per_trade_pct"]
'''
if old_after_submit in text:
    text = text.replace(old_after_submit, new_after_submit, 1)
    changes.append("same-run duplicate/risk guard")
elif 'working_syms.add(sig["symbol"])' in text and 'occupied_syms.add(sig["symbol"])' not in text:
    old = '''            open_syms.add(sig["symbol"])
            working_syms.add(sig["symbol"])
            risk_pct += CONFIG["risk_per_trade_pct"]
'''
    new = '''            open_syms.add(sig["symbol"])
            working_syms.add(sig["symbol"])
            occupied_syms.add(sig["symbol"])
            risk_pct = len(occupied_syms) * CONFIG["risk_per_trade_pct"]
'''
    if old not in text:
        raise SystemExit("Same-run occupied-risk patch target not found.")
    text = text.replace(old, new, 1)
    changes.append("same-run occupied-risk guard")

path.write_text(text, encoding="utf-8")
if changes:
    print("engine.py patched successfully: " + ", ".join(changes))
else:
    print("engine.py already contains all safety patches")
