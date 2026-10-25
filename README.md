# Telegram digest

Telegram bot that summarises the last few hours of your channels and returns a
PDF.

## What it does

- Reads channel history with a Telethon user session.
- Sends the posts to an LLM, which groups them into topic sections.
- Posts the result as a PDF, with clickable permalinks back to each story.

## Commands

| Command | Result |
|---|---|
| `/pdf` | digest for the last 6 hours |
| `/pdf 12` | digest for another window |
| `/raw` | plain message list, no LLM |
| `/help` | usage |

## Setup

```bash
pip install -r requirements.txt
python login.py     # one-time Telegram login, prints TELEGRAM_SESSION
cp .env.example .env
python bot.py
```

Fill `.env` with your Telegram API credentials, bot token, chat id, and LLM
endpoint. See `.env.example` for every option.

## Files

| File | Role |
|---|---|
| `bot.py` | long-polling bot, command handling, delivery |
| `main.py` | channel resolution, message collection, plain PDF |
| `summarize.py` | LLM digest and typographic PDF rendering |
| `login.py` | one-time session generator |

## Notes

- Reading channel history needs a user session; a bot token cannot do it.
- Private channel links (`t.me/c/...`) only open for members.
- `.env` holds the session and keys. It is gitignored, keep it private.
