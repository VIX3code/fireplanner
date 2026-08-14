#!/usr/bin/env python3
"""Send one Telegram message reusing an existing pipeline's credentials.

Standalone on purpose: standard library only, no imports from fireplanner or
from any existing pipeline module. Drop it next to your scheduled jobs and it
cannot break them — it only ever *reads* the config file, and writes nothing.

    python3 send_test_notification.py --config telegram_config.env \
        --message "FirePlanner test — please ignore"

    python3 send_test_notification.py --config telegram_config.env \
        --message "dry run" --dry-run          # show what would be sent

Exit codes: 0 sent, 1 config problem, 2 rejected by Telegram, 3 network error.
The token is never printed, logged, or echoed — only a masked fingerprint so you
can confirm the right credentials were picked up.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# Key names seen across common Telegram pipelines. The first one present wins,
# so a config using any of these spellings works without being edited.
TOKEN_KEYS = [
    "TELEGRAM_BOT_TOKEN", "BOT_TOKEN", "TELEGRAM_TOKEN",
    "TG_BOT_TOKEN", "TELEGRAM_API_TOKEN", "TOKEN",
]
CHAT_KEYS = [
    "TELEGRAM_CHAT_ID", "CHAT_ID", "TG_CHAT_ID",
    "TELEGRAM_CHATID", "TELEGRAM_TO", "CHAT",
]
THREAD_KEYS = ["TELEGRAM_THREAD_ID", "MESSAGE_THREAD_ID", "THREAD_ID", "TOPIC_ID"]


def parse_env(path: Path) -> dict[str, str]:
    """Read KEY=VALUE pairs. No interpolation, no shell semantics, no writes."""
    out: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.split(" #", 1)[0].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key.strip()] = value
    return out


def first_present(config: dict[str, str], keys: list[str]) -> tuple[str, str] | tuple[None, None]:
    for k in keys:
        if config.get(k):
            return k, config[k]
    return None, None


def fingerprint(token: str) -> str:
    """Enough to confirm which credential was loaded, useless to an onlooker."""
    bot_id = token.split(":", 1)[0] if ":" in token else "?"
    return f"bot id {bot_id}, token ends …{token[-4:]}, length {len(token)}"


def diagnose(status: int, body: str) -> str:
    """Turn Telegram's response into the actual thing to go fix."""
    try:
        desc = json.loads(body).get("description", body)
    except json.JSONDecodeError:
        desc = body
    low = desc.lower()

    if status == 401 or "unauthorized" in low:
        return ("The bot token is wrong or revoked. Check the token in the config file, "
                "or re-issue it with /token in @BotFather.")
    if "chat not found" in low:
        return ("The chat_id is wrong, or the bot has never been added to that chat. "
                "For a group, add the bot to it; for a channel, make the bot an admin. "
                "Group ids are negative and supergroup ids start -100.")
    if "bot was blocked" in low or "bot can't initiate" in low:
        return ("The bot is blocked, or you have never messaged it. Open the chat and "
                "send /start, then retry.")
    if "not enough rights" in low or "have no rights" in low:
        return "The bot is in the chat but lacks permission to post. Grant it send-message rights."
    if "message thread not found" in low:
        return "The thread/topic id does not exist in that chat. Drop --thread-id or correct it."
    if status == 429 or "too many requests" in low:
        return "Rate limited by Telegram. Wait and retry."
    return f"Telegram rejected it: {desc}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="telegram_config.env",
                    help="path to the existing config file (read-only)")
    ap.add_argument("--message", help="text to send")
    ap.add_argument("--message-file", help="read the text from a file instead")
    ap.add_argument("--chat-id", help="override the chat id from the config")
    ap.add_argument("--thread-id", help="forum topic id, if the chat uses topics")
    ap.add_argument("--parse-mode", default="", choices=["", "HTML", "MarkdownV2"],
                    help="leave empty to send as plain text (safest)")
    ap.add_argument("--dry-run", action="store_true",
                    help="resolve credentials and print the request, send nothing")
    ap.add_argument("--timeout", type=int, default=20)
    args = ap.parse_args(argv)

    # ---- message -------------------------------------------------------
    if args.message_file:
        text = Path(args.message_file).read_text().strip()
    else:
        text = (args.message or "").strip()
    if not text:
        print("error: no message. Pass --message \"...\" or --message-file PATH.", file=sys.stderr)
        return 1
    if text.startswith("<") and text.endswith(">") and "PUT YOUR" in text.upper():
        print(f"error: the message is still the placeholder {text!r} — "
              "replace it with the text you want to send.", file=sys.stderr)
        return 1

    # ---- credentials ---------------------------------------------------
    cfg_path = Path(args.config).expanduser()
    if not cfg_path.exists():
        print(f"error: no config file at {cfg_path}", file=sys.stderr)
        return 1

    config = parse_env(cfg_path)
    token_key, token = first_present(config, TOKEN_KEYS)
    chat_key, chat_id = first_present(config, CHAT_KEYS)
    _, thread_id = first_present(config, THREAD_KEYS)

    chat_id = args.chat_id or chat_id
    thread_id = args.thread_id or thread_id

    if not token:
        print(f"error: no bot token in {cfg_path}. Looked for: {', '.join(TOKEN_KEYS)}.\n"
              f"       Keys present: {', '.join(sorted(config)) or '(none)'}", file=sys.stderr)
        return 1
    if not chat_id:
        print(f"error: no chat id in {cfg_path}. Looked for: {', '.join(CHAT_KEYS)}.\n"
              f"       Keys present: {', '.join(sorted(config)) or '(none)'}", file=sys.stderr)
        return 1

    print(f"  config      {cfg_path}")
    print(f"  token       found as {token_key}  ({fingerprint(token)})")
    print(f"  chat_id     {chat_id}" + (f"  (from {chat_key})" if chat_key and not args.chat_id else ""))
    if thread_id:
        print(f"  thread_id   {thread_id}")
    print(f"  message     {text!r}")

    body = {"chat_id": chat_id, "text": text}
    if args.parse_mode:
        body["parse_mode"] = args.parse_mode
    if thread_id:
        body["message_thread_id"] = int(thread_id)

    if args.dry_run:
        print("\n  dry run — nothing sent.")
        return 0

    # ---- send ----------------------------------------------------------
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=args.timeout) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        print(f"\n  FAILED (HTTP {exc.code})", file=sys.stderr)
        print(f"  {diagnose(exc.code, detail)}", file=sys.stderr)
        return 2
    except urllib.error.URLError as exc:
        print(f"\n  FAILED — could not reach api.telegram.org: {exc.reason}", file=sys.stderr)
        return 3

    if payload.get("ok"):
        result = payload.get("result", {})
        print(f"\n  ok: true")
        print(f"  message_id  {result.get('message_id')}")
        chat = result.get("chat", {}) or {}
        if chat:
            label = chat.get("title") or chat.get("username") or chat.get("first_name") or ""
            print(f"  delivered   {chat.get('type', '?')} {chat.get('id', '')} {label}".rstrip())
        return 0

    print(f"\n  ok: false", file=sys.stderr)
    print(f"  {diagnose(200, json.dumps(payload))}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
