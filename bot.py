
import asyncio
import html
import json
import logging
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from telegram import Update
from telegram.constants import ChatType
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# -------------------- CONFIG --------------------

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID_RAW = os.getenv("OWNER_ID", "").strip()

if not BOT_TOKEN:
    raise RuntimeError("Missing BOT_TOKEN environment variable.")

if not OWNER_ID_RAW.isdigit():
    raise RuntimeError("OWNER_ID must be your numeric Telegram user ID.")

OWNER_ID = int(OWNER_ID_RAW)

NEWS_URL = "https://watcher.guru/news/"
CHECK_INTERVAL_SECONDS = 60
DATA_FILE = "bot_data.json"
MAX_SEEN_NEWS = 1000
REQUEST_TIMEOUT = 20

USER_AGENT = (
    "Mozilla/5.0 (compatible; CommunityNewsBot/1.0; "
    "+https://watcher.guru/)"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

log = logging.getLogger("pelican-news-bot")

session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT})

data_lock = threading.RLock()
data = {"chats": {}, "seen_news": []}

SIGNATURE = "Signature - CRYPTO OWL 🦉"


# -------------------- DATA STORAGE --------------------

def load_data():
    global data

    try:
        with open(DATA_FILE, "r", encoding="utf-8") as file:
            loaded = json.load(file)

        if isinstance(loaded, dict):
            data = {
                "chats": loaded.get("chats", {}),
                "seen_news": loaded.get("seen_news", []),
            }

    except FileNotFoundError:
        pass

    except Exception:
        log.exception("Could not load saved bot data.")


def save_data():
    with data_lock:
        temporary = DATA_FILE + ".tmp"

        with open(temporary, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)

        os.replace(temporary, DATA_FILE)


def remember_chat(chat):
    if chat.type not in (
        ChatType.GROUP,
        ChatType.SUPERGROUP,
        ChatType.CHANNEL,
    ):
        return

    with data_lock:
        data["chats"][str(chat.id)] = {
            "title": chat.title or str(chat.id),
            "type": chat.type,
        }

    save_data()

    log.info("Registered destination: %s (%s)", chat.title, chat.id)


def forget_chat(chat_id):
    with data_lock:
        data["chats"].pop(str(chat_id), None)

    save_data()


def get_chat_ids():
    with data_lock:
        return [int(chat_id) for chat_id in data["chats"]]


def has_seen(news_id):
    with data_lock:
        return news_id in data["seen_news"]


def mark_seen(news_id):
    with data_lock:
        if news_id not in data["seen_news"]:
            data["seen_news"].append(news_id)
            data["seen_news"] = data["seen_news"][-MAX_SEEN_NEWS:]

    save_data()


# -------------------- WATCHER.GURU SCRAPER --------------------

def image_from_container(container):
    if container is None:
        return None

    for image in container.find_all("img"):
        candidates = [
            image.get("data-src"),
            image.get("data-lazy-src"),
            image.get("data-original"),
            image.get("src"),
        ]

        srcset = image.get("data-srcset") or image.get("srcset")

        if srcset:
            candidates.append(
                srcset.split(",")[-1].strip().split(" ")[0]
            )

        for candidate in candidates:
            if not candidate:
                continue

            candidate = urljoin(NEWS_URL, candidate.strip())
            parsed = urlparse(candidate)

            if parsed.scheme not in ("http", "https"):
                continue

            if candidate.lower().endswith(".svg"):
                continue

            if "logo" in parsed.path.lower():
                continue

            if "icon" in parsed.path.lower():
                continue

            return candidate

    return None


def article_description(article_url):
    """Retrieve a short publisher-provided description."""

    try:
        response = session.get(
            article_url,
            timeout=REQUEST_TIMEOUT,
        )

        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")

        selectors = [
            'meta[property="og:description"]',
            'meta[name="description"]',
            'meta[name="twitter:description"]',
        ]

        for selector in selectors:
            tag = soup.select_one(selector)

            if tag and tag.get("content"):
                return " ".join(tag["content"].split())[:900]

    except requests.RequestException:
        log.warning("Could not retrieve description: %s", article_url)

    return ""


