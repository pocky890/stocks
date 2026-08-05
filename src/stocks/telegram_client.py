import requests

TELEGRAM_API_BASE = "https://api.telegram.org"
MAX_MESSAGE_LENGTH = 4096


def send_message(bot_token: str, chat_id: str, text: str) -> bool:
    """Send a message via the Telegram Bot API. Returns True on success, False on failure
    (never raises -- a notification failure shouldn't crash the monitoring loop)."""
    if not bot_token or not chat_id:
        print(f"[telegram] 未設定bot_token/chat_id，訊息未送出: {text[:80]}")
        return False

    url = f"{TELEGRAM_API_BASE}/bot{bot_token}/sendMessage"
    if len(text) > MAX_MESSAGE_LENGTH:
        text = text[: MAX_MESSAGE_LENGTH - 20] + "\n...(訊息過長已截斷)"

    try:
        resp = requests.post(url, data={"chat_id": chat_id, "text": text}, timeout=10)
        resp.raise_for_status()
        return True
    except requests.RequestException as exc:
        print(f"[telegram] 發送失敗: {exc}")
        return False
