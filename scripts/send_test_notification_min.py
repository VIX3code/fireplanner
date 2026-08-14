#!/usr/bin/env python3
"""Send one Telegram message using an existing pipeline's credentials. Read-only."""
import json, sys, urllib.request, urllib.error
from pathlib import Path

CFG = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("telegram_config.env")
MSG = sys.argv[2] if len(sys.argv) > 2 else "test"

cfg = {}
for line in CFG.read_text().splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    if line.startswith("export "):
        line = line[7:].lstrip()
    k, _, v = line.partition("=")
    v = v.split(" #", 1)[0].strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1]
    cfg[k.strip()] = v

tok = next((cfg[k] for k in ("TELEGRAM_BOT_TOKEN", "BOT_TOKEN", "TELEGRAM_TOKEN",
                             "TG_BOT_TOKEN", "TOKEN") if cfg.get(k)), None)
chat = next((cfg[k] for k in ("TELEGRAM_CHAT_ID", "CHAT_ID", "TG_CHAT_ID",
                              "TELEGRAM_TO") if cfg.get(k)), None)
if not tok or not chat:
    sys.exit("no token/chat_id in %s; keys present: %s" % (CFG, ", ".join(sorted(cfg))))
print("token ...%s (len %d), chat %s" % (tok[-4:], len(tok), chat))

req = urllib.request.Request(
    "https://api.telegram.org/bot%s/sendMessage" % tok,
    data=json.dumps({"chat_id": chat, "text": MSG}).encode(),
    headers={"Content-Type": "application/json"})
try:
    r = json.load(urllib.request.urlopen(req, timeout=20))
    print("ok:", r.get("ok"), "message_id:", r.get("result", {}).get("message_id"))
except urllib.error.HTTPError as e:
    body = e.read().decode(errors="replace")
    d = json.loads(body).get("description", body) if body.startswith("{") else body
    hint = ""
    low = d.lower()
    if e.code == 401 or "unauthorized" in low:
        hint = "  -> bad/revoked bot token"
    elif "chat not found" in low:
        hint = "  -> wrong chat_id, or the bot was never added to that chat"
    elif "blocked" in low or "initiate" in low:
        hint = "  -> open the chat and send /start to the bot, then retry"
    sys.exit("FAILED %s: %s%s" % (e.code, d, hint))
except urllib.error.URLError as e:
    sys.exit("FAILED - could not reach api.telegram.org: %s" % (e.reason,))
