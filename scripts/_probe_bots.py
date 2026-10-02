#!/usr/bin/env python3
"""One-off: verify which local Telegram bot tokens can reach chat 6438086899.

Prints ONLY: source path, bot username, reachability. NEVER prints tokens.
"""
import json
import re
import urllib.parse
import urllib.request
from pathlib import Path

CHAT_ID = "6438086899"
CANDIDATES = [
    ("/home/lab-master/.hermes/.env", "TELEGRAM_BOT_TOKEN"),
    ("/home/lab-master/alpaca-options-agent/.env", "TELEGRAM_BOT_TOKEN"),
    ("/home/lab-master/.hermes/secrets/guardius_telegram_bot_token", None),
]

def api(token, method, **params):
    url = f"https://api.telegram.org/bot{token}/{method}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            return json.load(r)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:80]}

for path, key in CANDIDATES:
    p = Path(path)
    if not p.exists():
        print(f"{path}: MISSING")
        continue
    raw = p.read_text(errors="ignore")
    if key:
        m = re.search(rf'^{key}="?([^"\n]+)"?', raw, re.M)
        tok = m.group(1).strip() if m else ""
    else:
        tok = raw.strip()
    if not re.match(r"^\d+:[A-Za-z0-9_-]{20,}$", tok):
        print(f"{path}: no valid-format token present")
        continue
    me = api(tok, "getMe")
    if not me.get("ok"):
        print(f"{path}: getMe FAILED ({me.get('error', me.get('description'))})")
        continue
    uname = me["result"].get("username", "?")
    chat = api(tok, "getChat", chat_id=CHAT_ID)
    reachable = bool(chat.get("ok"))
    print(f"{path} [{key or 'secret file'}]: bot=@{uname} getMe=ok "
          f"getChat({CHAT_ID})={'ok' if reachable else 'not reachable'}")
