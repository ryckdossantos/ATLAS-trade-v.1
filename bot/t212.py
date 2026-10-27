"""Client minimo per la Trading 212 Public API (v0)."""
from __future__ import annotations

import base64
import time

import requests

BASES = {
    "demo": "https://demo.trading212.com",
    "live": "https://live.trading212.com",
}


class T212Error(Exception):
    pass


class T212:
    def __init__(self, key: str, secret: str, env: str = "demo"):
        if env not in BASES:
            raise ValueError("env deve essere 'demo' o 'live'")
        token = base64.b64encode(f"{key}:{secret}".encode()).decode()
        self.base = BASES[env]
        self.env = env
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Basic {token}", "Content-Type": "application/json"})

    def _req(self, method: str, path: str, retry: bool = True, **kw):
        r = self.s.request(method, self.base + path, timeout=30, **kw)
        if r.status_code == 429 and retry:
            reset = r.headers.get("x-ratelimit-reset")
            wait = max(1, min(60, int(reset) - int(time.time()))) if reset else 10
            time.sleep(wait)
            return self._req(method, path, retry=False, **kw)
        if r.status_code >= 400:
            raise T212Error(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else None

    # Lettura
    def summary(self):
        return self._req("GET", "/api/v0/equity/account/summary")

    def positions(self):
        return self._req("GET", "/api/v0/equity/positions")

    def pending_orders(self):
        return self._req("GET", "/api/v0/equity/orders")

    def instruments(self):
        return self._req("GET", "/api/v0/equity/metadata/instruments")

    def history_orders(self, ticker: str, limit: int = 10):
        return self._req("GET", f"/api/v0/equity/history/orders?ticker={ticker}&limit={limit}")

    # Scrittura (non idempotente: chiamare una volta sola)
    def market(self, ticker: str, quantity: float):
        return self._req("POST", "/api/v0/equity/orders/market", retry=False,
                         json={"ticker": ticker, "quantity": quantity})

    def stop(self, ticker: str, quantity: float, stop_price: float):
        return self._req("POST", "/api/v0/equity/orders/stop", retry=False,
                         json={"ticker": ticker, "quantity": quantity,
                               "stopPrice": round(stop_price, 2), "timeValidity": "GOOD_TILL_CANCEL"})

    def cancel(self, order_id: int):
        return self._req("DELETE", f"/api/v0/equity/orders/{order_id}")


def resolve_tickers(instruments: list[dict], isins: list[str], currency: str = "EUR") -> dict:
    """ISIN -> ticker Trading 212, scegliendo la quotazione nella valuta del conto."""
    out = {}
    for isin in isins:
        matches = [i for i in instruments if i.get("isin") == isin]
        same_ccy = [i for i in matches if i.get("currencyCode") == currency]
        pick = (same_ccy or [])[:1]
        if pick:
            out[isin] = pick[0]["ticker"]
    return out
