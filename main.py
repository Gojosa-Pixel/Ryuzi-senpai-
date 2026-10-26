import asyncio
import gc
import logging
import os
import re
import sys
import tempfile
from contextlib import suppress
from urllib.parse import urljoin, urlparse

import aiohttp
from aiohttp import web
from motor.motor_asyncio import AsyncIOMotorClient
from pyrogram import Client, filters
from pyrogram.errors import FloodWait, RPCError
from pyrogram.types import Message

from config import API_ID, API_HASH, BOT_TOKEN, OWNER_ID, MONGO_URI, PORT

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("link-bypasser")

# Keep the client lean and process updates serially.
app = Client(
    "link_bypasser_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workers=1,
    max_concurrent_transmissions=1,
)

mongo = AsyncIOMotorClient(
    MONGO_URI,
    maxPoolSize=1,
    minPoolSize=0,
    maxConnecting=1,
    serverSelectionTimeoutMS=5000,
    connectTimeoutMS=5000,
    socketTimeoutMS=15000,
    retryReads=True,
    retryWrites=True,
)
db = mongo["BypasserBotDB"]
users_col = db["users"]
banned_col = db["banned_users"]

http_session: aiohttp.ClientSession | None = None
web_runner: web.AppRunner | None = None
bypass_semaphore = asyncio.Semaphore(2)
MAX_BODY_BYTES = 262_144

# Matches common meta-refresh and JS location assignments/redirects.
META_REFRESH_RE = re.compile(
    r"""<meta\b[^>]*http-equiv\s*=\s*["']?\s*refresh\s*["']?[^>]*content\s*=\s*["'][^"']*?url\s*=\s*([^;"']+)""",
    re.IGNORECASE,
)
# Keep the JS target capture separate for easier handling.
JS_TARGET_RE = re.compile(
    r"""(?:window\s*\.\s*)?location(?:\s*\.\s*href)?\s*(?:=|\.assign\s*\(|\.replace\s*\()\s*["']([^"']+)["']""",
    re.IGNORECASE,
)


def valid_http_url(value: str) -> bool:
    try:
        parsed = urlparse(value.strip())
        return parsed.scheme.lower() in ("http", "https") and bool(parsed.netloc)
    except (TypeError, ValueError):
        return False


def extract_url(text: str) -> str | None:
    candidate = text.strip().split()[0] if text.strip() else ""
    candidate = candidate.rstrip(".,!?)]}")
    return candidate if valid_http_url(candidate) else None


def extract_html_redirect(body: str, base_url: str) -> str | None:
    match = META_REFRESH_RE.search(body)
    if match:
        target = match.group(1).strip().strip("\"' ")
        target = re.sub(r"^\s*\d+(?:\.\d+)?\s*;\s*", "", target, flags=re.I)
        if target.lower().startswith("url="):
            target = target[4:].strip().strip("\"' ")
        candidate = urljoin(base_url, target)
        if valid_http_url(candidate):
            return candidate

    match = JS_TARGET_RE.search(body)
    if match:
        candidate = urljoin(base_url, match.group(1).strip())
        if valid_http_url(candidate):
            return candidate

    return None


async def is_banned(user_id: int) -> bool:
    return await banned_col.find_one({"_id": user_id}, {"_id": 1}) is not None


async def resolve_url(url: str, max_hops: int = 5) -> str:
    """Follow HTTP redirects and inspect limited HTML for meta/JS redirects."""
    if http_session is None:
        raise RuntimeError("HTTP client is not initialized")

    timeout = aiohttp.ClientTimeout(total=10, connect=4)
    current = url

    async with bypass_semaphore:
        for _ in range(max_hops):
            if not valid_http_url(current):
                raise RuntimeError("Invalid redirect URL")

            async with http_session.get(
                current,
                timeout=timeout,
                allow_redirects=False,
            ) as response:
                # aiohttp exposes redirect target without needing the body.
                if response.status in (301, 302, 303, 307, 308):
                    location = response.headers.get("Location")
                    if not location:
                        return current
                    current = urljoin(str(response.url), location)
                    continue

                # Read no more than 256 KiB of response data.
                body_bytes = await response.content.read(MAX_BODY_BYTES)
                charset = response.charset or "utf-8"
                body = body_bytes.decode(charset, errors="replace")
                candidate = extract_html_redirect(body, str(response.url))
                del body_bytes, body

                if candidate and candidate != current:
                    current = candidate
                    continue

                gc.collect()
                return str(response.url)

        gc.collect()
        return current


async def home(_: web.Request) -> web.Response:
    return web.Response(text="OK\n", content_type="text/plain")


async def start_web_server() -> None:
    global web_runner
    web_app = web.Application()
    web_app.router.add_get("/", home)
    web_runner = web.AppRunner(web_app, access_log=None)
    await web_runner.setup()
    site = web.TCPSite(web_runner, host="0.0.0.0", port=int(PORT))
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
    user_id = message.from_user.id
    if await is_banned(user_id):
        await message.reply_text("⛔ You are banned from using this bot.")
        return

    await users_col.update_one(
        {"_id": user_id},
        {
            "$set": {
                "user_id": user_id,
                "first_name": message.from_user.first_name or "",
                "username": message.from_user.username or "",
            }
        },
        upsert=True,
    )
    await message.reply_text(
        "👋 Welcome to Link Bypasser Bot!\n\n"
        "Send me a valid http:// or https:// link and I’ll try to find its destination."
    )


