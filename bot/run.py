"""Atlas: un giro completo del bot.

Ordine fisso: dati -> riconciliazione -> uscite forzate del guardiano ->
analisi di Claude (una volta al giorno) -> esecuzione validata -> stop -> dashboard.

Modalità:
- SIMULAZIONE: nessuna chiave Trading 212. Ordini simulati ai prezzi di mercato.
- DEMO: chiavi del conto demo di Trading 212 (T212_ENV=demo, predefinito).
- LIVE: soldi veri. Richiede T212_ENV=live E LIVE_CONFIRM=SI_HO_CAPITO.
"""
from __future__ import annotations

import html
import json
import os
import sys
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from . import guardian, market, timemachine
from .notify import telegram

ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = ROOT / "state" / "state.json"
DASH_FILE = ROOT / "docs" / "data" / "dashboard.json"
BERLIN = ZoneInfo("Europe/Berlin")


def log(msg: str) -> None:
    print(f"[atlas] {msg}", flush=True)


def load_state(budget: float) -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"cash": budget, "position": None, "trades": [], "decisions": [], "equity_history": [],
            "cooldowns": {}, "halted": False, "benchmark_start": None, "tickers": {},
            "costs_usd": 0.0, "started_at": datetime.now(BERLIN).isoformat(timespec="seconds")}


def save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1))


def market_is_open(now: datetime, cfg: dict, last_bar_date: str | None) -> bool:
    if now.weekday() >= 5:
        return False
    h = cfg["market_hours_berlin"]
    hhmm = now.strftime("%H:%M")
    if not (h["open"] <= hhmm <= h["close"]):
        return False
    # festività: se Yahoo non ha la barra di oggi, il mercato è chiuso
    return last_bar_date == now.date().isoformat()


class Broker:
    """Unifica T212 reale e simulazione dietro la stessa interfaccia."""

    def __init__(self, api, sim: bool):
        self.api, self.sim = api, sim

    def mode(self) -> str:
        return "SIMULAZIONE" if self.sim else self.api.env.upper()

    def available(self, fallback: float) -> float:
        if self.sim:
            return fallback
        return float(self.api.summary()["cash"]["availableToTrade"])

    def position(self, ticker: str) -> dict | None:
        if self.sim:
            return None
        for p in self.api.positions() or []:
            if p["instrument"]["ticker"] == ticker:
                return p
        return None

    def buy(self, ticker: str, qty: float) -> None:
        if not self.sim:
            self.api.market(ticker, qty)

    def sell(self, ticker: str, qty: float) -> None:
        if not self.sim:
            self.api.market(ticker, -qty)

    def place_stop(self, ticker: str, qty: float, price: float) -> int | None:
        if self.sim:
            return None
        try:
            return self.api.stop(ticker, -qty, price)["id"]
        except Exception as e:  # stop nativo non disponibile: resta lo stop software
            log(f"stop nativo non piazzato: {e}")
            return None

    def cancel_stops(self, ticker: str) -> None:
        if self.sim:
            return
        for o in self.api.pending_orders() or []:
            if o.get("ticker") == ticker and o.get("side") == "SELL":
                try:
                    self.api.cancel(o["id"])
                except Exception as e:
                    log(f"annullamento ordine {o['id']} fallito: {e}")
        time.sleep(2)

    def last_sell_fill(self, ticker: str) -> float | None:
        if self.sim:
            return None
        try:
            for item in self.api.history_orders(ticker, 10).get("items", []):
                if item["order"].get("side") == "SELL" and item.get("fill"):
                    return float(item["fill"]["price"])
        except Exception as e:
            log(f"storico ordini non disponibile: {e}")
        return None


def close_position(state, broker, price, reason, now, risk) -> str:
    pos = state["position"]
    broker.cancel_stops(pos["ticker"])
    broker.sell(pos["ticker"], pos["quantity"])
    return record_close(state, price, reason, now, risk)


