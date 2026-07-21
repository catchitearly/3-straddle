"""
Minimal Telegram bot message sender - just plain HTTP, no telegram library
dependency. Reads bot token / chat id from env (set as GitHub Actions secrets):
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID
"""

import os
import urllib.request
import urllib.parse
import json

TELEGRAM_BOT_TOKEN_ENV = "TELEGRAM_BOT_TOKEN"
TELEGRAM_CHAT_ID_ENV = "TELEGRAM_CHAT_ID"


def send_telegram_message(text, parse_mode="Markdown"):
    """
    Send a message via the Telegram Bot API. Never raises - prints a warning
    and returns False on failure, so a Telegram outage never crashes the
    live-notifier run (state still gets saved/committed either way).
    """
    token = os.environ.get(TELEGRAM_BOT_TOKEN_ENV)
    chat_id = os.environ.get(TELEGRAM_CHAT_ID_ENV)

    if not token or not chat_id:
        print(f"WARNING: {TELEGRAM_BOT_TOKEN_ENV}/{TELEGRAM_CHAT_ID_ENV} not set, "
              f"skipping Telegram send. Message was:\n{text}")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
    }).encode()

    try:
        req = urllib.request.Request(url, data=payload, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode())
            if not body.get("ok"):
                print(f"WARNING: Telegram API returned not-ok: {body}")
                return False
            return True
    except Exception as e:  # noqa: BLE001 - never let a Telegram hiccup kill the run
        print(f"WARNING: Telegram send failed: {e}")
        return False
