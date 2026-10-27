"""La macchina del tempo: migliaia di futuri possibili prima di ogni acquisto.

Due prove, con le stesse regole del guardiano (stop fisso, stop che segue il prezzo,
obiettivo, durata massima):

1. Simulazione: ricombina a blocchi i rendimenti giornalieri dell'ultimo anno
   (block bootstrap), così conserva le fasi calme e quelle agitate del mercato.
2. Storico: rigioca le regole su ogni giorno passato in cui l'ETF era in trend
   (prezzo sopra la media a 50 giorni, RSI sotto la soglia) e conta come è finita.

È una stima, non una previsione: il futuro può non somigliare all'ultimo anno.
"""
from __future__ import annotations

import numpy as np
import pandas as pd



def trading_days(risk: dict) -> int:
    """La durata massima è in giorni di calendario: in borsa sono circa 5 su 7."""
    return max(5, round(risk["max_holding_days"] * 5 / 7))


def _outcomes(paths: np.ndarray, risk: dict) -> tuple[np.ndarray, np.ndarray]:
    """paths: (N, T) prezzi relativi all'ingresso (1.0 = prezzo d'acquisto).
    Restituisce esito (1 obiettivo, -1 stop, 0 scadenza) e rendimento finale per percorso."""
    sl, trail, tp = risk["stop_loss_pct"], risk["trailing_pct"], risk["take_profit_pct"]
    peak = np.maximum.accumulate(np.concatenate([np.ones((len(paths), 1)), paths], axis=1), axis=1)[:, :-1]
    stop = np.maximum(1 - sl, peak * (1 - trail))
    hit_stop = paths <= stop
    hit_tp = paths >= 1 + tp
    big = paths.shape[1] + 1
    first_stop = np.where(hit_stop.any(1), hit_stop.argmax(1), big)
    first_tp = np.where(hit_tp.any(1), hit_tp.argmax(1), big)
    outcome = np.zeros(len(paths), dtype=int)
    outcome[first_tp < first_stop] = 1
    outcome[first_stop < first_tp] = -1
    rows = np.arange(len(paths))
    final = paths[:, -1].copy()
    stopped = outcome == -1
    final[stopped] = np.maximum(paths[rows[stopped], first_stop[stopped]], 0)  # fill al prezzo della chiusura del giorno
    final[outcome == 1] = 1 + tp
    return outcome, final - 1


def simulate(close: pd.Series, risk: dict, n: int = 5000, block: int = 5,
             lookback: int = 250, seed: int | None = 42) -> dict:
    rets = close.pct_change().dropna().to_numpy()[-lookback:]
    horizon = trading_days(risk)
    if len(rets) < block * 4:
        return {}
    rng = np.random.default_rng(seed)
    n_blocks = -(-horizon // block)
    starts = rng.integers(0, len(rets) - block, size=(n, n_blocks))
    idx = (starts[..., None] + np.arange(block)).reshape(n, -1)[:, :horizon]
    paths = np.cumprod(1 + rets[idx], axis=1)
    outcome, ret = _outcomes(paths, risk)
    return {
        "p_target": round(float((outcome == 1).mean()) * 100, 1),
        "p_stop": round(float((outcome == -1).mean()) * 100, 1),
        "p_neither": round(float((outcome == 0).mean()) * 100, 1),
        "exp_return_pct": round(float(ret.mean()) * 100, 2),
        "worst_5pct": round(float(np.percentile(ret, 5)) * 100, 2),
        "simulations": n,
    }


def history_check(close: pd.Series, risk: dict, min_gap: int = 5) -> dict:
    """Rigioca le regole sui giorni passati con segnale d'ingresso valido."""
    close = close.dropna()
    horizon = trading_days(risk)
    sma50 = close.rolling(50).mean()
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    rsi = 100 - 100 / (1 + gain / loss.replace(0, 1e-9))
    ok = (close > sma50) & (rsi < risk["max_rsi_entry"])
    values = close.to_numpy()
    paths, last = [], -min_gap
    for i in np.flatnonzero(ok.to_numpy()):
        if i + horizon >= len(values) or i - last < min_gap:
            continue
        paths.append(values[i + 1:i + 1 + horizon] / values[i])
        last = i
    if len(paths) < 5:
        return {"cases": len(paths)}
    outcome, ret = _outcomes(np.array(paths), risk)
    return {
        "cases": len(paths),
        "p_target": round(float((outcome == 1).mean()) * 100, 1),
        "p_stop": round(float((outcome == -1).mean()) * 100, 1),
        "avg_return_pct": round(float(ret.mean()) * 100, 2),
    }


def run(df: pd.DataFrame, risk: dict, cfg: dict) -> dict:
    close = df["Close"].dropna()
    sim = simulate(close, risk, n=cfg["simulations"], block=cfg["block_days"], lookback=cfg["lookback_days"])
    return {**sim, "history": history_check(close, risk)} if sim else {}


def edge_ok(tm: dict, cfg: dict) -> tuple[bool, str]:
    """Il guardiano usa questa verifica: rendimento atteso sopra la soglia e stop non troppo probabile.

    Con uno stop che segue il prezzo, gli stop sono spesso più frequenti degli obiettivi
    anche in una strategia che guadagna: per questo conta il rendimento atteso, non il confronto diretto."""
    if not tm:
        return False, "macchina del tempo senza dati sufficienti"
    if tm["exp_return_pct"] < cfg["min_expected_return_pct"]:
        return False, f"rendimento atteso simulato {tm['exp_return_pct']}% sotto la soglia {cfg['min_expected_return_pct']}%"
    if tm["p_stop"] > cfg["max_stop_probability_pct"]:
        return False, f"probabilità di stop {tm['p_stop']}% sopra il limite {cfg['max_stop_probability_pct']}%"
    return True, ""
