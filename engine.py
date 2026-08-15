import json, math, pathlib, requests
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP

from ib_insync import IB, Stock, LimitOrder, StopOrder

ROOT = pathlib.Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / "config.json").read_text())
STATE_PATH = ROOT / "state.json"
LOG_PATH = ROOT / "execution_log.jsonl"
TRADE_RESULTS_PATH = ROOT / "trade_results.jsonl"
PERFORMANCE_PATH = ROOT / "performance_summary.json"


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


_EXTERNAL_PRICE_CACHE = None

def fetch_external_asx_prices():
    global _EXTERNAL_PRICE_CACHE
    if _EXTERNAL_PRICE_CACHE is not None:
        return _EXTERNAL_PRICE_CACHE

    cfg = CONFIG.get("external_price_feed", {})
    if not cfg.get("enabled"):
        return {}
    if cfg.get("paper_only", True) and not CONFIG.get("paper_only"):
        return {}

    url = str(cfg.get("url") or "").strip()
    if not url:
        return {}

    timeout = int(cfg.get("timeout_seconds", 30))
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    payload = r.json()
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, list):
        raise RuntimeError("External ASX price feed returned unexpected payload")

    prices = {}
    for row in payload:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("Code") or "").strip().upper()
        try:
            price = float(row.get("Price") or 0)
        except (TypeError, ValueError):
            price = 0.0
        if symbol and price > 0 and math.isfinite(price):
            prices[symbol] = {
                "price": price,
                "status": row.get("Status"),
            }

    _EXTERNAL_PRICE_CACHE = prices
    return prices

def external_asx_price(symbol):
    row = fetch_external_asx_prices().get(str(symbol).upper())
    if not row:
        return None, None
    return float(row["price"]), row.get("status")


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


def trading_days_between_dates(start_date, end_date):
    if start_date > end_date:
        return 0
    return sum(
        1 for i in range(1, (end_date - start_date).days + 1)
        if (start_date + timedelta(days=i)).weekday() < 5
    )


def earliest_local_position_date(symbol):
    """Earliest ASX-local date where our log recorded symbol as an open position."""
    symbol = str(symbol or "").strip().upper()
    if not symbol or not LOG_PATH.exists():
        return None
    earliest = None
    try:
        with LOG_PATH.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if row.get("type") != "RECONCILE":
                    continue
                positions = row.get("positions") or {}
                if symbol not in positions:
                    continue
                raw_ts = str(row.get("timestamp") or "")
                try:
                    dt = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
                    local_date = dt.astimezone(ZoneInfo("Australia/Sydney")).date()
                except Exception:
                    continue
                if earliest is None or local_date < earliest:
                    earliest = local_date
    except Exception:
        return None
    return earliest


def current_session_buy_fill(ib, symbol):
    """Return earliest visible BUY execution for symbol from the current IBKR session/day."""
    symbol = str(symbol or "").strip().upper()
    matches = []
    try:
        fills = list(ib.fills())
        if not fills:
            fills = list(ib.reqExecutions())
    except Exception:
        fills = []
    for fill in fills:
        try:
            if str(fill.contract.symbol or "").strip().upper() != symbol:
                continue
            side = str(fill.execution.side or "").upper()
            if side not in {"BOT", "BUY"}:
                continue
            when = fill.execution.time
            if when is None:
                continue
            matches.append(when)
        except Exception:
            continue
    if not matches:
        return None
    return min(matches)


def signal_age_trading_days(sig):
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


def assert_expected_paper_account(ib):
    accounts = ib.managedAccounts()
    expected = str(CONFIG.get("ibkr", {}).get("account") or "").strip()
    if not expected:
        raise RuntimeError("Refusing execution: ibkr.account is not configured")
    if not expected.startswith("DU"):
        raise RuntimeError(f"Refusing execution: configured account {expected} is not a DU paper account")
    if expected not in accounts:
        raise RuntimeError(f"Refusing execution: expected paper account {expected} not connected; connected={accounts}")
    if len(accounts) != 1:
        raise RuntimeError(f"Refusing execution: expected exactly one managed account; connected={accounts}")
    return expected


def open_symbols(ib):
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


def occupied_symbols(ib):
    return open_symbols(ib) | working_order_symbols(ib)


def open_risk_pct(ib, equity):
    return len(occupied_symbols(ib)) * CONFIG["risk_per_trade_pct"]


def make_asx_contract(symbol):
    return Stock(symbol, "SMART", CONFIG["currency"], primaryExchange="ASX")


