
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
MAX_SEEN_NEWS = 1500
REQUEST_TIMEOUT = 20
LATEST_LIMIT = 5
TRENDING_LIMIT = 10
SIGNATURE = "Signature - CRYPTO OWL 🦉"

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
        log.exception("Could not load saved bot data; starting with empty data.")


def save_data():
    with data_lock:
        temporary = DATA_FILE + ".tmp"
        with open(temporary, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
        os.replace(temporary, DATA_FILE)


def remember_chat(chat):
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP, ChatType.CHANNEL):
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

def _is_article_url(url):
    parsed = urlparse(url)
    return (
        parsed.scheme in ("http", "https")
        and parsed.netloc in ("watcher.guru", "www.watcher.guru")
        and "/news/" in parsed.path
        and parsed.path.rstrip("/") != "/news"
    )


def _image_from_container(container):
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
            candidates.append(srcset.split(",")[-1].strip().split(" ")[0])

        for candidate in candidates:
            if not candidate:
                continue
            candidate = urljoin(NEWS_URL, candidate.strip())
            parsed = urlparse(candidate)
            path = parsed.path.lower()
            if (
                parsed.scheme in ("http", "https")
                and not path.endswith(".svg")
                and "logo" not in path
                and "icon" not in path
            ):
                return candidate
    return None


