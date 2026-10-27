"""Dati di mercato (Yahoo Finance) e indicatori tecnici."""
from __future__ import annotations

import pandas as pd


def rsi(close: pd.Series, n: int = 14) -> float:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = gain / loss.replace(0, 1e-9)
    return float((100 - 100 / (1 + rs)).iloc[-1])


def atr(df: pd.DataFrame, n: int = 14) -> float:
    prev = df["Close"].shift()
    tr = pd.concat([df["High"] - df["Low"], (df["High"] - prev).abs(), (df["Low"] - prev).abs()], axis=1).max(axis=1)
    return float(tr.rolling(n).mean().iloc[-1])


def compute_signals(df: pd.DataFrame) -> dict:
    close = df["Close"].dropna()
    price = float(close.iloc[-1])
    return {
        "price": round(price, 4),
        "date": str(close.index[-1].date()),
        "sma20": round(float(close.rolling(20).mean().iloc[-1]), 4),
        "sma50": round(float(close.rolling(50).mean().iloc[-1]), 4),
        "sma200": round(float(close.rolling(200).mean().iloc[-1]), 4) if len(close) >= 200 else None,
        "rsi14": round(rsi(close), 1),
        "atr_pct": round(atr(df) / price * 100, 2),
        "mom_20d_pct": round((price / float(close.iloc[-21]) - 1) * 100, 2),
        "from_52w_high_pct": round((price / float(close.tail(252).max()) - 1) * 100, 2),
    }


def fetch(symbols: list[str]) -> dict[str, pd.DataFrame]:
    import yfinance as yf  # importato qui così i test non lo richiedono

    out = {}
    for s in symbols:
        df = yf.Ticker(s).history(period="1y", interval="1d", auto_adjust=True)
        if len(df) >= 60:
            out[s] = df
    return out
