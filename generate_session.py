"""Generate a Telethon session string for the userbot.

Usage:
    python generate_session.py

You need TG_API_ID and TG_API_HASH from https://my.telegram.org
The resulting SESSION_STRING goes into your .env as TG_SESSION.

The user account must be an admin with invite rights in your channels.
Telegram may flag unusual activity, so use a secondary account.
"""
import asyncio
from telethon import TelegramClient
from telethon.sessions import StringSession


async def main():
    print("=== Telegram Userbot Session Generator ===\n")
    api_id = input("Enter your TG_API_ID (from my.telegram.org): ").strip()
    api_hash = input("Enter your TG_API_HASH (from my.telegram.org): ").strip()

    if not api_id or not api_hash:
        print("Error: both API_ID and API_HASH are required")
        return

    api_id = int(api_id)

    client = TelegramClient(StringSession(), api_id, api_hash)

    print("\nConnecting to Telegram...")
    await client.start()
    me = await client.get_me()
    username = f" (@{me.username})" if me.username else ""
    print(f"Logged in as: {me.first_name}{username}")

    session_string = client.session.save()

    print("\n=== SUCCESS ===")
    print("Add this line to your .env file:\n")
    print(f"TG_SESSION={session_string}\n")
    print("IMPORTANT: Keep this string secret! It gives full access to the account.\n")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
