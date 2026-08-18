"""
URL Checker Telegram Bot
=========================

Monitors a list of URLs per chat and notifies the chat when a
previously-down URL starts working again. Only responds to chat IDs
listed in ALLOWED_CHAT_IDS.

Commands:
  /start                    - introduce the bot
  /help                     - show all commands
  /list                     - show all added links (serial #, status, last checked)
  /add <link>                - add a link to be checked
  /rem <serial> <link>       - remove a link (serial + link must match)
  /change <minutes>          - change the auto-check interval for this chat

Requires: pyTelegramBotAPI, requests, pytz
    pip install pyTelegramBotAPI requests pytz
"""

import re
import sqlite3
import threading
import time
from datetime import datetime

import pytz
import requests
import telebot
from telebot import types

# --------------------------------------------------------------------------
# CONFIG - edit these before running
# --------------------------------------------------------------------------

BOT_TOKEN = "PUT_YOUR_BOT_TOKEN_HERE"

# Only these chat IDs may use the bot. Add your own chat id (a group id or
# your personal user id - message @userinfobot on Telegram to find yours).
ALLOWED_CHAT_IDS = {
    123456789,  # <-- replace with your real chat id(s)
}

DB_PATH = "urlchecker.db"
DEFAULT_INTERVAL_MINUTES = 2
CHECKER_TICK_SECONDS = 15     # how often the background loop wakes up to look for due checks
REQUEST_TIMEOUT_SECONDS = 10
DHAKA_TZ = pytz.timezone("Asia/Dhaka")

# --------------------------------------------------------------------------
# DATABASE
# --------------------------------------------------------------------------

_db_lock = threading.Lock()


