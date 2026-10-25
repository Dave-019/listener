# Telegram digest

Reads recent Telegram channel messages, has an LLM turn them into a curated
markdown digest, and delivers both the digest text and a PDF.

## How it works

1. `main.py` connects with a stored Telethon `StringSession`.
2. It resolves the channels in `TELEGRAM_CHANNELS` (`all` / `*` means every
   joined channel; archived channels are skipped unless
   `TELEGRAM_INCLUDE_ARCHIVED=1`).
3. For each channel it walks `iter_messages` newest-first and stops at the
   time cutoff, keeping text messages only.
4. `summarize.py` sends every post (channel, time, permalink, text) to the LLM,
   which returns a markdown digest grouped by topic.
5. `bot.py` sends the digest as chat messages, then the same digest as a PDF.

## Bot commands

| Command | Result |
|---|---|
| `/pdf` | LLM digest for the default window (6h), then the PDF |
| `/pdf 12` | same, for another window |
| `/raw` | plain message listing with permalinks, no LLM |
| `/help` | usage |

If the LLM call fails, the bot falls back to the raw listing and says so.

## Message ids and links

Every entry shows its message id (`#12345`) and a clickable permalink, so a post
can be opened directly in Telegram:

- public channel: `https://t.me/<username>/<message id>`
- private channel or megagroup: `https://t.me/c/<channel id>/<message id>`

Private links only open for an account that is a member of the channel. If the
post body contains an external URL, that is listed as a second link, so the
permalink is never lost.

## Setup

```bash
py -3.13 -m pip install --user -r requirements.txt
```

Create the user session once. This is interactive and needs a phone number and
the login code:

```bash
python login.py
```

Copy the printed `TELEGRAM_SESSION=` value into `.env`. `login.py` uses
`TELEGRAM_API_ID` / `TELEGRAM_API_HASH` from `.env` and needs
`TELEGRAM_PHONE` when the account has no saved session.

## Usage

```bash
python bot.py                        # long-poll the bot, serve /pdf
python bot.py --duration 240         # poll for N seconds (scheduled task)
python bot.py --detach-webhook       # one-time: remove an old webhook

python main.py                       # last 3 hours -> digest-YYYYmmdd-HHMM.pdf
python main.py --hours 6             # last 6 hours
python main.py --out report.pdf      # explicit output path
python main.py --send                # also upload the PDF through the bot
python main.py --limit 2000          # inspect more messages per channel
```

## LLM digest

`summarize.py` calls an `[OI]`-compatible endpoint and asks for a markdown report
grouped by topic, one bullet per item, each ending with the post permalink. The
prompt forbids inventing or shortening links and requires full URLs in tables.

```text
LLM_BASE_URL=https://api.a6api.com/v1
LLM_API_KEY=
LLM_MODEL=gpt-4o-mini
LLM_MAX_INPUT_CHARS=50000
LLM_PER_MESSAGE_CHARS=700
LLM_MAX_OUTPUT_TOKENS=4000
```

Input is capped by `LLM_MAX_INPUT_CHARS`; each post is truncated to
`LLM_PER_MESSAGE_CHARS`. If posts exceed the budget the oldest are dropped and
the model is told how many were omitted.

## Deploy (alwaysdata)

```bash
scp *.py requirements.txt bemusic-ssh-bemusic-user:~/lifeos/apps/listener/
ssh bemusic-ssh-bemusic-user
cd ~/lifeos/apps/listener && .venv/bin/pip install -r requirements.txt
./keepalive.sh          # starts bot.py if not running; safe to repeat
tail -f bot.log
```

## Notes

- Reading channel history requires a **user** session. A bot token cannot read
  channel history, so `TELEGRAM_BOT_TOKEN` is only used to deliver the PDF.
- The bot can only message a chat that has started it, and it can never message
  itself. `TELEGRAM_CHAT_ID` must be a real user or group id, not the bot id.
- `TELEGRAM_SESSION` is rewritten into `.env` on exit, so keep `.env` private.
- Non-latin text and emoji need a Unicode TTF. The renderer tries Segoe UI,
  Arial, and DejaVu Sans, and falls back to Helvetica with a warning.
- Messages starting with `/` are included; only empty messages are skipped.
- The PDF groups by channel, so channel order follows the earliest message in
  the window, not the channel list.