def capture_visible_executions(ib, state):
    journal = state.setdefault("execution_fills", {})
    try:
        fills = list(ib.fills())
        if not fills:
            fills = list(ib.reqExecutions())
    except Exception as exc:
        log_event("EXECUTION_CAPTURE_ERROR", {"error": str(exc)})
        return 0

    added = 0
    for fill in fills:
        try:
            ex = fill.execution
            symbol = str(fill.contract.symbol or "").strip().upper()
            exec_id = str(getattr(ex, "execId", "") or "").strip()
            if not symbol or not exec_id or exec_id in journal:
                continue
            when = getattr(ex, "time", None)
            journal[exec_id] = {
                "symbol": symbol,
                "side": str(getattr(ex, "side", "") or "").upper(),
                "shares": float(getattr(ex, "shares", 0) or 0),
                "price": float(getattr(ex, "price", 0) or 0),
                "time": when.isoformat() if hasattr(when, "isoformat") else str(when or ""),
                "order_id": int(getattr(ex, "orderId", 0) or 0),
                "perm_id": int(getattr(ex, "permId", 0) or 0),
            }
            added += 1
        except Exception:
            continue
    if added:
        save_state(state)
        log_event("EXECUTIONS_CAPTURED", {"new_fills": added})
    return added


def _parse_execution_time(raw):
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _weighted_fill(fills):
    qty = sum(float(x.get("shares") or 0) for x in fills)
    if qty <= 0:
        return None, 0.0
    value = sum(float(x.get("shares") or 0) * float(x.get("price") or 0) for x in fills)
    return value / qty, qty


def append_trade_result(record):
    with TRADE_RESULTS_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def load_trade_results():
    rows = []
    if not TRADE_RESULTS_PATH.exists():
        return rows
    with TRADE_RESULTS_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                row = json.loads(line)
            except Exception:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def _metric_block(rows):
    n = len(rows)
    if n == 0:
        return {
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate_pct": None,
            "total_pnl_aud": 0.0,
            "average_pnl_aud": None,
            "average_winner_aud": None,
            "average_loser_aud": None,
            "expectancy_aud_per_trade": None,
            "average_r": None,
            "profit_factor": None,
            "max_drawdown_aud": 0.0,
        }

    pnls = [float(r.get("pnl_aud") or 0.0) for r in rows]
    winners = [x for x in pnls if x > 0]
    losers = [x for x in pnls if x < 0]
    r_values = [
        float(r.get("r_multiple")) for r in rows
        if r.get("r_multiple") is not None
    ]

    gross_profit = sum(winners)
    gross_loss_abs = abs(sum(losers))
    profit_factor = None
    if gross_loss_abs > 0:
        profit_factor = gross_profit / gross_loss_abs
    elif gross_profit > 0:
        profit_factor = None

    equity_curve = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for pnl in pnls:
        equity_curve += pnl
        if equity_curve > peak:
            peak = equity_curve
        drawdown = peak - equity_curve
        if drawdown > max_drawdown:
            max_drawdown = drawdown

    wins = len(winners)
    losses = len(losers)
    return {
        "trades": n,
        "wins": wins,
        "losses": losses,
        "win_rate_pct": wins / n * 100.0,
        "total_pnl_aud": sum(pnls),
        "average_pnl_aud": sum(pnls) / n,
        "average_winner_aud": (sum(winners) / len(winners)) if winners else None,
        "average_loser_aud": (sum(losers) / len(losers)) if losers else None,
        "expectancy_aud_per_trade": sum(pnls) / n,
        "average_r": (sum(r_values) / len(r_values)) if r_values else None,
        "profit_factor": profit_factor,
        "max_drawdown_aud": max_drawdown,
    }


def compute_performance_statistics():
    rows = load_trade_results()
    by_strategy = {}
    strategies = sorted({str(r.get("strategy") or "UNKNOWN").upper() for r in rows})
    for strategy in strategies:
        subset = [r for r in rows if str(r.get("strategy") or "UNKNOWN").upper() == strategy]
        by_strategy[strategy] = _metric_block(subset)

    summary = {
        "timestamp": now_iso(),
        "overall": _metric_block(rows),
        "by_strategy": by_strategy,
    }
    PERFORMANCE_PATH.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    log_event("PERFORMANCE_SUMMARY", summary)
    return summary


