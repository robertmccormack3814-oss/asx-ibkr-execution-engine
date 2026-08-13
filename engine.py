import json, math, pathlib, requests
from datetime import datetime, timezone

from ib_insync import IB, Stock, LimitOrder, StopOrder

ROOT = pathlib.Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / "config.json").read_text())
STATE_PATH = ROOT / "state.json"
LOG_PATH = ROOT / "execution_log.jsonl"


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"seen_signals": {}, "orders": {}, "updated_at": None}


def save_state(state):
    state["updated_at"] = now_iso()
    STATE_PATH.write_text(json.dumps(state, indent=2))


def log_event(kind, payload):
    record = {"timestamp": now_iso(), "type": kind, **payload}
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
    print(json.dumps(record, indent=2))


def fetch_signals():
    r = requests.get(CONFIG["source_scanner_url"], timeout=30)
    r.raise_for_status()
    return r.json()


def position_size(equity, entry, stop):
    risk_budget = equity * CONFIG["risk_per_trade_pct"] / 100.0
    risk_per_share = abs(entry - stop)
    if risk_per_share <= 0:
        return 0
    units = math.floor(risk_budget / risk_per_share)
    max_value = equity * CONFIG["max_order_value_pct_of_equity"] / 100.0
    units = min(units, math.floor(max_value / entry))
    if units * entry < CONFIG["min_order_value_aud"]:
        return 0
    return max(units, 0)


def signal_key(sig):
    return f"{sig.get('symbol')}|{sig.get('signal_date')}|{sig.get('strategy')}|{sig.get('entry_price')}"


def current_equity(ib):
    vals = ib.accountSummary()
    for v in vals:
        if v.tag == "NetLiquidation" and v.currency == CONFIG["currency"]:
            return float(v.value)
    for v in vals:
        if v.tag == "NetLiquidation":
            return float(v.value)
    raise RuntimeError("Could not determine account equity")


def sizing_equity(actual_equity):
    override = CONFIG.get("test_equity_override_aud")
    if CONFIG.get("paper_only") and override is not None:
        override = float(override)
        if override <= 0:
            raise RuntimeError("test_equity_override_aud must be greater than zero")
        return override
    return actual_equity


def open_symbols(ib):
    return {p.contract.symbol for p in ib.positions() if p.position != 0}


def open_risk_pct(ib, equity):
    # Conservative v1 approximation: each live position consumes one configured risk budget.
    return len(open_symbols(ib)) * CONFIG["risk_per_trade_pct"]


def validate_signal(sig, market_price, equity, open_syms, total_open_risk_pct):
    symbol = sig.get("symbol")
    entry = float(sig.get("entry_price") or 0)
    stop = float(sig.get("stop_loss") or 0)
    target = float(sig.get("profit_target") or 0)
    if not symbol or not entry or not stop or not target:
        return False, "missing trade plan"
    if symbol in open_syms:
        return False, "position already open"
    if len(open_syms) >= CONFIG["max_open_positions"]:
        return False, "max open positions reached"
    if total_open_risk_pct + CONFIG["risk_per_trade_pct"] > CONFIG["max_total_open_risk_pct"]:
        return False, "portfolio risk ceiling reached"
    drift = abs(market_price - entry) / entry * 100
    if drift > CONFIG["max_entry_drift_pct"]:
        return False, f"entry drift {drift:.2f}% exceeds limit"
    qty = position_size(equity, entry, stop)
    if qty <= 0:
        return False, "position size below minimum"
    return True, qty


def place_bracket(ib, sig, qty):
    symbol = sig["symbol"]
    entry = round(float(sig["entry_price"]), 3)
    target = round(float(sig["profit_target"]), 3)
    stop = round(float(sig["stop_loss"]), 3)
    contract = Stock(symbol, CONFIG["exchange"], CONFIG["currency"])
    ib.qualifyContracts(contract)

    parent_id = ib.client.getReqId()
    take_id = ib.client.getReqId()
    stop_id = ib.client.getReqId()

    parent = LimitOrder("BUY", qty, entry, orderId=parent_id, transmit=False, tif=CONFIG["time_in_force"])
    take = LimitOrder("SELL", qty, target, orderId=take_id, parentId=parent_id, transmit=False, tif="GTC")
    protective = StopOrder("SELL", qty, stop, orderId=stop_id, parentId=parent_id, transmit=True, tif="GTC")

    trades = [ib.placeOrder(contract, parent), ib.placeOrder(contract, take), ib.placeOrder(contract, protective)]
    return trades