def record_close(state, price, reason, now, risk) -> str:
    pos = state["position"]
    proceeds = pos["quantity"] * price
    pnl = proceeds - pos["cost"]
    state["cash"] = round(state["cash"] + proceeds, 2)
    state["trades"].append({"time": now.isoformat(timespec="seconds"), "side": "SELL", "isin": pos["isin"],
                            "name": pos["name"], "quantity": pos["quantity"], "price": round(price, 4),
                            "pnl": round(pnl, 2), "reason": reason})
    if reason == "STOP_LOSS":
        state["cooldowns"][pos["isin"]] = (now + timedelta(days=risk["cooldown_days"])).isoformat(timespec="seconds")
    state["position"] = None
    sign = "+" if pnl >= 0 else ""
    return f"🔴 VENDUTO {pos['name']} a {price:.2f}€ ({reason}) · risultato {sign}{pnl:.2f}€"


def main() -> int:
    cfg = json.loads((ROOT / "config.json").read_text())
    risk, budget = cfg["risk"], cfg["budget_eur"]
    state = load_state(budget)
    now = datetime.now(BERLIN)
    events: list[str] = []

    # Modalità e sicurezza
    key, secret = os.getenv("T212_API_KEY"), os.getenv("T212_API_SECRET")
    env = (os.getenv("T212_ENV") or "demo").lower()
    if env == "live" and os.getenv("LIVE_CONFIRM") != "SI_HO_CAPITO":
        log("LIVE richiesto senza LIVE_CONFIRM=SI_HO_CAPITO: mi fermo.")
        telegram("⛔ Atlas: modalità LIVE bloccata. Manca la conferma LIVE_CONFIRM.")
        return 1
    if key and secret:
        from .t212 import T212, resolve_tickers
        api = T212(key, secret, env)
        broker = Broker(api, sim=False)
    else:
        api, broker = None, Broker(None, sim=True)
    log(f"modalità {broker.mode()}")

    # Dati di mercato e segnali
    universe = cfg["universe"]
    data = market.fetch([u["yahoo"] for u in universe] + [cfg["benchmark_yahoo"]])
    signals, by_isin = {}, {u["isin"]: u for u in universe}
    for u in universe:
        if u["yahoo"] in data:
            signals[u["isin"]] = market.compute_signals(data[u["yahoo"]])
            signals[u["isin"]]["tm"] = timemachine.run(data[u["yahoo"]], risk, cfg["time_machine"])
    bench_df = data.get(cfg["benchmark_yahoo"])
    bench_price = float(bench_df["Close"].iloc[-1]) if bench_df is not None else None
    if state["benchmark_start"] is None and bench_price:
        state["benchmark_start"] = bench_price
    last_bar = max((s["date"] for s in signals.values()), default=None)
    is_open = market_is_open(now, cfg, last_bar)
    log(f"mercato {'aperto' if is_open else 'chiuso'} · segnali per {len(signals)} strumenti")

    # Ticker Trading 212
    if api and len(state["tickers"]) < len(universe):
        state["tickers"] = resolve_tickers(api.instruments(), [u["isin"] for u in universe])
        log(f"ticker risolti: {state['tickers']}")
    if not api:
        state["tickers"] = {u["isin"]: u["yahoo"] for u in universe}

    # Riconciliazione: lo stop nativo può aver venduto tra un giro e l'altro
    pos = state["position"]
    if pos and api:
        live = broker.position(pos["ticker"])
        if live is None:
            fill = broker.last_sell_fill(pos["ticker"]) or signals.get(pos["isin"], {}).get("price", pos["entry_price"])
            events.append(record_close(state, fill, "STOP_LOSS", now, risk))
            pos = None
        else:
            pos["quantity"] = float(live.get("quantityAvailableForTrading") or live["quantity"])

    # Prezzo corrente della posizione e uscite forzate
    if state["position"]:
        pos = state["position"]
        price = signals.get(pos["isin"], {}).get("price", pos["entry_price"])
        if api:
            live = broker.position(pos["ticker"])
            price = float(live["currentPrice"]) if live else price
        pos["peak_price"] = max(pos["peak_price"], price)
        pos["last_price"] = price
        reason = guardian.check_exit(pos, price, now, risk)
        if reason and is_open:
            events.append(close_position(state, broker, price, reason, now, risk))

    # Freno di emergenza
    cur_price = state["position"]["last_price"] if state["position"] else None
    equity = guardian.bot_equity(state, cur_price)
    if not state["halted"] and guardian.check_drawdown(state, equity, budget, risk):
        state["halted"] = True
        events.append(f"⛔ BLOCCO: capitale del bot a {equity:.2f}€, oltre la perdita massima. Atlas non apre più posizioni.")
        if state["position"] and is_open:
            events.append(close_position(state, broker, cur_price, "BLOCCO", now, risk))

    # Analisi di Claude: una volta per giorno di borsa
    today = now.date().isoformat()
    analysed_today = any(d["time"][:10] == today for d in state["decisions"])
    anthropic_key = os.getenv("ANTHROPIC_API_KEY")
    if is_open and not analysed_today and not state["halted"] and anthropic_key:
        from .brain import decide
        equity = guardian.bot_equity(state, state["position"]["last_price"] if state["position"] else None)
        decision, usage = decide(cfg, universe, signals, state["position"], state["decisions"], equity, anthropic_key)
        cost = (usage["input_tokens"] * 2 + usage["output_tokens"] * 10) / 1e6 + usage["web_searches"] * 0.01
        state["costs_usd"] = round(state["costs_usd"] + cost, 4)
        entry = {"time": now.isoformat(timespec="seconds"), **decision, "outcome": "", "cost_usd": round(cost, 4)}
        log(f"decisione Claude: {decision['action']} {decision['isin']} ({decision['confidence']:.2f})")

        if decision["action"] == "SELL" and state["position"]:
            p = state["position"]["last_price"]
            events.append(close_position(state, broker, p, "DECISIONE_CLAUDE", now, risk))
            entry["outcome"] = "eseguita"
        elif decision["action"] == "SELL":
            entry["outcome"] = "nessuna posizione da vendere"
        elif decision["action"] == "BUY":
            verdict = guardian.validate_buy(decision, signals, state, now, risk, cfg["time_machine"])
            entry["tm"] = signals.get(decision["isin"], {}).get("tm")
            if not verdict.ok:
                entry["outcome"] = "bloccata: " + "; ".join(verdict.reasons)
            else:
                isin = decision["isin"]
                ticker = state["tickers"].get(isin)
                price = signals[isin]["price"]
                qty = guardian.size_quantity(state["cash"], price, broker.available(state["cash"]), risk) if ticker else 0
                if not ticker or qty <= 0:
                    entry["outcome"] = "bloccata: strumento non disponibile o importo troppo piccolo"
                else:
                    broker.buy(ticker, qty)
                    if api:
                        time.sleep(4)
                        live = broker.position(ticker)
                        if live:
                            price = float(live["averagePricePaid"])
                            qty = float(live["quantity"])
                    cost_eur = round(qty * price, 2)
                    state["cash"] = round(state["cash"] - cost_eur, 2)
                    state["position"] = {"isin": isin, "ticker": ticker, "name": by_isin[isin]["name"],
                                         "quantity": qty, "entry_price": price, "peak_price": price,
                                         "last_price": price, "cost": cost_eur,
                                         "opened_at": now.isoformat(timespec="seconds"), "stop_order": None}
                    stop = guardian.stop_price(state["position"], risk)
                    state["position"]["stop_order"] = {"id": broker.place_stop(ticker, qty, stop), "price": stop}
                    state["trades"].append({"time": now.isoformat(timespec="seconds"), "side": "BUY", "isin": isin,
                                            "name": by_isin[isin]["name"], "quantity": qty,
                                            "price": round(price, 4), "pnl": 0, "reason": "DECISIONE_CLAUDE"})
                    entry["outcome"] = "eseguita"
                    events.append(f"🟢 COMPRATO {by_isin[isin]['name']}: {qty} quote a {price:.2f}€ "
                                  f"(stop {stop:.2f}€)\n{html.escape(decision['thesis'])}")
        else:
            entry["outcome"] = "nessuna azione"
        state["decisions"].append(entry)
        state["decisions"] = state["decisions"][-120:]

    # Stop nativo che sale con il prezzo (trailing)
    pos = state["position"]
    if pos and api and is_open:
        new_stop = guardian.stop_price(pos, risk)
        old = pos.get("stop_order") or {}
        if not old.get("id") or new_stop > old.get("price", 0) * 1.005:
            broker.cancel_stops(pos["ticker"])
            pos["stop_order"] = {"id": broker.place_stop(pos["ticker"], pos["quantity"], new_stop), "price": new_stop}
            log(f"stop aggiornato a {new_stop}")

    # Fotografia giornaliera
    cur_price = state["position"]["last_price"] if state["position"] else None
    equity = guardian.bot_equity(state, cur_price)
    bench_eq = round(budget * bench_price / state["benchmark_start"], 2) if bench_price else None
    hist = [h for h in state["equity_history"] if h["date"] != today]
    hist.append({"date": today, "equity": equity, "benchmark": bench_eq})
    state["equity_history"] = hist[-400:]
    state["last_run"] = now.isoformat(timespec="seconds")
    state["mode"] = broker.mode()

    save_json(STATE_FILE, state)
    save_json(DASH_FILE, build_dashboard(state, cfg, signals, equity, bench_eq, is_open))

    for e in events:
        telegram(f"<b>Atlas · {broker.mode()}</b>\n{e}")
    if state["decisions"] and state["decisions"][-1]["time"][:10] == today and not events and analysed_today is False:
        d = state["decisions"][-1]
        telegram(f"<b>Atlas · {broker.mode()}</b>\n📊 Analisi del giorno: {d['action']} · {d['outcome']}\n"
                 f"Capitale bot {equity:.2f}€\n{html.escape(d['thesis'])}")
    log(f"fine giro · capitale bot {equity:.2f}€")
    return 0


