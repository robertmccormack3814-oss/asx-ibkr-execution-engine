import json
import math
import urllib.request
from pathlib import Path
from ib_insync import IB

ROOT = Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / "config.json").read_text())


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "asx-rsi-ibkr-dry-run/1.1"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def num(v):
    try:
        x=float(v)
        return x if math.isfinite(x) else None
    except Exception:return None


def score_signal(sig):
    """Conservative ranking: reward useful RSI40 upside, oversold depth and SMA200 support.
    This ranks candidates; it does not predict or guarantee returns."""
    price=num(sig.get("price")) or num(sig.get("entry_price"))
    target=num(sig.get("rsi40_target_price"))
    move=num(sig.get("rsi40_move_pct"))
    if move is None and price and target:
        move=(target-price)/price*100
    rsi=num(sig.get("rsi10")) or num(sig.get("entry_rsi10"))
    sma=num(sig.get("sma200"))
    sma_buffer=((price-sma)/sma*100) if price and sma and sma>0 else None

    # Cap components so an extreme falling stock cannot dominate merely because
    # its theoretical rebound target is huge.
    move_component=max(0,min(move if move is not None else 0,12))/12*55
    oversold=max(0,min((30-rsi) if rsi is not None else 0,12))/12*25
    support=max(0,min(sma_buffer if sma_buffer is not None else 0,20))/20*20
    score=move_component+oversold+support
    return score,price,target,move,rsi,sma_buffer


def main():
    if not CONFIG.get("paper_only", True) or not CONFIG.get("dry_run", True):
        raise RuntimeError("Safety stop: rsi_dry_run.py requires paper_only=true and dry_run=true")
    if CONFIG.get("kill_switch", False):
        print("KILL SWITCH ON — no strategy actions evaluated."); return

    ibcfg=CONFIG["ibkr"]; ib=IB(); ib.connect(ibcfg["host"],int(ibcfg["port"]),clientId=int(ibcfg["client_id"]),readonly=True)
    try:
        accounts=ib.managedAccounts(); expected=ibcfg.get("account","").strip()
        if expected and expected not in accounts: raise RuntimeError(f"Safety stop: configured account {expected} not connected; connected={accounts}")
        scanner=fetch_json(CONFIG["source_scanner_url"]); active=scanner.get("active_signals",[]); completed=scanner.get("completed_trades",[])
        positions={p.contract.symbol.upper():p for p in ib.positions() if p.position!=0}; open_symbols=set()
        for trade in ib.openTrades():
            try:
                if trade.orderStatus.status not in {"Filled","Cancelled","Inactive"}:open_symbols.add(trade.contract.symbol.upper())
            except Exception:pass
        active_by_symbol={str(s.get("symbol","")).upper():s for s in active if s.get("symbol")}; latest_completed={}
        for t in completed:
            sym=str(t.get("symbol","")).upper()
            if sym:latest_completed[sym]=t

        trade_value=float(CONFIG.get("trade_value_aud",1000)); max_positions=int(CONFIG.get("max_open_positions",5)); free_slots=max(0,max_positions-len(positions)-len(open_symbols))
        ranked=[]
        for sym,sig in active_by_symbol.items():
            if sym in positions or sym in open_symbols:continue
            score,price,target,move,rsi,sma_buffer=score_signal(sig)
            if not price or price<=0:continue
            qty=max(0,int(trade_value//price)); value=qty*price
            if qty<1:continue
            ranked.append({"sym":sym,"sig":sig,"score":score,"price":price,"target":target,"move":move,"rsi":rsi,"sma_buffer":sma_buffer,"qty":qty,"value":value})
        ranked.sort(key=lambda x:(x["score"],x["move"] if x["move"] is not None else -999),reverse=True)
        selected=ranked[:free_slots]

        print("RSI -> IBKR DRY RUN — RANKED SELECTOR")
        print("CONNECTED:",ib.isConnected()); print("ACCOUNT:",expected or accounts); print("POSITIONS:",sorted(positions)); print("OPEN ORDER SYMBOLS:",sorted(open_symbols)); print("SCANNER ACTIVE SIGNALS:",len(active_by_symbol)); print(f"A${trade_value:,.0f} PER TRADE | MAX POSITIONS {max_positions} | FREE SLOTS {free_slots}"); print("--- TOP RANKED CANDIDATES ---")
        for i,x in enumerate(ranked[:15],1):
            chosen="SELECT" if x in selected else "RESERVE"
            mv=f"{x['move']:+.2f}%" if x['move'] is not None else "—"; tgt=f"A${x['target']:.3f}" if x['target'] is not None else "—"; rv=f"{x['rsi']:.2f}" if x['rsi'] is not None else "—"; sb=f"{x['sma_buffer']:+.1f}%" if x['sma_buffer'] is not None else "—"
            print(f"#{i:02d} {chosen:7} {x['sym']:6} score {x['score']:5.1f} | RSI {rv} | est move->40 {mv} | target {tgt} | SMA200 buffer {sb} | {x['qty']} shares ~A${x['value']:.2f}")
        print("--- PROPOSED PAPER PORTFOLIO ---")
        if not selected:print("No new positions selected.")
        for x in selected:print(f"WOULD BUY {x['sym']}: {x['qty']} shares @ ~A${x['price']:.3f} = ~A${x['value']:.2f} | score {x['score']:.1f}")
        for sym,pos in sorted(positions.items()):
            if sym in active_by_symbol:print(f"HOLD {sym}: already held; no duplicate buy");continue
            trade=latest_completed.get(sym)
            if trade and str(trade.get("exit_reason","")).strip():print(f"WOULD SELL {sym}: scanner resolved | {trade.get('exit_reason')} | scanner exit {trade.get('exit_price')}")
            else:print(f"UNMANAGED POSITION {sym}: no active RSI signal; NO ACTION")
        print("---"); print(f"Eligible ranked candidates: {len(ranked)} | Selected now: {len(selected)}"); print("NO ORDERS SUBMITTED. Ranking is experimental and must be validated in paper trading.")
    finally:ib.disconnect()

if __name__=="__main__":main()
