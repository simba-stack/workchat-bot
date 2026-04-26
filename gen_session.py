"""Stateless gen_session: state passed via env vars (PHONE_CODE_HASH, INITIAL_SESSION).

Phase 1: TG_CODE empty -> send_code_request, print PHONE_CODE_HASH and INITIAL_SESSION to logs, idle.
Phase 2: TG_CODE + PHONE_CODE_HASH + INITIAL_SESSION set -> sign_in, print STRING_SESSION, idle.
"""
import os, asyncio
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
PHONE = os.getenv("USERBOT_PHONE", "").strip()
CODE = os.getenv("TG_CODE", "").strip()
PASSWORD = os.getenv("TG_PASSWORD", "").strip()
PHC = os.getenv("PHONE_CODE_HASH", "").strip()
INITIAL = os.getenv("INITIAL_SESSION", "").strip()


def log(m): print(f"[gen_session] {m}", flush=True)


async def idle():
    log("idle (container stays alive)...")
    while True:
        await asyncio.sleep(60)


async def main():
    if not (API_ID and API_HASH and PHONE):
        log("FATAL: missing API_ID / API_HASH / USERBOT_PHONE"); await idle(); return

    if not (CODE and PHC and INITIAL):
        log(f"PHASE 1 - requesting login code for {PHONE}")
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        await client.connect()
        try:
            sent = await client.send_code_request(PHONE)
            ss = client.session.save()
            log(f"PHONE_CODE_HASH={sent.phone_code_hash}")
            log(f"INITIAL_SESSION={ss}")
            log("Code sent. Set TG_CODE, PHONE_CODE_HASH, INITIAL_SESSION in Variables, redeploy.")
        except Exception as e:
            log(f"ERR send_code_request: {type(e).__name__}: {e}")
        finally:
            await client.disconnect()
        await idle(); return

    log("PHASE 2 - signing in with TG_CODE + PHONE_CODE_HASH + INITIAL_SESSION")
    client = TelegramClient(StringSession(INITIAL), API_ID, API_HASH)
    await client.connect()
    try:
        try:
            await client.sign_in(phone=PHONE, code=CODE, phone_code_hash=PHC)
        except SessionPasswordNeededError:
            if not PASSWORD:
                log("ERR: 2FA password required (TG_PASSWORD)"); return
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
        log("OK - copy STRING_SESSION above into Railway Variables.")
    except Exception as e:
        log(f"ERR sign_in: {type(e).__name__}: {e}")
    finally:
        await client.disconnect()
    await idle()


if __name__ == "__main__":
    asyncio.run(main())
