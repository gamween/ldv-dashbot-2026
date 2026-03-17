# LDV DashBot 2026

A personal Telegram bot for ESILV students that sends real-time notifications for attendance and grades, built on top of the ldv-dashbot library.

---

## Credits

This project is a fork of [ldv-dashbot](https://github.com/merlleu/ldv-dashbot), originally created by **Remi Langdorph** and **Antoine Plin**. Their work provides the core library that handles authentication and communication with the ESILV / Leonard de Vinci student portal. All the heavy lifting (login flows, API integration, grade scraping, presence tracking) comes from their project.

---

## What this fork adds

The original repository provides a Python library and a Discord-oriented watcher system. This fork builds on that foundation by adding a **Telegram bot** that acts as a personal assistant for day-to-day school life.

### Attendance notifications

When a professor opens the attendance roll call for a class, the bot sends you a Telegram message immediately. You know the moment it opens, without having to keep checking the app.

### Auto-presence for remote classes

For online classes (identified by the course name or the presence of a Zoom link), the bot can automatically mark you as present after a short delay. If you want to stay absent for a particular session, you simply press a button in the chat to cancel the auto-mark before it happens.

### Grade notifications

The bot regularly checks for new grades. When a new grade appears, it sends you a detailed notification with the subject, exam name, your grade, and the class average.

### Room information

When available, the bot includes the room or location of the class in its notifications, pulled from the school calendar feed.

---

## Setup

### Requirements

- Python 3.10 or later
- A Telegram bot token (from [@BotFather](https://t.me/BotFather))
- Your ESILV portal credentials

### Installation

1. Clone this repository.

2. Install the dependencies:
   ```
   pip install -r telegram_requirements.txt
   ```

3. Copy the example configuration and fill in your credentials:
   ```
   cp telegram_config.example.yaml telegram_config.yaml
   ```

4. Edit `telegram_config.yaml` with your email, password, Telegram token, and chat ID. Instructions for obtaining each value are included in the file.

5. Start the bot:
   ```
   python telegram_bot.py
   ```

The bot will confirm it is running by sending you a message on Telegram.

### Bot commands

| Command             | Description                            |
|---------------------|----------------------------------------|
| `/start`            | Display help and available commands    |
| `/status`           | Show current bot state and active class|
| `/mockattendance`   | Preview what an attendance alert looks like |
| `/mockgrade`        | Preview what a grade notification looks like |

---

## How it works

The bot relies on two authentication methods provided by the original ldv-dashbot library:

- **OAuth2 API** (MyDeVinci app) -- used for checking presences, marking attendance, and fetching profile and calendar data.
- **Web scraper** (ADFS/SAML login) -- used for retrieving grades, which are only available through the student portal HTML pages.

Both run as background polling loops. The bot checks for open presences every few seconds and for new grades every few minutes (intervals are configurable). Notifications are sent to your Telegram chat as soon as a change is detected.

---

## Original project

For details on the underlying library, the watcher system, or the Discord webhook integration, refer to the original repository: [github.com/merlleu/ldv-dashbot](https://github.com/merlleu/ldv-dashbot).