def _article_description(article_url):
    """Use publisher metadata, not copied article body text."""
    try:
        response = session.get(article_url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        for selector in (
            'meta[property="og:description"]',
            'meta[name="description"]',
            'meta[name="twitter:description"]',
        ):
            tag = soup.select_one(selector)
            if tag and tag.get("content"):
                return " ".join(tag["content"].split())[:700]
    except requests.RequestException:
        log.warning("Could not retrieve article description: %s", article_url)
    return ""


def _article_image_from_page(article_url):
    try:
        response = session.get(article_url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        meta = soup.find("meta", property="og:image")
        if meta and meta.get("content"):
            image_url = urljoin(article_url, meta["content"])
            if urlparse(image_url).scheme in ("http", "https"):
                return image_url
    except requests.RequestException:
        log.warning("Could not retrieve article image: %s", article_url)
    return None


def _make_article_item(anchor, section_name):
    article_url = urljoin(NEWS_URL, anchor.get("href", ""))
    if not _is_article_url(article_url):
        return None

    title_node = anchor.find(re.compile(r"^h[1-6]$"))
    title = title_node.get_text(" ", strip=True) if title_node else ""
    if not title:
        title = anchor.get_text(" ", strip=True)
    title = " ".join(title.split())

    if len(title) < 10:
        image = anchor.find("img")
        title = (image.get("alt", "") if image else "").strip()
    if len(title) < 10:
        return None

    container = anchor
    image_url = None
    for _ in range(5):
        image_url = _image_from_container(container)
        if image_url:
            break
        container = container.parent
        if container is None:
            break

    if not image_url:
        image_url = _article_image_from_page(article_url)

    return {
        "id": article_url.rstrip("/"),
        "title": title,
        "url": article_url,
        "image": image_url,
        "description": _article_description(article_url),
        "sections": [section_name],
    }


def _find_section_articles(soup, heading_phrases, section_name, limit):
    headings = soup.find_all(re.compile(r"^h[1-6]$"))
    target_heading = None

    for heading in headings:
        heading_text = " ".join(heading.get_text(" ", strip=True).lower().split())
        if any(phrase in heading_text for phrase in heading_phrases):
            target_heading = heading
            break

    if target_heading is None:
        return []

    selected_links = []
    ancestor = target_heading

    for _ in range(9):
        if ancestor is None:
            break

        candidate_links = []
        candidate_urls = set()

        for link in ancestor.find_all("a", href=True):
            url = urljoin(NEWS_URL, link.get("href", ""))
            if _is_article_url(url) and url not in candidate_urls:
                candidate_urls.add(url)
                candidate_links.append(link)

        if len(candidate_links) >= 3:
            selected_links = candidate_links
            break

        ancestor = ancestor.parent

    articles = []
    seen = set()

    for link in selected_links:
        item = _make_article_item(link, section_name)
        if not item or item["id"] in seen:
            continue

        seen.add(item["id"])
        articles.append(item)

        if len(articles) >= limit:
            break

    return articles


def _fallback_recent_articles(soup, limit):
    articles = []
    seen = set()

    for heading in soup.select("article h1, article h2, article h3, article h4"):
        link = heading.find_parent("a", href=True)
        if link is None:
            link = heading.find("a", href=True)
        if link is None:
            continue

        item = _make_article_item(link, "Latest News")
        if not item or item["id"] in seen:
            continue

        seen.add(item["id"])
        articles.append(item)

        if len(articles) >= limit:
            break

    return articles


def scrape_news_bundle():
    """Return latest five articles plus this week's trending stories."""
    response = session.get(NEWS_URL, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")

    latest = _find_section_articles(
        soup,
        ["recent articles"],
        "Latest News",
        LATEST_LIMIT,
    )

    if not latest:
        latest = _fallback_recent_articles(soup, LATEST_LIMIT)

    trending = _find_section_articles(
        soup,
        ["trending posts", "weekly news"],
        "This Week's Trending",
        TRENDING_LIMIT,
    )

    # Merge duplicates but preserve both section labels.
    merged = {}

    for item in latest + trending:
        existing = merged.get(item["id"])

        if existing:
            for label in item["sections"]:
                if label not in existing["sections"]:
                    existing["sections"].append(label)
        else:
            merged[item["id"]] = item

    result = list(merged.values())

    log.info(
        "Scraped %d latest articles and %d trending entries (%d unique).",
        len(latest),
        len(trending),
        len(result),
    )

    return result, len(latest), len(trending)


# -------------------- ARTICLE-STYLE TELEGRAM POST --------------------

def build_rich_article_html(item):
    title = html.escape(item.get("title") or "Latest News")
    description = html.escape(item.get("description") or "")
    image_url = item.get("image")
    sections = item.get("sections", [])

    if "Latest News" in sections and "This Week's Trending" in sections:
        category_label = "LATEST NEWS  •  WEEKLY TRENDING"
    elif "This Week's Trending" in sections:
        category_label = "THIS WEEK'S TRENDING"
    else:
        category_label = "LATEST NEWS"

    parts = [
        f"<p><b>🦉 CRYPTO OWL  |  {html.escape(category_label)}</b></p>",
        f"<h1>📰 {title}</h1>",
    ]

    if image_url:
        safe_image_url = html.escape(image_url, quote=True)
        parts.append(
            f'<figure><img src="{safe_image_url}"/>'
            "<figcaption>Market news update</figcaption></figure>"
        )

    parts.append("<h3>THE STORY</h3>")

    if description:
        parts.append(f"<p>{description}</p>")
    else:
        parts.append(
            "<p>Open the full report to read the latest details and developments.</p>"
        )

    parts.extend([
        "<p><i>Source: Watcher.Guru</i></p>",
        "<hr/>",
        f"<footer><b>{html.escape(SIGNATURE)}</b></footer>",
    ])

    return "\n".join(parts)


def send_rich_article_sync(chat_id, item):
    """Send rich article with a native blue Telegram URL button."""
    endpoint = f"https://api.telegram.org/bot{BOT_TOKEN}/sendRichMessage"
    article_url = item.get("url") or NEWS_URL

    payload = {
        "chat_id": chat_id,
        "rich_message": {
            "html": build_rich_article_html(item)
        },
        "reply_markup": {
            "inline_keyboard": [[
                {
                    "text": "🔵 READ MORE",
                    "url": article_url
                }
            ]]
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
            result.get("description", "Telegram Rich Message failed")
        )

    return result.get("result")


async def send_news_to_chat(bot, chat_id, item):
    await asyncio.to_thread(
        send_rich_article_sync,
        chat_id,
        item,
    )


async def broadcast_news(bot, item):
    chat_ids = get_chat_ids()
    sent = 0
    failed = []

    for chat_id in chat_ids:
        try:
            await send_news_to_chat(bot, chat_id, item)
            sent += 1

        except TelegramError as exc:
            failed.append((chat_id, str(exc)[:180]))
            error_text = str(exc).lower()

            if "chat not found" in error_text or "bot was kicked" in error_text:
                forget_chat(chat_id)

        except Exception as exc:
            failed.append((chat_id, str(exc)[:180]))
            log.warning("Delivery failed for %s: %s", chat_id, exc)

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


async def post_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not await owner_only(update):
        return

    message = update.effective_message
    original_text = message.text or message.caption or ""
    payload = original_text.partition(" ")[2].strip()

    if message.photo:
        if not payload:
            await message.reply_text(
                "Send a photo with caption `/post your message`, "
                "or use `/post your text`.",
                parse_mode="Markdown",
            )
            return
    elif not payload:
        await message.reply_text("Usage: /post Your announcement here")
        return

    chat_ids = get_chat_ids()
    sent = 0
    failed = []

    for chat_id in chat_ids:
        try:
            if message.photo:
                await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=message.photo[-1].file_id,
                    caption=f"{payload}\n\n{SIGNATURE}"[:1024],
                )
            else:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"{payload}\n\n{SIGNATURE}",
                )

            sent += 1

        except TelegramError as exc:
            failed.append((chat_id, str(exc)[:180]))

        await asyncio.sleep(0.05)

    report = (
        "📣 <b>Broadcast report</b>\n\n"
        f"✅ Sent: <b>{sent}</b>\n"
        f"❌ Failed: <b>{len(failed)}</b>\n"
        f"📋 Destinations: <b>{len(chat_ids)}</b>"
    )

    if failed:
        report += "\n\n<b>Failed destinations:</b>\n" + "\n".join(
            f"• <code>{chat_id}</code>: {html.escape(reason)}"
            for chat_id, reason in failed[:20]
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
    caption = update.effective_message.caption or ""

    if re.match(r"^/post(?:@\w+)?(?:\s|$)", caption):
        await post_command(update, context)


async def extract_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not await owner_only(update):
        return

    status = await update.effective_message.reply_text(
        "🔎 Fetching the latest 5 stories and this week's trending posts..."
    )

    try:
        items, latest_count, trending_count = await asyncio.to_thread(
            scrape_news_bundle
        )

    except Exception as exc:
        log.exception("Manual extraction failed")
        await status.edit_text(
            f"Could not fetch news: {str(exc)[:300]}"
        )
        return

    if not items:
        await status.edit_text(
            "No articles could be extracted. The website may have changed its layout."
        )
        return

    total_sent = 0
    total_failed = 0

    for item in items:
        sent, failed = await broadcast_news(context.bot, item)
        total_sent += sent
        total_failed += len(failed)
        mark_seen(item["id"])

    await status.edit_text(
        "✅ <b>News broadcast finished.</b>\n\n"
        f"Latest stories found: <b>{latest_count}</b>\n"
        f"Weekly trending entries found: <b>{trending_count}</b>\n"
        f"Unique articles posted: <b>{len(items)}</b>\n"
        f"Successful deliveries: <b>{total_sent}</b>\n"
        f"Failed deliveries: <b>{total_failed}</b>",
        parse_mode="HTML",
    )


# -------------------- AUTOMATIC NEWS LOOP --------------------

async def initialize_news_baseline(application: Application):
    """Record current stories so startup does not flood the groups."""
    try:
        items, latest_count, trending_count = await asyncio.to_thread(
            scrape_news_bundle
        )

        for item in items:
            mark_seen(item["id"])

        log.info(
            "Initialized baseline: %d latest, %d trending, %d unique.",
            latest_count,
            trending_count,
            len(items),
        )

    except Exception:
        log.exception(
            "Could not initialize news baseline; scheduled checks will retry."
        )


async def automatic_news_job(
    context: ContextTypes.DEFAULT_TYPE,
):
    try:
        items, latest_count, trending_count = await asyncio.to_thread(
            scrape_news_bundle
        )

        new_items = [
            item for item in items
            if not has_seen(item["id"])
        ]

        # Send oldest first when several stories appear between checks.
        for item in reversed(new_items):
            sent, failed = await broadcast_news(context.bot, item)
            mark_seen(item["id"])

            log.info(
                "Automatic story: %s | success=%d failed=%d",
                item["title"],
                sent,
                len(failed),
            )

    except Exception:
        log.exception("Automatic news check failed.")


# -------------------- RENDER HTTP SERVER --------------------

class HealthHandler(BaseHTTPRequestHandler):

    def _send_health_response(self, include_body=True):
        if self.path not in ("/", "/health", "/healthz"):
            self.send_response(404)
            self.end_headers()

            if include_body:
                self.wfile.write(b"Not found")
            return

        body = b"Pelican Telegram bot is running."

        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
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

    threading.Thread(
        target=server.serve_forever,
        daemon=True,
    ).start()

    log.info("HTTP health server listening on 0.0.0.0:%s", port)
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

    application.add_handler(CommandHandler("post", post_command))
    application.add_handler(CommandHandler("extract", extract_command))

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

    # Python 3.14 compatibility.
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    application.run_polling(
        allowed_updates=["message", "my_chat_member"],
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