def scrape_latest_news(limit=20):
    response = session.get(
        NEWS_URL,
        timeout=REQUEST_TIMEOUT,
    )

    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    found = []
    seen_urls = set()

    headings = soup.select(
        "article h1, article h2, article h3, article h4"
    )

    if not headings:
        headings = soup.select("h1, h2, h3, h4")

    for heading in headings:

        title = " ".join(
            heading.get_text(" ", strip=True).split()
        )

        if not title or len(title) < 12:
            continue

        link = heading.find_parent("a", href=True)

        if link is None:
            link = heading.find("a", href=True)

        if link is None:
            parent = heading.parent

            for _ in range(4):
                if parent is None:
                    break

                link = parent.find("a", href=True)

                if link:
                    break

                parent = parent.parent

        if link is None:
            continue

        article_url = urljoin(
            NEWS_URL,
            link.get("href", ""),
        )

        parsed = urlparse(article_url)

        if parsed.netloc not in (
            "watcher.guru",
            "www.watcher.guru",
        ):
            continue

        if "/news/" not in parsed.path:
            continue

        if parsed.path.rstrip("/") == "/news":
            continue

        if article_url in seen_urls:
            continue

        container = heading
        image_url = None

        for _ in range(6):
            image_url = image_from_container(container)

            if image_url:
                break

            container = container.parent

            if container is None:
                break

        if not image_url:
            try:
                article_response = session.get(
                    article_url,
                    timeout=REQUEST_TIMEOUT,
                )

                article_response.raise_for_status()

                article_soup = BeautifulSoup(
                    article_response.text,
                    "html.parser",
                )

                meta = article_soup.find(
                    "meta",
                    property="og:image",
                )

                if meta and meta.get("content"):
                    image_url = urljoin(
                        article_url,
                        meta["content"],
                    )

            except requests.RequestException:
                log.warning("Could not retrieve article image.")

        news_id = article_url.rstrip("/")

        description = article_description(article_url)

        found.append({
            "id": news_id,
            "title": title,
            "url": article_url,
            "image": image_url,
            "description": description,
        })

        seen_urls.add(article_url)

        if len(found) >= limit:
            break

    return found


# -------------------- TELEGRAM RICH MESSAGE --------------------

def build_rich_article_html(item):

    title = html.escape(
        item.get("title") or "Latest News"
    )

    description = html.escape(
        item.get("description") or ""
    )

    article_url = html.escape(
        item.get("url") or NEWS_URL,
        quote=True,
    )

    image_url = item.get("image")

    parts = [
        f"<h1>{title}</h1>"
    ]

    if image_url:
        safe_image_url = html.escape(
            image_url,
            quote=True,
        )

        parts.append(
            f'<figure><img src="{safe_image_url}"/>'
            '<figcaption>News image</figcaption></figure>'
        )

    if description:
        parts.append(f"<p>{description}</p>")
    else:
        parts.append(
            "<p>Read the original report for the latest "
            "details and developments.</p>"
        )

    parts.append(
        f'<p><b>Source:</b> Watcher.Guru · '
        f'<a href="{article_url}">Read the full report</a></p>'
    )

    parts.append("<hr/>")

    parts.append(
        f"<footer><b>{html.escape(SIGNATURE)}</b></footer>"
    )

    return "\n".join(parts)


def send_rich_article_sync(chat_id, item):

    endpoint = (
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendRichMessage"
    )

    payload = {
        "chat_id": chat_id,
        "rich_message": {
            "html": build_rich_article_html(item)
        },
    }

    response = requests.post(
        endpoint,
        json=payload,
        timeout=REQUEST_TIMEOUT,
    )

    response.raise_for_status()

    result = response.json()

    if not result.get("ok"):
        raise RuntimeError(
            result.get(
                "description",
                "Telegram Rich Message failed",
            )
        )

    return result.get("result")


async def send_news_to_chat(bot, chat_id, item):
    await asyncio.to_thread(
        send_rich_article_sync,
        chat_id,
        item,
    )


# -------------------- BROADCAST NEWS --------------------

