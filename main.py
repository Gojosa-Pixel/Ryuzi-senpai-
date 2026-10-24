import asyncio
import logging
import os
import sys
import tempfile
from contextlib import suppress
from urllib.parse import urlparse

import aiohttp
from aiohttp import web
from motor.motor_asyncio import AsyncIOMotorClient
from pyrogram import Client, filters
from pyrogram.errors import FloodWait, RPCError
from pyrogram.types import Message

from config import (
    API_ID, API_HASH, BOT_TOKEN, OWNER_ID, MONGO_URI,
    BYPASS_API_URL, PORT,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("link-bypasser")

app = Client(
    "link_bypasser_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
)

mongo = AsyncIOMotorClient(MONGO_URI)
db = mongo["BypasserBotDB"]
users_col = db["users"]
banned_col = db["banned_users"]

http_session: aiohttp.ClientSession | None = None
web_runner: web.AppRunner | None = None


def valid_http_url(value: str) -> bool:
    try:
        parsed = urlparse(value.strip())
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
    except ValueError:
        return False


def extract_url(text: str) -> str | None:
    # Supports a message containing a single URL, optionally surrounded by whitespace.
    candidate = text.strip().split()[0] if text.strip() else ""
    candidate = candidate.rstrip(".,!?)]}")
    return candidate if valid_http_url(candidate) else None


async def is_banned(user_id: int) -> bool:
    return await banned_col.find_one({"_id": user_id}) is not None


async def bypass_url(url: str) -> str:
    """Calls the configured bypass API using ?url=<target>.

    Accepted API responses:
      - plain-text URL
      - JSON with one of: url, result, destination, target, bypassed
      - JSON string
    """
    assert http_session is not None
    timeout = aiohttp.ClientTimeout(total=45)
    async with http_session.get(
        BYPASS_API_URL,
        params={"url": url},
        timeout=timeout,
        allow_redirects=True,
    ) as response:
        body = await response.text()
        if response.status >= 400:
            raise RuntimeError(f"Bypass API returned HTTP {response.status}")
        try:
            payload = await response.json(content_type=None)
        except (ValueError, aiohttp.ContentTypeError):
            payload = body.strip()

    result = None
    if isinstance(payload, str):
        result = payload.strip()
    elif isinstance(payload, dict):
        for key in ("url", "result", "destination", "target", "bypassed"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                result = value.strip()
                break

    if not result or not valid_http_url(result):
        raise RuntimeError("Bypass API did not return a valid http/https URL")
    return result


async def home(_: web.Request) -> web.Response:
    return web.Response(text="Bot is Alive & Running!", content_type="text/plain")


async def start_web_server() -> None:
    global web_runner
    web_app = web.Application()
    web_app.router.add_get("/", home)
    web_runner = web.AppRunner(web_app)
    await web_runner.setup()
    site = web.TCPSite(web_runner, host="0.0.0.0", port=PORT)
    await site.start()
    log.info("Health server listening on port %s", PORT)


def owner_only(func):
    async def wrapper(client: Client, message: Message):
        if not message.from_user or message.from_user.id != OWNER_ID:
            await message.reply_text("⛔ This command is for the bot owner only.")
            return
        return await func(client, message)
    wrapper.__name__ = func.__name__
    return wrapper


@app.on_message(filters.private & filters.command("start"))
async def start_command(_: Client, message: Message):
    if not message.from_user:
        return
    if await is_banned(message.from_user.id):
        await message.reply_text("⛔ You are banned from using this bot.")
        return
    await users_col.update_one(
        {"_id": message.from_user.id},
        {"$set": {"user_id": message.from_user.id,
                  "first_name": message.from_user.first_name or "",
                  "username": message.from_user.username or ""}},
        upsert=True,
    )
    await message.reply_text(
        "👋 Welcome to Link Bypasser Bot!\n\n"
        "Send me a valid http:// or https:// link and I’ll try to find its destination."
    )


@app.on_message(filters.private & filters.text & ~filters.command(["start", "stats", "users", "ban", "unban", "banned", "broadcast", "restart"]))
async def handle_link(_: Client, message: Message):
    if not message.from_user:
        return
    user_id = message.from_user.id
    if await is_banned(user_id):
        await message.reply_text("⛔ You are banned from using this bot.")
        return

    await users_col.update_one(
        {"_id": user_id},
        {"$set": {"user_id": user_id,
                  "first_name": message.from_user.first_name or "",
                  "username": message.from_user.username or ""}},
        upsert=True,
    )

    url = extract_url(message.text or "")
    if not url:
        await message.reply_text("Please send one valid http:// or https:// link.")
        return

    status = await message.reply_text("⏳ Bypassing...")
    try:
        result = await bypass_url(url)
        await status.edit_text(result, disable_web_page_preview=True)
    except asyncio.TimeoutError:
        await status.edit_text("❌ The bypass API timed out. Please try again later.")
    except (aiohttp.ClientError, RuntimeError) as exc:
        log.warning("Bypass failed for user %s: %s", user_id, exc)
        await status.edit_text("❌ Could not bypass this link. Please try again later.")
    except Exception:
        log.exception("Unexpected bypass error")
        await status.edit_text("❌ An unexpected error occurred.")


@app.on_message(filters.private & filters.command("stats"))
@owner_only
async def stats_command(_: Client, message: Message):
    total_users = await users_col.count_documents({})
    total_banned = await banned_col.count_documents({})
    await message.reply_text(
        f"📊 Bot Stats\n\n👥 Total users: {total_users}\n🚫 Banned users: {total_banned}"
    )


@app.on_message(filters.private & filters.command("users"))
@owner_only
async def users_command(_: Client, message: Message):
    ids = [str(doc["_id"]) async for doc in users_col.find({}, {"_id": 1}).sort("_id", 1)]
    if not ids:
        await message.reply_text("No users found.")
        return
    if len(ids) > 30:
        path = os.path.join(tempfile.gettempdir(), "users.txt")
        with open(path, "w", encoding="utf-8") as file:
            file.write("\n".join(ids))
        try:
            await message.reply_document(path, caption=f"👥 Total users: {len(ids)}")
        finally:
            with suppress(FileNotFoundError):
                os.remove(path)
    else:
        await message.reply_text("👥 User IDs:\n" + "\n".join(ids))


def parse_target_id(message: Message) -> int | None:
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user.id
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        return None
    try:
        return int(parts[1].strip())
    except ValueError:
        return None


@app.on_message(filters.private & filters.command("ban"))
@owner_only
async def ban_command(_: Client, message: Message):
    target_id = parse_target_id(message)
    if target_id is None:
        await message.reply_text("Usage: /ban <user_id> or reply to a user's message with /ban")
        return
    if target_id == OWNER_ID:
        await message.reply_text("You cannot ban the owner.")
        return
    await banned_col.update_one(
        {"_id": target_id},
        {"$set": {"user_id": target_id, "banned_by": OWNER_ID}},
        upsert=True,
    )
    await message.reply_text(f"🚫 User {target_id} has been banned.")


@app.on_message(filters.private & filters.command("unban"))
@owner_only
async def unban_command(_: Client, message: Message):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.reply_text("Usage: /unban <user_id>")
        return
    try:
        target_id = int(parts[1].strip())
    except ValueError:
        await message.reply_text("Please provide a valid numeric user ID.")
        return
    result = await banned_col.delete_one({"_id": target_id})
    await message.reply_text(
        f"✅ User {target_id} has been unbanned." if result.deleted_count
        else f"User {target_id} was not in the ban list."
    )


@app.on_message(filters.private & filters.command("banned"))
@owner_only
async def banned_command(_: Client, message: Message):
    ids = [str(doc["_id"]) async for doc in banned_col.find({}, {"_id": 1}).sort("_id", 1)]
    if not ids:
        await message.reply_text("No banned users.")
    elif len(ids) > 80:
        path = os.path.join(tempfile.gettempdir(), "banned_users.txt")
        with open(path, "w", encoding="utf-8") as file:
            file.write("\n".join(ids))
        try:
            await message.reply_document(path, caption=f"🚫 Banned users: {len(ids)}")
        finally:
            with suppress(FileNotFoundError):
                os.remove(path)
    else:
        await message.reply_text("🚫 Banned user IDs:\n" + "\n".join(ids))


@app.on_message(filters.private & filters.command("broadcast"))
@owner_only
async def broadcast_command(client: Client, message: Message):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.reply_text("Usage: /broadcast <message>")
        return
    text_to_send = parts[1].strip()
    user_ids = [doc["_id"] async for doc in users_col.find({}, {"_id": 1})]
    success = failed = 0
    progress = await message.reply_text(f"📣 Broadcasting to {len(user_ids)} users...")
    for user_id in user_ids:
        if await is_banned(user_id):
            continue
        while True:
            try:
                await client.send_message(user_id, text_to_send, disable_web_page_preview=True)
                success += 1
                break
            except FloodWait as exc:
                await asyncio.sleep(exc.value + 1)
            except (RPCError, Exception) as exc:
                log.warning("Broadcast failed for %s: %s", user_id, exc)
                failed += 1
                break
    await progress.edit_text(
        f"📣 Broadcast complete.\n\n✅ Success: {success}\n❌ Failed: {failed}"
    )


@app.on_message(filters.private & filters.command("restart"))
@owner_only
async def restart_command(_: Client, message: Message):
    await message.reply_text("♻️ Restarting bot process...")
    await asyncio.sleep(1)
    os.execv(sys.executable, [sys.executable] + sys.argv)


async def main():
    global http_session
    http_session = aiohttp.ClientSession()
    await start_web_server()
    try:
        await app.start()
        log.info("Pyrogram bot started")
        # Keep the event loop alive while the aiohttp health server is running.
        await asyncio.Event().wait()
    finally:
        with suppress(Exception):
            await app.stop()
        if web_runner is not None:
            with suppress(Exception):
                await web_runner.cleanup()
        if http_session is not None:
            with suppress(Exception):
                await http_session.close()
        mongo.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
