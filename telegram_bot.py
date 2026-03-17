#!/usr/bin/env python3
"""
ESILV Telegram Bot
- Presence alerts (notifies when attendance opens)
- Smart auto-presence for remote/visio classes (auto-marks after 60s unless cancelled)
- Grade notifications (notifies when new grades appear)
"""

import asyncio
import fcntl
import time
import json
import logging
import os
import sys
from html import escape as html_escape

import requests
from yaml import load, Loader

import ldv_dashbot
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes


# ── Config ────────────────────────────────────────────────────────────────────

CONFIG_FILE = os.environ.get("ESILV_BOT_CONFIG", "telegram_config.yaml")

try:
    with open(CONFIG_FILE) as f:
        cfg = load(f, Loader=Loader)
except FileNotFoundError:
    print(f"Config file not found: {CONFIG_FILE}")
    print("Copy telegram_config.example.yaml to telegram_config.yaml and fill in your credentials.")
    sys.exit(1)

logging.basicConfig(
    level=getattr(logging, cfg.get("log_level", "INFO")),
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
log = logging.getLogger("esilv-bot")

TIMEOUT = cfg.get("auto_presence_timeout", 60)


# ── State ─────────────────────────────────────────────────────────────────────

api: ldv_dashbot.Api = None
scraper: ldv_dashbot.Bot = None
calendar: ldv_dashbot.ICSCalendar = None

known_presences: dict = {}       # seance_id -> presence dict from API
pending_tasks: dict = {}         # seance_id -> asyncio.Task (auto-present timer)
last_calendar_refresh: float = 0 # timestamp of last ICS calendar refresh
CALENDAR_REFRESH_INTERVAL = 300  # refresh calendar every 5 minutes
pending_messages: dict = {}      # seance_id -> telegram Message object


# ── Helpers ───────────────────────────────────────────────────────────────────

def h(text) -> str:
    """Escape text for Telegram HTML parse mode."""
    return html_escape(str(text))


def _is_open(p: dict) -> bool:
    """Check if a presence's attendance is currently open."""
    s = p.get("etat_ouverture")
    return s is not None and s not in ("ferme", "fermé")


def is_remote(p: dict, room: str = None) -> bool:
    """Detect remote/visio class: name contains 'CMo' or room is 'ZOOM'."""
    return "CMo" in p.get("nom", "") or (room and room.upper() == "ZOOM")


def _get_room(p: dict) -> str:
    """Look up the room/location from the ICS calendar for a given presence."""
    if calendar is None:
        return None
    try:
        date = p["date"].split("-")
        start = p["horaire"].split(" ")[0].split(":")
        ts = "".join(date) + "T" + "".join(start)
        for ev in calendar.data.get("VEVENT", []):
            if ev.get("DTSTART") == ts:
                return ev.get("LOCATION")
    except Exception:
        log.debug("Could not look up room for %s", p.get("nom"))
    return None


def _refresh_calendar():
    """Fetch and parse the ICS calendar feed."""
    global calendar, last_calendar_refresh
    try:
        profile = api.get_profile()
        token = profile.get("ical_token")
        if not token:
            log.warning("No ical_token in profile, room info unavailable")
            return
        url = ldv_dashbot.API_STUDENT_ICAL.format(token)
        calendar = ldv_dashbot.ICSCalendar(requests.get(url).text)
        last_calendar_refresh = time.time()
        log.info("ICS calendar loaded (%d events)", len(calendar.data.get("VEVENT", [])))
    except Exception:
        log.exception("Failed to load ICS calendar")


# ── Presence Polling ──────────────────────────────────────────────────────────

async def presence_loop(app: Application):
    global api
    loop = asyncio.get_event_loop()

    log.info("Initializing API (OAuth2)...")
    api = await loop.run_in_executor(
        None, lambda: ldv_dashbot.Api(cfg["email"], cfg["password"])
    )
    log.info("API ready. Polling presences every %ds...", cfg.get("poll_presence", 10))

    # Load ICS calendar for room info
    await loop.run_in_executor(None, _refresh_calendar)

    # Seed known_presences and find current class for startup message
    presences = await loop.run_in_executor(None, api.get_presences)
    current_subject = None
    current_room = None
    for p in presences:
        if _is_open(p):
            known_presences[p["seance_id"]] = p
            if current_subject is None:
                current_subject = p.get("nom")
                current_room = _get_room(p)

    if current_subject:
        text = f"Bot en marche, tu es actuellement en {h(current_subject)}"
        if current_room:
            text += f" (salle {h(current_room)})"
        text += "."
    else:
        text = "Bot en marche, aucun cours en ce moment."
    await app.bot.send_message(chat_id=cfg["chat_id"], text=text, parse_mode="HTML")

    while True:
        try:
            # Periodic calendar refresh every 5 minutes
            if time.time() - last_calendar_refresh >= CALENDAR_REFRESH_INTERVAL:
                await loop.run_in_executor(None, _refresh_calendar)

            presences = await loop.run_in_executor(None, api.get_presences)

            current_ids = set()
            for p in presences:
                sid = p["seance_id"]
                current_ids.add(sid)
                is_open = _is_open(p)

                if is_open and sid not in known_presences:
                    # Refresh calendar on new presence for room lookup
                    await loop.run_in_executor(None, _refresh_calendar)
                    known_presences[sid] = p
                    await _notify_presence(app, p)
                elif not is_open:
                    known_presences.pop(sid, None)

            # Remove presences no longer returned by API
            for sid in list(known_presences):
                if sid not in current_ids:
                    known_presences.pop(sid, None)

        except Exception:
            log.exception("Presence poll error")

        await asyncio.sleep(cfg.get("poll_presence", 10))


async def _notify_presence(app: Application, p: dict):
    """Send a Telegram notification for a newly opened presence."""
    sid = p["seance_id"]
    name = h(p.get("nom", "?"))
    room = _get_room(p)
    chat_id = cfg["chat_id"]

    room_text = f" (salle {h(room)})" if room else ""

    if is_remote(p, room):
        kb = [[InlineKeyboardButton("\U0001f534 Absent", callback_data=f"skip:{sid}")]]

        text = f"L'appel pour {name}{room_text} est ouvert.\nAuto-presence dans {TIMEOUT}s..."

        zoom = p.get("zoom_url")
        if zoom:
            text += f'\n<a href="{h(zoom)}">Lien Zoom</a>'

        msg = await app.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(kb),
        )
        pending_messages[sid] = msg
        pending_tasks[sid] = asyncio.create_task(_auto_present(app, sid))
        log.info("Remote presence opened: %s (seance %d) — timer started", p.get("nom"), sid)

    else:
        text = f"L'appel pour {name}{room_text} est ouvert."
        await app.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
        log.info("In-person presence opened: %s (seance %d)", p.get("nom"), sid)