@app.on_message(
    filters.private
    & filters.text
    & ~filters.command(
        ["start", "stats", "users", "ban", "unban", "banned", "broadcast", "restart"]
    )
)
async def handle_link(_: Client, message: Message):
    if not message.from_user:
        return
    user_id = message.from_user.id
    if await is_banned(user_id):
        await message.reply_text("⛔ You are banned from using this bot.")
        return

    await users_col.update_one(
        {"_id": user_id},
        {
            "$set": {
                "user_id": user_id,
                "first_name": message.from_user.first_name or "",
                "username": message.from_user.username or "",
            }
        },
        upsert=True,
    )

    url = extract_url(message.text or "")
    if not url:
        await message.reply_text("Please send one valid http:// or https:// link.")
        return

    status = await message.reply_text("⏳ Resolving redirects...")
    try:
        result = await resolve_url(url)
        await status.edit_text(result, disable_web_page_preview=True)
    except asyncio.TimeoutError:
        await status.edit_text("❌ The website timed out. Please try again.")
    except (aiohttp.ClientError, RuntimeError, ValueError) as exc:
        log.warning("Resolve failed for user %s: %s", user_id, exc)
        await status.edit_text("❌ Could not resolve this link.")
    except Exception:
        log.exception("Unexpected resolve error")
        await status.edit_text("❌ An unexpected error occurred.")
    finally:
        gc.collect()


@app.on_message(filters.private & filters.command("stats"))
@owner_only
async def stats_command(_: Client, message: Message):
    total_users = await users_col.count_documents({})
    total_banned = await banned_col.count_documents({})
    await message.reply_text(
        f"📊 Bot Stats\n\n👥 Total users: {total_users}\n🚫 Banned users: {total_banned}"
    )


async def export_ids(collection, path: str) -> int:
    count = 0
    cursor = collection.find({}, {"_id": 1}).sort("_id", 1).batch_size(100)
    try:
        with open(path, "w", encoding="utf-8") as file:
            async for doc in cursor:
                file.write(f"{doc['_id']}\n")
                count += 1
                del doc
    finally:
        await cursor.close()
        gc.collect()
    return count


@app.on_message(filters.private & filters.command("users"))
@owner_only
async def users_command(_: Client, message: Message):
    path = os.path.join(tempfile.gettempdir(), "users.txt")
    try:
        count = await export_ids(users_col, path)
        if count == 0:
            await message.reply_text("No users found.")
        elif count <= 30:
            with open(path, "r", encoding="utf-8") as file:
                ids_text = file.read().strip()
            await message.reply_text("👥 User IDs:\n" + ids_text)
            del ids_text
        else:
            await message.reply_document(path, caption=f"👥 Total users: {count}")
    finally:
        with suppress(FileNotFoundError):
            os.remove(path)
        gc.collect()


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
        await message.reply_text(
            "Usage: /ban <user_id> or reply to a user's message with /ban"
        )
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
        f"✅ User {target_id} has been unbanned."
        if result.deleted_count
        else f"User {target_id} was not in the ban list."
    )


@app.on_message(filters.private & filters.command("banned"))
@owner_only
async def banned_command(_: Client, message: Message):
    path = os.path.join(tempfile.gettempdir(), "banned_users.txt")
    try:
        count = await export_ids(banned_col, path)
        if count == 0:
            await message.reply_text("No banned users.")
        elif count <= 80:
            with open(path, "r", encoding="utf-8") as file:
                ids_text = file.read().strip()
            await message.reply_text("🚫 Banned user IDs:\n" + ids_text)
            del ids_text
        else:
            await message.reply_document(path, caption=f"🚫 Banned users: {count}")
    finally:
        with suppress(FileNotFoundError):
            os.remove(path)
        gc.collect()


@app.on_message(filters.private & filters.command("broadcast"))
@owner_only
async def broadcast_command(client: Client, message: Message):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.reply_text("Usage: /broadcast <message>")
        return

    text_to_send = parts[1].strip()
    total_users = await users_col.count_documents({})
    success = failed = 0
    progress = await message.reply_text(f"📣 Broadcasting to {total_users} users...")
    cursor = users_col.find({}, {"_id": 1}).batch_size(100)

    try:
        async for doc in cursor:
            user_id = doc["_id"]
            del doc
            if await is_banned(user_id):
                continue

            while True:
                try:
                    await client.send_message(
                        user_id,
                        text_to_send,
                        disable_web_page_preview=True,
                    )
                    success += 1
                    break
                except FloodWait as exc:
                    await asyncio.sleep(exc.value + 1)
                except (RPCError, Exception) as exc:
                    log.warning("Broadcast failed for %s: %s", user_id, exc)
                    failed += 1
                    break
    finally:
        await cursor.close()
        del text_to_send
        gc.collect()

    await progress.edit_text(
        f"📣 Broadcast complete.\n\n✅ Success: {success}\n❌ Failed: {failed}"
    )
    gc.collect()


@app.on_message(filters.private & filters.command("restart"))
@owner_only
async def restart_command(_: Client, message: Message):
    await message.reply_text("♻️ Restarting bot process...")
    await asyncio.sleep(1)
    os.execv(sys.executable, [sys.executable] + sys.argv)


async def main():
    global http_session
    connector = aiohttp.TCPConnector(
        limit=2,
        limit_per_host=1,
        force_close=True,
        enable_cleanup_closed=True,
    )
    http_session = aiohttp.ClientSession(
        connector=connector,
        timeout=aiohttp.ClientTimeout(total=10, connect=4),
    )

    try:
        await start_web_server()
        await app.start()
        log.info("Pyrogram bot started")
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
        gc.collect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