def main():
    if CONFIG["kill_switch"]:
        raise SystemExit("Kill switch is ON")
    if not CONFIG["paper_only"]:
        raise SystemExit("paper_only must remain true in v1")

    scanner = fetch_signals()
    active = scanner.get("active_trades", [])
    state = load_state()

    ib = IB()
    # In dry-run mode, connect explicitly read-only so TWS never receives order-capable requests.
    ib.connect(
        CONFIG["ibkr"]["host"],
        CONFIG["ibkr"]["port"],
        clientId=CONFIG["ibkr"]["client_id"],
        timeout=10,
        readonly=bool(CONFIG["dry_run"]),
    )
    try:
        # Prefer live data when entitled; otherwise IBKR will return delayed data when available.
        ib.reqMarketDataType(3)
        actual_equity = current_equity(ib)
        equity = sizing_equity(actual_equity)
        open_syms = open_symbols(ib)
        risk_pct = open_risk_pct(ib, equity)
        log_event("ACCOUNT", {
            "ibkr_equity": actual_equity,
            "sizing_equity": equity,
            "test_equity_override": equity != actual_equity,
            "open_positions": sorted(open_syms),
            "open_risk_pct": risk_pct,
            "risk_per_trade_pct": CONFIG["risk_per_trade_pct"],
            "risk_budget_aud": equity * CONFIG["risk_per_trade_pct"] / 100.0,
            "dry_run": CONFIG["dry_run"],
        })

        for sig in active:
            key = signal_key(sig)
            if key in state["seen_signals"] and state["seen_signals"][key].get("status") in {"SUBMITTED", "FILLED"}:
                continue

            contract = Stock(sig["symbol"], CONFIG["exchange"], CONFIG["currency"])
            ib.qualifyContracts(contract)
            ticker = ib.reqMktData(contract, "", False, False)
            ib.sleep(2)
            market_price = ticker.marketPrice()
            price_source = "IBKR"
            if not market_price or math.isnan(market_price):
                market_price = float(sig["entry_price"])
                price_source = "SIGNAL_FALLBACK"
            ib.cancelMktData(contract)

            ok, result = validate_signal(sig, market_price, equity, open_syms, risk_pct)
            if not ok:
                if not CONFIG["dry_run"]:
                    state["seen_signals"][key] = {"status": "SKIPPED", "reason": result, "timestamp": now_iso()}
                log_event("SKIP", {"signal": key, "symbol": sig.get("symbol"), "reason": result, "market_price": market_price, "price_source": price_source})
                continue

            qty = result
            if CONFIG["dry_run"]:
                # Dry runs are repeatable and deliberately do not consume the signal in state.json.
                log_event("DRY_RUN", {
                    "signal": key,
                    "symbol": sig["symbol"],
                    "qty": qty,
                    "market_price": market_price,
                    "price_source": price_source,
                    "entry": sig["entry_price"],
                    "stop": sig["stop_loss"],
                    "target": sig["profit_target"],
                    "planned_risk_aud": qty * abs(float(sig["entry_price"]) - float(sig["stop_loss"])),
                    "position_value_aud": qty * float(sig["entry_price"]),
                    "sizing_equity_aud": equity,
                })
                continue

            trades = place_bracket(ib, sig, qty)
            state["seen_signals"][key] = {"status": "SUBMITTED", "qty": qty, "order_ids": [t.order.orderId for t in trades], "timestamp": now_iso()}
            log_event("SUBMIT", {"signal": key, "symbol": sig["symbol"], "qty": qty, "order_ids": [t.order.orderId for t in trades], "sizing_equity_aud": equity})
            open_syms.add(sig["symbol"])
            risk_pct += CONFIG["risk_per_trade_pct"]

        save_state(state)
    finally:
        ib.disconnect()


if __name__ == "__main__":
    main()