def build_dashboard(state, cfg, signals, equity, bench_eq, is_open) -> dict:
    risk, budget = cfg["risk"], cfg["budget_eur"]
    pos = state["position"]
    closed = [t for t in state["trades"] if t["side"] == "SELL"]
    wins = [t for t in closed if t["pnl"] > 0]
    peak = max([h["equity"] for h in state["equity_history"]] + [budget])
    return {
        "updated": state["last_run"], "mode": state["mode"], "market_open": is_open,
        "budget": budget, "equity": equity, "benchmark": bench_eq, "halted": state["halted"],
        "return_pct": round((equity / budget - 1) * 100, 2),
        "benchmark_return_pct": round((bench_eq / budget - 1) * 100, 2) if bench_eq else None,
        "drawdown_pct": round((equity / peak - 1) * 100, 2),
        "halt_level": round(budget * (1 - risk["max_drawdown_pct"]), 2),
        "trades_closed": len(closed), "win_rate": round(len(wins) / len(closed) * 100) if closed else None,
        "realized_pnl": round(sum(t["pnl"] for t in closed), 2),
        "costs_usd": state["costs_usd"],
        "position": None if not pos else {**pos, "stop": guardian.stop_price(pos, risk),
                                          "take_profit": round(pos["entry_price"] * (1 + risk["take_profit_pct"]), 4),
                                          "pnl": round(pos["quantity"] * pos["last_price"] - pos["cost"], 2)},
        "equity_history": state["equity_history"],
        "trades": state["trades"][-40:][::-1],
        "decisions": state["decisions"][-30:][::-1],
        "signals": [{"isin": u["isin"], "name": u["name"], **signals[u["isin"]]}
                    for u in cfg["universe"] if u["isin"] in signals],
        "rules": risk,
        "tm_rules": cfg["time_machine"],
        "started_at": state["started_at"],
    }


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        telegram("⚠️ Atlas: errore durante il giro. Controlla i log su GitHub Actions.")
        sys.exit(1)
