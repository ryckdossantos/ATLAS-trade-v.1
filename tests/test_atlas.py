import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from bot import guardian, market, run

RISK = json.load(open("config.json"))["risk"]
NOW = datetime(2026, 10, 27, 11, 0, tzinfo=ZoneInfo("Europe/Berlin"))


def pos(entry=100.0, peak=100.0, days=0):
    return {"entry_price": entry, "peak_price": peak, "quantity": 0.9, "cost": 90.0,
            "opened_at": (NOW - timedelta(days=days)).isoformat()}


def test_stop_fixed_then_trailing():
    assert guardian.stop_price(pos(), RISK) == 95.0
    assert guardian.stop_price(pos(peak=110), RISK) == pytest.approx(103.4)


def test_exits():
    assert guardian.check_exit(pos(), 94.9, NOW, RISK) == "STOP_LOSS"
    assert guardian.check_exit(pos(), 112.1, NOW, RISK) == "TAKE_PROFIT"
    assert guardian.check_exit(pos(days=31), 101, NOW, RISK) == "MAX_HOLDING"
    assert guardian.check_exit(pos(), 101, NOW, RISK) is None


def base_state():
    return {"cash": 100.0, "position": None, "trades": [], "cooldowns": {}, "halted": False}


SIG = {"X": {"price": 100, "sma50": 95, "rsi14": 55}}


def test_buy_ok():
    d = {"action": "BUY", "isin": "X", "confidence": 0.8}
    assert guardian.validate_buy(d, SIG, base_state(), NOW, RISK).ok


@pytest.mark.parametrize("mutate,expected", [
    (lambda d, s, st: d.update(confidence=0.5), "fiducia"),
    (lambda d, s, st: s["X"].update(price=90), "media a 50"),
    (lambda d, s, st: s["X"].update(rsi14=75), "RSI"),
    (lambda d, s, st: d.update(isin="NOPE"), "fuori dall'universo"),
    (lambda d, s, st: st.update(halted=True), "bloccato"),
    (lambda d, s, st: st.update(position={"x": 1}), "già una posizione"),
    (lambda d, s, st: st["cooldowns"].update(X=(NOW + timedelta(days=1)).isoformat()), "pausa"),
    (lambda d, s, st: st.update(cash=5), "liquidità"),
])
def test_buy_blocked(mutate, expected):
    d = {"action": "BUY", "isin": "X", "confidence": 0.8}
    s = {"X": dict(SIG["X"])}
    st = base_state()
    mutate(d, s, st)
    v = guardian.validate_buy(d, s, st, NOW, RISK)
    assert not v.ok and any(expected in r for r in v.reasons)


def test_one_buy_per_day():
    st = base_state()
    st["trades"] = [{"side": "BUY", "time": NOW.isoformat()}]
    v = guardian.validate_buy({"isin": "X", "confidence": 0.9}, SIG, st, NOW, RISK)
    assert any("giornalieri" in r for r in v.reasons)


def test_sizing_never_exceeds_budget_or_account():
    assert guardian.size_quantity(100, 50, 10_000, RISK) == 1.9
    assert guardian.size_quantity(100, 50, 40, RISK) == 0.76
    assert guardian.size_quantity(100, 50, 5, RISK) == 0.0


def test_drawdown():
    assert guardian.check_drawdown({}, 79.9, 100, RISK)
    assert not guardian.check_drawdown({}, 81, 100, RISK)


def fake_df(trend=0.002, n=260, seed=1):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(end=NOW.date(), periods=n)
    close = 100 * np.cumprod(1 + trend + rng.normal(0, 0.005, n))
    return pd.DataFrame({"Close": close, "High": close * 1.005, "Low": close * 0.995}, index=idx)


def test_signals_shape():
    s = market.compute_signals(fake_df())
    assert s["price"] > s["sma50"] and 0 <= s["rsi14"] <= 100 and s["date"] == NOW.date().isoformat()


