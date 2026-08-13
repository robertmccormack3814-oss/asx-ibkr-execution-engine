from pathlib import Path

path = Path(__file__).with_name("engine.py")
text = path.read_text(encoding="utf-8")
changes = []

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
    print("engine.py already contains no-market-price safeguard")
