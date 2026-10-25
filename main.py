"""Life OS Telegram digest.

Connects to Telegram with a stored StringSession, pulls every text message
posted to the configured channels in the last N hours, and renders it as a
PDF. Nothing is stored between runs and no external service is called.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.sax.saxutils import escape

import httpx
from dotenv import load_dotenv
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    HRFlowable,
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
)
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import Channel, Chat, User

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("lifeos.digest")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telethon").setLevel(logging.WARNING)

URL_RE = re.compile(r"https?://[^\s<>\"')]+", re.IGNORECASE)
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
NON_BMP_RE = re.compile(r"[^\u0000-\uffff]")
SPACE_RE = re.compile(r"[ \t\u00a0]+")


@dataclass
class Config:
    api_id: int
    api_hash: str
    session: str
    channels: list[str]
    bot_token: str = ""
    chat_ids: list[str] = field(default_factory=list)


@dataclass
class Message:
    channel: str
    message_id: int
    text: str
    url: str | None
    published: datetime
    permalink: str | None = None


def load_config() -> Config:
    api_id = os.getenv("TELEGRAM_API_ID", "")
    api_hash = os.getenv("TELEGRAM_API_HASH", "")
    session = os.getenv("TELEGRAM_SESSION", "")
    channels = [part.strip() for part in os.getenv("TELEGRAM_CHANNELS", "").split(",") if part.strip()]
    missing = [
        name
        for name, value in [
            ("TELEGRAM_API_ID", api_id),
            ("TELEGRAM_API_HASH", api_hash),
            ("TELEGRAM_SESSION", session),
        ]
        if not value
    ]
    if missing:
        raise SystemExit(f"Missing environment variables: {', '.join(missing)}")
    return Config(
        api_id=int(api_id),
        api_hash=api_hash,
        session=session,
        channels=channels,
        bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        chat_ids=[part.strip() for part in os.getenv("TELEGRAM_CHAT_ID", "").split(",") if part.strip()],
    )


def peer_label(entity: object) -> str:
    if isinstance(entity, (Channel, Chat)):
        username = getattr(entity, "username", None)
        return f"@{username}" if username else getattr(entity, "title", "telegram")
    if isinstance(entity, User):
        return f"@{entity.username}" if entity.username else entity.first_name or "telegram"
    return "telegram"


def message_url(entity: object, message_id: int) -> str | None:
    """Deep link to a single post.

    Public channels use the username form. Private channels and megagroups have
    no username, so they use the internal form `t.me/c/<channel id>/<message id>`,
    which opens in any Telegram client where the account is a member.
    """
    username = getattr(entity, "username", None)
    if username:
        return f"https://t.me/{username}/{message_id}"
    identifier = getattr(entity, "id", None)
    if isinstance(entity, Channel) and isinstance(identifier, int) and identifier:
        return f"https://t.me/c/{identifier}/{message_id}"
    return None


def persist_session(session: str) -> None:
    path = Path(os.getenv("ENV_FILE", ".env"))
    if not path.exists() or not session:
        return
    existing = path.read_text(encoding="utf-8")
    line = f"TELEGRAM_SESSION={session}"
    if re.search(r"^TELEGRAM_SESSION=.*$", existing, flags=re.MULTILINE):
        updated = re.sub(r"^TELEGRAM_SESSION=.*$", line, existing, flags=re.MULTILINE)
    else:
        updated = existing.rstrip("\n") + "\n" + line + "\n"
    if updated != existing:
        path.write_text(updated, encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass


def sanitize(value: str) -> str:
    text = CONTROL_RE.sub(" ", value)
    text = NON_BMP_RE.sub("", text)
    return SPACE_RE.sub(" ", text).strip()


def font_candidates() -> list[Path]:
    return [
        Path("C:/Windows/Fonts/segoeui.ttf"),
        Path("C:/Windows/Fonts/arial.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/TTF/DejaVuSans.ttf"),
        Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
    ]


def register_body_font() -> tuple[str, bool]:
    for candidate in font_candidates():
        if not candidate.exists():
            continue
        try:
            pdfmetrics.registerFont(TTFont("DigestBody", str(candidate)))
            return "DigestBody", True
        except Exception as error:  # noqa: BLE001 - try the next candidate
            log.debug("cannot register font %s: %s", candidate, error)
    log.warning("no unicode font found; falling back to Helvetica (latin-1 only)")
    return "Helvetica", False


def include_archived() -> bool:
    raw = os.getenv("TELEGRAM_INCLUDE_ARCHIVED", "0").strip().lower()
    return raw in {"1", "true", "yes", "on"}


async def resolve_entities(config: Config, client: TelegramClient) -> list:
    entities = []
    if not config.channels or any(channel.lower() in {"all", "*"} for channel in config.channels):
        seen: set[int] = set()
        keep_archived = include_archived()
        archived = 0
        async for dialog in client.iter_dialogs():
            entity = dialog.entity
            if getattr(dialog, "archived", False):
                if not keep_archived:
                    continue
                archived += 1
            if not isinstance(entity, Channel) or getattr(entity, "left", False):
                continue
            identifier = int(getattr(entity, "id", 0))
            if identifier in seen:
                continue
            seen.add(identifier)
            entities.append(entity)
        log.info(
            "resolved all joined channels count=%s archived=%s include_archived=%s",
            len(entities),
            archived,
            keep_archived,
        )
    else:
        for channel in config.channels:
            try:
                entities.append(await client.get_entity(channel))
            except Exception as error:  # noqa: BLE001 - keep other channels alive
                log.error("cannot resolve channel %s: %s", channel, error)
    return entities


async def collect_messages(
    config: Config,
    client: TelegramClient,
    hours: float,
    per_channel_limit: int,
) -> tuple[list[Message], int]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    entities = await resolve_entities(config, client)
    collected: list[Message] = []
    for entity in entities:
        count = 0
        async for message in client.iter_messages(entity, limit=per_channel_limit):
            published = message.date
            if not isinstance(published, datetime):
                continue
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
            if published < cutoff:
                break
            text = (message.message or "").strip()
            if not text:
                continue
            urls = URL_RE.findall(text)
            external = urls[0].rstrip(".,;:!?") if urls else None
            collected.append(
                Message(
                    channel=peer_label(entity),
                    message_id=message.id,
                    text=text,
                    url=external,
                    published=published.astimezone(),
                    permalink=message_url(entity, message.id),
                )
            )
            count += 1
        log.info("collected channel=%s messages=%s", peer_label(entity), count)
    collected.sort(key=lambda item: item.published)
    return collected, len(entities)


def build_styles(font: str) -> dict[str, ParagraphStyle]:
    return {
        "title": ParagraphStyle(
            "DigestTitle",
            fontName=font,
            fontSize=19,
            leading=23,
            alignment=TA_LEFT,
            textColor=colors.HexColor("#111827"),
            spaceAfter=2,
        ),
        "meta": ParagraphStyle(
            "DigestMeta",
            fontName=font,
            fontSize=9,
            leading=13,
            textColor=colors.HexColor("#6b7280"),
        ),
        "channel": ParagraphStyle(
            "DigestChannel",
            fontName=font,
            fontSize=12.5,
            leading=16,
            textColor=colors.HexColor("#1f2937"),
            spaceBefore=14,
            spaceAfter=4,
        ),
        "time": ParagraphStyle(
            "DigestTime",
            fontName=font,
            fontSize=8,
            leading=11,
            textColor=colors.HexColor("#9ca3af"),
        ),
        "body": ParagraphStyle(
            "DigestBody",
            fontName=font,
            fontSize=9.5,
            leading=13.5,
            textColor=colors.HexColor("#111827"),
            spaceBefore=2,
        ),
        "link": ParagraphStyle(
            "DigestLink",
            fontName=font,
            fontSize=7.5,
            leading=10,
            textColor=colors.HexColor("#2563eb"),
            spaceBefore=1,
        ),
    }


def page_footer(font: str, label: str):  # type: ignore[no-untyped-def]
    def draw(canvas, doc) -> None:  # type: ignore[no-untyped-def]
        canvas.saveState()
        canvas.setFont(font, 7.5)
        canvas.setFillColor(colors.HexColor("#9ca3af"))
        canvas.drawString(18 * mm, 10 * mm, label)
        canvas.drawRightString(A4[0] - 18 * mm, 10 * mm, f"Page {doc.page}")
        canvas.restoreState()

    return draw


def render_pdf(messages: list[Message], channel_count: int, hours: float, out_path: Path) -> None:
    font, unicode_ok = register_body_font()
    styles = build_styles(font)

    def text(value: str) -> str:
        cleaned = sanitize(value)
        if not unicode_ok:
            cleaned = cleaned.encode("latin-1", "replace").decode("latin-1")
        return escape(cleaned)

    generated = datetime.now().astimezone()
    window_start = generated - timedelta(hours=hours)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    doc = SimpleDocTemplate(
        str(out_path),
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=18 * mm,
        title=f"Telegram digest - last {hours:g}h",
        author="lifeos-listener",
    )

    story: list = [
        Paragraph(f"Telegram digest &mdash; last {hours:g} hours", styles["title"]),
        Paragraph(
            f"{window_start:%Y-%m-%d %H:%M} &rarr; {generated:%Y-%m-%d %H:%M} "
            f"({generated.tzname() or 'local'}) &middot; {len(messages)} messages "
            f"&middot; {channel_count} channels",
            styles["meta"],
        ),
        Spacer(1, 6),
        HRFlowable(width="100%", thickness=0.7, color=colors.HexColor("#e5e7eb")),
    ]

    if not messages:
        story.append(Spacer(1, 18))
        story.append(Paragraph("No messages in this window.", styles["body"]))
    else:
        grouped: dict[str, list[Message]] = {}
        for item in messages:
            grouped.setdefault(item.channel, []).append(item)

        for channel, items in grouped.items():
            story.append(Paragraph(f"{text(channel)} &middot; {len(items)}", styles["channel"]))
            for index, item in enumerate(items):
                block: list = [
                    Paragraph(
                        f"{item.published:%Y-%m-%d %H:%M} &middot; #{item.message_id}",
                        styles["time"],
                    ),
                    Paragraph(text(item.text).replace("\n", "<br/>"), styles["body"]),
                ]
                links: list[str] = []
                if item.permalink:
                    links.append(
                        f'<link href="{escape(item.permalink)}">{text(item.permalink)}</link>'
                    )
                if item.url and item.url != item.permalink:
                    links.append(f'<link href="{escape(item.url)}">{text(item.url)}</link>')
                if links:
                    block.append(Paragraph("<br/>".join(links), styles["link"]))
                story.append(KeepTogether(block))
                if index < len(items) - 1:
                    story.append(
                        HRFlowable(
                            width="100%",
                            thickness=0.4,
                            color=colors.HexColor("#f3f4f6"),
                            spaceBefore=6,
                            spaceAfter=6,
                        )
                    )
            story.append(Spacer(1, 6))

    label = f"lifeos-listener \u00b7 {generated:%Y-%m-%d %H:%M}"
    doc.build(story, onFirstPage=page_footer(font, label), onLaterPages=page_footer(font, label))
    log.info("pdf written path=%s messages=%s bytes=%s", out_path, len(messages), out_path.stat().st_size)


async def send_document(
    client: httpx.AsyncClient,
    token: str,
    chat_id: str,
    path: Path,
    caption: str,
) -> tuple[bool, str]:
    try:
        with path.open("rb") as stream:
            response = await client.post(
                f"https://api.telegram.org/bot{token}/sendDocument",
                data={"chat_id": chat_id, "caption": caption[:1000]},
                files={"document": (path.name, stream, "application/pdf")},
            )
        body = response.json() if response.content else {}
    except (httpx.HTTPError, ValueError) as error:
        return False, f"{type(error).__name__}: {error}"
    if response.status_code == 200 and body.get("ok"):
        return True, "ok"
    return False, str(body.get("description") or f"HTTP {response.status_code}")[:300]


async def deliver_pdf(
    config: Config,
    path: Path,
    caption: str,
    chat_ids: list[str] | None = None,
) -> bool:
    targets = chat_ids if chat_ids is not None else config.chat_ids
    if not config.bot_token or not targets:
        log.warning("bot delivery skipped: TELEGRAM_BOT_TOKEN or chat id is empty")
        return False
    delivered = False
    async with httpx.AsyncClient(timeout=120.0) as client:
        for chat_id in targets:
            ok, detail = await send_document(client, config.bot_token, chat_id, path, caption)
            if ok:
                log.info("pdf delivered chat=%s", chat_id)
                delivered = True
            else:
                log.error("pdf delivery failed chat=%s: %s", chat_id, detail)
    return delivered


async def run(args: argparse.Namespace) -> None:
    config = load_config()
    client = TelegramClient(StringSession(config.session), config.api_id, config.api_hash)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise SystemExit("Session is not authorized. Run login.py and set TELEGRAM_SESSION.")
        messages, channel_count = await collect_messages(
            config, client, args.hours, args.limit
        )
        out_path = Path(args.out).expanduser() if args.out else Path(
            f"digest-{datetime.now():%Y%m%d-%H%M}.pdf"
        )
        render_pdf(messages, channel_count, args.hours, out_path)
        log.info("done path=%s", out_path)
        if args.send:
            caption = f"Telegram digest - last {args.hours:g}h - {len(messages)} messages"
            if not await deliver_pdf(config, out_path, caption):
                raise SystemExit("PDF was written but delivery to Telegram failed.")
    finally:
        try:
            persist_session(client.session.save())
        except Exception:  # noqa: BLE001 - shutdown must not fail
            pass
        await client.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description="Print the last N hours of Telegram channel messages as a PDF")
    parser.add_argument(
        "--hours",
        type=float,
        default=3.0,
        help="How far back to read, in hours (default: 3).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=500,
        help="Maximum messages to inspect per channel (default: 500).",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Output PDF path (default: digest-YYYYmmdd-HHMM.pdf in the current directory).",
    )
    parser.add_argument(
        "--send",
        action="store_true",
        help="Upload the finished PDF to TELEGRAM_CHAT_ID using TELEGRAM_BOT_TOKEN.",
    )
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
