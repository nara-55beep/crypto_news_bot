"""One-time interactive Telegram login for the news listener.

Normal bot startup never asks for credentials. Run this file deliberately when the
saved Telethon session is missing or no longer authorized, then restart ``dev.bat``.
"""

from __future__ import annotations

import asyncio

import config
import telegram_listener


async def main() -> None:
    print("Telegram one-time login")
    print(f"Session: {config.TELEGRAM_SESSION}")
    print("Enter your phone in full international format (starting with + and country code).")
    print("Telegram may then ask for the code sent in its app and your 2-step password.\n")
    client = telegram_listener.build_client()
    try:
        await client.start()
        me = await client.get_me()
        identity = getattr(me, "username", None) or getattr(me, "first_name", None) or me.id
        print(f"\nTelegram login complete: {identity}")
        print("You can now close this window and open dev.bat again.")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nTelegram login cancelled.")
