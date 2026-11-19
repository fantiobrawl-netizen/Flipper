"""
Один раз получает строку сессии Telegram (TG_SESSION) для бота.
Строка придёт вам в «Избранное» в Telegram. Вставьте её в GitHub → Settings → Secrets.

Запуск на компьютере:  pip install kurigram tgcrypto && python login.py
"""
import asyncio

from pyrogram import Client


async def main():
    api_id = int(input("API_ID: ").strip())
    api_hash = input("API_HASH: ").strip()
    async with Client("login", api_id=api_id, api_hash=api_hash, in_memory=True) as client:
        session = await client.export_session_string()
        await client.send_message("me", "TG_SESSION для Portals Flipper. НИКОМУ не показывайте:")
        await client.send_message("me", session)
    print("✅ Готово: строка сессии отправлена вам в «Избранное».")


if __name__ == "__main__":
    asyncio.run(main())
