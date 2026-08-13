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

# 2) Discover working IBKR orders independently of local state.json. This
# protects against duplicate brackets after a restart or state reset.
old_helper = '''def open_symbols(ib):
    return {p.contract.symbol for p in ib.positions() if p.position != 0}


def open_risk_pct(ib, equity):
'''
new_helper = '''def open_symbols(ib):
    return {p.contract.symbol for p in ib.positions() if p.position != 0}


def working_order_symbols(ib):
    # Ask TWS for all currently open orders so duplicate protection survives
    # engine restarts and local state.json resets.
    trades = ib.reqAllOpenOrders()
    working_statuses = {"PendingSubmit", "PreSubmitted", "Submitted", "PendingCancel"}
    return {
        trade.contract.symbol
        for trade in trades
        if trade.contract.symbol and trade.orderStatus.status in working_statuses
    }


def open_risk_pct(ib, equity):
'''
if old_helper in text:
    text = text.replace(old_helper, new_helper, 1)
    changes.append("working-order helper")
elif "def working_order_symbols(ib):" not in text:
    raise SystemExit("Working-order helper patch target not found; refusing partial patch.")

old_init = '''        equity = sizing_equity(actual_equity)
        open_syms = open_symbols(ib)
        risk_pct = open_risk_pct(ib, equity)
'''
new_init = '''        equity = sizing_equity(actual_equity)
        open_syms = open_symbols(ib)
        working_syms = working_order_symbols(ib)
        risk_pct = open_risk_pct(ib, equity)
'''
if old_init in text:
    text = text.replace(old_init, new_init, 1)
    changes.append("working-order scan")
elif "working_syms = working_order_symbols(ib)" not in text:
    raise SystemExit("Working-order scan patch target not found; refusing partial patch.")

old_log = '''            "open_positions": sorted(open_syms),
            "open_risk_pct": risk_pct,
'''
new_log = '''            "open_positions": sorted(open_syms),
            "working_order_symbols": sorted(working_syms),
            "open_risk_pct": risk_pct,
'''
if old_log in text:
    text = text.replace(old_log, new_log, 1)
    changes.append("account working-order log")
elif '"working_order_symbols": sorted(working_syms)' not in text:
    raise SystemExit("Account log patch target not found; refusing partial patch.")

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
            risk_pct += CONFIG["risk_per_trade_pct"]
'''
if old_after_submit in text:
    text = text.replace(old_after_submit, new_after_submit, 1)
    changes.append("same-run duplicate guard")
elif 'working_syms.add(sig["symbol"])' not in text:
    raise SystemExit("Same-run guard patch target not found; refusing partial patch.")

path.write_text(text, encoding="utf-8")
if changes:
    print("engine.py patched successfully: " + ", ".join(changes))
else:
    print("engine.py already contains all safety patches")
