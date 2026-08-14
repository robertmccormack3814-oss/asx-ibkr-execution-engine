from pathlib import Path

path = Path(__file__).with_name("engine.py")
text = path.read_text(encoding="utf-8")
changes = []

# Add a cached external ASX price loader for paper testing only.
if "def fetch_external_asx_prices(" not in text:
    marker = '''def fetch_signals():
    r = requests.get(CONFIG["source_scanner_url"], timeout=30)
    r.raise_for_status()
    return r.json()
'''
    helper = marker + '''\n\n_EXTERNAL_PRICE_CACHE = None\n\ndef fetch_external_asx_prices():\n    global _EXTERNAL_PRICE_CACHE\n    if _EXTERNAL_PRICE_CACHE is not None:\n        return _EXTERNAL_PRICE_CACHE\n\n    cfg = CONFIG.get("external_price_feed", {})\n    if not cfg.get("enabled"):\n        return {}\n    if cfg.get("paper_only", True) and not CONFIG.get("paper_only"):\n        return {}\n\n    url = str(cfg.get("url") or "").strip()\n    if not url:\n        return {}\n\n    timeout = int(cfg.get("timeout_seconds", 30))\n    r = requests.get(url, timeout=timeout)\n    r.raise_for_status()\n    payload = r.json()\n    if isinstance(payload, str):\n        payload = json.loads(payload)\n    if not isinstance(payload, list):\n        raise RuntimeError("External ASX price feed returned unexpected payload")\n\n    prices = {}\n    for row in payload:\n        if not isinstance(row, dict):\n            continue\n        symbol = str(row.get("Code") or "").strip().upper()\n        try:\n            price = float(row.get("Price") or 0)\n        except (TypeError, ValueError):\n            price = 0.0\n        if symbol and price > 0 and math.isfinite(price):\n            prices[symbol] = {\n                "price": price,\n                "status": row.get("Status"),\n            }\n\n    _EXTERNAL_PRICE_CACHE = prices\n    return prices\n\ndef external_asx_price(symbol):\n    row = fetch_external_asx_prices().get(str(symbol).upper())\n    if not row:\n        return None, None\n    return float(row["price"]), row.get("status")\n'''
    if marker not in text:
        raise SystemExit("fetch_signals patch target not found; refusing partial patch.")
    text = text.replace(marker, helper, 1)
    changes.append("external ASX price helper")

# Replace the strict no-market-price skip with IBKR-first, paper-only external fallback.
old = '''            market_price = ticker.marketPrice()
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
new = '''            market_price = ticker.marketPrice()
            price_source = "IBKR"
            external_status = None
            ib.cancelMktData(contract)

            if not market_price or math.isnan(market_price) or market_price <= 0:
                market_price, external_status = external_asx_price(sig.get("symbol"))
                if market_price:
                    price_source = "ASX_EXTERNAL_PAPER"

            # Never fall back to the scanner's signal price. If neither IBKR
            # nor the paper-only external feed has a usable current price,
            # skip safely.
            if not market_price or not math.isfinite(float(market_price)) or float(market_price) <= 0:
                log_event("SKIP_NO_MARKET_PRICE", {
                    "signal": key,
                    "symbol": sig.get("symbol"),
                    "reason": "no usable IBKR or external ASX market price",
                })
                continue

            ok, result = validate_signal(sig, market_price, equity, open_syms, risk_pct)
'''
if old in text:
    text = text.replace(old, new, 1)
    changes.append("paper-only external price fallback")
elif '"ASX_EXTERNAL_PAPER"' not in text:
    raise SystemExit("External-price fallback target not found; refusing partial patch.")

# Include external feed status in logs when present, without changing core validation.
old_dry = '''                    "price_source": price_source,
                    "entry": sig["entry_price"],
'''
new_dry = '''                    "price_source": price_source,
                    "external_market_status": external_status,
                    "entry": sig["entry_price"],
'''
if old_dry in text and '"external_market_status": external_status' not in text:
    text = text.replace(old_dry, new_dry, 1)
    changes.append("external price logging")

path.write_text(text, encoding="utf-8")
if changes:
    print("engine.py patched successfully: " + ", ".join(changes))
else:
    print("engine.py already contains paper-only external ASX price fallback")