def finalize_closed_trades(state, active_signals):
    tracking = state.setdefault("position_tracking", {})
    journal = list((state.get("execution_fills") or {}).values())
    completed = state.setdefault("completed_trades", {})
    active_by_symbol = {
        str(s.get("symbol") or "").strip().upper(): s
        for s in active_signals if s.get("symbol")
    }
    written = 0

    for symbol, rec in list(tracking.items()):
        if rec.get("status") != "CLOSED_PENDING_LOG":
            continue
        trade_id = str(rec.get("trade_id") or f"{symbol}|{rec.get('holding_start_date') or rec.get('first_seen_date')}")
        if trade_id in completed:
            rec["status"] = "CLOSED_LOGGED"
            continue

        start_raw = rec.get("fill_timestamp")
        start_dt = _parse_execution_time(start_raw)
        if start_dt is None:
            try:
                d = datetime.strptime(
                    str(rec.get("holding_start_date") or rec.get("first_seen_date")), "%Y-%m-%d"
                ).date()
                start_dt = datetime(d.year, d.month, d.day, tzinfo=ZoneInfo("Australia/Sydney"))
            except Exception:
                start_dt = None

        sell_fills = []
        for x in journal:
            if str(x.get("symbol") or "").upper() != symbol:
                continue
            if str(x.get("side") or "").upper() not in {"SLD", "SELL"}:
                continue
            dt = _parse_execution_time(x.get("time"))
            if start_dt is not None and dt is not None and dt < start_dt:
                continue
            sell_fills.append(x)

        exit_price, exit_qty = _weighted_fill(sell_fills)
        if exit_price is None or exit_qty <= 0:
            log_event("TRADE_LOG_PENDING", {
                "symbol": symbol,
                "reason": "position closed but no captured SELL execution is available yet",
            })
            continue

        sig = active_by_symbol.get(symbol) or {}
        entry_price = float(rec.get("avg_cost") or sig.get("entry_price") or 0)
        qty = min(abs(float(rec.get("quantity") or exit_qty)), exit_qty)
        target = float(rec.get("target") or sig.get("profit_target") or 0)
        stop = float(rec.get("stop") or sig.get("stop_loss") or 0)
        strategy = str(rec.get("strategy") or sig.get("strategy") or "").upper()
        signal_date = rec.get("signal_date") or sig.get("signal_date")

        exit_times = [d for d in (_parse_execution_time(x.get("time")) for x in sell_fills) if d is not None]
        exit_dt = max(exit_times) if exit_times else None
        exit_date = None
        if exit_dt is not None:
            exit_date = str(exit_dt.astimezone(ZoneInfo("Australia/Sydney")).date())

        start_date_raw = rec.get("holding_start_date") or rec.get("first_seen_date")
        holding_days = None
        if start_date_raw and exit_date:
            try:
                holding_days = trading_days_between_dates(
                    datetime.strptime(str(start_date_raw), "%Y-%m-%d").date(),
                    datetime.strptime(exit_date, "%Y-%m-%d").date(),
                )
            except Exception:
                holding_days = None

        pnl = (exit_price - entry_price) * qty if entry_price > 0 else None
        return_pct = ((exit_price / entry_price) - 1.0) * 100.0 if entry_price > 0 else None
        risk_per_share = entry_price - stop if entry_price > 0 and stop > 0 and entry_price > stop else None
        initial_risk = risk_per_share * qty if risk_per_share else None
        r_multiple = pnl / initial_risk if pnl is not None and initial_risk and initial_risk > 0 else None

        if rec.get("time_exit_requested") or rec.get("time_exit_due"):
            exit_reason = "TIME_EXIT"
        elif target > 0 and exit_price >= target * 0.995:
            exit_reason = "TARGET"
        elif stop > 0 and exit_price <= stop * 1.005:
            exit_reason = "STOP"
        else:
            exit_reason = "OTHER"

        record = {
            "timestamp": now_iso(),
            "trade_id": trade_id,
            "symbol": symbol,
            "strategy": strategy,
            "signal_date": signal_date,
            "entry_date": start_date_raw,
            "entry_price": entry_price,
            "exit_date": exit_date,
            "exit_price": exit_price,
            "quantity": qty,
            "target": target or None,
            "stop": stop or None,
            "holding_trading_days": holding_days,
            "pnl_aud": pnl,
            "return_pct": return_pct,
            "r_multiple": r_multiple,
            "exit_reason": exit_reason,
        }
        append_trade_result(record)
        log_event("TRADE_COMPLETED", record)
        completed[trade_id] = record
        rec["status"] = "CLOSED_LOGGED"
        rec["closed_trade_id"] = trade_id
        written += 1

    if written:
        save_state(state)
    return written


