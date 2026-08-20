import json
import urllib.request
from pathlib import Path
from ib_insync import IB

ROOT = Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / "config.json").read_text())


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "asx-rsi-ibkr-dry-run/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


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

        scanner = fetch_json(CONFIG["source_scanner_url"])
        active = scanner.get("active_signals", [])
        completed = scanner.get("completed_trades", [])

        positions = {p.contract.symbol.upper(): p for p in ib.positions() if p.position != 0}
        open_symbols = set()
        for trade in ib.openTrades():
            try:
                if trade.orderStatus.status not in {"Filled", "Cancelled", "Inactive"}:
                    open_symbols.add(trade.contract.symbol.upper())
            except Exception:
                pass

        active_by_symbol = {str(s.get("symbol", "")).upper(): s for s in active if s.get("symbol")}
        latest_completed = {}
        for t in completed:
            sym = str(t.get("symbol", "")).upper()
            if sym:
                latest_completed[sym] = t

        print("RSI -> IBKR DRY RUN")
        print("CONNECTED:", ib.isConnected())
        print("ACCOUNT:", expected or accounts)
        print("POSITIONS:", sorted(positions))
        print("OPEN ORDER SYMBOLS:", sorted(open_symbols))
        print("SCANNER ACTIVE SIGNALS:", len(active_by_symbol))
        print("---")

        candidates = []
        for sym, sig in sorted(active_by_symbol.items()):
            if sym in positions:
                print(f"HOLD {sym}: already held in IBKR; no duplicate buy")
                continue
            if sym in open_symbols:
                print(f"WAIT {sym}: IBKR already has an open order")
                continue
            candidates.append((sym, sig))
            print(f"WOULD BUY {sym}: scanner active | entry {sig.get('entry_price')} | RSI {sig.get('entry_rsi10')}")

        for sym, pos in sorted(positions.items()):
            if sym in active_by_symbol:
                continue
            trade = latest_completed.get(sym)
            if trade and str(trade.get("exit_reason", "")).strip():
                print(f"WOULD SELL {sym}: scanner resolved | {trade.get('exit_reason')} | scanner exit {trade.get('exit_price')}")
            else:
                print(f"UNMANAGED POSITION {sym}: held at IBKR but no active RSI signal; NO ACTION")

        print("---")
        print(f"Dry-run candidates: {len(candidates)}")
        print("NO ORDERS SUBMITTED. TWS/API remains read-only.")
    finally:
        ib.disconnect()


if __name__ == "__main__":
    main()
