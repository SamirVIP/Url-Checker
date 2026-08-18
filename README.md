# URL Checker Telegram Bot

Monitors links you add and messages the chat the moment a link that
wasn't working starts working again.

## Setup

1. Install dependencies:
   ```
   pip install -r requirements.txt
   ```

2. Create a bot with [@BotFather](https://t.me/BotFather) on Telegram and
   copy the token it gives you.

3. Open `bot.py` and set:
   - `BOT_TOKEN` — the token from BotFather.
   - `ALLOWED_CHAT_IDS` — the chat ID(s) allowed to use the bot. Message
     [@userinfobot](https://t.me/userinfobot) to find your own chat ID
     (or a group's ID, if you want the bot usable in a group).

4. Run it:
   ```
   python bot.py
   ```

The bot keeps running and polling Telegram; keep the process alive
(e.g. with `screen`, `tmux`, `pm2`, or a systemd service / Docker
container on a server) for it to check links continuously.

## Commands

| Command | Description |
|---|---|
| `/start` | Introduces the bot |
| `/help` | Shows all commands |
| `/list` | Shows all added links with serial number, status, and last checked time |
| `/add <link>` | Adds a link to monitor |
| `/rem <serial> <link>` | Removes a link (serial number **and** link must match, as a safety check) |
| `/change <minutes>` | Changes how often this chat's links are auto-checked (e.g. `/change 5`) |

## How checking works

- A background thread wakes up every 15 seconds and checks any link
  whose chat interval has elapsed since it was last checked (default
  interval: 2 minutes, changeable per chat with `/change`).
- A link is checked with an HTTP GET request; a 2xx/3xx status counts
  as "Working", anything else (or a failed request) counts as "Not
  Working".
- A message is sent **only** on the transition from Not Working →
  Working, in this format:
  ```
  Status: 200 OK (Working ✅)
  URL: https://example.com
  Time: 18 Aug 2026, 09:41:12 PM
  ```
  Time is shown in Bangladesh (Asia/Dhaka) time, 12-hour format.
- `/list` always shows the current status and last-checked time for
  every link, whether working or not.

## Data storage

Everything is stored in a local SQLite file, `urlchecker.db`, created
automatically next to `bot.py` the first time you run it. No external
database is required.

## Notes / things you may want to adjust

- The bot only responds inside chats whose ID is in
  `ALLOWED_CHAT_IDS`; anyone else gets a polite "not authorized" reply.
- Serial numbers in `/list` are just the current position in the list
  (1, 2, 3…), so they can shift after a removal — always check `/list`
  before running `/rem`.
- `/change` sets the interval per chat, not globally, so different
  chats (if you allow more than one) can have different check
  frequencies.