async def broadcast_news(bot, item):

    chat_ids = get_chat_ids()

    sent = 0
    failed = []

    for chat_id in chat_ids:

        try:
            await send_news_to_chat(
                bot,
                chat_id,
                item,
            )

            sent += 1

        except TelegramError as exc:

            failed.append(
                (chat_id, str(exc)[:180])
            )

            error_text = str(exc).lower()

            if (
                "chat not found" in error_text
                or "bot was kicked" in error_text
            ):
                forget_chat(chat_id)

        except Exception as exc:

            failed.append(
                (chat_id, str(exc)[:180])
            )

            log.warning(
                "Delivery failed for %s: %s",
                chat_id,
                exc,
            )

        await asyncio.sleep(0.05)

    return sent, failed


# -------------------- TELEGRAM HANDLERS --------------------

async def register_group_updates(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    member_update = update.my_chat_member

    if not member_update:
        return

    chat = member_update.chat
    new_status = member_update.new_chat_member.status

    if new_status in ("member", "administrator"):
        remember_chat(chat)

    elif new_status in ("left", "kicked"):
        forget_chat(chat.id)


async def register_group_activity(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    chat = update.effective_chat

    if chat and chat.type in (
        ChatType.GROUP,
        ChatType.SUPERGROUP,
        ChatType.CHANNEL,
    ):
        remember_chat(chat)


async def owner_only(update: Update):

    user = update.effective_user

    if not user or user.id != OWNER_ID:

        if update.effective_message:
            await update.effective_message.reply_text(
                "⛔ This command is owner-only."
            )

        return False

    if (
        update.effective_chat
        and update.effective_chat.type != ChatType.PRIVATE
    ):

        await update.effective_message.reply_text(
            "Please use this owner command in the bot's private chat."
        )

        return False

    return True


# -------------------- /POST COMMAND --------------------

async def post_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not await owner_only(update):
        return

    message = update.effective_message

    text = message.text or message.caption or ""

    payload = text.partition(" ")[2].strip()

    if message.photo:

        if not payload:
            await message.reply_text(
                "Send a photo with caption /post Your message"
            )
            return

    elif not payload:

        await message.reply_text(
            "Usage: /post Your announcement here"
        )

        return

    chat_ids = get_chat_ids()

    sent = 0
    failed = []

    for chat_id in chat_ids:

        try:

            if message.photo:

                photo = message.photo[-1]

                await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=photo.file_id,
                    caption=(
                        f"{payload}\n\n{SIGNATURE}"
                    )[:1024],
                )

            else:

                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"{payload}\n\n{SIGNATURE}",
                )

            sent += 1

        except TelegramError as exc:

            failed.append(
                (chat_id, str(exc)[:180])
            )

        await asyncio.sleep(0.05)

    report = (
        "📣 <b>Broadcast report</b>\n\n"
        f"✅ Sent: <b>{sent}</b>\n"
        f"❌ Failed: <b>{len(failed)}</b>\n"
        f"📋 Destinations: <b>{len(chat_ids)}</b>"
    )

    if failed:

        report += (
            "\n\n<b>Failed destinations:</b>\n"
            + "\n".join(
                f"• <code>{chat_id}</code>: "
                f"{html.escape(reason)}"
                for chat_id, reason in failed[:20]
            )
        )

    await context.bot.send_message(
        chat_id=OWNER_ID,
        text=report,
        parse_mode="HTML",
    )


async def photo_post_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    message = update.effective_message
    caption = message.caption or ""

    if re.match(
        r"^/post(?:@\w+)?(?:\s|$)",
        caption,
    ):
        await post_command(update, context)


# -------------------- /EXTRACT COMMAND --------------------

