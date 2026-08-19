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
  /rem <serial>               - remove a link by its serial number
  /change <minutes>          - change the auto-check interval for this chat

Requires: pyTelegramBotAPI, requests, pytz
    pip install pyTelegramBotAPI requests pytz
"""

import html
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


def remove_link_by_serial(chat_id: int, serial: int):
    """serial is the 1-based position in the /list ordering for this chat."""
    rows = list_links(chat_id)
    if serial < 1 or serial > len(rows):
        return None
    target = rows[serial - 1]
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


IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg")


def check_url(url: str):
    """Returns (is_working, status_code_or_None, reason_text, content_type_or_None)."""
    try:
        resp = requests.get(
            url, timeout=REQUEST_TIMEOUT_SECONDS, allow_redirects=True,
            headers={"User-Agent": "URLCheckerBot/1.0"},
        )
        is_working = 200 <= resp.status_code < 400
        content_type = resp.headers.get("Content-Type", "")
        return is_working, resp.status_code, resp.reason, content_type
    except requests.RequestException:
        return False, None, "Request failed", None


def looks_like_image(url: str, content_type: str) -> bool:
    if content_type and content_type.lower().startswith("image/"):
        return True
    path = url.split("?", 1)[0].lower()
    return path.endswith(IMAGE_EXTENSIONS)


def is_allowed(chat_id: int) -> bool:
    return chat_id in ALLOWED_CHAT_IDS


# --------------------------------------------------------------------------
# BOT
# --------------------------------------------------------------------------

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")


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
        "👋 Welcome to <b>URL Checker Bot</b>!\n\n"
        "I automatically check links you add and let you know the moment "
        "a link that wasn't working starts working again — with a screenshot "
        "attached if the link is an image.\n\n"
        "🔹 Add a link with /add\n"
        "🔹 See everything you're tracking with /list\n"
        "🔹 Type /help any time to see all commands\n\n"
        f"This chat's ID: <code>{message.chat.id}</code>",
    )


@bot.message_handler(commands=["help"])
@guard
def cmd_help(message):
    interval = get_interval_minutes(message.chat.id)
    bot.reply_to(
        message,
        "<b>Available commands:</b>\n\n"
        "<b>/add</b> &lt;link&gt; - Add a link to monitor\n"
        "  e.g. <code>/add https://example.com</code>\n\n"
        "<b>/list</b> - Show all added links: serial number, status "
        "(Working ✅ / Not Working ❌) and last checked time\n\n"
        "<b>/check</b> &lt;serial&gt; - Check one link right now, on demand\n"
        "  e.g. <code>/check 1</code>\n\n"
        "<b>/rem</b> &lt;serial&gt; - Remove a link by its serial number "
        "(see /list for numbers)\n"
        "  e.g. <code>/rem 1</code>\n\n"
        "<b>/change</b> &lt;minutes&gt; - Change how often links are "
        "auto-checked for this chat\n"
        "  e.g. <code>/change 5</code>\n\n"
        "<b>/interval</b> - Show the current auto-check interval\n\n"
        "<b>/clear</b> - Remove every link you're tracking (asks to confirm)\n\n"
        f"Links are currently auto-checked every <b>{interval} minute(s)</b>.\n"
        "When a link that was down starts working, I'll message you here — "
        "with the image attached if the link points to a picture.",
    )


@bot.message_handler(commands=["add"])
@guard
def cmd_add(message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        bot.reply_to(message, "Usage: /add &lt;link&gt;\nExample: /add https://example.com")
        return
    url = normalize_url(parts[1])
    link_id = add_link(message.chat.id, url)
    serial = link_id_to_serial(message.chat.id, link_id)
    interval = get_interval_minutes(message.chat.id)
    bot.reply_to(
        message,
        f"✅ Added as link #{serial}:\n{html.escape(url)}\n\n"
        f"I'll check it automatically every {interval} minute(s) and message "
        f"you here the moment it starts working.",
    )


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
        bot.reply_to(message, "No links added yet. Use /add &lt;link&gt; to add one.")
        return

    lines = [f"<b>Your monitored links</b> ({len(rows)}):\n"]
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

        # html.escape keeps underscores, asterisks, etc. in the URL intact -
        # Markdown parse mode used to misread "_" in links as italics.
        safe_url = html.escape(r["url"])
        lines.append(
            f"{i}. {safe_url}\n"
            f"   Status: {status_text}\n"
            f"   Last checked: {checked_text}"
        )

    bot.reply_to(message, "\n\n".join(lines))


@bot.message_handler(commands=["rem"])
@guard
def cmd_rem(message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().isdigit():
        bot.reply_to(message, "Usage: /rem &lt;serial number&gt;\nExample: /rem 1\n(Check /list to see current serial numbers.)")
        return

    serial = int(parts[1].strip())
    result = remove_link_by_serial(message.chat.id, serial)

    if result is None:
        bot.reply_to(message, f"❌ No link found at serial #{serial}. Use /list to check current numbers.")
    else:
        bot.reply_to(message, f"🗑️ Removed link #{serial}:\n{html.escape(result['url'])}")


@bot.message_handler(commands=["change"])
@guard
def cmd_change(message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().isdigit():
        bot.reply_to(message, "Usage: /change &lt;minutes&gt;\nExample: /change 5")
        return

    minutes = int(parts[1].strip())
    if minutes < 1:
        bot.reply_to(message, "Please choose a value of 1 minute or more.")
        return

    set_interval_minutes(message.chat.id, minutes)
    bot.reply_to(message, f"⏱️ Check interval updated. Links in this chat will now be checked every {minutes} minute(s).")


@bot.message_handler(commands=["interval"])
@guard
def cmd_interval(message):
    interval = get_interval_minutes(message.chat.id)
    bot.reply_to(message, f"⏱️ Links in this chat are auto-checked every {interval} minute(s).\nChange it with /change &lt;minutes&gt;.")


@bot.message_handler(commands=["check"])
@guard
def cmd_check(message):
    """Check one link right now, on demand, without waiting for the auto-check."""
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().isdigit():
        bot.reply_to(message, "Usage: /check &lt;serial number&gt;\nExample: /check 1")
        return

    serial = int(parts[1].strip())
    rows = list_links(message.chat.id)
    if serial < 1 or serial > len(rows):
        bot.reply_to(message, f"❌ No link found at serial #{serial}. Use /list to check current numbers.")
        return

    row = rows[serial - 1]
    bot.reply_to(message, f"🔍 Checking link #{serial}...")
    perform_check_and_notify(message.chat.id, row["id"], row["url"], row["status"], force_send=True)


@bot.message_handler(commands=["clear"])
@guard
def cmd_clear(message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or parts[1].strip().lower() != "confirm":
        count = len(list_links(message.chat.id))
        bot.reply_to(
            message,
            f"⚠️ This will remove all {count} link(s) you're tracking in this chat.\n"
            f"Type <code>/clear confirm</code> to proceed.",
        )
        return
    with _db_lock, get_conn() as conn:
        conn.execute("DELETE FROM links WHERE chat_id = ?", (message.chat.id,))
        conn.commit()
    bot.reply_to(message, "🗑️ All links cleared.")


# --------------------------------------------------------------------------
# BACKGROUND CHECKER
# --------------------------------------------------------------------------

def build_status_message(url: str, is_working: bool, code, reason: str) -> str:
    if is_working:
        code_text = f"{code} {reason}" if code else "200 OK"
        status_word = "Working ✅"
    else:
        code_text = f"{code} {reason}" if code else "No response"
        status_word = "Not Working ❌"
    return (
        f"Status: {code_text} ({status_word})\n"
        f"URL: {html.escape(url)}\n"
        f"Time: {now_dhaka_12h()}"
    )


def send_status_update(chat_id: int, url: str, is_working: bool, code, reason: str, content_type: str):
    text = build_status_message(url, is_working, code, reason)

    # If it's working and looks like an image, attach the image itself.
    if is_working and looks_like_image(url, content_type or ""):
        try:
            bot.send_photo(chat_id, photo=url, caption=text)
            return
        except Exception as photo_err:
            print(f"[checker] couldn't send as photo, falling back to text: {photo_err}")

    try:
        bot.send_message(chat_id, text)
    except Exception as send_err:
        print(f"[checker] failed to send message to {chat_id}: {send_err}")


def perform_check_and_notify(chat_id: int, link_id: int, url: str, previous_status: str, force_send: bool = False):
    """Runs one check, saves the result, and notifies the chat if warranted.

    Automatic background checks only notify on a down -> working transition.
    force_send=True (used by /check) always notifies, regardless of status.
    """
    is_working, code, reason, content_type = check_url(url)
    now_iso = datetime.utcnow().isoformat()
    new_status = "working" if is_working else "down"
    update_link_status(link_id, new_status, code, now_iso)

    was_working = previous_status == "working"
    if force_send or (is_working and not was_working):
        send_status_update(chat_id, url, is_working, code, reason, content_type)

    return is_working


def checker_loop():
    while True:
        try:
            for row in due_links():
                try:
                    perform_check_and_notify(row["chat_id"], row["id"], row["url"], row["status"])
                except Exception as check_err:
                    print(f"[checker] error checking {row['url']}: {check_err}")
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
