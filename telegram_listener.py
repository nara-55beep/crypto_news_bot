"""
================================================================================
 telegram_listener.py  —  TELEGRAM INGESTION  (Telethon / MTProto, free)
================================================================================
This is your fastest free news source. It now uses TWO methods together so news
can't silently stop:

  1. PUSH (events.NewMessage) — instant, but Telegram only pushes live messages
     for channels your account has JOINED. A channel you haven't joined will
     resolve fine ("listening on N channels") yet never deliver a single live
     message. That is the trap this file used to fall into.

  2. POLLING (get_messages) — every couple of seconds it reads each channel
     directly. Reading a PUBLIC channel does NOT require joining it, so this
     catches everything regardless of membership, and survives disconnects.

A shared de-dupe set means a message seen by BOTH paths is only processed once.
On startup it records the latest message id per channel as a baseline but does
NOT post them — so a restart never replays headlines that were already on the
feed. Only genuinely new messages (arriving after startup) are delivered.

For the FASTEST possible delivery, JOIN the channels in your Telegram app — then
push fires in ~0.3s. If you don't, polling still gets them within a few seconds.

First run only: it asks (in the terminal) for your phone number + the login code.
After that the session file in data/ keeps you logged in. Credentials live in
config.py (free at https://my.telegram.org).
================================================================================
"""

from __future__ import annotations

import asyncio

from telethon import TelegramClient, events

try:
    from telethon.errors import FloodWaitError
except Exception:                       # pragma: no cover - very old telethon
    class FloodWaitError(Exception):
        seconds = 5

import config


# ---------------------------------------------------------------------------
# Channels to listen to. This lives here (not config.py) so the list can be
# updated by dropping in this one file. A handle that can't be resolved is
# skipped and named in the console. Verify each @handle in your Telegram app.
# ---------------------------------------------------------------------------
CHANNELS = [
    "BWEnews",
    "trad_fin",
    "SynopticNewswire",
    "WalterBloomberg",
    "marketfeed",
    "Cointelegraph",
    "whale_alert",
]

# How often the poller re-reads every channel (seconds). Lower = faster catch
# for channels you haven't joined, but more API calls. 2s is a safe sweet spot.
POLL_INTERVAL = 2.0


# ---------------------------------------------------------------------------
# de-dupe: a message can arrive via BOTH push and poll. Process each once.
# ---------------------------------------------------------------------------
_processed: set = set()


def _seen(chat_id, msg_id) -> bool:
    key = (chat_id, msg_id)
    if key in _processed:
        return True
    _processed.add(key)
    if len(_processed) > 6000:          # keep memory bounded
        for k in list(_processed)[:3000]:
            _processed.discard(k)
    return False


def _src(entity) -> str:
    return ("tg:" + str(getattr(entity, "username", None)
            or getattr(entity, "title", None)
            or getattr(entity, "id", "?")))


def build_client() -> TelegramClient:
    # connection_retries=-1 -> retry forever; auto_reconnect keeps the session
    # alive across blips so the feed doesn't silently die.
    return TelegramClient(
        config.TELEGRAM_SESSION,
        config.TELEGRAM_API_ID,
        config.TELEGRAM_API_HASH,
        connection_retries=-1,
        retry_delay=5,
        auto_reconnect=True,
        request_retries=5,
    )


async def _connect_authorized(client) -> bool:
    """Connect without ever starting Telethon's interactive login flow.

    ``client.start()`` asks for a phone number whenever the session is missing, expired,
    or unauthorized. That is appropriate for a one-time setup command, but not for a
    long-running bot: a reconnect loop otherwise keeps prompting forever and makes the
    whole application look stuck. Normal startup is deliberately non-interactive.
    """
    await client.connect()
    if await client.is_user_authorized():
        return True
    print(
        "[telegram] disabled: the saved Telegram session is not authorized. "
        "Close dev.bat and run: venv\\Scripts\\python.exe telegram_login.py"
    )
    await client.disconnect()
    return False


async def _resolve_channels(client):
    """Resolve each configured channel; skip + log failures so one bad handle
    can't break the whole feed. Returns the entities that worked."""
    live, bad = [], []
    for ch in CHANNELS:
        try:
            live.append(await client.get_entity(ch))
        except Exception as e:
            bad.append(f"{ch} ({type(e).__name__})")
        await asyncio.sleep(0.3)        # gentle: avoid flood-wait on bulk resolve
    if live:
        names = [getattr(e, "username", None) or getattr(e, "title", None)
                 or str(getattr(e, "id", "?")) for e in live]
        print(f"[telegram] resolved {len(live)} channels: {', '.join(names)}")
    if bad:
        print(f"[telegram] could NOT resolve {len(bad)} (skipped): {', '.join(bad)}")
        print("[telegram]   -> check each @handle is exact and the channel is public.")
    return live


