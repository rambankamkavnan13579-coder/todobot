"""
Kamalesh's To-Do Scheduler Bot
------------------------------
Commands:
/start          - welcome message
/add <task> [time]   - add a task for today (time optional, e.g. 18:30)
/list           - show today's tasks
/done <number>  - mark a task as done (use number shown in /list)
/score          - today's completion score (out of 100)
/monthscore     - this month's average score (days with 0 tasks are skipped)

100% free: uses python-telegram-bot (free library) + SQLite (built into Python, no server needed).
"""

import sqlite3
import asyncio
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import date
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes


# ---------- TINY WEB SERVER (so Render's free Web Service stays alive) ----------
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is alive")

    def log_message(self, format, *args):
        pass  # keeps logs clean


def run_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

DB_FILE = "todobot.db"
# On your laptop: paste your token directly between the quotes below.
# On Render: leave this line as-is — Render supplies it via Environment Variables.
TOKEN = os.environ.get("BOT_TOKEN", "PUT_YOUR_BOTFATHER_TOKEN_HERE")


# ---------- DATABASE SETUP ----------
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            task_date TEXT,
            description TEXT,
            time_str TEXT,
            done INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()


def get_conn():
    return sqlite3.connect(DB_FILE)


# ---------- COMMANDS ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hey! I'm your personal task scheduler \U0001F525\n\n"
        "Commands:\n"
        "/add <task> [time] - add a task (e.g. /add Study DSA 18:00)\n"
        "/list - see today's tasks\n"
        "/done <number> - mark a task complete\n"
        "/score - today's score\n"
        "/monthscore - this month's average score"
    )


async def add_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /add <task> [time]\nExample: /add Revise physics 19:00")
        return

    args = context.args
    time_str = None
    if ":" in args[-1] and len(args) > 1:
        time_str = args[-1]
        description = " ".join(args[:-1])
    else:
        description = " ".join(args)

    today = date.today().isoformat()
    user_id = update.effective_user.id

    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "INSERT INTO tasks (user_id, task_date, description, time_str, done) VALUES (?, ?, ?, ?, 0)",
        (user_id, today, description, time_str),
    )
    conn.commit()
    conn.close()

    time_part = f" at {time_str}" if time_str else ""
    await update.message.reply_text(f'\u2705 Added: "{description}"{time_part}')


async def list_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = date.today().isoformat()
    user_id = update.effective_user.id

    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "SELECT id, description, time_str, done FROM tasks WHERE user_id=? AND task_date=? ORDER BY id",
        (user_id, today),
    )
    rows = c.fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("No tasks for today yet. Add one with /add <task>")
        return

    lines = ["\U0001F4CB Today's tasks:\n"]
    for idx, (task_id, desc, time_str, done) in enumerate(rows, start=1):
        box = "\u2611\ufe0f" if done else "\u2b1c"
        time_part = f" ({time_str})" if time_str else ""
        lines.append(f"{idx}. {box} {desc}{time_part}")

    lines.append("\nMark done with: /done <number>")
    await update.message.reply_text("\n".join(lines))


async def done_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /done <number>  (get the number from /list)")
        return

    try:
        position = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Please give a valid task number. Check /list first.")
        return

    today = date.today().isoformat()
    user_id = update.effective_user.id

    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "SELECT id, description FROM tasks WHERE user_id=? AND task_date=? ORDER BY id",
        (user_id, today),
    )
    rows = c.fetchall()

    if position < 1 or position > len(rows):
        await update.message.reply_text("That task number doesn't exist. Check /list.")
        conn.close()
        return

    task_id, desc = rows[position - 1]
    c.execute("UPDATE tasks SET done=1 WHERE id=?", (task_id,))
    conn.commit()
    conn.close()

    await update.message.reply_text(f'\U0001F389 Marked done: "{desc}"')


async def score(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = date.today().isoformat()
    user_id = update.effective_user.id

    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "SELECT done FROM tasks WHERE user_id=? AND task_date=?",
        (user_id, today),
    )
    rows = c.fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("No tasks added today, so no score yet. Add some with /add")
        return

    total = len(rows)
    completed = sum(r[0] for r in rows)
    pct = round((completed / total) * 100)

    await update.message.reply_text(f"\U0001F4CA Today's score: {pct}/100  ({completed}/{total} tasks done)")


async def month_score(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    year_month = date.today().strftime("%Y-%m")

    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "SELECT task_date, done FROM tasks WHERE user_id=? AND task_date LIKE ?",
        (user_id, f"{year_month}%"),
    )
    rows = c.fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("No tasks logged this month yet.")
        return

    days = {}
    for task_date, done in rows:
        days.setdefault(task_date, []).append(done)

    daily_scores = []
    for task_date, done_list in days.items():
        pct = (sum(done_list) / len(done_list)) * 100
        daily_scores.append(pct)

    avg = round(sum(daily_scores) / len(daily_scores))
    await update.message.reply_text(
        f"\U0001F4C6 This month's average score: {avg}/100\n"
        f"(based on {len(daily_scores)} active day(s); days with no tasks are skipped)"
    )


# ---------- MAIN ----------
def main():
    init_db()

    # Start the tiny web server in the background so Render's free tier
    # sees this as "alive" and doesn't shut it down
    threading.Thread(target=run_health_server, daemon=True).start()

    # Python 3.14 needs an event loop created explicitly before this runs
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add", add_task))
    app.add_handler(CommandHandler("list", list_tasks))
    app.add_handler(CommandHandler("done", done_task))
    app.add_handler(CommandHandler("score", score))
    app.add_handler(CommandHandler("monthscore", month_score))

    print("Bot is running... press Ctrl+C to stop.")
    app.run_polling()


if __name__ == "__main__":
    main()