def test_full_simulated_cycle(tmp_path, monkeypatch):
    """Giro completo in simulazione: compra, poi il guardiano vende sullo stop."""
    monkeypatch.setattr(run, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(run, "DASH_FILE", tmp_path / "dash.json")
    for k in ("T212_API_KEY", "T212_API_SECRET", "TELEGRAM_BOT_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")

    dfs = {}
    monkeypatch.setattr(run.market, "fetch", lambda syms: {s: dfs.get(s, fake_df(seed=hash(s) % 99)) for s in syms})

    class FakeDT(datetime):
        current = NOW

        @classmethod
        def now(cls, tz=None):
            return cls.current
    monkeypatch.setattr(run, "datetime", FakeDT)

    import bot.brain as brain
    monkeypatch.setattr(brain, "decide", lambda *a, **k: (
        {"action": "BUY", "isin": "IE00B4L5Y983", "confidence": 0.8, "thesis": "test", "risks": "", "news": ""},
        {"input_tokens": 1000, "output_tokens": 200, "web_searches": 1}))

    dfs["EUNL.DE"] = fake_df(trend=0.002)
    assert run.main() == 0
    st = json.loads((tmp_path / "state.json").read_text())
    assert st["position"]["isin"] == "IE00B4L5Y983"
    assert st["cash"] < 10 and st["mode"] == "SIMULAZIONE"

    # Giorno dopo: crollo del 10% -> stop loss del guardiano
    FakeDT.current = NOW + timedelta(days=1)
    crash = fake_df(trend=0.002)
    crash.index = crash.index + pd.offsets.BDay(1)
    crash.iloc[-1, crash.columns.get_loc("Close")] *= 0.88
    dfs["EUNL.DE"] = crash
    assert run.main() == 0
    st = json.loads((tmp_path / "state.json").read_text())
    assert st["position"] is None
    assert st["trades"][-1]["reason"] == "STOP_LOSS" and st["trades"][-1]["pnl"] < 0
    assert "IE00B4L5Y983" in st["cooldowns"]
    dash = json.loads((tmp_path / "dash.json").read_text())
    assert dash["trades_closed"] == 1 and len(dash["equity_history"]) == 2


def test_brain_reads_tool_call(monkeypatch):
    """Il cervello estrae la decisione dallo strumento e gestisce pause_turn."""
    from types import SimpleNamespace as NS
    import anthropic
    from bot import brain

    calls = []
    replies = [
        NS(stop_reason="pause_turn", content=[NS(type="text", text="cerco")],
           usage=NS(input_tokens=100, output_tokens=10, server_tool_use=NS(web_search_requests=1))),
        NS(stop_reason="tool_use", content=[NS(type="tool_use", name="submit_decision",
           input={"action": "buy", "isin": "X", "confidence": 1.7, "thesis": "t", "risks": "r", "news": "n"})],
           usage=NS(input_tokens=200, output_tokens=20, server_tool_use=None)),
    ]

    class Fake:
        def __init__(self, **k):
            self.messages = NS(create=lambda **kw: calls.append(kw) or replies[len(calls) - 1])
    monkeypatch.setattr(anthropic, "Anthropic", Fake)
    cfg = json.load(open("config.json"))
    d, usage = brain.decide(cfg, cfg["universe"], {}, None, [], 100.0, "k")
    assert d["action"] == "BUY" and d["confidence"] == 1.0
    assert usage == {"input_tokens": 300, "output_tokens": 30, "web_searches": 1}
    assert calls[0]["tools"][1]["strict"] is True and len(calls) == 2


def test_time_machine_rules():
    from bot import timemachine
    cfg = json.load(open("config.json"))
    up = timemachine.run(fake_df(trend=0.002), RISK, cfg["time_machine"])
    down = timemachine.run(fake_df(trend=-0.003), RISK, cfg["time_machine"])
    assert up["exp_return_pct"] > 0 and timemachine.edge_ok(up, cfg["time_machine"])[0]
    assert down["exp_return_pct"] < 0 and not timemachine.edge_ok(down, cfg["time_machine"])[0]
    assert abs(up["p_target"] + up["p_stop"] + up["p_neither"] - 100) < 0.5
    assert timemachine.trading_days(RISK) == 21


def test_time_machine_matches_hand_path():
    """Un percorso costruito a mano: sale del 10% e poi scende, lo stop che segue il prezzo deve chiudere."""
    import numpy as np
    from bot.timemachine import _outcomes
    path = np.array([[1.04, 1.10, 1.06, 1.03, 1.02]])
    out, ret = _outcomes(path, RISK)
    assert out[0] == -1 and abs(ret[0] - 0.03) < 1e-9  # stop a 1.10*0.94=1.034, toccato a 1.03
