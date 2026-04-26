"""One-shot Telethon session generator for Railway.

Phase 1 (TG_CODE not set):
    - send_code_request to USERBOT_PHONE
    - Telegram delivers the login code to the userbot account
    - phone_code_hash + StringSession-so-far is persisted to /app/data/.code_state.json
    - container idles forever; user pastes the code into Railway env var TG_CODE.

Phase 2 (TG_CODE set):
    - load phone_code_hash + StringSession from disk
    - sign_in with code (and optionally TG_PASSWORD for 2FA)
    - print the resulting StringSession to the logs surrounded by markers
    - container idles forever; user copies the session into STRING_SESSION env var.

After STRING_SESSION is updated, restore railway.json startCommand to "python -u bot.py".
"""
import os
import json
import asyncio
import sys

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
PHONE = os.getenv("USERBOT_PHONE", "").strip()
CODE = os.getenv("TG_CODE", "").strip()
PASSWORD = os.getenv("TG_PASSWORD", "").strip()
STATE_FILE = "/app/data/.code_state.json"


def log(msg: str):
    print(f"[gen_session] {msg}", flush=True)


async def idle():
    log("idle (container stays alive)...")
    while True:
        await asyncio.sleep(60)


async def main():
    if not (API_ID and API_HASH and PHONE):
        log("FATAL: missing API_ID / API_HASH / USERBOT_PHONE")
        await idle()
        return

    if not CODE:
        log(f"PHASE 1 - requesting login code for {PHONE}")
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        await client.connect()
        try:
            sent = await client.send_code_request(PHONE)
            session_str = client.session.save()
            os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
            with open(STATE_FILE, "w") as f:
                json.dump({
                    "phone_code_hash": sent.phone_code_hash,
                    "phone": PHONE,
                    "session": session_str,
                }, f)
            log("OK - code sent to your Telegram app on the userbot account.")
            log("Next: paste the received code into Railway -> Variables -> TG_CODE, then redeploy.")
        except Exception as e:
            log(f"ERR send_code_request failed: {type(e).__name__}: {e}")
        finally:
            await client.disconnect()
        await idle()
        return

    log("PHASE 2 - signing in with TG_CODE")
    if not os.path.exists(STATE_FILE):
        log(f"FATAL: {STATE_FILE} not found. Did Phase 1 complete? Clear TG_CODE and redeploy.")
        await idle()
        return

    with open(STATE_FILE) as f:
        st = json.load(f)
    phone_code_hash = st["phone_code_hash"]
    session_str = st.get("session", "")

    client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
    await client.connect()
    try:
        try:
            await client.sign_in(phone=PHONE, code=CODE, phone_code_hash=phone_code_hash)
        except SessionPasswordNeededError:
            if not PASSWORD:
                log("ERR: 2FA password required. Set TG_PASSWORD and redeploy.")
                return
            log("2FA detected, signing in with password")
            await client.sign_in(password=PASSWORD)

        ss = client.session.save()
        me = await client.get_me()
        log(f"signed in as {me.first_name} (@{me.username}, id={me.id})")
        print("=" * 80, flush=True)
        print("STRING_SESSION_BEGIN", flush=True)
        print(ss, flush=True)
        print("STRING_SESSION_END", flush=True)
        print("=" * 80, flush=True)
        log("OK - copy STRING_SESSION above and put it into Railway Variables.")
        try:
            os.remove(STATE_FILE)
        except Exception:
            pass
    except Exception as e:
        log(f"ERR sign_in failed: {type(e).__name__}: {e}")
    finally:
        await client.disconnect()

    await idle()


if __name__ == "__main__":
    asyncio.run(main())
