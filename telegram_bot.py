#!/usr/bin/env python3
"""
ESILV Telegram Bot
- Presence alerts (notifies when attendance opens)
- Smart auto-presence for remote/visio classes (auto-marks after 60s unless cancelled)
- Grade notifications (notifies when new grades appear)
"""

import asyncio
import json
import logging
import os
import sys
from html import escape as html_escape

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

known_presences: dict = {}       # seance_id -> presence dict from API
pending_tasks: dict = {}         # seance_id -> asyncio.Task (auto-present timer)
pending_messages: dict = {}      # seance_id -> telegram Message object


# ── Helpers ───────────────────────────────────────────────────────────────────

def h(text) -> str:
    """Escape text for Telegram HTML parse mode."""
    return html_escape(str(text))


def is_remote(p: dict) -> bool:
    """Detect remote/visio class: name contains 'CMo'."""
    return "CMo" in p.get("nom", "")


def _get_current_subject() -> str:
    """Return the name of the current subject from known presences, or None."""
    for p in known_presences.values():
        return p.get("nom", "?")
    return None


# ── Presence Polling ──────────────────────────────────────────────────────────

async def presence_loop(app: Application):
    global api
    loop = asyncio.get_event_loop()

    log.info("Initializing API (OAuth2)...")
    api = await loop.run_in_executor(
        None, lambda: ldv_dashbot.Api(cfg["email"], cfg["password"])
    )
    log.info("API ready. Polling presences every %ds...", cfg.get("poll_presence", 10))

    # Send startup message
    presences = await loop.run_in_executor(None, api.get_presences)
    current_subject = None
    for p in presences:
        if p.get("etat_ouverture") and p["etat_ouverture"] not in ("ferme", "fermé"):
            current_subject = p.get("nom")
            break

    if current_subject:
        text = f"Bot en marche, tu es actuellement en {h(current_subject)}."
    else:
        text = "Bot en marche, aucun cours en ce moment."
    await app.bot.send_message(chat_id=cfg["chat_id"], text=text, parse_mode="HTML")

    while True:
        try:
            presences = await loop.run_in_executor(None, api.get_presences)

            current_ids = set()
            for p in presences:
                sid = p["seance_id"]
                current_ids.add(sid)
                is_open = p.get("etat_ouverture") and p["etat_ouverture"] not in ("ferme", "fermé")

                if is_open and sid not in known_presences:
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
    chat_id = cfg["chat_id"]

    if is_remote(p):
        kb = [[InlineKeyboardButton("Rester absent", callback_data=f"skip:{sid}")]]

        text = f"L'appel pour {name} est ouvert.\nAuto-presence dans {TIMEOUT}s..."

        zoom = p.get("zoom_url", "")
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
        text = f"L'appel pour {name} est ouvert."
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
            await msg.edit_text(
                f"Marque present pour {name}.",
                parse_mode="HTML",
            )

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

    p = known_presences.get(sid, {})
    name = h(p.get("nom", "?"))

    await query.edit_message_text(
        f"Reste absent pour {name}.",
        parse_mode="HTML",
    )
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
    fake = {
        "seance_id": 99999,
        "nom": "Mathematiques S6",
        "date": "17/03/2026",
        "horaire": "10h00 - 12h00",
        "etat_ouverture": "ouvert",
        "zoom_url": None,
    }
    name = h(fake["nom"])
    await update.message.reply_text(
        f"L'appel pour {name} est ouvert.",
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
    await update.message.reply_text(
        "<b>ESILV Telegram Bot</b>\n\n"
        "Notifications :\n"
        "- Appels de presence ouverts\n"
        "- Auto-presence pour les cours en visio\n"
        "- Nouvelles notes\n\n"
        "<b>Commandes :</b>\n"
        "/start - Ce message\n"
        "/status - Etat du bot\n"
        "/mockattendance - Test affichage appel\n"
        "/mockgrade - Test affichage note\n\n"
        f"Chat ID : <code>{update.effective_chat.id}</code>",
        parse_mode="HTML",
    )


async def cmd_status(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"<b>Status</b>\n\n"
        f"Appels suivis : {len(known_presences)}\n"
        f"Auto-presence en attente : {len(pending_tasks)}",
        parse_mode="HTML",
    )


# ── Startup ───────────────────────────────────────────────────────────────────

async def post_init(app: Application):
    asyncio.create_task(presence_loop(app))
    asyncio.create_task(grades_loop(app))
    log.info("Polling loops started.")


def main():
    os.makedirs("data", exist_ok=True)

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
