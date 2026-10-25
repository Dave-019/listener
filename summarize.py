"""LLM digest: turn collected Telegram messages into a curated markdown report.

The model receives every post with its channel, time, message id and permalink,
and must return exactly one entry per post, without merging, dropping or
inferring anything. The markdown is then rendered with a sans/serif typographic
hierarchy and numbered entries (no bullet glyphs).

Markdown contract produced by the model and consumed by `render_digest_pdf`:

    # Speed run YYYY-MM-DD period N

    ## Section name

    ### Entry title
    @channel · 2026-10-25 13:38 · #6464
    Body text, faithful to the post.
    https://t.me/hack_less/6464
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from xml.sax.saxutils import escape

import httpx
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    HRFlowable,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

from main import Message, sanitize

log = logging.getLogger("lifeos.llm")

DEFAULT_MODEL = os.getenv("LLM_MODEL", "deepseek-v4.1-flash")
BASE_URL = os.getenv("LLM_BASE_URL", "").rstrip("/")
MAX_INPUT_CHARS = int(os.getenv("LLM_MAX_INPUT_CHARS", "120000") or 120000)
PER_MESSAGE_CHARS = int(os.getenv("LLM_PER_MESSAGE_CHARS", "1200") or 1200)
MAX_OUTPUT_TOKENS = int(os.getenv("LLM_MAX_OUTPUT_TOKENS", "8000") or 8000)
BATCH_POSTS = int(os.getenv("LLM_BATCH_POSTS", "20") or 20)

SECTION_ORDER = [
    "Tech & Security News",
    "Cyber Threat Intel",
    "Infrastructure / Ops",
    "Jobs",
    "Learning",
    "Other",
]

INK = colors.HexColor("#111827")
SECONDARY = colors.HexColor("#4b5563")
MUTED = colors.HexColor("#9ca3af")
ACCENT = colors.HexColor("#b45309")
RULE = colors.HexColor("#e5e7eb")
LINK = colors.HexColor("#1d4ed8")

SYSTEM_PROMPT = """You are an editor writing a dense digest of the last {hours:g} hours of \
Telegram channel posts. The reader wants to scan what happened, fast.

Core rule: summarise, do not rewrite. Each bullet is a compressed fact line, not a \
reproduction of the post. Cut marketing, self-description, greetings and filler. Keep \
names, numbers, versions and identifiers.

Output format, exactly:

# Speed run YYYY-MM-DD period N

## Section name
- **Short title**: one or two clauses of hard facts, separated by semicolons. · https://permalink
- **Short title**: facts. · https://permalink (post: https://second-permalink)

Rules:
- One bullet per story, never one bullet per post. If several posts cover the same \
story, write a single bullet and cite each permalink: the main one after the middle \
dot, the rest in parentheses as `(post: URL)`.
- A bullet is one line. No sub-bullets, no paragraphs, no blank lines inside a bullet.
- Title: at most 12 words, factual, no trailing colon, no editorialising.
- The summary after the colon must be dense and concrete. Prefer enumerating real \
details (versions, RFC numbers, CVE ids, subnets, salaries, deadlines) over describing \
what the post is about.
- NEVER use the em dash character (U+2014) or the en dash character (U+2013) as \
punctuation. Use a colon, semicolon, comma, parentheses, or the middle dot instead. \
Hyphens in ranges such as 90,000-130,000 stay as plain ASCII hyphens.
- Never invent. No fact may appear that is not in the posts. Do not explain what a \
product, company or acronym is. Do not add background, history, or implications.
- Never translate. If a post is not in English, summarise its facts in English but do \
not include the original text and do not add translation notes, transliterations or \
the source language name.
- Copy exact values character for character: names, versions, prices, salaries, dates, \
CVE ids, IP addresses, subnets, file sizes.
- Omit noise entirely: greetings, single characters, bare punctuation, thanks, reaction \
spam, and posts that are only a title with no information. A short digest is correct.
- Sections, in this order, only when non-empty: Tech & Security News, \
Cyber Threat Intel, Infrastructure / Ops, Jobs, Learning, Other.
- For a set of parallel vacancies or listings, use a section heading plus a markdown \
table with a header row. Every cell must contain only values present in the posts. Put \
the apply link in its own column as a bare URL.
- Do not add any closing note, comparison with earlier periods, commentary about the \
digest, counts, or window metadata. The digest ends with the last bullet.
- Never write filler such as "no further details given", "details unavailable", \
"not specified", or "see post". If a post carries nothing beyond its headline, output \
the bullet as `- **Headline**: · https://permalink` or simply \
`- **Headline** · https://permalink` with no body text.

