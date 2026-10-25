"""Telegram bot front-end for the digest.

Long-polls the bot API, and on `/pdf` builds the last N hours of channel
messages and sends the PDF back to the requesting chat.

Run it as a long-lived process, or with `--duration` from an Alwaysdata
scheduled task so it can run on the free plan.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession

from main import (
    Config,
    collect_messages,
    load_config,
    persist_session,
    render_pdf,
    send_document,
)
from summarize import (
    chunk_for_telegram,
    period_label,
    render_digest_pdf,
    summarize,
)

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("lifeos.bot")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telethon").setLevel(logging.WARNING)

API_ROOT = "https://api.telegram.org"
DEFAULT_HOURS = float(os.getenv("DIGEST_DEFAULT_HOURS", "6") or 6)
MAX_HOURS = float(os.getenv("DIGEST_MAX_HOURS", "168") or 168)
OUTPUT_DIR = Path(os.getenv("DIGEST_OUTPUT_DIR", "out")).expanduser()
SEND_TEXT = os.getenv("DIGEST_SEND_TEXT", "0").strip().lower() in {"1", "true", "yes", "on"}

HELP = (
    "Send /pdf to get a digest of the last {default:g} hours as a PDF.\n"
    "Use /pdf 12 for a different window (max {max:g}h).\n"
    "/text also posts the digest as chat text.\n"
    "/raw sends the plain message listing instead of an LLM digest."
)


def allowed_chats(config: Config) -> set[str]:
    raw = os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", "") or os.getenv("TELEGRAM_CHAT_ID", "")
    values = {part.strip() for part in raw.split(",") if part.strip()}
    values.update(config.chat_ids)
    return values


class BotApi:
    def __init__(self, token: str) -> None:
        self.token = token
        self._client = httpx.AsyncClient(timeout=90.0)

    async def close(self) -> None:
        await self._client.aclose()

    async def call(self, method: str, **payload) -> dict:
        try:
            response = await self._client.post(f"{API_ROOT}/bot{self.token}/{method}", json=payload)
            body = response.json() if response.content else {}
        except (httpx.HTTPError, ValueError) as error:
            log.warning("bot api %s failed: %s", method, error)
            return {"ok": False}
        if not body.get("ok"):
            log.warning("bot api %s rejected: %s", method, str(body.get("description"))[:200])
        return body

    async def send_message(self, chat_id: str, text: str) -> None:
        await self.call("sendMessage", chat_id=chat_id, text=text, disable_web_page_preview=True)

    async def get_updates(self, offset: int, timeout: int) -> list[dict]:
        try:
            response = await self._client.post(
                f"{API_ROOT}/bot{self.token}/getUpdates",
                json={"offset": offset, "timeout": timeout, "allowed_updates": ["message"]},
                timeout=timeout + 20,
            )
            body = response.json() if response.content else {}
        except (httpx.HTTPError, ValueError) as error:
            log.warning("getUpdates failed: %s", error)
            await asyncio.sleep(3)
            return []
        if not body.get("ok"):
            description = str(body.get("description") or "")
            if "409" in description or "webhook" in description.lower():
                log.error(
                    "getUpdates conflict: a webhook is still set. "
                    "Run with --detach-webhook once, or delete it with deleteWebhook."
                )
                await asyncio.sleep(10)
            return []
        return body.get("result") or []

    async def detach_webhook(self) -> None:
        result = await self.call("deleteWebhook", drop_pending_updates=False)
        log.info("webhook detached ok=%s", result.get("ok"))


def parse_hours(text: str) -> float | None:
    parts = text.split()
    if len(parts) < 2:
        return DEFAULT_HOURS
    raw = parts[1].replace("h", "").strip()
    try:
        value = float(raw)
    except ValueError:
        return None
    if value <= 0 or value > MAX_HOURS:
        return None
    return value


async def build_digest(
    config: Config,
    tg: TelegramClient,
    hours: float,
    use_llm: bool = True,
) -> tuple[Path, int, str | None]:
    messages, channel_count = await collect_messages(config, tg, hours, per_channel_limit=2000)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")

    if use_llm and messages:
        try:
            markdown, count = await summarize(messages, hours)
            title, start, end = period_label(hours)
            path = OUTPUT_DIR / f"digest-{stamp}-{hours:g}h.pdf"
            subtitle = (
                f"{start:%Y-%m-%d %H:%M} \u2192 {end:%Y-%m-%d %H:%M} \u00b7 "
                f"{count} posts \u00b7 {channel_count} channels"
            )
            render_digest_pdf(markdown, path, subtitle)
            return path, count, markdown
        except Exception as error:  # noqa: BLE001 - fall back to the raw listing
            log.exception("llm digest failed; falling back to raw listing")
            await_message = f"LLM digest failed ({type(error).__name__}), sending raw listing instead."
            log.warning(await_message)

    path = OUTPUT_DIR / f"digest-{stamp}-{hours:g}h.pdf"
    render_pdf(messages, channel_count, hours, path)
    return path, len(messages), None


async def handle_command(
    api: BotApi,
    config: Config,
    tg: TelegramClient,
    chat_id: str,
    text: str,
) -> None:
    command = text.split()[0].split("@")[0].lower()
    if command in {"/start", "/help"}:
        await api.send_message(chat_id, HELP.format(default=DEFAULT_HOURS, max=MAX_HOURS))
        return
    if command not in {"/pdf", "/raw", "/digest", "/text"}:
        await api.send_message(chat_id, "Unknown command.\n\n" + HELP.format(default=DEFAULT_HOURS, max=MAX_HOURS))
        return

    hours = parse_hours(text)
    if hours is None:
        await api.send_message(chat_id, f"Window must be between 0 and {MAX_HOURS:g} hours, e.g. /pdf 6")
        return

    use_llm = command != "/raw"
    want_text = command == "/text" or SEND_TEXT
    await api.send_message(
        chat_id,
        f"Building {'digest' if use_llm else 'listing'} for the last {hours:g}h...",
    )
    try:
        path, count, markdown = await build_digest(config, tg, hours, use_llm=use_llm)
    except Exception as error:  # noqa: BLE001 - report instead of crashing the bot
        log.exception("digest failed")
        await api.send_message(chat_id, f"Digest failed: {type(error).__name__}: {str(error)[:200]}")
        return

    ok, detail = await send_document(
        api._client,
        config.bot_token,
        chat_id,
        path,
        f"Digest - last {hours:g}h - {count} posts",
    )
    if not ok:
        await api.send_message(chat_id, f"Could not send the PDF: {detail}")
        return

    if markdown and want_text:
        for chunk in chunk_for_telegram(markdown):
            await api.send_message(chat_id, chunk)

    log.info(
        "digest delivered chat=%s posts=%s hours=%s llm=%s text=%s",
        chat_id, count, hours, bool(markdown), bool(markdown and want_text),
    )


async def run(args: argparse.Namespace) -> None:
    config = load_config()
    if not config.bot_token:
        raise SystemExit("Missing environment variable: TELEGRAM_BOT_TOKEN")

    api = BotApi(config.bot_token)
    if args.detach_webhook:
        await api.detach_webhook()

    allowed = allowed_chats(config)
    tg = TelegramClient(StringSession(config.session), config.api_id, config.api_hash)
    deadline = time.monotonic() + args.duration if args.duration else None
    offset = 0

    try:
        await tg.connect()
        if not await tg.is_user_authorized():
            raise SystemExit("Session is not authorized. Run login.py and set TELEGRAM_SESSION.")
        me = await tg.get_me()
        log.info("bot ready as @%s session=%s allowed=%s", getattr(me, "username", "?"), getattr(me, "id", "?"), len(allowed))

        while deadline is None or time.monotonic() < deadline:
            timeout = 50 if deadline is None else max(1, min(50, int(deadline - time.monotonic())))
            updates = await api.get_updates(offset, timeout)
            for update in updates:
                offset = max(offset, int(update.get("update_id", 0)) + 1)
                message = update.get("message") or {}
                chat = message.get("chat") or {}
                chat_id = str(chat.get("id", ""))
                text = (message.get("text") or "").strip()
                if not chat_id or not text:
                    continue
                if not allowed:
                    log.warning("no allowlist configured; sender chat_id=%s", chat_id)
                    await api.send_message(
                        chat_id,
                        f"Your chat id is {chat_id}. Add it to TELEGRAM_ALLOWED_CHAT_IDS in .env and restart.",
                    )
                    continue
                if chat_id not in allowed:
                    log.warning("ignoring chat_id=%s (not allowed)", chat_id)
                    continue
                await handle_command(api, config, tg, chat_id, text)
    finally:
        try:
            persist_session(tg.session.save())
        except Exception:  # noqa: BLE001 - shutdown must not fail
            pass
        await tg.disconnect()
        await api.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Telegram bot that returns channel digests as PDFs")
    parser.add_argument(
        "--duration",
        type=int,
        default=None,
        help="Poll for N seconds then exit. Use with Alwaysdata scheduled tasks.",
    )
    parser.add_argument(
        "--detach-webhook",
        action="store_true",
        help="Call deleteWebhook first. Required once if a webhook still points at the old Worker.",
    )
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