async def extract_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not await owner_only(update):
        return

    status = await update.effective_message.reply_text(
        "🔎 Fetching the latest five stories..."
    )

    try:

        news_items = await asyncio.to_thread(
            scrape_latest_news,
            5,
        )

    except Exception as exc:

        log.exception("Manual extraction failed")

        await status.edit_text(
            f"Could not fetch news: {str(exc)[:300]}"
        )

        return

    if not news_items:

        await status.edit_text(
            "No articles could be extracted from the news page."
        )

        return

    total_sent = 0
    total_failed = 0

    for item in news_items:

        sent, failed = await broadcast_news(
            context.bot,
            item,
        )

        total_sent += sent
        total_failed += len(failed)

    await status.edit_text(
        "✅ <b>Latest 5 news broadcast finished.</b>\n\n"
        f"Stories found: <b>{len(news_items)}</b>\n"
        f"Successful deliveries: <b>{total_sent}</b>\n"
        f"Failed deliveries: <b>{total_failed}</b>",
        parse_mode="HTML",
    )


# -------------------- AUTOMATIC NEWS LOOP --------------------

async def initialize_news_baseline(application: Application):

    try:

        items = await asyncio.to_thread(
            scrape_latest_news,
            30,
        )

        for item in items:
            mark_seen(item["id"])

        log.info(
            "Initialized news baseline with %d stories.",
            len(items),
        )

    except Exception:
        log.exception("Could not initialize news baseline.")


async def automatic_news_job(
    context: ContextTypes.DEFAULT_TYPE,
):

    try:

        items = await asyncio.to_thread(
            scrape_latest_news,
            30,
        )

        new_items = [
            item
            for item in items
            if not has_seen(item["id"])
        ]

        for item in reversed(new_items):

            sent, failed = await broadcast_news(
                context.bot,
                item,
            )

            mark_seen(item["id"])

            log.info(
                "Automatic story sent: %s | success=%d failed=%d",
                item["title"],
                sent,
                len(failed),
            )

    except Exception:
        log.exception("Automatic news check failed.")


# -------------------- RENDER HEALTH SERVER --------------------

class HealthHandler(BaseHTTPRequestHandler):

    def _send_health_response(self, include_body=True):

        if self.path not in (
            "/",
            "/health",
            "/healthz",
        ):

            self.send_response(404)
            self.end_headers()

            if include_body:
                self.wfile.write(b"Not found")

            return

        body = b"Pelican Telegram bot is running."

        self.send_response(200)

        self.send_header(
            "Content-Type",
            "text/plain; charset=utf-8",
        )

        self.send_header(
            "Content-Length",
            str(len(body)),
        )

        self.end_headers()

        if include_body:
            self.wfile.write(body)

    def do_GET(self):
        self._send_health_response(include_body=True)

    def do_HEAD(self):
        self._send_health_response(include_body=False)

    def log_message(self, format_string, *args):
        log.info(
            "HTTP %s - %s",
            self.address_string(),
            format_string % args,
        )


def start_http_server():

    port = int(os.getenv("PORT", "10000"))

    server = ThreadingHTTPServer(
        ("0.0.0.0", port),
        HealthHandler,
    )

    thread = threading.Thread(
        target=server.serve_forever,
        daemon=True,
    )

    thread.start()

    log.info(
        "HTTP health server listening on 0.0.0.0:%s",
        port,
    )

    return server


# -------------------- MAIN --------------------

def main():

    load_data()

    start_http_server()

    async def post_init(app: Application):

        await initialize_news_baseline(app)

        app.job_queue.run_repeating(
            automatic_news_job,
            interval=CHECK_INTERVAL_SECONDS,
            first=CHECK_INTERVAL_SECONDS,
            name="automatic-watcher-guru-news",
        )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    application.add_handler(
        ChatMemberHandler(
            register_group_updates,
            ChatMemberHandler.MY_CHAT_MEMBER,
        )
    )

    application.add_handler(
        CommandHandler("post", post_command)
    )

    application.add_handler(
        CommandHandler("extract", extract_command)
    )

    application.add_handler(
        MessageHandler(
            filters.PHOTO & filters.CAPTION,
            photo_post_command,
        )
    )

    application.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS,
            register_group_activity,
        ),
        group=1,
    )

    log.info("Starting Pelican Telegram bot.")

    # Python 3.14 compatibility fix:
    # Create an event loop before python-telegram-bot calls run_polling().
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    application.run_polling(
        allowed_updates=[
            "message",
            "my_chat_member",
        ],
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
