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
from pyrogram import Client, filters, idle
from pyrogram.errors import FloodWait, RPCError
from pyrogram.types import Message

from config import API_ID, API_HASH, BOT_TOKEN, OWNER_ID, MONGO_URI, PORT

# -------------------- Logging --------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("link-bypasser")

# -------------------- Pyrogram --------------------
app = Client(
    "link_bypasser_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workers=1,
    max_concurrent_transmissions=1,
)

# -------------------- MongoDB --------------------
# Motor operations are awaited asynchronously. Driver-level limits and
# DB_OPERATION_TIMEOUT bound how long handlers wait during an Atlas outage.
mongo = AsyncIOMotorClient(
    MONGO_URI,
    maxPoolSize=1,
    minPoolSize=0,
    maxConnecting=1,
    serverSelectionTimeoutMS=3000,
    connectTimeoutMS=3000,
    socketTimeoutMS=4000,
    waitQueueTimeoutMS=3000,
    retryReads=True,
    retryWrites=True,
)
db = mongo["BypasserBotDB"]
users_col = db["users"]
banned_col = db["banned_users"]
DB_OPERATION_TIMEOUT = 4.0

# -------------------- HTTP / health server --------------------
http_session: aiohttp.ClientSession | None = None
web_runner: web.AppRunner | None = None
bypass_semaphore = asyncio.Semaphore(2)
MAX_RESPONSE_BYTES = 262_144  # 256 KiB

META_TAG_RE = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
META_REFRESH_ATTR_RE = re.compile(
    r"""http-equiv\s*=\s*["']?\s*refresh\s*["']?""", re.IGNORECASE
)
META_CONTENT_RE = re.compile(
    r"""content\s*=\s*["']([^"']*?url\s*=\s*[^"']+)["']""", re.IGNORECASE
)
JS_REDIRECT_RE = re.compile(
    r"""(?:window\s*\.\s*)?location(?:\s*\.\s*href)?\s*"""
    r"""(?:=\s*|\.assign\s*\(\s*|\.replace\s*\(\s*)["']([^"']+)["']""",
    re.IGNORECASE,
)


async def db_wait(awaitable, *, fallback=None, operation="MongoDB operation"):
    """Wait a bounded time for a Motor operation; log and return fallback on error."""
    try:
        return await asyncio.wait_for(awaitable, timeout=DB_OPERATION_TIMEOUT)
    except Exception as exc:
        log.warning("%s failed or timed out: %s", operation, exc)
        return fallback


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


def find_page_redirect(body: str, base_url: str) -> str | None:
    """Inspect a bounded HTML string for common Meta Refresh / JS redirects."""
    for tag in META_TAG_RE.findall(body):
        if not META_REFRESH_ATTR_RE.search(tag):
            continue
        content_match = META_CONTENT_RE.search(tag)
        if not content_match:
            continue

        content = content_match.group(1).strip()
        # Accept the common "0; url=https://..." and "url=..." forms.
        target_match = re.search(r"url\s*=\s*(.+)$", content, re.IGNORECASE)
        if target_match:
            target = target_match.group(1).strip().strip(" \t\r\n\"'")
            candidate = urljoin(base_url, target)
            if valid_http_url(candidate):
                return candidate

    js_match = JS_REDIRECT_RE.search(body)
    if js_match:
        candidate = urljoin(base_url, js_match.group(1).strip())
        if valid_http_url(candidate):
            return candidate

    return None


async def is_banned(user_id: int) -> bool:
    # Fail open during DB outages so a database hiccup cannot block /start.
    # The timeout is logged by db_wait.
    result = await db_wait(
        banned_col.find_one({"_id": user_id}, {"_id": 1}),
        fallback=None,
        operation="ban lookup",
    )
    return result is not None


async def register_user(message: Message) -> None:
    user = message.from_user
    if user is None:
        return

    await db_wait(
        users_col.update_one(
            {"_id": user.id},
            {
                "$set": {
                    "user_id": user.id,
                    "first_name": user.first_name or "",
                    "username": user.username or "",
                }
            },
            upsert=True,
        ),
        fallback=None,
        operation="user registration",
    )


