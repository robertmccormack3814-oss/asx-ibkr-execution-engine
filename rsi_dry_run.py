import json
import math
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from ib_insync import IB

ROOT = Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / "config.json").read_text())
SYDNEY = ZoneInfo("Australia/Sydney")


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "asx-rsi-ibkr-dry-run/1.3"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def num(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except Exception:
        return None


def enriched_signal(sig, stock=None, live=None):
    out = dict(stock or {})
    out.update(sig or {})
    live = live or {}
    if live.get("price") is not None:
        out["price"] = live["price"]
        out["latest_price"] = live["price"]
    if live.get("provisional_rsi10") is not None:
        out["rsi10"] = live["provisional_rsi10"]
        out["latest_rsi10"] = live["provisional_rsi10"]
    if live.get("rsi40_target_price") is not None:
        out["rsi40_target_price"] = live["rsi40_target_price"]
    if live.get("rsi40_move_pct") is not None:
        out["rsi40_move_pct"] = live["rsi40_move_pct"]
    return out


def signal_freshness(sig):
    """Only allow new signals generated today; never backfill historical active signals.
    If an exact observed timestamp exists, also require it to be recent enough for entry."""
    today = datetime.now(SYDNEY).date().isoformat()
    if str(sig.get("entry_date", "")) != today:
        return False, "historical active signal — no backfill"

    observed = sig.get("entry_observed_at")
    if observed:
        try:
            dt = datetime.fromisoformat(str(observed).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            age_minutes = (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds() / 60
            max_age = float(CONFIG.get("max_entry_signal_age_minutes", 20))
            if age_minutes > max_age:
                return False, f"entry signal {age_minutes:.0f} min old"
        except Exception:
            return False, "invalid entry timestamp"
    return True, "fresh"


def entry_eligibility(sig, market_on):
    if not market_on:
        return False, "ASX 200 below SMA200"

    fresh, reason = signal_freshness(sig)
    if not fresh:
        return False, reason

    price = num(sig.get("latest_price")) or num(sig.get("price"))
    rsi = num(sig.get("latest_rsi10"))
    if rsi is None:
        rsi = num(sig.get("rsi10"))
    sma = num(sig.get("sma200"))
    if sma is None:
        sma = num(sig.get("entry_sma200"))

    if price is None or price <= 0:
        return False, "no valid current price"
    if rsi is None:
        return False, "no current RSI"
    if sma is None or sma <= 0:
        return False, "no SMA200 data"
    if rsi >= 30:
        return False, f"current RSI {rsi:.2f} is not below 30"
    if price <= sma:
        return False, f"current price is below SMA200 ({(price-sma)/sma*100:+.1f}%)"

    target = num(sig.get("rsi40_target_price"))
    move = num(sig.get("rsi40_move_pct"))
    if target is None or move is None:
        return False, "RSI40 target not ready"
    if target <= price or move <= 0:
        return False, "RSI40 target is not above current price"

    # Avoid nonsensical paper orders in extremely low-priced names until we add
    # a proper dollar-volume/spread filter from market data.
    min_price = float(CONFIG.get("min_entry_price_aud", 0.10))
    if price < min_price:
        return False, f"price below temporary liquidity floor A${min_price:.2f}"

    return True, "eligible"


def score_signal(sig):
    price = num(sig.get("latest_price")) or num(sig.get("price")) or num(sig.get("entry_price"))
    target = num(sig.get("rsi40_target_price"))
    move = num(sig.get("rsi40_move_pct"))
    if move is None and price and target:
        move = (target - price) / price * 100
    rsi = num(sig.get("latest_rsi10"))
    if rsi is None:
        rsi = num(sig.get("rsi10"))
    sma = num(sig.get("sma200"))
    if sma is None:
        sma = num(sig.get("entry_sma200"))
    sma_buffer = ((price - sma) / sma * 100) if price and sma and sma > 0 else None

    move_component = max(0, min(move if move is not None else 0, 12)) / 12 * 55
    oversold = max(0, min((30 - rsi) if rsi is not None else 0, 12)) / 12 * 20
    support = max(0, min(sma_buffer if sma_buffer is not None else 0, 20)) / 20 * 25
    score = move_component + oversold + support
    return score, price, target, move, rsi, sma_buffer


def main():
    if not CONFIG.get("paper_only", True) or not CONFIG.get("dry_run", True):
        raise RuntimeError("Safety stop: rsi_dry_run.py requires paper_only=true and dry_run=true")
    if CONFIG.get("kill_switch", False):
        print("KILL SWITCH ON — no strategy actions evaluated.")
        return

    ibcfg = CONFIG["ibkr"]
    ib = IB()
    ib.connect(ibcfg["host"], int(ibcfg["port"]), clientId=int(ibcfg["client_id"]), readonly=True)
    try:
        accounts = ib.managedAccounts()
        expected = ibcfg.get("account", "").strip()
        if expected and expected not in accounts:
            raise RuntimeError(f"Safety stop: configured account {expected} not connected; connected={accounts}")

        scanner_url = CONFIG["source_scanner_url"]
        scanner = fetch_json(scanner_url)
        intraday_url = scanner_url.rsplit("/", 1)[0] + "/intraday_state.json"
        try:
            intraday = fetch_json(intraday_url)
        except Exception:
            intraday = {"latest": {}}

        active = scanner.get("active_signals", [])
        completed = scanner.get("completed_trades", [])
        stock_map = {str(x.get("symbol", "")).upper(): x for x in scanner.get("stocks", []) if x.get("symbol")}
        live_map = intraday.get("latest", {}) or {}
        market_on = bool((scanner.get("market") or {}).get("above_sma200", False))

        positions = {p.contract.symbol.upper(): p for p in ib.positions() if p.position != 0}
        open_symbols = set()
        for trade in ib.openTrades():
            try:
                if trade.orderStatus.status not in {"Filled", "Cancelled", "Inactive"}:
                    open_symbols.add(trade.contract.symbol.upper())
            except Exception:
                pass

        active_by_symbol = {}
        for s in active:
            sym = str(s.get("symbol", "")).upper()
            if not sym:
                continue
            key = f"{sym}|{s.get('entry_date', '')}"
            active_by_symbol[sym] = enriched_signal(s, stock_map.get(sym), live_map.get(key))

        latest_completed = {}
        for t in completed:
            sym = str(t.get("symbol", "")).upper()
            if sym:
                latest_completed[sym] = t

        trade_value = float(CONFIG.get("trade_value_aud", 1000))
        max_positions = int(CONFIG.get("max_open_positions", 5))
        free_slots = max(0, max_positions - len(positions) - len(open_symbols))

        ranked = []
        rejected = []
        for sym, sig in active_by_symbol.items():
            if sym in positions or sym in open_symbols:
                continue
            eligible, reason = entry_eligibility(sig, market_on)
            if not eligible:
                rejected.append((sym, reason))
                continue
            score, price, target, move, rsi, sma_buffer = score_signal(sig)
            qty = max(0, int(trade_value // price)) if price else 0
            if qty < 1:
                rejected.append((sym, "A$1,000 position cannot buy one share"))
                continue
            value = qty * price
            ranked.append({"sym": sym, "sig": sig, "score": score, "price": price, "target": target, "move": move, "rsi": rsi, "sma_buffer": sma_buffer, "qty": qty, "value": value})

        ranked.sort(key=lambda x: (x["score"], x["move"]), reverse=True)
        selected = ranked[:free_slots]

        print("RSI -> IBKR DRY RUN — FRESH-SIGNAL SELECTOR")
        print("CONNECTED:", ib.isConnected())
        print("ACCOUNT:", expected or accounts)
        print("POSITIONS:", sorted(positions))
        print("OPEN ORDER SYMBOLS:", sorted(open_symbols))
        print("SCANNER ACTIVE SIGNALS:", len(active_by_symbol))
        print("ASX 200 ABOVE SMA200:", market_on)
        print(f"A${trade_value:,.0f} PER TRADE | MAX POSITIONS {max_positions} | FREE SLOTS {free_slots}")
        print(f"ENTRY-ELIGIBLE NOW: {len(ranked)} | REJECTED/STALE: {len(rejected)}")
        print("--- TOP ELIGIBLE CANDIDATES ---")
        if not ranked:
            print("No fresh signals currently satisfy every entry rule. Historical active signals are intentionally not backfilled.")
        for i, x in enumerate(ranked[:15], 1):
            chosen = "SELECT" if x in selected else "RESERVE"
            sb = f"{x['sma_buffer']:+.1f}%" if x["sma_buffer"] is not None else "—"
            print(f"#{i:02d} {chosen:7} {x['sym']:6} score {x['score']:5.1f} | RSI {x['rsi']:.2f} | est move->40 {x['move']:+.2f}% | target A${x['target']:.3f} | SMA200 buffer {sb} | {x['qty']} shares ~A${x['value']:.2f}")

        print("--- REJECTION SUMMARY (first 15) ---")
        for sym, reason in rejected[:15]:
            print(f"SKIP {sym}: {reason}")

        print("--- PROPOSED PAPER PORTFOLIO ---")
        if not selected:
            print("No new positions selected.")
        for x in selected:
            print(f"WOULD BUY {x['sym']}: {x['qty']} shares @ ~A${x['price']:.3f} = ~A${x['value']:.2f} | target {x['move']:+.2f}% | score {x['score']:.1f}")

        for sym, pos in sorted(positions.items()):
            if sym in active_by_symbol:
                print(f"HOLD {sym}: already held; no duplicate buy")
                continue
            trade = latest_completed.get(sym)
            if trade and str(trade.get("exit_reason", "")).strip():
                print(f"WOULD SELL {sym}: scanner resolved | {trade.get('exit_reason')} | scanner exit {trade.get('exit_price')}")
            else:
                print(f"UNMANAGED POSITION {sym}: no active RSI signal; NO ACTION")

        print("---")
        print(f"Eligible now: {len(ranked)} | Selected now: {len(selected)} | Rejected/stale: {len(rejected)}")
        print("NO ORDERS SUBMITTED. TWS/API remains read-only.")
    finally:
        ib.disconnect()


if __name__ == "__main__":
    main()
