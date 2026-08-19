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
| `/check <serial>` | Checks one link immediately, without waiting for the auto-check |
| `/rem <serial>` | Removes a link by its serial number (see `/list` for current numbers) |
| `/change <minutes>` | Changes how often this chat's links are auto-checked (e.g. `/change 5`) |
| `/interval` | Shows the current auto-check interval for this chat |
| `/clear` | Removes every link in this chat (asks for `/clear confirm` first) |

## How checking works

- A background thread wakes up every 15 seconds and checks any link
  whose chat interval has elapsed since it was last checked (default
  interval: 2 minutes, changeable per chat with `/change`).
- A link is checked with an HTTP GET request; a 2xx/3xx status counts
  as "Working", anything else (or a failed request) counts as "Not
  Working".
- A message is sent **only** on the transition from Not Working →
  Working (unless you used `/check`, which always reports the result),
  in this format:
  ```
  Status: 200 OK (Working ✅)
  URL: https://example.com
  Time: 18 Aug 2026, 09:41:12 PM
  ```
  Time is shown in Bangladesh (Asia/Dhaka) time, 12-hour format.
- **If the working link is an image** (detected by file extension —
  `.jpg`, `.png`, `.gif`, `.webp`, etc. — or by the response's
  `Content-Type` header), the bot sends the image itself as a photo,
  with the status text as the caption, instead of a plain text message.
- `/list` always shows the current status and last-checked time for
  every link, whether working or not.

### Bug fix: underscores disappearing/italicizing in links

Earlier versions used Telegram's Markdown formatting, which treats
`_..._` as *italic*. A link containing underscores (e.g.
`.../BP_GRAND_Prize01_101.jpg`) got its underscores silently eaten by
the parser instead of being shown as-is. The bot now uses HTML
formatting with proper escaping (`&lt;`, `&gt;`, `&amp;`), which
doesn't touch underscores, asterisks, or any other punctuation in
URLs — links are always shown exactly as added.

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