async def _auto_present(app: Application, sid: int):
    """Wait TIMEOUT seconds, then auto-mark present via API."""
    loop = asyncio.get_event_loop()
    p = known_presences.get(sid, {})
    name = h(p.get("nom", "?"))

    try:
        await asyncio.sleep(TIMEOUT)

        result = await loop.run_in_executor(None, api.set_present, sid)
        log.info("Auto-marked present for seance %d: %s", sid, result)

        msg = pending_messages.pop(sid, None)
        if msg:
            await msg.edit_text("\u2705 Marked as present")

    except asyncio.CancelledError:
        pass
    except Exception as e:
        log.exception("Auto-present failed for seance %d", sid)
        msg = pending_messages.pop(sid, None)
        if msg:
            await msg.edit_text(
                f"Erreur auto-presence pour {name} : <code>{h(e)}</code>",
                parse_mode="HTML",
            )
    finally:
        pending_tasks.pop(sid, None)


async def on_skip_callback(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    """Handle the 'stay absent' button press."""
    query = update.callback_query
    await query.answer()

    sid = int(query.data.split(":")[1])

    task = pending_tasks.pop(sid, None)
    if task:
        task.cancel()
    pending_messages.pop(sid, None)

    await query.edit_message_text("\u274c Stayed absent")
    log.info("User chose to stay absent for seance %d", sid)


# ── Grade Polling ─────────────────────────────────────────────────────────────

async def grades_loop(app: Application):
    global scraper
    loop = asyncio.get_event_loop()

    cache_file = "data/grades_tg.json"
    os.makedirs("data", exist_ok=True)

    log.info("Initializing scraper (crawler) for grades...")
    try:
        scraper = await loop.run_in_executor(
            None,
            lambda: ldv_dashbot.Bot(
                cfg["email"], cfg["password"], cookies_cache="data/cookies_tg.cache"
            ),
        )
    except Exception:
        log.exception("Failed to initialize scraper — grade notifications disabled")
        return
    log.info("Scraper ready. Polling grades every %ds...", cfg.get("poll_grades", 60))

    # Load cached grades
    old_data = None
    try:
        with open(cache_file) as f:
            old_data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    while True:
        try:
            raw = await loop.run_in_executor(None, scraper.get_grades)
            new_data = ldv_dashbot.DataClass.json(raw)

            if old_data is not None:
                await _diff_grades(app, old_data, new_data)

            old_data = new_data
            with open(cache_file, "w") as f:
                json.dump(new_data, f, indent=2)

        except Exception:
            log.exception("Grade poll error")

        await asyncio.sleep(cfg.get("poll_grades", 60))


def _grade_map(data: list) -> dict:
    """Flatten the semester/unit/subject/grade tree into a flat dict keyed by (unit, subject, exam)."""
    m = {}
    for sem in data:
        for unit in sem.get("units", []):
            uname = unit.get("name", "?")
            for subj in unit.get("subjects", []):
                sname = subj.get("name", "?")
                sid = subj.get("id", "")
                label = f"{sid} {sname}".strip()

                for g in subj.get("grades", []):
                    m[(uname, label, g.get("name", "?"))] = g

                if subj.get("final_grade") is not None:
                    m[(uname, label, "@final")] = {
                        "grade": subj["final_grade"],
                        "max_grade": subj.get("max_grade"),
                        "promo_average": subj.get("promo_average"),
                    }
    return m


async def _diff_grades(app: Application, old_data, new_data):
    """Compare old and new grade snapshots, notify on newly available grades."""
    old_map = _grade_map(old_data)
    new_map = _grade_map(new_data)

    for key, new_g in new_map.items():
        new_val = new_g.get("grade")
        old_g = old_map.get(key)
        old_val = old_g.get("grade") if old_g else None

        if new_val is not None and old_val is None:
            await _send_grade_notification(app, key, new_g)


async def _send_grade_notification(app: Application, key: tuple, g: dict):
    """Send a Telegram message for a new grade."""
    _unit, subj, exam = key

    lines = ["<b>Nouvelle note</b>\n"]
    lines.append(f"Matiere : {h(subj)}")

    if exam == "@final":
        lines.append("Note finale")
    else:
        lines.append(f"Examen : {h(exam)}")

    if g.get("grade") is not None:
        lines.append(f"Note : <b>{g['grade']} / {g.get('max_grade', 20)}</b>")
    if g.get("promo_average"):
        lines.append(f"Moyenne promo : {g['promo_average']} / {g.get('max_grade', 20)}")

    await app.bot.send_message(
        chat_id=cfg["chat_id"],
        text="\n".join(lines),
        parse_mode="HTML",
    )
    log.info("Grade notification sent: %s", key)


# ── Mock Commands ─────────────────────────────────────────────────────────────

async def cmd_mockattendance(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    """Send a fake attendance notification for display testing."""
    await update.message.reply_text(
        "L'appel pour Mathematiques S6 (salle S305) est ouvert.",
        parse_mode="HTML",
    )


async def cmd_mockgrade(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    """Send a fake grade notification for display testing."""
    lines = [
        "<b>Nouvelle note</b>\n",
        "Matiere : INF203 Algorithmes avances",
        "Examen : Partiel S6",
        "Note : <b>15.5 / 20</b>",
        "Moyenne promo : 12.3 / 20",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ── Bot Commands ──────────────────────────────────────────────────────────────

async def cmd_start(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    status = await _status_text()
    await update.message.reply_text(
        "<b>ESILV Telegram Bot</b>\n\n"
        "<b>Notifications :</b>\n"
        "- Appels de presence ouverts\n"
        "- Auto-presence pour les cours en visio\n"
        "- Nouvelles notes\n\n"
        f"{status}\n\n"
        "<b>Commandes :</b>\n"
        "/start - Ce message\n"
        "/status - Etat du bot\n"
        "/mockattendance - Exemple de notification d'appel\n"
        "/mockgrade - Exemple de notification de note",
        parse_mode="HTML",
    )


async def _status_text() -> str:
    """Build the status message, same format as startup."""
    loop = asyncio.get_event_loop()
    try:
        presences = await loop.run_in_executor(None, api.get_presences)
        for p in presences:
            if _is_open(p):
                room = _get_room(p)
                text = f"Bot en marche, tu es actuellement en {h(p.get('nom', '?'))}"
                if room:
                    text += f" (salle {h(room)})"
                text += "."
                return text
    except Exception:
        log.exception("Status check error")
    return "Bot en marche, aucun cours en ce moment."


async def cmd_status(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    text = await _status_text()
    await update.message.reply_text(text, parse_mode="HTML")


# ── Startup ───────────────────────────────────────────────────────────────────

async def post_init(app: Application):
    # Register commands with Telegram so the command menu stays in sync
    from telegram import BotCommand
    await app.bot.set_my_commands([
        BotCommand("start", "Afficher l'aide"),
        BotCommand("status", "Etat du bot"),
        BotCommand("mockattendance", "Exemple de notification d'appel"),
        BotCommand("mockgrade", "Exemple de notification de note"),
    ])
    asyncio.create_task(presence_loop(app))
    asyncio.create_task(grades_loop(app))
    log.info("Polling loops started.")


_lock_fd = None

def _acquire_lock():
    """Ensure only one bot instance runs at a time."""
    global _lock_fd
    lock_path = os.path.join("data", "telegram_bot.lock")
    _lock_fd = open(lock_path, "w")
    try:
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("ERROR: Another bot instance is already running. Kill it first.")
        sys.exit(1)
    _lock_fd.write(str(os.getpid()))
    _lock_fd.flush()


def main():
    os.makedirs("data", exist_ok=True)
    _acquire_lock()

    app = (
        Application.builder()
        .token(cfg["telegram_token"])
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("mockattendance", cmd_mockattendance))
    app.add_handler(CommandHandler("mockgrade", cmd_mockgrade))
    app.add_handler(CallbackQueryHandler(on_skip_callback, pattern=r"^skip:"))

    log.info("Bot starting...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
