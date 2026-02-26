import os
import logging
import asyncio
import sqlite3
from datetime import datetime

from aiohttp import web

from dotenv import load_dotenv

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ------------------ ENV ------------------
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBHOOK_BASE = os.getenv("WEBHOOK_BASE", "").strip().rstrip("/")  # https://xxx.onrender.com
TMDB_API_KEY = os.getenv("TMDB_API_KEY", "").strip()
DEFAULT_REGION = os.getenv("DEFAULT_REGION", "UZ").strip().upper()  # UZ recommended
PORT = int(os.getenv("PORT", "10000"))

WEBHOOK_PATH = "/webhook"  # full: https://xxx.onrender.com/webhook

TMDB_API = "https://api.themoviedb.org/3"
TMDB_IMG = "https://image.tmdb.org/t/p/w500"

DB_PATH = os.getenv("DB_PATH", "data.db").strip()

# ------------------ LOGGING ------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("movie-bot")


# ------------------ DB ------------------
def db_init():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS watchlist (
            user_id INTEGER NOT NULL,
            movie_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            year TEXT,
            added_at TEXT NOT NULL,
            PRIMARY KEY (user_id, movie_id)
        )
        """
    )
    con.commit()
    con.close()


def db_add_watch(user_id: int, movie_id: int, title: str, year: str | None):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO watchlist(user_id, movie_id, title, year, added_at) VALUES(?,?,?,?,?)",
        (user_id, movie_id, title, year or "", datetime.utcnow().isoformat()),
    )
    con.commit()
    con.close()


def db_remove_watch(user_id: int, movie_id: int):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("DELETE FROM watchlist WHERE user_id=? AND movie_id=?", (user_id, movie_id))
    con.commit()
    con.close()


def db_list_watch(user_id: int):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        "SELECT movie_id, title, year, added_at FROM watchlist WHERE user_id=? ORDER BY added_at DESC LIMIT 50",
        (user_id,),
    )
    rows = cur.fetchall()
    con.close()
    return rows


# ------------------ UI ------------------
def main_menu_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton("🔎 Kino qidirish"), KeyboardButton("🔥 Trending")],
            [KeyboardButton("⭐ Watchlist"), KeyboardButton("ℹ️ Yordam")],
        ],
        resize_keyboard=True,
    )


def results_inline_kb(results: list[dict]):
    # callback: m:<movie_id>
    buttons = []
    for r in results[:5]:
        title = r.get("title") or "Unknown"
        year = (r.get("release_date") or "")[:4]
        label = f"{title} ({year})" if year else title
        buttons.append([InlineKeyboardButton(label[:60], callback_data=f"m:{r['id']}")])
    buttons.append([InlineKeyboardButton("❌ Bekor qilish", callback_data="cancel")])
    return InlineKeyboardMarkup(buttons)


def movie_actions_kb(movie_id: int):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("⭐ Watchlistga qo‘shish", callback_data=f"w:add:{movie_id}"),
                InlineKeyboardButton("🗑 O‘chirish", callback_data=f"w:del:{movie_id}"),
            ],
            [InlineKeyboardButton("⬅️ Menyu", callback_data="menu")],
        ]
    )


# ------------------ TMDB (HTTP) ------------------
async def tmdb_get_json(session, path: str, params: dict):
    url = f"{TMDB_API}{path}"
    params = dict(params)
    params["api_key"] = TMDB_API_KEY

    async with session.get(url, params=params, timeout=15) as resp:
        if resp.status != 200:
            text = await resp.text()
            raise RuntimeError(f"TMDB error {resp.status}: {text[:200]}")
        return await resp.json()


async def search_movie(session, query: str):
    data = await tmdb_get_json(session, "/search/movie", {"query": query, "include_adult": "false", "language": "en-US"})
    return data.get("results", []) or []


async def get_movie_details(session, movie_id: int):
    return await tmdb_get_json(session, f"/movie/{movie_id}", {"language": "en-US"})


async def get_movie_providers(session, movie_id: int):
    # returns by country codes: { "results": { "UZ": {...}, "US": {...} } }
    return await tmdb_get_json(session, f"/movie/{movie_id}/watch/providers", {})


def format_providers(providers_json: dict, region: str) -> str:
    results = (providers_json or {}).get("results", {}) or {}
    info = results.get(region)

    # fallback: try US if region not found
    used_region = region
    if not info:
        info = results.get("US")
        used_region = "US"

    if not info:
        return "❌ Hozircha rasmiy platformalarda topilmadi."

    def names(key: str) -> list[str]:
        arr = info.get(key) or []
        out = []
        for p in arr:
            name = p.get("provider_name")
            if name and name not in out:
                out.append(name)
        return out

    flatrate = names("flatrate")  # subscription
    rent = names("rent")
    buy = names("buy")

    lines = [f"🎬 Qayerda ko‘rish mumkin ({used_region}):"]
    if flatrate:
        lines.append("📺 Obuna bilan: " + ", ".join(flatrate))
    if rent:
        lines.append("💰 Ijara: " + ", ".join(rent))
    if buy:
        lines.append("🛒 Sotib olish: " + ", ".join(buy))

    if len(lines) == 1:
        return "❌ Hozircha rasmiy platformalarda topilmadi."
    return "\n".join(lines)


# ------------------ BOT HANDLERS ------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎬 Assalomu alaykum!\n\n"
        "Kino nomini yozing — men topib beraman.\n"
        "Tanlaganingizdan keyin: poster, syujet, reyting va qayerda ko‘rish mumkinligini chiqaraman.\n\n"
        "Pastdagi tugmalardan ham foydalanishingiz mumkin.",
        reply_markup=main_menu_kb(),
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ℹ️ Yordam:\n"
        "1) Kino nomini yozing (masalan: Interstellar)\n"
        "2) Bot top 5 variant beradi — birini tanlaysiz\n"
        "3) 'Qayerda ko‘rish mumkin' bo‘limida rasmiy platformalar chiqadi\n\n"
        "🔥 Trending — mashhur kinolar\n"
        "⭐ Watchlist — saqlagan kinolaringiz",
        reply_markup=main_menu_kb(),
    )


async def show_trending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # use message or callback message
    msg = update.effective_message
    await msg.reply_text("🔥 Trending kinolarni yuklayapman...")

    async with context.application.bot_data["http_session"] as session:
        data = await tmdb_get_json(session, "/trending/movie/day", {"language": "en-US"})
        results = data.get("results", [])[:10]

    if not results:
        await msg.reply_text("Hozircha trending topilmadi.", reply_markup=main_menu_kb())
        return

    text_lines = ["🔥 Bugungi TOP kinolar:"]
    kb_items = []
    for r in results[:5]:
        title = r.get("title") or "Unknown"
        year = (r.get("release_date") or "")[:4]
        text_lines.append(f"• {title} ({year})" if year else f"• {title}")
        kb_items.append([InlineKeyboardButton(f"{title} ({year})"[:60], callback_data=f"m:{r['id']}")])

    kb_items.append([InlineKeyboardButton("⬅️ Menyu", callback_data="menu")])
    await msg.reply_text("\n".join(text_lines), reply_markup=InlineKeyboardMarkup(kb_items))


async def show_watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    uid = update.effective_user.id
    rows = db_list_watch(uid)

    if not rows:
        await msg.reply_text("⭐ Watchlist bo‘sh. Kino tanlab ⭐ bosib saqlang.", reply_markup=main_menu_kb())
        return

    lines = ["⭐ Watchlist:"]
    kb = []
    for (movie_id, title, year, _added_at) in rows[:10]:
        label = f"{title} ({year})" if year else title
        lines.append(f"• {label}")
        kb.append([InlineKeyboardButton(label[:60], callback_data=f"m:{movie_id}")])

    kb.append([InlineKeyboardButton("⬅️ Menyu", callback_data="menu")])
    await msg.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(kb))


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        return

    if text == "🔎 Kino qidirish":
        await update.message.reply_text("Kino nomini yozing (masalan: Inception).", reply_markup=main_menu_kb())
        return
    if text == "🔥 Trending":
        await show_trending(update, context)
        return
    if text == "⭐ Watchlist":
        await show_watchlist(update, context)
        return
    if text == "ℹ️ Yordam":
        await help_cmd(update, context)
        return

    # Otherwise treat as movie query
    if not TMDB_API_KEY:
        await update.message.reply_text(
            "TMDB_API_KEY sozlanmagan. Render -> Environment ga TMDB_API_KEY qo‘ying.",
            reply_markup=main_menu_kb(),
        )
        return

    await update.message.reply_text("🔎 Qidiryapman...")

    async with context.application.bot_data["http_session"] as session:
        results = await search_movie(session, text)

    if not results:
        await update.message.reply_text("Hech narsa topilmadi. Boshqa nom bilan urinib ko‘ring.", reply_markup=main_menu_kb())
        return

    # store last results (optional)
    context.user_data["last_query"] = text

    await update.message.reply_text(
        "Topilgan natijalar (birini tanlang):",
        reply_markup=results_inline_kb(results),
    )


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data or ""
    await q.answer()

    if data == "cancel":
        await q.message.reply_text("Bekor qilindi.", reply_markup=main_menu_kb())
        return

    if data == "menu":
        await q.message.reply_text("Menyu:", reply_markup=main_menu_kb())
        return

    if data.startswith("w:add:"):
        try:
            movie_id = int(data.split(":")[2])
        except Exception:
            return
        last_movie = context.user_data.get("last_movie")
        if last_movie and last_movie.get("id") == movie_id:
            title = last_movie.get("title") or "Unknown"
            year = (last_movie.get("release_date") or "")[:4]
        else:
            title = "Movie"
            year = ""
        db_add_watch(update.effective_user.id, movie_id, title, year)
        await q.message.reply_text("⭐ Watchlistga qo‘shildi!", reply_markup=main_menu_kb())
        return

    if data.startswith("w:del:"):
        try:
            movie_id = int(data.split(":")[2])
        except Exception:
            return
        db_remove_watch(update.effective_user.id, movie_id)
        await q.message.reply_text("🗑 Watchlistdan o‘chirildi.", reply_markup=main_menu_kb())
        return

    if data.startswith("m:"):
        if not TMDB_API_KEY:
            await q.message.reply_text("TMDB_API_KEY sozlanmagan.", reply_markup=main_menu_kb())
            return

        movie_id = int(data.split(":")[1])

        async with context.application.bot_data["http_session"] as session:
            details = await get_movie_details(session, movie_id)
            providers = await get_movie_providers(session, movie_id)

        # remember last movie for watchlist add
        context.user_data["last_movie"] = details

        title = details.get("title") or "Unknown"
        year = (details.get("release_date") or "")[:4]
        overview = (details.get("overview") or "—").strip()
        vote = details.get("vote_average")
        genres = ", ".join([g.get("name") for g in (details.get("genres") or []) if g.get("name")]) or "—"

        providers_text = format_providers(providers, DEFAULT_REGION)

        text = (
            f"🎬 *{title}*"
            + (f" ({year})" if year else "")
            + "\n\n"
            f"⭐ Reyting: *{vote}*\n"
            f"🎭 Janr: {genres}\n\n"
            f"📖 {overview}\n\n"
            f"{providers_text}"
        )

        poster_path = details.get("poster_path")
        if poster_path:
            photo_url = f"{TMDB_IMG}{poster_path}"
            await q.message.reply_photo(
                photo=photo_url,
                caption=text[:1020],
                parse_mode="Markdown",
                reply_markup=movie_actions_kb(movie_id),
            )
        else:
            await q.message.reply_text(
                text,
                parse_mode="Markdown",
                reply_markup=movie_actions_kb(movie_id),
            )
        return


# ------------------ AIOHTTP WEB SERVER ------------------
async def handle_ok(request):
    return web.Response(text="OK")


async def handle_webhook(request: web.Request):
    app: Application = request.app["tg_app"]
    try:
        data = await request.json()
    except Exception:
        return web.Response(status=400, text="Bad JSON")

    update = Update.de_json(data, app.bot)
    await app.update_queue.put(update)
    return web.Response(text="ok")


async def on_startup(aio_app: web.Application):
    tg_app: Application = aio_app["tg_app"]
    await tg_app.initialize()
    await tg_app.start()

    webhook_url = f"{WEBHOOK_BASE}{WEBHOOK_PATH}"
    logger.info("Setting webhook: %s", webhook_url)
    await tg_app.bot.set_webhook(
        url=webhook_url,
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


async def on_cleanup(aio_app: web.Application):
    tg_app: Application = aio_app["tg_app"]
    try:
        await tg_app.stop()
        await tg_app.shutdown()
    except Exception:
        pass


def build_aiohttp_app(tg_app: Application) -> web.Application:
    aio_app = web.Application()
    aio_app["tg_app"] = tg_app

    aio_app.router.add_get("/", handle_ok)
    aio_app.router.add_get("/healthz", handle_ok)
    aio_app.router.add_post(WEBHOOK_PATH, handle_webhook)

    aio_app.on_startup.append(on_startup)
    aio_app.on_cleanup.append(on_cleanup)
    return aio_app


# ------------------ ENTRY ------------------
def check_env():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN yo'q. Render -> Environment ga BOT_TOKEN qo‘ying.")
    if not WEBHOOK_BASE:
        raise RuntimeError("WEBHOOK_BASE yo'q. Masalan: https://your-app.onrender.com")


async def main_async():
    check_env()
    db_init()

    # Telegram app
    tg_app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Keep ONE shared aiohttp ClientSession for TMDB
    # We store it in bot_data as an async context manager-like holder.
    # Easiest: create session once and store it; close on exit.
    import aiohttp
    session = aiohttp.ClientSession()
    tg_app.bot_data["http_session"] = session

    # Handlers
    tg_app.add_handler(CommandHandler("start", start))
    tg_app.add_handler(CommandHandler("help", help_cmd))

    tg_app.add_handler(CallbackQueryHandler(on_callback))
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # aiohttp server
    aio_app = build_aiohttp_app(tg_app)

    runner = web.AppRunner(aio_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    logger.info("Listening on 0.0.0.0:%s", PORT)
    await site.start()

    # run forever
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        try:
            await session.close()
        except Exception:
            pass
        await runner.cleanup()


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()

def main():
    check_env()
    db_init()

    # Telegram app
    tg_app = ApplicationBuilder().token(BOT_TOKEN).build()

    tg_app.add_handler(CommandHandler("start", start))
    tg_app.add_handler(CommandHandler("help", help_cmd))
    tg_app.add_handler(CallbackQueryHandler(on_callback))
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # ✅ LOCAL TEST: polling
    if not os.getenv("WEBHOOK_BASE", "").strip():
        print("✅ LOCAL MODE: run_polling()")
        tg_app.run_polling(drop_pending_updates=True)
        return

    # ✅ RENDER MODE: webhook (public URL bor)
    asyncio.run(main_async())