def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _db_lock, get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                url TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'unknown',   -- 'working' | 'down' | 'unknown'
                status_code INTEGER,
                last_checked TEXT,
                added_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                chat_id INTEGER PRIMARY KEY,
                interval_minutes INTEGER NOT NULL DEFAULT 2
            )
            """
        )
        conn.commit()


def get_interval_minutes(chat_id: int) -> int:
    with _db_lock, get_conn() as conn:
        row = conn.execute(
            "SELECT interval_minutes FROM settings WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        return row["interval_minutes"] if row else DEFAULT_INTERVAL_MINUTES


def set_interval_minutes(chat_id: int, minutes: int) -> None:
    with _db_lock, get_conn() as conn:
        conn.execute(
            """
            INSERT INTO settings (chat_id, interval_minutes) VALUES (?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET interval_minutes = excluded.interval_minutes
            """,
            (chat_id, minutes),
        )
        conn.commit()


def add_link(chat_id: int, url: str) -> int:
    now = datetime.utcnow().isoformat()
    with _db_lock, get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO links (chat_id, url, status, last_checked, added_at) "
            "VALUES (?, ?, 'unknown', NULL, ?)",
            (chat_id, url, now),
        )
        conn.commit()
        return cur.lastrowid


def list_links(chat_id: int):
    with _db_lock, get_conn() as conn:
        return conn.execute(
            "SELECT id, url, status, status_code, last_checked FROM links "
            "WHERE chat_id = ? ORDER BY id ASC",
            (chat_id,),
        ).fetchall()


def remove_link_by_serial(chat_id: int, serial: int, url: str):
    """serial is the 1-based position in the /list ordering for this chat."""
    rows = list_links(chat_id)
    if serial < 1 or serial > len(rows):
        return None
    target = rows[serial - 1]
    if target["url"].strip().lower() != url.strip().lower():
        return "mismatch"
    with _db_lock, get_conn() as conn:
        conn.execute("DELETE FROM links WHERE id = ?", (target["id"],))
        conn.commit()
    return target


def update_link_status(link_id: int, status: str, status_code, checked_at_iso: str):
    with _db_lock, get_conn() as conn:
        conn.execute(
            "UPDATE links SET status = ?, status_code = ?, last_checked = ? WHERE id = ?",
            (status, status_code, checked_at_iso, link_id),
        )
        conn.commit()


def due_links():
    """Return links whose chat interval has elapsed since last_checked (or never checked)."""
    now_utc = datetime.utcnow()
    with _db_lock, get_conn() as conn:
        rows = conn.execute(
            "SELECT l.id, l.chat_id, l.url, l.status, l.last_checked, "
            "COALESCE(s.interval_minutes, ?) AS interval_minutes "
            "FROM links l LEFT JOIN settings s ON s.chat_id = l.chat_id",
            (DEFAULT_INTERVAL_MINUTES,),
        ).fetchall()

    result = []
    for row in rows:
        if row["last_checked"] is None:
            result.append(row)
            continue
        last_checked = datetime.fromisoformat(row["last_checked"])
        elapsed_minutes = (now_utc - last_checked).total_seconds() / 60.0
        if elapsed_minutes >= row["interval_minutes"]:
            result.append(row)
    return result


# --------------------------------------------------------------------------
# HELPERS
# --------------------------------------------------------------------------

URL_RE = re.compile(r"^https?://", re.IGNORECASE)


def normalize_url(raw: str) -> str:
    raw = raw.strip()
    if not URL_RE.match(raw):
        raw = "https://" + raw
    return raw


def now_dhaka_12h() -> str:
    return datetime.now(DHAKA_TZ).strftime("%d %b %Y, %I:%M:%S %p")


def check_url(url: str):
    """Returns (is_working, status_code_or_None, reason_text)."""
    try:
        resp = requests.get(
            url, timeout=REQUEST_TIMEOUT_SECONDS, allow_redirects=True,
            headers={"User-Agent": "URLCheckerBot/1.0"},
        )
        is_working = 200 <= resp.status_code < 400
        return is_working, resp.status_code, resp.reason
    except requests.RequestException:
        return False, None, "Request failed"


def is_allowed(chat_id: int) -> bool:
    return chat_id in ALLOWED_CHAT_IDS


# --------------------------------------------------------------------------
# BOT
# --------------------------------------------------------------------------

bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None)


def guard(handler):
    """Decorator: ignore/reject messages from chats not in ALLOWED_CHAT_IDS."""

    def wrapped(message):
        if not is_allowed(message.chat.id):
            bot.reply_to(
                message,
                f"⛔ This bot is restricted. Your chat ID ({message.chat.id}) is not authorized.",
            )
            return
        return handler(message)

    return wrapped


@bot.message_handler(commands=["start"])
@guard
def cmd_start(message):
    bot.reply_to(
        message,
        "👋 Welcome to *URL Checker Bot*!\n\n"
        "I automatically check links you add and let you know the moment "
        "a link that wasn't working starts working again.\n\n"
        "Type /help to see all commands.",
        parse_mode="Markdown",
    )


@bot.message_handler(commands=["help"])
@guard
def cmd_help(message):
    interval = get_interval_minutes(message.chat.id)
    bot.reply_to(
        message,
        "*Available commands:*\n\n"
        "/add <link> - Add a link to monitor\n"
        "  e.g. /add https://example.com\n\n"
        "/list - Show all added links, their serial number, status "
        "(Working ✅ / Not Working ❌) and last checked time\n\n"
        "/rem <serial> <link> - Remove a link\n"
        "  e.g. /rem 2 https://example.com\n\n"
        "/change <minutes> - Change how often links are auto-checked "
        "for this chat\n"
        "  e.g. /change 5\n\n"
        f"Links are currently checked automatically every *{interval} minute(s)*.\n"
        "When a link that was down starts working, I'll message you here.",
        parse_mode="Markdown",
    )


@bot.message_handler(commands=["add"])
@guard
def cmd_add(message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        bot.reply_to(message, "Usage: /add <link>\nExample: /add https://example.com")
        return
    url = normalize_url(parts[1])
    link_id = add_link(message.chat.id, url)
    bot.reply_to(message, f"✅ Added link #{link_id_to_serial(message.chat.id, link_id)}: {url}\nIt will be checked automatically.")


def link_id_to_serial(chat_id, link_id) -> int:
    rows = list_links(chat_id)
    for i, r in enumerate(rows, start=1):
        if r["id"] == link_id:
            return i
    return -1


@bot.message_handler(commands=["list"])
@guard
def cmd_list(message):
    rows = list_links(message.chat.id)
    if not rows:
        bot.reply_to(message, "No links added yet. Use /add <link> to add one.")
        return

    lines = ["*Your monitored links:*\n"]
    for i, r in enumerate(rows, start=1):
        if r["status"] == "working":
            status_text = "Working ✅"
        elif r["status"] == "down":
            status_text = "Not Working ❌"
        else:
            status_text = "Not checked yet ⏳"

        if r["last_checked"]:
            checked_utc = datetime.fromisoformat(r["last_checked"])
            checked_dhaka = pytz.utc.localize(checked_utc).astimezone(DHAKA_TZ)
            checked_text = checked_dhaka.strftime("%d %b %Y, %I:%M:%S %p")
        else:
            checked_text = "Never"

        lines.append(
            f"{i}. {r['url']}\n"
            f"   Status: {status_text}\n"
            f"   Last checked: {checked_text}"
        )

    bot.reply_to(message, "\n\n".join(lines), parse_mode="Markdown")


@bot.message_handler(commands=["rem"])
@guard
def cmd_rem(message):
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3 or not parts[1].isdigit():
        bot.reply_to(message, "Usage: /rem <serial number> <link>\nExample: /rem 2 https://example.com")
        return

    serial = int(parts[1])
    url = normalize_url(parts[2])
    result = remove_link_by_serial(message.chat.id, serial, url)

    if result is None:
        bot.reply_to(message, f"❌ No link found at serial #{serial}. Use /list to check current numbers.")
    elif result == "mismatch":
        bot.reply_to(
            message,
            f"❌ Serial #{serial} doesn't match that link. Use /list to confirm the correct serial and URL.",
        )
    else:
        bot.reply_to(message, f"🗑️ Removed link #{serial}: {result['url']}")


@bot.message_handler(commands=["change"])
@guard
def cmd_change(message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().isdigit():
        bot.reply_to(message, "Usage: /change <minutes>\nExample: /change 5")
        return

    minutes = int(parts[1].strip())
    if minutes < 1:
        bot.reply_to(message, "Please choose a value of 1 minute or more.")
        return

    set_interval_minutes(message.chat.id, minutes)
    bot.reply_to(message, f"⏱️ Check interval updated. Links in this chat will now be checked every {minutes} minute(s).")


# --------------------------------------------------------------------------
# BACKGROUND CHECKER
# --------------------------------------------------------------------------

def checker_loop():
    while True:
        try:
            for row in due_links():
                is_working, code, reason = check_url(row["url"])
                now_iso = datetime.utcnow().isoformat()
                new_status = "working" if is_working else "down"
                was_working = row["status"] == "working"

                update_link_status(row["id"], new_status, code, now_iso)

                # Notify only on a down -> working transition
                if is_working and not was_working:
                    code_text = f"{code} {reason}" if code else "200 OK"
                    text = (
                        "Status: {code_text} (Working ✅)\n"
                        "URL: {url}\n"
                        "Time: {time}"
                    ).format(code_text=code_text, url=row["url"], time=now_dhaka_12h())
                    try:
                        bot.send_message(row["chat_id"], text)
                    except Exception as send_err:
                        print(f"[checker] failed to send message to {row['chat_id']}: {send_err}")
        except Exception as loop_err:
            print(f"[checker] error: {loop_err}")

        time.sleep(CHECKER_TICK_SECONDS)


# --------------------------------------------------------------------------
# ENTRYPOINT
# --------------------------------------------------------------------------

def main():
    init_db()

    checker_thread = threading.Thread(target=checker_loop, daemon=True)
    checker_thread.start()

    print("URL Checker Bot is running...")
    bot.infinity_polling(skip_pending=True)


if __name__ == "__main__":
    main()
