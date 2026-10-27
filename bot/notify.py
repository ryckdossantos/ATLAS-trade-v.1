"""Notifiche Telegram (facoltative: senza token non fa nulla)."""
from __future__ import annotations

import os

import requests


def telegram(text: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat, "text": text, "parse_mode": "HTML",
                            "disable_web_page_preview": True}, timeout=15)
    except requests.RequestException:
        pass  # una notifica persa non deve fermare il bot