async def resolve_url(start_url: str, max_hops: int = 5) -> str:
    """Resolve HTTP redirects and common HTML-based redirect patterns."""
    if http_session is None:
        raise RuntimeError("HTTP session is not initialized")

    timeout = aiohttp.ClientTimeout(total=10, connect=4)
    current_url = start_url

    async with bypass_semaphore:
        for _ in range(max_hops):
            if not valid_http_url(current_url):
                raise RuntimeError("Invalid redirect URL")

            async with http_session.get(
                current_url,
                timeout=timeout,
                allow_redirects=False,
            ) as response:
                # Resolve HTTP redirect headers without reading their bodies.
                if response.status in (301, 302, 303, 307, 308):
                    location = response.headers.get("Location")
                    if not location:
                        final_url = str(response.url)
                        gc.collect()
                        return final_url
                    current_url = urljoin(str(response.url), location)
                    continue

                # Strictly cap page data held in memory at 256 KiB.
                body_bytes = await response.content.read(MAX_RESPONSE_BYTES)
                charset = response.charset or "utf-8"
                body = body_bytes.decode(charset, errors="replace")
                page_redirect = find_page_redirect(body, str(response.url))
                del body_bytes, body

                if page_redirect and page_redirect != current_url:
                    current_url = page_redirect
                    continue

                final_url = str(response.url)
                gc.collect()
                return final_url

        gc.collect()
        return current_url


# -------------------- Health check --------------------
async def health(_: web.Request) -> web.Response:
    return web.Response(text="OK\n", content_type="text/plain")


async def start_web_server() -> None:
    global web_runner
    web_app = web.Application()
    web_app.router.add_get("/", health)
    web_runner = web.AppRunner(web_app, access_log=None)
    await web_runner.setup()
    site = web.TCPSite(web_runner, host="0.0.0.0", port=int(PORT))
    await site.start()
    log.info("Health server listening on 0.0.0.0:%s", PORT)


# -------------------- Owner command guard --------------------
def owner_only(func):
    async def wrapper(client: Client, message: Message):
        if not message.from_user or message.from_user.id != OWNER_ID:
            await message.reply_text("⛔ This command is for the bot owner only.")
            return
        return await func(client, message)

    wrapper.__name__ = func.__name__
    return wrapper


# -------------------- User commands --------------------
@app.on_message(filters.private & filters.command("start"))
async def start_command(_: Client, message: Message):
    sender_id = message.from_user.id if message.from_user else "unknown"
    log.info("Received /start from %s", sender_id)

    if not message.from_user:
        return

    if await is_banned(message.from_user.id):
        await message.reply_text("⛔ You are banned from using this bot.")
        return

    # Respond promptly; Mongo registration is bounded and happens afterward.
    await message.reply_text(
        "👋 Welcome to Link Bypasser Bot!\n\n"
        "Send me a valid http:// or https:// link and I’ll try to resolve its destination."
    )
    await register_user(message)
    gc.collect()


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

    url = extract_url(message.text or "")
    if not url:
        await message.reply_text("Please send one valid http:// or https:// link.")
        return

    status = await message.reply_text("⏳ Resolving redirects...")
    try:
        resolved_url = await resolve_url(url)
        await status.edit_text(resolved_url, disable_web_page_preview=True)
    except asyncio.TimeoutError:
        await status.edit_text("❌ The website timed out. Please try again.")
    except (aiohttp.ClientError, RuntimeError, ValueError) as exc:
        log.warning("Resolve failed for user %s: %s", user_id, exc)
        await status.edit_text("❌ Could not resolve this link.")
    except Exception:
        log.exception("Unexpected resolve error")
        await status.edit_text("❌ An unexpected error occurred.")
    finally:
        await register_user(message)
        gc.collect()


# -------------------- Owner commands --------------------
@app.on_message(filters.private & filters.command("stats"))
@owner_only
async def stats_command(_: Client, message: Message):
    total_users = await db_wait(
        users_col.count_documents({}), fallback=0, operation="user count"
    )
    total_banned = await db_wait(
        banned_col.count_documents({}), fallback=0, operation="ban count"
    )
    await message.reply_text(
        f"📊 Bot Stats\n\n👥 Total users: {total_users}\n🚫 Banned users: {total_banned}"
    )


