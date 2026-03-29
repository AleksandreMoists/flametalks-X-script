import asyncio
import sqlite3
import json
import os
import sys
import logging
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from twikit import Client

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("x_monitor")

# ── Config ──────────────────────────────────────────────────────────
X_USERNAME = os.getenv("X_USERNAME", "")
X_EMAIL = os.getenv("X_EMAIL", "")
X_PASSWORD = os.getenv("X_PASSWORD", "")

TG_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

COOKIES_FILE = Path(__file__).parent / "cookies.json"
DB_FILE = Path(__file__).parent / "seen_posts.db"

SEARCH_QUERIES = [
    '"CharacterAI alternative"',
    '"character ai alternative"',
    '"uncensored AI chat"',
    '"uncensored AI bot"',
    '"AI girlfriend" bot',
    '"AI companion" chat',
    '"tired of CharacterAI"',
    '"leaving CharacterAI"',
    '"CharacterAI sucks"',
    '"NSFW AI" chat',
    '"NSFW chatbot"',
    "JanitorAI alternative",
    "SpicyChat alternative",
    "CrushonAI alternative",
]

MAX_RESULTS_PER_QUERY = 5
SCAN_INTERVAL_SECONDS = 900  # 15 minutes


# ── SQLite dedup ────────────────────────────────────────────────────
def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS seen (
            tweet_id TEXT PRIMARY KEY,
            query    TEXT,
            seen_at  TEXT
        )"""
    )
    conn.commit()
    return conn


def is_seen(conn: sqlite3.Connection, tweet_id: str) -> bool:
    row = conn.execute("SELECT 1 FROM seen WHERE tweet_id = ?", (tweet_id,)).fetchone()
    return row is not None


def mark_seen(conn: sqlite3.Connection, tweet_id: str, query: str):
    conn.execute(
        "INSERT OR IGNORE INTO seen (tweet_id, query, seen_at) VALUES (?, ?, ?)",
        (tweet_id, query, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


# ── Telegram helpers ────────────────────────────────────────────────
def send_telegram(text: str, reply_markup: dict | None = None) -> dict | None:
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TG_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    resp = requests.post(url, json=payload, timeout=10)
    if not resp.ok:
        log.warning("Telegram send failed: %s", resp.text)
        return None
    return resp.json().get("result")


def get_telegram_updates(offset: int) -> list:
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/getUpdates"
    resp = requests.get(
        url,
        params={"offset": offset, "timeout": 5, "allowed_updates": '["message"]'},
        timeout=15,
    )
    if not resp.ok:
        return []
    return resp.json().get("result", [])


def format_notification(tweet, query: str) -> str:
    username = tweet.user.screen_name if tweet.user else "unknown"
    name = tweet.user.name if tweet.user else "Unknown"
    text = (tweet.text or "")[:280]
    tweet_url = f"https://x.com/{username}/status/{tweet.id}"

    likes = tweet.favorite_count or 0
    retweets = tweet.retweet_count or 0
    replies = tweet.reply_count or 0

    return (
        f"<b>New lead on X</b>\n"
        f"<b>{name}</b> (@{username})\n\n"
        f"<i>{text}</i>\n\n"
        f"Likes {likes} | RT {retweets} | Replies {replies}\n"
        f"Query: <code>{query}</code>\n"
        f'<a href="{tweet_url}">View tweet</a>\n\n'
        f"<b>Reply to this message to post a reply on X</b>\n"
        f"<code>tweet_id:{tweet.id}</code>"
    )


# ── X client ────────────────────────────────────────────────────────
async def get_x_client() -> Client:
    client = Client("en-US")
    if COOKIES_FILE.exists():
        log.info("Loading saved cookies...")
        client.load_cookies(str(COOKIES_FILE))
    else:
        log.info("Logging in to X as @%s...", X_USERNAME)
        await client.login(
            auth_info_1=X_USERNAME,
            auth_info_2=X_EMAIL,
            password=X_PASSWORD,
        )
        client.save_cookies(str(COOKIES_FILE))
        log.info("Logged in and cookies saved.")
    return client


# ── Scan for leads ──────────────────────────────────────────────────
async def scan_leads(client: Client, conn: sqlite3.Connection) -> int:
    new_leads = 0
    for query in SEARCH_QUERIES:
        log.info("Searching: %s", query)
        try:
            tweets = await client.search_tweet(query, "Latest", count=MAX_RESULTS_PER_QUERY)
        except Exception as e:
            log.warning("Search failed for '%s': %s", query, e)
            continue

        for tweet in tweets:
            if is_seen(conn, tweet.id):
                continue

            mark_seen(conn, tweet.id, query)
            msg = format_notification(tweet, query)
            send_telegram(msg, reply_markup={"force_reply": True, "selective": True})
            new_leads += 1
            log.info(
                "New lead: @%s — %s",
                tweet.user.screen_name if tweet.user else "?",
                tweet.id,
            )

        await asyncio.sleep(2)

    return new_leads


# ── Process replies from Telegram ───────────────────────────────────
async def process_replies(client: Client, offset: int) -> int:
    updates = get_telegram_updates(offset)
    new_offset = offset

    for update in updates:
        new_offset = update["update_id"] + 1
        message = update.get("message")
        if not message:
            continue

        # Only process replies from the admin
        if str(message.get("chat", {}).get("id")) != TG_CHAT_ID:
            continue

        # Must be a reply to a lead notification
        reply_to = message.get("reply_to_message")
        if not reply_to:
            continue

        # Extract tweet_id from the original notification
        original_text = reply_to.get("text", "")
        tweet_id = None
        for line in original_text.split("\n"):
            if line.startswith("tweet_id:"):
                tweet_id = line.replace("tweet_id:", "").strip()
                break

        if not tweet_id:
            continue

        reply_text = message.get("text", "").strip()
        if not reply_text:
            continue

        # Post reply on X
        try:
            await client.create_tweet(reply_text, reply_to=tweet_id)
            send_telegram(f"Posted reply to tweet {tweet_id}")
            log.info("Replied to tweet %s: %s", tweet_id, reply_text[:50])
        except Exception as e:
            send_telegram(f"Failed to reply: {e}")
            log.error("Failed to reply to tweet %s: %s", tweet_id, e)

    return new_offset


# ── Main loop ───────────────────────────────────────────────────────
async def main():
    if not all([X_USERNAME, X_PASSWORD, TG_BOT_TOKEN, TG_CHAT_ID]):
        log.error("Missing required env vars. Check .env file.")
        sys.exit(1)

    client = await get_x_client()
    conn = init_db()
    tg_offset = 0

    # If run with --once flag, do a single scan and exit (for cron)
    if "--once" in sys.argv:
        new_leads = await scan_leads(client, conn)
        tg_offset = await process_replies(client, tg_offset)
        log.info("Single run done. %d new leads.", new_leads)
        conn.close()
        return

    # Persistent mode: scan + listen for replies in a loop
    log.info("Starting persistent monitor (scan every %ds)...", SCAN_INTERVAL_SECONDS)
    last_scan = 0

    while True:
        now = asyncio.get_event_loop().time()

        # Scan for leads on interval
        if now - last_scan >= SCAN_INTERVAL_SECONDS:
            new_leads = await scan_leads(client, conn)
            log.info("Scan complete. %d new leads.", new_leads)
            last_scan = now

        # Check for Telegram replies every 5 seconds
        tg_offset = await process_replies(client, tg_offset)
        await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