def manage_filled_positions(ib, state, active_signals):
    """Track filled positions and identify strategy-specific time exits.

    The first run that observes a filled symbol records first_seen_date in the
    ASX timezone. Subsequent restarts reuse that persisted date. This avoids
    using signal_date as a substitute for fill date.
    """
    today = datetime.now(ZoneInfo("Australia/Sydney")).date()
    tracking = state.setdefault("position_tracking", {})
    active_by_symbol = {}
    for sig in active_signals:
        symbol = str(sig.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        current = active_by_symbol.get(symbol)
        if current is None or str(sig.get("signal_date") or "") > str(current.get("signal_date") or ""):
            active_by_symbol[symbol] = sig

    open_positions = {
        str(p.contract.symbol or "").strip().upper(): p
        for p in ib.positions()
        if p.position != 0 and p.contract.symbol
    }

    # Remove tracking only after a position is no longer open. Keep a closed
    # snapshot for later trade-result logging.
    for symbol in list(tracking):
        if symbol not in open_positions and tracking[symbol].get("status") == "OPEN":
            tracking[symbol]["status"] = "CLOSED_PENDING_LOG"
            tracking[symbol]["last_seen_open_date"] = str(today)

    results = []
    limits = CONFIG.get("max_holding_days_by_strategy", {})
    for symbol, position in open_positions.items():
        sig = active_by_symbol.get(symbol)
        strategy = str((sig or {}).get("strategy") or tracking.get(symbol, {}).get("strategy") or "").upper()
        signal_date = (sig or {}).get("signal_date") or tracking.get(symbol, {}).get("signal_date")

        rec = tracking.get(symbol)
        if not rec or rec.get("status") != "OPEN":
            fill_dt = current_session_buy_fill(ib, symbol)
            historical_date = earliest_local_position_date(symbol)
            if fill_dt is not None:
                if fill_dt.tzinfo is None:
                    fill_dt = fill_dt.replace(tzinfo=timezone.utc)
                start_date = fill_dt.astimezone(ZoneInfo("Australia/Sydney")).date()
                start_source = "IBKR_EXECUTION"
                fill_timestamp = fill_dt.isoformat()
            elif historical_date is not None:
                start_date = historical_date
                start_source = "LOCAL_RECONCILE_HISTORY"
                fill_timestamp = None
            else:
                start_date = today
                start_source = "FIRST_OBSERVED_FALLBACK"
                fill_timestamp = None

            rec = {
                "status": "OPEN",
                "first_seen_date": str(start_date),
                "holding_start_date": str(start_date),
                "holding_start_source": start_source,
                "fill_timestamp": fill_timestamp,
                "first_seen_timestamp": now_iso(),
                "quantity": float(position.position),
                "avg_cost": float(position.avgCost or 0),
                "strategy": strategy,
                "signal_date": signal_date,
                "target": float((sig or {}).get("profit_target") or 0),
                "stop": float((sig or {}).get("stop_loss") or 0),
                "planned_entry": float((sig or {}).get("entry_price") or 0),
                "trade_id": signal_key(sig) if sig else None,
            }
            tracking[symbol] = rec
        else:
            rec["quantity"] = float(position.position)
            rec["avg_cost"] = float(position.avgCost or 0)
            if strategy:
                rec["strategy"] = strategy
            if signal_date:
                rec["signal_date"] = signal_date
            if sig:
                rec["target"] = float(sig.get("profit_target") or rec.get("target") or 0)
                rec["stop"] = float(sig.get("stop_loss") or rec.get("stop") or 0)
                rec["planned_entry"] = float(sig.get("entry_price") or rec.get("planned_entry") or 0)
                rec["trade_id"] = rec.get("trade_id") or signal_key(sig)

            existing_date = None
            try:
                existing_date = datetime.strptime(
                    str(rec.get("holding_start_date") or rec.get("first_seen_date")), "%Y-%m-%d"
                ).date()
            except Exception:
                pass
            fill_dt = current_session_buy_fill(ib, symbol)
            historical_date = earliest_local_position_date(symbol)
            candidates = [d for d in [existing_date, historical_date] if d is not None]
            if fill_dt is not None:
                if fill_dt.tzinfo is None:
                    fill_dt = fill_dt.replace(tzinfo=timezone.utc)
                fill_date = fill_dt.astimezone(ZoneInfo("Australia/Sydney")).date()
                candidates.append(fill_date)
                rec["fill_timestamp"] = fill_dt.isoformat()
                rec["holding_start_source"] = "IBKR_EXECUTION"
            elif historical_date is not None and rec.get("holding_start_source") != "IBKR_EXECUTION":
                rec["holding_start_source"] = "LOCAL_RECONCILE_HISTORY"
            if candidates:
                start_date = min(candidates)
                rec["holding_start_date"] = str(start_date)
                rec["first_seen_date"] = str(start_date)

        try:
            first_seen = datetime.strptime(
                str(rec.get("holding_start_date") or rec["first_seen_date"]), "%Y-%m-%d"
            ).date()
        except Exception:
            first_seen = today
            rec["first_seen_date"] = str(today)

        held_days = trading_days_between_dates(first_seen, today)
        max_days = int(limits.get(rec.get("strategy"), 0) or 0)
        due = bool(max_days and held_days >= max_days)
        payload = {
            "symbol": symbol,
            "strategy": rec.get("strategy"),
            "quantity": float(position.position),
            "avg_cost": float(position.avgCost or 0),
            "first_seen_date": rec.get("first_seen_date"),
            "holding_start_date": rec.get("holding_start_date") or rec.get("first_seen_date"),
            "holding_start_source": rec.get("holding_start_source"),
            "fill_timestamp": rec.get("fill_timestamp"),
            "holding_trading_days": held_days,
            "max_holding_trading_days": max_days or None,
            "time_exit_due": due,
        }
        log_event("POSITION_STATUS", payload)
        if due:
            event_type = "TIME_EXIT_DRY_RUN" if CONFIG.get("dry_run") else "TIME_EXIT_DUE"
            log_event(event_type, payload)
        results.append(payload)

    save_state(state)
    return results


def reconcile_ibkr_state(ib, state, active_signals):
    working_statuses = {"PendingSubmit", "PreSubmitted", "Submitted", "PendingCancel"}

    positions = {}
    for p in ib.positions():
        if not p.contract.symbol or p.position == 0:
            continue
        positions[p.contract.symbol] = {
            "quantity": float(p.position),
            "avg_cost": float(p.avgCost or 0),
            "account": p.account,
        }

    working_orders = []
    working_symbols = set()
    for t in ib.reqAllOpenOrders():
        if t.orderStatus.status not in working_statuses:
            continue
        symbol = str(t.contract.symbol or "").strip().upper()
        if not symbol:
            continue
        working_symbols.add(symbol)
        working_orders.append({
            "symbol": symbol,
            "order_id": int(t.order.orderId or 0),
            "parent_id": int(t.order.parentId or 0),
            "action": str(t.order.action or ""),
            "order_type": str(t.order.orderType or ""),
            "status": str(t.orderStatus.status or ""),
            "quantity": float(t.order.totalQuantity or 0),
            "limit_price": float(getattr(t.order, "lmtPrice", 0) or 0),
            "stop_price": float(getattr(t.order, "auxPrice", 0) or 0),
            "order_ref": str(getattr(t.order, "orderRef", "") or ""),
        })

    open_symbols = set(positions)
    occupied = open_symbols | working_symbols

    known_signal_keys = {signal_key(sig) for sig in active_signals}
    active_symbols = {str(sig.get("symbol") or "").strip().upper() for sig in active_signals}
    untracked = []
    for symbol in sorted(occupied):
        refs = {o["order_ref"] for o in working_orders if o["symbol"] == symbol and o["order_ref"]}
        if refs and any(ref in known_signal_keys for ref in refs):
            continue
        if symbol in active_symbols:
            continue
        untracked.append(symbol)

    snapshot = {
        "timestamp": now_iso(),
        "positions": positions,
        "working_orders": working_orders,
        "open_symbols": sorted(open_symbols),
        "working_order_symbols": sorted(working_symbols),
        "occupied_symbols": sorted(occupied),
        "planned_open_risk_pct": len(occupied) * CONFIG["risk_per_trade_pct"],
        "untracked_symbols": untracked,
    }
    state["ibkr_reconciliation"] = snapshot
    save_state(state)
    log_event("RECONCILE", snapshot)
    return snapshot


def expire_stale_unfilled_entries(ib, active_signals, open_syms):
    """Expire stale unfilled entry brackets without touching filled positions.

    In dry-run mode this is observation-only and logs ENTRY_EXPIRY_DRY_RUN.
    In paper execution mode it cancels only the matching parent BUY and its
    attached children. A filled symbol is always excluded from expiry.
    """
    max_age = int(CONFIG.get("entry_expiry_trading_days", 2))
    working_statuses = {"PendingSubmit", "PreSubmitted", "Submitted", "PendingCancel"}
    trades = [
        t for t in ib.reqAllOpenOrders()
        if t.orderStatus.status in working_statuses
    ]

    active_by_symbol = {}
    for sig in active_signals:
        symbol = str(sig.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        current = active_by_symbol.get(symbol)
        if current is None or str(sig.get("signal_date") or "") > str(current.get("signal_date") or ""):
            active_by_symbol[symbol] = sig

    events = []
    for parent_trade in trades:
        symbol = str(parent_trade.contract.symbol or "").strip().upper()
        order = parent_trade.order

        # Only parent BUY entries can expire here. Protective children and any
        # symbol that has already filled are explicitly excluded.
        if not symbol or symbol in open_syms:
            continue
        if str(order.action).upper() != "BUY" or int(order.parentId or 0) != 0:
            continue

        sig = None
        ref = str(getattr(order, "orderRef", "") or "")
        if ref:
            for candidate in active_signals:
                if signal_key(candidate) == ref:
                    sig = candidate
                    break
        if sig is None:
            sig = active_by_symbol.get(symbol)
        if sig is None:
            log_event("ENTRY_EXPIRY_UNTRACKED", {
                "symbol": symbol,
                "order_id": order.orderId,
                "reason": "working parent BUY has no matching active signal",
            })
            continue

        age_days = signal_age_trading_days(sig)
        if age_days is None or age_days <= max_age:
            continue

        bracket = [
            t for t in trades
            if str(t.contract.symbol or "").strip().upper() == symbol
            and (
                t.order.orderId == order.orderId
                or int(t.order.parentId or 0) == int(order.orderId)
            )
        ]
        payload = {
            "symbol": symbol,
            "signal": signal_key(sig),
            "signal_date": sig.get("signal_date"),
            "signal_age_trading_days": age_days,
            "entry_expiry_trading_days": max_age,
            "parent_order_id": order.orderId,
            "bracket_order_ids": [t.order.orderId for t in bracket],
        }

        if CONFIG.get("dry_run"):
            log_event("ENTRY_EXPIRY_DRY_RUN", payload)
            events.append({**payload, "action": "WOULD_CANCEL"})
            continue

        if not CONFIG.get("paper_only"):
            raise RuntimeError("Refusing automatic entry expiry outside paper_only mode")

        for trade in sorted(bracket, key=lambda t: 0 if t.order.parentId else 1):
            if trade.orderStatus.status in working_statuses:
                ib.cancelOrder(trade.order)
        ib.sleep(1)
        log_event("ENTRY_EXPIRED", payload)
        events.append({**payload, "action": "CANCELLED"})

    return events


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


def contract_market_rule(ib, contract):
    details = ib.reqContractDetails(contract)
    if not details:
        raise RuntimeError(f"No contract details returned for {contract.symbol}")

    detail = details[0]
    exchanges = [x.strip() for x in str(detail.validExchanges or "").split(",") if x.strip()]
    rule_ids = [x.strip() for x in str(detail.marketRuleIds or "").split(",") if x.strip()]
    desired_exchange = str(contract.primaryExchange or "ASX")

    market_rule_id = None
    if desired_exchange in exchanges:
        idx = exchanges.index(desired_exchange)
        if idx < len(rule_ids) and rule_ids[idx]:
            market_rule_id = int(rule_ids[idx])
    elif rule_ids:
        market_rule_id = int(rule_ids[0])

    if market_rule_id is None:
        raise RuntimeError(
            f"Could not determine market rule for {contract.symbol}; "
            f"exchange={desired_exchange}, validExchanges={exchanges}, marketRuleIds={rule_ids}"
        )

    increments = ib.reqMarketRule(market_rule_id)
    if not increments:
        raise RuntimeError(f"No price increments returned for market rule {market_rule_id} ({contract.symbol})")

    return market_rule_id, sorted(increments, key=lambda x: float(x.lowEdge))


def increment_for_price(price, increments):
    applicable = increments[0]
    for inc in increments:
        if float(price) >= float(inc.lowEdge):
            applicable = inc
        else:
            break
    tick = float(applicable.increment)
    if tick <= 0:
        raise RuntimeError(f"Invalid market-rule tick {tick} for price {price}")
    return tick


def snap_to_tick(price, tick, mode="nearest"):
    p = Decimal(str(price))
    t = Decimal(str(tick))
    units = p / t
    if mode == "up":
        rounded_units = units.to_integral_value(rounding=ROUND_CEILING)
    elif mode == "down":
        rounded_units = units.to_integral_value(rounding=ROUND_FLOOR)
    else:
        rounded_units = units.to_integral_value(rounding=ROUND_HALF_UP)
    return float(rounded_units * t)


def executable_bracket_prices(ib, contract, sig):
    market_rule_id, increments = contract_market_rule(ib, contract)

    raw_entry = float(sig["entry_price"])
    raw_target = float(sig["profit_target"])
    raw_stop = float(sig["stop_loss"])

    entry_tick = increment_for_price(raw_entry, increments)
    target_tick = increment_for_price(raw_target, increments)
    stop_tick = increment_for_price(raw_stop, increments)

    entry = snap_to_tick(raw_entry, entry_tick, "nearest")
    target = snap_to_tick(raw_target, target_tick, "down")
    stop = snap_to_tick(raw_stop, stop_tick, "up")

    if not (stop < entry < target):
        raise RuntimeError(
            f"Invalid snapped bracket for {sig['symbol']}: stop={stop}, entry={entry}, target={target}"
        )

    return {
        "market_rule_id": market_rule_id,
        "raw_entry": raw_entry,
        "raw_target": raw_target,
        "raw_stop": raw_stop,
        "entry": entry,
        "target": target,
        "stop": stop,
        "entry_tick": entry_tick,
        "target_tick": target_tick,
        "stop_tick": stop_tick,
    }


def place_bracket(ib, sig, qty):
    symbol = sig["symbol"]
    contract = make_asx_contract(symbol)
    ib.qualifyContracts(contract)
    prices = executable_bracket_prices(ib, contract, sig)

    parent_id = ib.client.getReqId()
    take_id = ib.client.getReqId()
    stop_id = ib.client.getReqId()

    parent = LimitOrder(
        "BUY", qty, prices["entry"], orderId=parent_id, transmit=False,
        tif=CONFIG["time_in_force"], account=CONFIG["ibkr"]["account"],
        orderRef=signal_key(sig)
    )
    take = LimitOrder(
        "SELL", qty, prices["target"], orderId=take_id, parentId=parent_id,
        transmit=False, tif="GTC", account=CONFIG["ibkr"]["account"],
        orderRef=signal_key(sig)
    )
    protective = StopOrder(
        "SELL", qty, prices["stop"], orderId=stop_id, parentId=parent_id,
        transmit=True, tif="GTC", account=CONFIG["ibkr"]["account"],
        orderRef=signal_key(sig)
    )

    log_event("PRICE_SNAP", {
        "symbol": symbol,
        "route": "SMART",
        "primary_exchange": "ASX",
        **prices,
    })

    trades = []
    trades.append(ib.placeOrder(contract, parent))
    ib.sleep(0.1)
    trades.append(ib.placeOrder(contract, take))
    ib.sleep(0.1)
    trades.append(ib.placeOrder(contract, protective))
    ib.sleep(5)
    return trades


def trade_snapshot(trade):
    logs = []
    for item in trade.log:
        logs.append({
            "time": str(getattr(item, "time", "")),
            "status": getattr(item, "status", ""),
            "message": getattr(item, "message", ""),
            "error_code": getattr(item, "errorCode", 0),
        })

    return {
        "order_id": trade.order.orderId,
        "perm_id": getattr(trade.order, "permId", 0),
        "client_id": getattr(trade.order, "clientId", 0),
        "parent_id": trade.order.parentId,
        "action": trade.order.action,
        "order_type": trade.order.orderType,
        "status": trade.orderStatus.status,
        "why_held": getattr(trade.orderStatus, "whyHeld", ""),
        "mkt_cap_price": getattr(trade.orderStatus, "mktCapPrice", 0),
        "transmit": trade.order.transmit,
        "limit_price": getattr(trade.order, "lmtPrice", None),
        "aux_price": getattr(trade.order, "auxPrice", None),
        "advanced_error": str(getattr(trade, "advancedError", "") or ""),
        "log": logs,
    }


def bracket_acknowledged(trades):
    acceptable_statuses = {"PreSubmitted", "Submitted", "Filled"}
    snapshots = [trade_snapshot(t) for t in trades]
    ids = {s["order_id"] for s in snapshots}
    parent_id = snapshots[0]["order_id"]
    structure_ok = (
        len(trades) == 3
        and len(ids) == 3
        and snapshots[1]["parent_id"] == parent_id
        and snapshots[2]["parent_id"] == parent_id
        and snapshots[0]["action"] == "BUY"
        and snapshots[1]["action"] == "SELL"
        and snapshots[2]["action"] == "SELL"
        and snapshots[0]["order_type"] == "LMT"
        and snapshots[1]["order_type"] == "LMT"
        and snapshots[2]["order_type"] == "STP"
        and snapshots[2]["transmit"] is True
    )
    status_ok = all(s["status"] in acceptable_statuses for s in snapshots)

    # IBKR normally marks attached bracket children as held until the parent
    # fills.  These are expected protective states, not acknowledgement
    # failures.  The parent itself should not be held.
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


def main():
    if CONFIG["kill_switch"]:
        raise SystemExit("Kill switch is ON")
    if not CONFIG["paper_only"]:
        raise SystemExit("paper_only must remain true in v1")

    scanner = fetch_signals()
    active = scanner.get("active_trades", [])
    state = load_state()

    ib = IB()
    ib.connect(
        CONFIG["ibkr"]["host"],
        CONFIG["ibkr"]["port"],
        clientId=CONFIG["ibkr"]["client_id"],
        timeout=10,
        readonly=bool(CONFIG["dry_run"]),
    )
    try:
        paper_account = assert_expected_paper_account(ib)
        ib.reqMarketDataType(3)
        actual_equity = current_equity(ib)
        equity = sizing_equity(actual_equity)
        open_syms = open_symbols(ib)
        working_syms = working_order_symbols(ib)
        occupied_syms = open_syms | working_syms
        risk_pct = len(occupied_syms) * CONFIG["risk_per_trade_pct"]
        reconcile_ibkr_state(ib, state, active)
        capture_visible_executions(ib, state)
        manage_filled_positions(ib, state, active)
        finalize_closed_trades(state, active)
        compute_performance_statistics()
        expire_stale_unfilled_entries(ib, active, open_syms)
        submitted_this_run = 0
        max_new = int(CONFIG.get("max_new_orders_per_run", 1))

        log_event("ACCOUNT", {
            "account": paper_account,
            "paper_guard_verified": True,
            "ibkr_equity": actual_equity,
            "sizing_equity": equity,
            "test_equity_override": equity != actual_equity,
            "open_positions": sorted(open_syms),
            "working_order_symbols": sorted(working_syms),
            "occupied_symbols": sorted(occupied_syms),
            "open_risk_pct": risk_pct,
            "risk_per_trade_pct": CONFIG["risk_per_trade_pct"],
            "risk_budget_aud": equity * CONFIG["risk_per_trade_pct"] / 100.0,
            "dry_run": CONFIG["dry_run"],
            "max_new_orders_per_run": max_new,
        })

        for sig in active:
            key = signal_key(sig)
            if key in state["seen_signals"] and state["seen_signals"][key].get("status") in {"SUBMITTED", "FILLED"}:
                continue

            age_days = signal_age_trading_days(sig)
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


            if sig["symbol"] in working_syms:
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
            ticker = ib.reqMktData(contract, "", False, False)
            ib.sleep(2)
            market_price = ticker.marketPrice()
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
            if not ok:
                if not CONFIG["dry_run"]:
                    state["seen_signals"][key] = {"status": "SKIPPED", "reason": result, "timestamp": now_iso()}
                log_event("SKIP", {"signal": key, "symbol": sig.get("symbol"), "reason": result, "market_price": market_price, "price_source": price_source})
                continue

            qty = result
            if CONFIG["dry_run"]:
                log_event("DRY_RUN", {
                    "signal": key,
                    "symbol": sig["symbol"],
                    "qty": qty,
                    "market_price": market_price,
                    "price_source": price_source,
                    "external_market_status": external_status,
                    "entry": sig["entry_price"],
                    "stop": sig["stop_loss"],
                    "target": sig["profit_target"],
                    "planned_risk_aud": qty * abs(float(sig["entry_price"]) - float(sig["stop_loss"])),
                    "position_value_aud": qty * float(sig["entry_price"]),
                    "sizing_equity_aud": equity,
                })
                continue

            if submitted_this_run >= max_new:
                log_event("DEFER", {
                    "signal": key,
                    "symbol": sig["symbol"],
                    "reason": f"max_new_orders_per_run={max_new} reached",
                })
                continue

            trades = place_bracket(ib, sig, qty)
            acknowledged, snapshots = bracket_acknowledged(trades)
            log_event("ORDER_ACK", {"signal": key, "symbol": sig["symbol"], "orders": snapshots, "acknowledged": acknowledged})
            if not acknowledged:
                state["seen_signals"][key] = {"status": "ACK_FAILED", "qty": qty, "orders": snapshots, "timestamp": now_iso()}
                save_state(state)
                raise RuntimeError(f"Bracket acknowledgement failed for {sig['symbol']}; stopping before any further orders")

            state["seen_signals"][key] = {"status": "SUBMITTED", "qty": qty, "order_ids": [t.order.orderId for t in trades], "orders": snapshots, "timestamp": now_iso()}
            log_event("SUBMIT", {"signal": key, "symbol": sig["symbol"], "qty": qty, "order_ids": [t.order.orderId for t in trades], "account": paper_account, "sizing_equity_aud": equity})
            open_syms.add(sig["symbol"])
            working_syms.add(sig["symbol"])
            occupied_syms.add(sig["symbol"])
            risk_pct = len(occupied_syms) * CONFIG["risk_per_trade_pct"]
            submitted_this_run += 1

        save_state(state)
    finally:
        ib.disconnect()


if __name__ == "__main__":
    main()
