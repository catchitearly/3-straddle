"""
Minimal Telegram bot message/document sender - just plain HTTP, no telegram
library dependency. Reads bot token / chat id from env (set as GitHub Actions
secrets, or in a local .env file):
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID

Text alerts (entries/exits/strike-fix/heartbeat) are gated by
config.SEND_TRADE_TEXT_MESSAGES - when False (the default), send_telegram_message()
just prints the text to the console instead of calling the Telegram API, so
you still see every event locally without getting pinged on your phone for
each one. send_telegram_document() (used for the periodic dashboard-file
push) is NOT gated by this flag - it always sends, independent of the text
alerts setting.
"""

import json
import mimetypes
import os
import urllib.request
import urllib.parse
import uuid

import config

TELEGRAM_BOT_TOKEN_ENV = "TELEGRAM_BOT_TOKEN"
TELEGRAM_CHAT_ID_ENV = "TELEGRAM_CHAT_ID"


def send_telegram_message(text, parse_mode="Markdown"):
    """
    Send a text message via the Telegram Bot API. Never raises - prints a
    warning and returns False on failure, so a Telegram outage never crashes
    the live-notifier run (state still gets saved/committed either way).

    If config.SEND_TRADE_TEXT_MESSAGES is False, this just prints the text
    to the console and returns False without calling the Telegram API at
    all - trade alerts stay visible locally without pinging your phone for
    every entry/exit/heartbeat. The dashboard file itself still gets pushed
    separately via send_telegram_document(), unaffected by this flag.
    """
    if not config.SEND_TRADE_TEXT_MESSAGES:
        print(f"[trade alert - Telegram text disabled, console only]\n{text}")
        return False

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


def send_telegram_document(file_path, caption=None):
    """
    Send a file via the Telegram Bot API's sendDocument endpoint - used to
    push the live dashboard HTML file itself to Telegram periodically.
    Pure stdlib multipart/form-data encoding (no `requests` dependency, same
    philosophy as send_telegram_message above). NOT gated by
    config.SEND_TRADE_TEXT_MESSAGES - always sends. Never raises - prints a
    warning and returns False on failure.
    """
    token = os.environ.get(TELEGRAM_BOT_TOKEN_ENV)
    chat_id = os.environ.get(TELEGRAM_CHAT_ID_ENV)

    if not token or not chat_id:
        print(f"WARNING: {TELEGRAM_BOT_TOKEN_ENV}/{TELEGRAM_CHAT_ID_ENV} not set, "
              f"skipping Telegram document send: {file_path}")
        return False

    if not os.path.exists(file_path):
        print(f"WARNING: {file_path} doesn't exist yet, skipping Telegram document send.")
        return False

    url = f"https://api.telegram.org/bot{token}/sendDocument"
    boundary = uuid.uuid4().hex
    filename = os.path.basename(file_path)
    mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"

    with open(file_path, "rb") as f:
        file_data = f.read()

    parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="chat_id"\r\n\r\n{chat_id}\r\n'.encode()]
    if caption:
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="caption"\r\n\r\n{caption}\r\n'.encode())
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="{filename}"\r\n'
        f'Content-Type: {mime_type}\r\n\r\n'.encode()
    )
    parts.append(file_data)
    parts.append(f'\r\n--{boundary}--\r\n'.encode())
    body = b"".join(parts)

    try:
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
            if not result.get("ok"):
                print(f"WARNING: Telegram document send returned not-ok: {result}")
                return False
            return True
    except Exception as e:  # noqa: BLE001 - never let a Telegram hiccup kill the run
        print(f"WARNING: Telegram document send failed: {e}")
        return False
