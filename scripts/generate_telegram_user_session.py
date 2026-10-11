"""Create a Telethon StringSession for Index deletion monitoring.

The generated value grants access to the Telegram account. Keep it private and
store it only in the deployment secret named TELEGRAM_USER_SESSION.
"""

from __future__ import annotations

import asyncio
from getpass import getpass

from telethon import TelegramClient
from telethon.sessions import StringSession


async def main() -> None:
    try:
        api_id = int(input("Telegram API ID: ").strip())
    except ValueError as exc:
        raise SystemExit("API ID must be a number.") from exc
    api_hash = getpass("Telegram API hash (input hidden): ").strip()
    phone = input("Alternate account phone number with country code: ").strip()
    if not api_hash or not phone:
        raise SystemExit("API hash and phone number are required.")

    client = TelegramClient(StringSession(), api_id, api_hash)
    try:
        await client.start(
            phone=phone,
            code_callback=lambda: getpass("Telegram login code (input hidden): "),
            password=lambda: getpass("Telegram two-step password (input hidden): "),
        )
        account = await client.get_me()
        if account is None or account.bot:
            raise SystemExit("Use a regular Telegram user account, not a bot account.")
        print(
            "\nCopy this session string directly into the TELEGRAM_USER_SESSION "
            "deployment secret. Do not share it or commit it:\n"
        )
        print(client.session.save())
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