async def export_ids(collection, path: str) -> int:
    """Stream only IDs into a temporary file using bounded cursor batches."""
    count = 0
    cursor = collection.find({}, {"_id": 1}).sort("_id", 1).batch_size(100)
    try:
        with open(path, "w", encoding="utf-8") as output:
            async for document in cursor:
                output.write(f"{document['_id']}\n")
                count += 1
                del document
    finally:
        with suppress(Exception):
            await cursor.close()
        gc.collect()
    return count


@app.on_message(filters.private & filters.command("users"))
@owner_only
async def users_command(_: Client, message: Message):
    path = os.path.join(tempfile.gettempdir(), "users.txt")
    try:
        count = await db_wait(
            export_ids(users_col, path), fallback=0, operation="users export"
        )
        if count == 0:
            await message.reply_text("No users found (or database unavailable).")
        elif count <= 30:
            with open(path, "r", encoding="utf-8") as input_file:
                ids_text = input_file.read().strip()
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

    result = await db_wait(
        banned_col.update_one(
            {"_id": target_id},
            {"$set": {"user_id": target_id, "banned_by": OWNER_ID}},
            upsert=True,
        ),
        fallback=None,
        operation="ban user",
    )
    if result is None:
        await message.reply_text("⚠️ Database unavailable; could not save ban.")
    else:
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

    result = await db_wait(
        banned_col.delete_one({"_id": target_id}),
        fallback=None,
        operation="unban user",
    )
    if result is None:
        await message.reply_text("⚠️ Database unavailable; could not update ban list.")
    elif result.deleted_count:
        await message.reply_text(f"✅ User {target_id} has been unbanned.")
    else:
        await message.reply_text(f"User {target_id} was not in the ban list.")


@app.on_message(filters.private & filters.command("banned"))
@owner_only
async def banned_command(_: Client, message: Message):
    path = os.path.join(tempfile.gettempdir(), "banned_users.txt")
    try:
        count = await db_wait(
            export_ids(banned_col, path), fallback=0, operation="banned export"
        )
        if count == 0:
            await message.reply_text("No banned users (or database unavailable).")
        elif count <= 80:
            with open(path, "r", encoding="utf-8") as input_file:
                ids_text = input_file.read().strip()
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

    broadcast_text = parts[1].strip()
    total_users = await db_wait(
        users_col.count_documents({}), fallback=0, operation="broadcast count"
    )
    progress = await message.reply_text(f"📣 Broadcasting to {total_users} users...")
    success = failed = 0
    cursor = users_col.find({}, {"_id": 1}).batch_size(100)

    try:
        async for document in cursor:
            user_id = document["_id"]
            del document

            if await is_banned(user_id):
                continue

            try:
                await client.send_message(
                    user_id,
                    broadcast_text,
                    disable_web_page_preview=True,
                )
                success += 1
            except FloodWait as exc:
                await asyncio.sleep(exc.value + 1)
                try:
                    await client.send_message(
                        user_id,
                        broadcast_text,
                        disable_web_page_preview=True,
                    )
                    success += 1
                except Exception as retry_error:
                    log.warning("Broadcast retry failed for %s: %s", user_id, retry_error)
                    failed += 1
            except Exception as exc:
                log.warning("Broadcast failed for %s: %s", user_id, exc)
                failed += 1
    except Exception as exc:
        log.warning("Broadcast cursor failed: %s", exc)
    finally:
        with suppress(Exception):
            await cursor.close()
        del broadcast_text
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


# -------------------- Application lifecycle --------------------
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
        # Both services share this asyncio loop. aiohttp's runner starts its
        # server without blocking; Pyrogram then dispatches updates on the loop.
        await start_web_server()
        await app.start()
        log.info("Pyrogram bot started; awaiting Telegram updates")
        await idle()
    finally:
        with suppress(Exception):
            if app.is_connected:
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
