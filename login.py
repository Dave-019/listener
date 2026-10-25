"""Generate a Telethon StringSession once, then store it as TELEGRAM_SESSION.

Run locally or over SSH, complete the Telegram login, and copy the printed
string into the Alwaysdata environment. Set WRITE_ENV=1 to write it into the
local .env file instead of printing it.
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession

load_dotenv()


async def main() -> None:
    api_id = int(os.environ["TELEGRAM_API_ID"])
    api_hash = os.environ["TELEGRAM_API_HASH"]
    phone = os.environ.get("TELEGRAM_PHONE") or None
    client = TelegramClient(StringSession(), api_id, api_hash)
    await client.start(phone=phone)
    session = client.session.save()
    await client.disconnect()

    if os.environ.get("WRITE_ENV", "").lower() in {"1", "true", "yes"}:
        env_path = Path(os.environ.get("ENV_FILE", ".env"))
        existing = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
        line = f"TELEGRAM_SESSION={session}"
        if re.search(r"^TELEGRAM_SESSION=.*$", existing, flags=re.MULTILINE):
            existing = re.sub(r"^TELEGRAM_SESSION=.*$", line, existing, flags=re.MULTILINE)
        else:
            existing = existing.rstrip("\n") + ("\n" if existing else "") + line + "\n"
        env_path.write_text(existing, encoding="utf-8")
        try:
            env_path.chmod(0o600)
        except OSError:
            pass
        print(f"\nWrote TELEGRAM_SESSION to {env_path}\n")
    else:
        print("\nTELEGRAM_SESSION=" + session + "\n")


if __name__ == "__main__":
    asyncio.run(main())