No preamble, no closing remarks, no code fences, no numbered lists, no headings other \
than the ones specified."""




def period_label(hours: float, now: datetime | None = None) -> tuple[str, datetime, datetime]:
    current = now or datetime.now().astimezone()
    start = current - timedelta(hours=hours)
    period = int(start.hour // max(hours, 1)) + 1
    return f"Speed run {start:%Y-%m-%d} period {period}", start, current


def build_input(messages: list[Message]) -> tuple[str, int]:
    budget = MAX_INPUT_CHARS
    used = 0
    omitted = 0
    blocks: list[str] = []
    for item in messages:
        body = sanitize(item.text)
        if len(body) > PER_MESSAGE_CHARS:
            body = body[: PER_MESSAGE_CHARS - 1].rstrip() + "\u2026"
        block = (
            f"channel: {item.channel}\n"
            f"time: {item.published:%Y-%m-%d %H:%M}\n"
            f"message_id: {item.message_id}\n"
            f"permalink: {item.permalink or item.url or 'none'}\n"
            f"text: {body}"
        )
        if used + len(block) > budget:
            omitted += 1
            continue
        blocks.append(block)
        used += len(block)
    return "\n\n---\n\n".join(blocks), omitted


async def call_model(prompt: str, model: str, timeout: float = 420.0) -> str:
    api_key = os.getenv("LLM_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("LLM_API_KEY is not set")
    if not BASE_URL:
        raise RuntimeError("LLM_BASE_URL is not set")
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": MAX_OUTPUT_TOKENS,
        "temperature": 0.1,
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    last_error = "unknown error"
    for attempt in range(4):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(f"{BASE_URL}/chat/completions", json=payload, headers=headers)
            if response.status_code in (401, 403):
                raise RuntimeError(f"LLM auth rejected ({response.status_code}): {response.text[:200]}")
            if response.status_code >= 500 or response.status_code == 429:
                last_error = f"HTTP {response.status_code}: {response.text[:200]}"
                await asyncio.sleep(2**attempt)
                continue
            response.raise_for_status()
            body = response.json()
            choices = body.get("choices") or []
            if not choices:
                raise RuntimeError(f"LLM returned no choices: {str(body)[:200]}")
            choice = choices[0]
            message = choice.get("message") or {}
            content = (message.get("content") or "").strip()
            if not content:
                # Reasoning models can spend the whole budget on hidden tokens and
                # return an empty message. Retry, then let the caller split the batch.
                last_error = (
                    f"empty content (finish_reason={choice.get('finish_reason')}, "
                    f"usage={body.get('usage', {})})"
                )
                log.warning("llm empty content attempt=%s %s", attempt + 1, last_error)
                await asyncio.sleep(1 + attempt)
                continue
            if choice.get("finish_reason") == "length":
                log.warning("llm output truncated at max_tokens=%s; digest may be incomplete", MAX_OUTPUT_TOKENS)
            return content
        except (httpx.HTTPError, ValueError) as error:
            last_error = f"{type(error).__name__}: {error}"
            await asyncio.sleep(2**attempt)
    raise RuntimeError(f"LLM call failed after retries: {last_error}")


def strip_fences(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```$", "", cleaned)
    return cleaned.strip()


# The gateway masks the provider name "OpenAI" as "[OI]" in both requests and
# responses, so restore it before the digest is rendered.
MASKED_VENDOR = re.compile(r"\[\s*OI\s*\]")


def restore_masked(text: str) -> str:
    return MASKED_VENDOR.sub("OpenAI", text)


EM_DASH_RE = re.compile(r"\s*\u2014\s*")


def normalize_dashes(text: str) -> str:
    """Replace em dashes with a middle dot, per the house style.

    En dashes are left alone because they appear inside numeric ranges.
    """
    return EM_DASH_RE.sub(" \u00b7 ", text)


def count_entries(markdown: str) -> int:
    return len(re.findall(r"^\s*-\s+\*\*", markdown, flags=re.MULTILINE))


def parse_sections(markdown: str) -> list[tuple[str, list[str]]]:
    """Split model output into (section, body lines) pairs.

    Body lines are the bullet lines and table rows belonging to that section.
    Text before the first section heading is dropped.
    """
    sections: list[tuple[str, list[str]]] = []
    section: str | None = None
    body: list[str] = []
    for raw in markdown.splitlines():
        line = raw.rstrip()
        if line.startswith("## "):
            if section is not None:
                sections.append((section, body))
            section = line[3:].strip() or "Other"
            body = []
            continue
        if line.startswith("# "):
            continue
        if section is None:
            continue
        if line.strip():
            body.append(line)
    if section is not None:
        sections.append((section, body))
    return [(name, lines) for name, lines in sections if lines]


def merge_sections(sections: list[tuple[str, list[str]]], title: str) -> str:
    """Rebuild one document, grouping sections in canonical order."""
    grouped: dict[str, list[str]] = {}
    for name, lines in sections:
        grouped.setdefault(name, []).extend(lines)

    ordered: list[str] = [name for name in SECTION_ORDER if name in grouped]
    for name in grouped:
        if name not in ordered:
            ordered.append(name)

    out: list[str] = [f"# {title}", ""]
    for name in ordered:
        out.append(f"## {name}")
        out.extend(grouped[name])
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def batch_slices(items: list[Message], size: int) -> list[list[Message]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


async def summarize_batch(
    batch: list[Message],
    hours: float,
    title: str,
    model: str,
    batch_index: int,
    batch_total: int,
) -> list[tuple[str, list[str]]]:
    payload, omitted = build_input(batch)
    prompt = (
        f"Window: {hours:g} hours. Batch {batch_index} of {batch_total} "
        f"covering {len(batch)} posts.\n"
        f"Use this exact title: {title}\n\n"
        f"Posts:\n\n{payload}"
    )
    if omitted:
        prompt += f"\n\nNote: {omitted} posts were dropped for length and are not shown."
    try:
        content = restore_masked(strip_fences(await call_model(prompt, model)))
    except RuntimeError as error:
        if len(batch) <= 1:
            log.error("batch %s/%s failed on a single post: %s", batch_index, batch_total, error)
            return []
        middle = len(batch) // 2
        log.warning(
            "batch %s/%s failed (%s); splitting %s posts into %s + %s",
            batch_index,
            batch_total,
            error,
            len(batch),
            middle,
            len(batch) - middle,
        )
        left = await summarize_batch(
            batch[:middle], hours, title, model, batch_index, batch_total
        )
        right = await summarize_batch(
            batch[middle:], hours, title, model, batch_index, batch_total
        )
        return left + right

    sections = parse_sections(normalize_dashes(content))
    if not sections:
        log.warning("batch %s/%s produced no sections", batch_index, batch_total)
    return sections


async def summarize(messages: list[Message], hours: float, model: str | None = None) -> tuple[str, int]:
    if not messages:
        return "", 0
    title, start, end = period_label(hours)
    chosen = model or DEFAULT_MODEL
    batches = batch_slices(messages, max(1, BATCH_POSTS))
    log.info("llm digest start model=%s posts=%s batches=%s", chosen, len(messages), len(batches))

    collected: list[tuple[str, list[str]]] = []
    for index, batch in enumerate(batches, start=1):
        sections = await summarize_batch(batch, hours, title, chosen, index, len(batches))
        collected.extend(sections)
        log.info("llm batch %s/%s posts=%s sections=%s", index, len(batches), len(batch), len(sections))

    content = normalize_dashes(merge_sections(collected, title))
    bullets = count_entries(content)
    log.info(
        "llm digest built model=%s posts=%s bullets=%s chars=%s",
        chosen,
        len(messages),
        bullets,
        len(content),
    )
    return content, len(messages)


def chunk_for_telegram(text: str, limit: int = 3800) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.splitlines():
        addition = len(line) + 1
        if size + addition > limit and current:
            chunks.append("\n".join(current))
            current, size = [], 0
        if addition > limit:
            line = line[: limit - 1] + "\u2026"
            addition = len(line) + 1
        current.append(line)
        size += addition
    if current:
        chunks.append("\n".join(current))
    return chunks


INLINE_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")
BARE_URL_RE = re.compile(r"(?<![\"'=>])(https?://[^\s<>\"]+)")
ITALIC_RE = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")


def clean_url(url: str) -> str:
    """Drop punctuation that the model glued to the end of a URL.

    A closing paren is kept only when the URL itself contains an opening one,
    which is how Wikipedia-style links look.
    """
    value = url
    while value and value[-1] in ".,;:!?":
        value = value[:-1]
    while value.endswith(")") and value.count(")") > value.count("("):
        value = value[:-1]
    return value


def _anchor(url: str, label: str) -> str:
    return (
        f'<link href="{url}">'
        f'<font color="#1d4ed8">{label}</font>'
        f"</link>"
    )


def inline_markup(text: str) -> str:
    value = escape(text)
    value = INLINE_LINK_RE.sub(
        lambda match: _anchor(clean_url(match.group(2)), match.group(1)), value
    )
    value = BOLD_RE.sub(lambda match: f"<b>{match.group(1)}</b>", value)
    value = ITALIC_RE.sub(lambda match: f"<i>{match.group(1)}</i>", value)
    value = BARE_URL_RE.sub(
        lambda match: _anchor(clean_url(match.group(1)), clean_url(match.group(1))), value
    )
    return value


def _register(name: str, candidates: list[Path]) -> bool:
    for candidate in candidates:
        if not candidate.exists():
            continue
        try:
            pdfmetrics.registerFont(TTFont(name, str(candidate)))
            return True
        except Exception as error:  # noqa: BLE001 - try the next candidate
            log.debug("cannot register %s from %s: %s", name, candidate, error)
    return False


def register_fonts() -> tuple[str, str, bool]:
    """Register a sans face for headings and a serif face for body text.

    Returns (sans, serif, unicode_ok). Windows and Debian/Ubuntu paths are both
    tried so the same code renders locally and on alwaysdata.
    """
    sans_ok = _register(
        "DigestSans",
        [
            Path("C:/Windows/Fonts/segoeui.ttf"),
            Path("C:/Windows/Fonts/arial.ttf"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
            Path("/usr/share/fonts/TTF/DejaVuSans.ttf"),
            Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
        ],
    )
    _register(
        "DigestSansBold",
        [
            Path("C:/Windows/Fonts/segoeuib.ttf"),
            Path("C:/Windows/Fonts/arialbd.ttf"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
            Path("/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"),
            Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
        ],
    )
    serif_ok = _register(
        "DigestSerif",
        [
            Path("C:/Windows/Fonts/georgia.ttf"),
            Path("C:/Windows/Fonts/cambria.ttc"),
            Path("C:/Windows/Fonts/times.ttf"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf"),
            Path("/usr/share/fonts/TTF/DejaVuSerif.ttf"),
            Path("/System/Library/Fonts/Supplemental/Georgia.ttf"),
        ],
    )
    _register(
        "DigestSerifBold",
        [
            Path("C:/Windows/Fonts/georgiab.ttf"),
            Path("C:/Windows/Fonts/cambriab.ttf"),
            Path("C:/Windows/Fonts/timesbd.ttf"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf"),
            Path("/usr/share/fonts/TTF/DejaVuSerif-Bold.ttf"),
            Path("/System/Library/Fonts/Supplemental/Georgia Bold.ttf"),
        ],
    )
    from reportlab.pdfbase.pdfmetrics import getRegisteredFontNames

    registered = set(getRegisteredFontNames())
    if sans_ok:
        sans_bold_face = "DigestSansBold" if "DigestSansBold" in registered else "DigestSans"
        pdfmetrics.registerFontFamily(
            "DigestSans",
            normal="DigestSans",
            bold=sans_bold_face,
            italic="DigestSans",
            boldItalic=sans_bold_face,
        )
    if serif_ok:
        serif_bold_face = "DigestSerifBold" if "DigestSerifBold" in registered else "DigestSerif"
        pdfmetrics.registerFontFamily(
            "DigestSerif",
            normal="DigestSerif",
            bold=serif_bold_face,
            italic="DigestSerif",
            boldItalic=serif_bold_face,
        )

    if not sans_ok:
        log.warning("no unicode sans font found; falling back to Helvetica")
    if not serif_ok:
        log.warning("no unicode serif font found; falling back to Times-Roman")
    sans = "DigestSans" if sans_ok else "Helvetica"
    serif = "DigestSerif" if serif_ok else "Times-Roman"
    return sans, serif, sans_ok and serif_ok


def digest_styles(sans: str, serif: str) -> dict[str, ParagraphStyle]:
    sans_bold = "DigestSansBold" if sans == "DigestSans" else "Helvetica-Bold"
    serif_bold = "DigestSerifBold" if serif == "DigestSerif" else "Times-Bold"
    return {
        "h1": ParagraphStyle(
            "DigestH1", fontName=sans_bold, fontSize=19, leading=23, textColor=INK,
            spaceAfter=10,
        ),
        "section": ParagraphStyle(
            "DigestSection", fontName=sans_bold, fontSize=10.6, leading=13.5,
            textColor=ACCENT, spaceBefore=13, spaceAfter=5,
        ),
        "bullet": ParagraphStyle(
            "DigestBullet", fontName=serif, fontSize=9.4, leading=13.4,
            textColor=INK, leftIndent=10, firstLineIndent=-10, spaceAfter=5,
        ),
        "note": ParagraphStyle(
            "DigestNote", fontName=serif, fontSize=9, leading=13,
            textColor=SECONDARY, spaceBefore=4, spaceAfter=4,
        ),
        "cell": ParagraphStyle(
            "DigestCell", fontName=sans, fontSize=7.8, leading=10.4, textColor=INK,
        ),
        "cell_head": ParagraphStyle(
            "DigestCellHead", fontName=sans_bold, fontSize=7.6, leading=10,
            textColor=colors.HexColor("#78350f"),
        ),
    }


def table_from_rows(rows: list[list[str]], styles: dict[str, ParagraphStyle]) -> Table:
    header = [Paragraph(inline_markup(cell), styles["cell_head"]) for cell in rows[0]]
    body = [[Paragraph(inline_markup(cell), styles["cell"]) for cell in row] for row in rows[1:]]
    width = (A4[0] - 36 * mm) / max(len(rows[0]), 1)
    table = Table([header] + body, colWidths=[width] * len(rows[0]), repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#fdf6ec")),
                ("LINEBELOW", (0, 0), (-1, 0), 0.8, colors.HexColor("#e7c9a9")),
                ("INNERGRID", (0, 1), (-1, -1), 0.25, RULE),
                ("BOX", (0, 0), (-1, -1), 0.4, colors.HexColor("#e7c9a9")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    return table


def render_digest_pdf(markdown: str, out_path: Path, subtitle: str = "") -> None:
    markdown = normalize_dashes(markdown)
    sans, serif, unicode_ok = register_fonts()
    styles = digest_styles(sans, serif)
    sans_bold = "DigestSansBold" if sans == "DigestSans" else "Helvetica-Bold"

    def text(value: str) -> str:
        cleaned = sanitize(value)
        if not unicode_ok:
            cleaned = cleaned.encode("latin-1", "replace").decode("latin-1")
        return cleaned

    def markup(value: str) -> str:
        return inline_markup(value) if unicode_ok else escape(text(value))

    out_path.parent.mkdir(parents=True, exist_ok=True)

    doc = BaseDocTemplate(
        str(out_path),
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=17 * mm,
        bottomMargin=17 * mm,
        title="Telegram digest",
        author="lifeos-listener",
    )
    frame = Frame(
        doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="body",
        leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0,
    )

    doc_title = "Telegram digest"

    def decorate(canvas, doc_) -> None:  # type: ignore[no-untyped-def]
        canvas.saveState()
        canvas.setFont(sans, 7.2)
        canvas.setFillColor(MUTED)
        canvas.drawRightString(A4[0] - doc.rightMargin, 9 * mm, f"{doc_.page}")
        canvas.restoreState()

    doc.addPageTemplates([PageTemplate(id="main", frames=[frame], onPage=decorate)])

    story: list = []
    rows: list[list[str]] = []

    def flush_table() -> None:
        nonlocal rows
        if rows:
            width = max(len(row) for row in rows)
            rows = [row + [""] * (width - len(row)) for row in rows]
            story.append(Spacer(1, 3))
            story.append(table_from_rows(rows, styles))
            story.append(Spacer(1, 4))
            rows = []

    for raw in markdown.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("|"):
            cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            if all(set(cell) <= {"-", ":", " "} for cell in cells):
                continue
            rows.append(cells)
            continue
        flush_table()

        if stripped.startswith("## "):
            story.append(Paragraph(text(stripped[3:]).upper(), styles["section"]))
            continue
        if stripped.startswith("# "):
            story.append(Paragraph(markup(stripped[2:].strip()), styles["h1"]))
            continue
        if stripped.startswith(("- ", "* ")):
            body = stripped[2:].strip()
            match = re.match(r"^\*\*(.+?)\*\*\s*:?\s*(.*)$", body)
            if match:
                heading = match.group(1).strip()
                rest = match.group(2).strip()
                rendered = (
                    f'<font name="{sans_bold}" color="#0f172a">{markup(heading)}</font>'
                )
                if rest and not rest.startswith(("\u2014", "-", "\u2013", "(", "[", "\u00b7")):
                    rendered += f'<font color="#b45309">:</font> {markup(rest)}'
                elif rest:
                    rendered += f" {markup(rest)}"
            else:
                rendered = markup(body)
            story.append(Paragraph(rendered, styles["bullet"]))
            continue
        story.append(Paragraph(markup(stripped), styles["note"]))
    flush_table()

    if not story:
        story.append(Paragraph("No content.", styles["bullet"]))

    doc.build(story)
    log.info("digest pdf written path=%s bytes=%s", out_path, out_path.stat().st_size)