async def _deliver(on_news, source, text, tag):
    """Print + hand a single message to the bot, once."""
    print(f"[telegram] ({tag}) {source}: {text[:70].replace(chr(10), ' ')}")
    try:
        await on_news(source, text)
    except Exception as e:
        print(f"[telegram] on_news error: {type(e).__name__}: {e}")


def _register(client, channels, on_news):
    """PUSH path: instant delivery for channels you've joined."""
    @client.on(events.NewMessage(chats=channels))
    async def _handler(event):
        text = event.message.message or ""
        if not text.strip():
            return
        cid = getattr(event, "chat_id", None)
        if cid is None:
            cid = getattr(event.message, "chat_id", None)
        if _seen(cid, event.message.id):
            return
        try:
            source = _src(await event.get_chat())
        except Exception:
            source = "tg:?"
        await _deliver(on_news, source, text, "live")


async def _poll_loop(client, channels, on_news):
    """POLL path: read each channel directly so membership is NOT required."""
    last_id: dict = {}

    # --- startup: record the latest message id per channel WITHOUT delivering it.
    #     This is the anti-replay guard: after a restart we must only ever post
    #     genuinely NEW messages, never re-post headlines that were already on the
    #     feed before the restart. We also mark them seen so the push path can't
    #     re-deliver them either.
    for ent in channels:
        try:
            msgs = await client.get_messages(ent, limit=1)
            if msgs:
                m = msgs[0]
                last_id[ent.id] = m.id
                _seen(ent.id, m.id)         # remember it so neither poll nor push replays it
            else:
                last_id[ent.id] = 0
        except Exception as e:
            last_id[ent.id] = 0
            print(f"[telegram] startup read failed for {_src(ent)}: {type(e).__name__}")
        await asyncio.sleep(0.2)
    print("[telegram] startup: marked latest message per channel as the baseline "
          "(no replay) — only NEW messages from here on.")

    print(f"[telegram] polling {len(channels)} channels every {POLL_INTERVAL:g}s "
          f"(works even for channels you have NOT joined)")

    # --- live loop: only messages newer than what we've already seen
    while True:
        for ent in channels:
            base = last_id.get(ent.id, 0)
            try:
                msgs = await client.get_messages(ent, limit=25, min_id=base)
            except FloodWaitError as e:
                await asyncio.sleep(getattr(e, "seconds", 5) + 1)
                continue
            except Exception:
                continue                # transient (e.g. mid-reconnect); try next cycle
            for m in reversed(msgs):    # get_messages is newest-first; go oldest-first
                if m.id <= base:
                    continue
                last_id[ent.id] = max(last_id.get(ent.id, 0), m.id)
                text = m.message or ""
                if not text.strip():
                    continue
                if _seen(ent.id, m.id):
                    continue
                await _deliver(on_news, _src(ent), text, "poll")
            await asyncio.sleep(0.1)    # small gap between channels
        await asyncio.sleep(POLL_INTERVAL)


async def run_listener_forever(client, on_news):
    """Connect, resolve channels, start BOTH push + polling, and keep alive.
    If the connection drops for good, reconnect and resume."""
    registered = False
    while True:
        try:
            if not await _connect_authorized(client):
                return                  # never prompt from a background/service process
            if not registered:
                channels = await _resolve_channels(client)
                if channels:
                    _register(client, channels, on_news)            # push
                    asyncio.create_task(_poll_loop(client, channels, on_news))  # poll
                    registered = True
                    print("[telegram] live — push + polling both active.")
                else:
                    print("[telegram] no channels resolved yet — retrying in 10s…")
                    await asyncio.sleep(10)
                    continue
            await client.run_until_disconnected()
        except Exception as e:
            print(f"[telegram] connection error: {type(e).__name__}: {e}")
        print("[telegram] disconnected — reconnecting in 5s…")
        await asyncio.sleep(5)


# legacy thin wrapper (kept for compatibility); prefer run_listener_forever
async def start_listener(client, on_news):
    if not await _connect_authorized(client):
        return
    channels = await _resolve_channels(client)
    _register(client, channels, on_news)
    asyncio.create_task(_poll_loop(client, channels, on_news))
    print(f"[telegram] listening (legacy) on {len(channels)} channels")
