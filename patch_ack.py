from pathlib import Path

path = Path(__file__).with_name("engine.py")
text = path.read_text(encoding="utf-8")
changes = []

# Ensure timezone/trading-day imports exist.
if "from zoneinfo import ZoneInfo" not in text:
    text = text.replace(
        "from datetime import datetime, timezone",
        "from datetime import datetime, timezone, timedelta\nfrom zoneinfo import ZoneInfo",
        1,
    )
    changes.append("ASX timezone imports")

# Add helper functions once.
if "def signal_age_trading_days(" not in text:
    marker = '''def signal_key(sig):
    return f"{sig.get('symbol')}|{sig.get('signal_date')}|{sig.get('strategy')}|{sig.get('entry_price')}"
'''
    helper = marker + '''\n\ndef signal_age_trading_days(sig):
    """Weekday-based signal age in the ASX timezone.

    v1 intentionally counts Monday-Friday only. ASX public-holiday awareness can
    be added later if testing shows it matters for entry expiry.
    """
    raw = str(sig.get("signal_date") or "").strip()
    if not raw:
        return None
    try:
        signal_date = datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return None
    today = datetime.now(ZoneInfo("Australia/Sydney")).date()
    if signal_date > today:
        return -1
    return sum(
        1
        for i in range(1, (today - signal_date).days + 1)
        if (signal_date + timedelta(days=i)).weekday() < 5
    )
'''
    if marker not in text:
        raise SystemExit("signal_key patch target not found; refusing partial patch.")
    text = text.replace(marker, helper, 1)
    changes.append("signal-age helper")

# Insert stale check after existing working-order protection when present, or
# immediately after signal key/state checks otherwise. Existing working orders
# are deliberately protected first: this guard prevents NEW stale entries; it
# does not cancel AVH/BVS or any other already-working bracket.
if '"SKIP_STALE_SIGNAL"' not in text:
    stale_block = '''            age_days = signal_age_trading_days(sig)
            max_age = int(CONFIG.get("max_signal_age_trading_days", 2))
            if age_days is None:
                log_event("SKIP_STALE_SIGNAL", {
                    "signal": key,
                    "symbol": sig.get("symbol"),
                    "reason": "missing or invalid signal_date",
                })
                continue
            if age_days < 0:
                log_event("SKIP_STALE_SIGNAL", {
                    "signal": key,
                    "symbol": sig.get("symbol"),
                    "reason": "signal_date is in the future",
                    "signal_age_trading_days": age_days,
                })
                continue
            if age_days > max_age:
                log_event("SKIP_STALE_SIGNAL", {
                    "signal": key,
                    "symbol": sig.get("symbol"),
                    "reason": f"signal age {age_days} trading days exceeds limit {max_age}",
                    "signal_age_trading_days": age_days,
                    "max_signal_age_trading_days": max_age,
                })
                continue

'''
    # Local engine has duplicate-order guard from prior patch.
    working_anchor = '''            if sig.get("symbol") in working_order_symbols:
                log_event("SKIP_WORKING_ORDER", {
                    "signal": key,
                    "symbol": sig.get("symbol"),
                    "reason": "working IBKR order already exists for symbol"
                })
                continue
'''
    if working_anchor in text:
        text = text.replace(working_anchor, working_anchor + "\n" + stale_block, 1)
    else:
        state_anchor = '''            if key in state["seen_signals"] and state["seen_signals"][key].get("status") in {"SUBMITTED", "FILLED"}:
                continue
'''
        if state_anchor not in text:
            raise SystemExit("stale-signal insertion target not found; refusing partial patch.")
        text = text.replace(state_anchor, state_anchor + "\n" + stale_block, 1)
    changes.append("2-trading-day stale-signal guard")

# Preserve the no-market-price safeguard from the previous patch.
old = '''            market_price = ticker.marketPrice()
            price_source = "IBKR"
            if not market_price or math.isnan(market_price):
                market_price = float(sig["entry_price"])
                price_source = "SIGNAL_FALLBACK"
            ib.cancelMktData(contract)

            ok, result = validate_signal(sig, market_price, equity, open_syms, risk_pct)
'''
new = '''            market_price = ticker.marketPrice()
            price_source = "IBKR"
            ib.cancelMktData(contract)

            # Never use the signal price as a substitute for current market
            # data. Doing so can make entry drift appear to be zero and allow
            # a stale trade through. If IBKR cannot provide a usable current
            # price, skip this signal and try again on a later run.
            if not market_price or math.isnan(market_price):
                log_event("SKIP_NO_MARKET_PRICE", {
                    "signal": key,
                    "symbol": sig.get("symbol"),
                    "reason": "no usable current IBKR market price",
                })
                continue

            ok, result = validate_signal(sig, market_price, equity, open_syms, risk_pct)
'''
if old in text:
    text = text.replace(old, new, 1)
    changes.append("no-market-price guard")
elif '"SKIP_NO_MARKET_PRICE"' not in text:
    raise SystemExit("Market-price safeguard patch target not found; refusing partial patch.")

path.write_text(text, encoding="utf-8")
if changes:
    print("engine.py patched successfully: " + ", ".join(changes))
else:
    print("engine.py already contains stale-signal and no-market-price safeguards")
