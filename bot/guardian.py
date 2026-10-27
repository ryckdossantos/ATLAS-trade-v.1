"""Il guardiano: regole di rischio in codice puro.

Claude propone, il guardiano decide. Nessuna funzione qui chiama la rete,
così ogni regola è testabile e Claude non può scavalcarla.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta


@dataclass
class Verdict:
    ok: bool
    reasons: list[str] = field(default_factory=list)


def stop_price(position: dict, risk: dict) -> float:
    """Stop più alto tra stop fisso dall'ingresso e trailing dal massimo."""
    fixed = position["entry_price"] * (1 - risk["stop_loss_pct"])
    trailing = position["peak_price"] * (1 - risk["trailing_pct"])
    return round(max(fixed, trailing), 4)


def check_exit(position: dict, price: float, now: datetime, risk: dict) -> str | None:
    """Restituisce il motivo di uscita forzata, oppure None."""
    if price <= stop_price(position, risk):
        return "STOP_LOSS"
    if price >= position["entry_price"] * (1 + risk["take_profit_pct"]):
        return "TAKE_PROFIT"
    opened = datetime.fromisoformat(position["opened_at"])
    if now - opened >= timedelta(days=risk["max_holding_days"]):
        return "MAX_HOLDING"
    return None


def bot_equity(state: dict, price: float | None) -> float:
    pos = state.get("position")
    value = pos["quantity"] * price if pos and price else 0.0
    return round(state["cash"] + value, 2)


def check_drawdown(state: dict, equity: float, budget: float, risk: dict) -> bool:
    """True se il capitale del bot è sceso oltre la perdita massima: blocco totale."""
    return equity <= budget * (1 - risk["max_drawdown_pct"])


def in_cooldown(state: dict, isin: str, now: datetime, risk: dict) -> bool:
    until = state.get("cooldowns", {}).get(isin)
    return bool(until) and now < datetime.fromisoformat(until)


def buys_today(state: dict, now: datetime) -> int:
    day = now.date().isoformat()
    return sum(1 for t in state.get("trades", []) if t["side"] == "BUY" and t["time"][:10] == day)


def validate_buy(decision: dict, signals: dict, state: dict, now: datetime, risk: dict,
                 tm_cfg: dict | None = None) -> Verdict:
    reasons = []
    isin = decision.get("isin", "")
    sig = signals.get(isin)
    if state.get("halted"):
        reasons.append("bot bloccato per perdita massima")
    if state.get("position"):
        reasons.append("c'è già una posizione aperta")
    if sig is None:
        reasons.append(f"strumento {isin or '?'} fuori dall'universo consentito")
    else:
        if sig["price"] <= sig["sma50"]:
            reasons.append("prezzo sotto la media a 50 giorni (niente acquisti contro trend)")
        if sig["rsi14"] >= risk["max_rsi_entry"]:
            reasons.append(f"RSI {sig['rsi14']:.0f} troppo alto (ipercomprato)")
        if tm_cfg is not None:
            from .timemachine import edge_ok
            ok, why = edge_ok(sig.get("tm") or {}, tm_cfg)
            if not ok:
                reasons.append(why)
    if decision.get("confidence", 0) < risk["min_confidence"]:
        reasons.append(f"fiducia {decision.get('confidence', 0):.2f} sotto la soglia {risk['min_confidence']}")
    if in_cooldown(state, isin, now, risk):
        reasons.append("strumento in pausa dopo uno stop recente")
    if buys_today(state, now) >= risk["max_buys_per_day"]:
        reasons.append("limite di acquisti giornalieri raggiunto")
    if state["cash"] * risk["invest_fraction"] < risk["min_order_eur"]:
        reasons.append("liquidità del bot insufficiente")
    return Verdict(ok=not reasons, reasons=reasons)


def size_quantity(cash: float, price: float, available_to_trade: float, risk: dict) -> float:
    """Quantità frazionaria arrotondata per difetto, mai oltre il budget del bot né il conto."""
    amount = min(cash, available_to_trade) * risk["invest_fraction"]
    if amount < risk["min_order_eur"] or price <= 0:
        return 0.0
    return math.floor(amount / price * 1000) / 1000
