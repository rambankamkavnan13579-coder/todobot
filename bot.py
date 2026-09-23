"""
Kamalesh's To-Do Scheduler Bot
------------------------------
Commands:
/start
/add <task> [HH:MM]         - add a task; get notified at that time
/list                       - show today's tasks
/done <number>              - mark a task done
/delete <number>            - delete a task
/edit <number> <text> [HH:MM] - edit a task
/score                      - today's score, as a progress ring image
/weekscore                  - last 7 days, as a bar chart image
/monthscore                 - this month, as a calendar heatmap image
/remind <HH:MM>             - daily reminder
/stopremind                 - turn off the reminder
"""

import sqlite3
import asyncio
import os
import threading
import datetime
import calendar
from io import BytesIO
from http.server import BaseHTTPRequestHandler, HTTPServer

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

DB_FILE = "todobot.db"
TOKEN = os.environ.get("BOT_TOKEN", "PUT_YOUR_BOTFATHER_TOKEN_HERE")


# ---------- TINY WEB SERVER (Render free-tier keep-alive target) ----------
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is alive")

    def log_message(self, format, *args):
        pass


def run_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()


# ---------- DATABASE ----------
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
    c.execute("""
        CREATE TABLE IF NOT EXISTS reminders (
            user_id INTEGER PRIMARY KEY,
            hour INTEGER,
            minute INTEGER
        )
    """)
    conn.commit()
    conn.close()


def get_conn():
    return sqlite3.connect(DB_FILE)


def day_pct(rows):
    if not rows:
        return None
    done = sum(r[0] for r in rows)
    return round((done / len(rows)) * 100)


def get_all_day_scores(user_id):
    """Returns {date_str: pct} for every day this user has tasks."""
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT task_date, done FROM tasks WHERE user_id=?", (user_id,))
    rows = c.fetchall()
    conn.close()
    days = {}
    for task_date, done in rows:
        days.setdefault(task_date, []).append(done)
    return {d: day_pct([(x,) for x in v]) for d, v in days.items()}


def compute_streak(day_scores):
    streak = 0
    d = datetime.date.today()
    today_pct = day_scores.get(d.isoformat())
    if today_pct is not None and today_pct >= 70:
        streak += 1
    d -= datetime.timedelta(days=1)
    for _ in range(400):
        pct = day_scores.get(d.isoformat())
        if pct is not None and pct >= 70:
            streak += 1
            d -= datetime.timedelta(days=1)
        else:
            break
    return streak


def badge_for_streak(streak):
    if streak >= 30:
        return "\U0001F3C6 Gold streak"
    if streak >= 14:
        return "\U0001F948 Silver streak"
    if streak >= 7:
        return "\U0001F949 Bronze streak"
    return "Keep going"


# ---------- VISUAL 1: PROGRESS RING (today) ----------
def make_progress_ring(pct: int, label: str) -> BytesIO:
    pct = max(0, min(100, pct))
    color = "#2ecc71" if pct >= 70 else ("#f1c40f" if pct >= 40 else "#e74c3c")

    fig, ax = plt.subplots(figsize=(3.2, 3.2), subplot_kw=dict(aspect="equal"))
    ax.pie(
        [pct, 100 - pct],
        colors=[color, "#2a2a3d"],
        startangle=90,
        counterclock=False,
        wedgeprops=dict(width=0.32, edgecolor="none"),
    )
    ax.text(0, 0.08, f"{pct}%", ha="center", va="center", fontsize=28, fontweight="bold", color=color)
    ax.text(0, -0.22, label, ha="center", va="center", fontsize=11, color="#888")

    fig.patch.set_alpha(0)
    buf = BytesIO()
    plt.savefig(buf, format="png", transparent=True, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf


# ---------- VISUAL 2: WEEKLY BAR CHART ----------
def make_week_bar_chart(day_scores: dict) -> BytesIO:
    today = datetime.date.today()
    dates = [today - datetime.timedelta(days=i) for i in range(6, -1, -1)]
    labels = [d.strftime("%a") for d in dates]
    values = [day_scores.get(d.isoformat()) for d in dates]

    colors = []
    plot_values = []
    for v in values:
        if v is None:
            plot_values.append(0)
            colors.append("#2a2a3d")
        else:
            plot_values.append(v)
            colors.append("#2ecc71" if v >= 70 else ("#f1c40f" if v >= 40 else "#e74c3c"))

    fig, ax = plt.subplots(figsize=(6, 3.2))
    bars = ax.bar(labels, plot_values, color=colors, width=0.55)

    for bar, v in zip(bars, values):
        if v is not None:
            ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 3, f"{v}%",
                    ha="center", fontsize=9, color="#333")

    ax.set_ylim(0, 115)
    ax.set_yticks([])
    for spine in ["top", "right", "left"]:
        ax.spines[spine].set_visible(False)
    ax.tick_params(axis="x", labelsize=10)
    fig.patch.set_alpha(0)
    ax.patch.set_alpha(0)

    buf = BytesIO()
    plt.savefig(buf, format="png", transparent=True, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf


# ---------- VISUAL 3: MONTH CALENDAR HEATMAP ----------
def make_month_heatmap(day_scores: dict) -> BytesIO:
    today = datetime.date.today()
    year, month = today.year, today.month
    cal = calendar.Calendar(firstweekday=6)  # Sunday first
    weeks = cal.monthdayscalendar(year, month)

    fig, ax = plt.subplots(figsize=(6, 1 + 0.9 * len(weeks)))
    ax.set_xlim(0, 7)
    ax.set_ylim(0, len(weeks))
    ax.invert_yaxis()
    ax.axis("off")

    weekday_labels = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
    for i, wd in enumerate(weekday_labels):
        ax.text(i + 0.5, -0.3, wd, ha="center", fontsize=9, color="#888")

    for week_idx, week in enumerate(weeks):
        for day_idx, day in enumerate(week):
            if day == 0:
                continue
            date_obj = datetime.date(year, month, day)
            pct = day_scores.get(date_obj.isoformat())
            if date_obj > today:
                color = "#e8e8e8"
            elif pct is None:
                color = "#e0e0e0"
            elif pct >= 70:
                color = "#2ecc71"
            elif pct >= 40:
                color = "#f1c40f"
            else:
                color = "#e74c3c"

            rect = mpatches.FancyBboxPatch(
                (day_idx + 0.08, week_idx + 0.08), 0.84, 0.84,
                boxstyle="round,pad=0,rounding_size=0.12",
                facecolor=color, edgecolor="none"
            )
            ax.add_patch(rect)
            text_color = "#222" if color in ("#e8e8e8", "#e0e0e0", "#f1c40f") else "#fff"
            ax.text(day_idx + 0.5, week_idx + 0.5, str(day), ha="center", va="center",
                    fontsize=9, color=text_color)

    ax.set_title(today.strftime("%B %Y"), fontsize=13, color="#333", pad=14)
    fig.patch.set_alpha(0)

    buf = BytesIO()
    plt.savefig(buf, format="png", transparent=True, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf


# ---------- PER-TASK TIME NOTIFICATIONS ----------
async def task_reminder_callback(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    await context.bot.send_message(chat_id=job.chat_id, text=f"\u23F0 Time for: {job.data}")


def cancel_task_job(app: Application, task_id: int):
    for job in app.job_queue.get_jobs_by_name(f"task_{task_id}"):
        job.schedule_removal()


def schedule_task_reminder(app: Application, user_id: int, task_id: int, description: str, time_str: str):
    if not time_str or ":" not in time_str:
        return
    try:
        hour, minute = map(int, time_str.split(":"))
        assert 0 <= hour <= 23 and 0 <= minute <= 59
    except (ValueError, AssertionError):
        return

    now = datetime.datetime.now()
    when = datetime.datetime.combine(datetime.date.today(), datetime.time(hour, minute))
    if when <= now:
        return

    cancel_task_job(app, task_id)
    app.job_queue.run_once(
        task_reminder_callback,
        when=when,
        chat_id=user_id,
        data=description,
        name=f"task_{task_id}",
    )


def load_task_reminders(app: Application):
    today = datetime.date.today().isoformat()
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "SELECT id, user_id, description, time_str FROM tasks WHERE task_date=? AND done=0 AND time_str IS NOT NULL",
        (today,),
    )
    rows = c.fetchall()
    conn.close()
    for task_id, user_id, desc, time_str in rows:
        schedule_task_reminder(app, user_id, task_id, desc, time_str)


# ---------- COMMANDS ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hey! I'm your personal task scheduler \U0001F525\n\n"
        "Commands:\n"
        "/add <task> [HH:MM] - add a task, get notified at that time\n"
        "/list - see today's tasks\n"
        "/done <number> - mark complete\n"
        "/delete <number> - remove a task\n"
        "/edit <number> <text> [HH:MM] - change a task\n"
        "/score - today's progress ring\n"
        "/weekscore - last 7 days as a bar chart\n"
        "/monthscore - this month as a calendar heatmap\n"
        "/remind <HH:MM> - daily reminder\n"
        "/stopremind - turn off the reminder\n\n"
        "Tip: use HH:MM with a colon, like 19:00"
    )


async def add_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /add <task> [HH:MM]\nExample: /add Maths 19:00")
        return

    args = context.args
    time_str = None
    if ":" in args[-1] and len(args) > 1:
        time_str = args[-1]
        description = " ".join(args[:-1])
    else:
        description = " ".join(args)

    today = datetime.date.today().isoformat()
    user_id = update.effective_user.id

    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "INSERT INTO tasks (user_id, task_date, description, time_str, done) VALUES (?, ?, ?, ?, 0)",
        (user_id, today, description, time_str),
    )
    task_id = c.lastrowid
    conn.commit()
    conn.close()

    time_part = ""
    if time_str:
        schedule_task_reminder(context.application, user_id, task_id, description, time_str)
        time_part = f" at {time_str} \u2014 I'll notify you then"

    await update.message.reply_text(f'\u2705 Added: "{description}"{time_part}')


async def list_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = datetime.date.today().isoformat()
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

    lines.append("\n/done <number>  /delete <number>  /edit <number> <text>")
    await update.message.reply_text("\n".join(lines))


async def done_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /done <number>")
        return
    try:
        position = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Please give a valid task number. Check /list first.")
        return

    today = datetime.date.today().isoformat()
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

    cancel_task_job(context.application, task_id)
    await update.message.reply_text(f'\U0001F389 Marked done: "{desc}"')


async def delete_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /delete <number>")
        return
    try:
        position = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Please give a valid task number. Check /list first.")
        return

    today = datetime.date.today().isoformat()
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
    c.execute("DELETE FROM tasks WHERE id=?", (task_id,))
    conn.commit()
    conn.close()

    cancel_task_job(context.application, task_id)
    await update.message.reply_text(f'\U0001F5D1 Deleted: "{desc}"')


async def edit_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /edit <number> <new text> [HH:MM]")
        return

    try:
        position = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Please give a valid task number. Check /list first.")
        return

    rest = context.args[1:]
    time_str = None
    if ":" in rest[-1] and len(rest) > 1:
        time_str = rest[-1]
        new_desc = " ".join(rest[:-1])
    else:
        new_desc = " ".join(rest)

    today = datetime.date.today().isoformat()
    user_id = update.effective_user.id

    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "SELECT id FROM tasks WHERE user_id=? AND task_date=? ORDER BY id",
        (user_id, today),
    )
    rows = c.fetchall()

    if position < 1 or position > len(rows):
        await update.message.reply_text("That task number doesn't exist. Check /list.")
        conn.close()
        return

    task_id = rows[position - 1][0]
    if time_str:
        c.execute("UPDATE tasks SET description=?, time_str=? WHERE id=?", (new_desc, time_str, task_id))
    else:
        c.execute("UPDATE tasks SET description=? WHERE id=?", (new_desc, task_id))
    conn.commit()
    conn.close()

    cancel_task_job(context.application, task_id)
    if time_str:
        schedule_task_reminder(context.application, user_id, task_id, new_desc, time_str)

    await update.message.reply_text(f'\u270F\ufe0f Updated task {position}: "{new_desc}"')


async def score(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = datetime.date.today().isoformat()
    user_id = update.effective_user.id

    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT done FROM tasks WHERE user_id=? AND task_date=?", (user_id, today))
    rows = c.fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("No tasks added today, so no score yet. Add some with /add")
        return

    total = len(rows)
    completed = sum(r[0] for r in rows)
    pct = round((completed / total) * 100)

    day_scores = get_all_day_scores(user_id)
    streak = compute_streak(day_scores)
    badge = badge_for_streak(streak)

    img = make_progress_ring(pct, "Today")
    caption = (
        f"\U0001F4CA Today's score: {pct}/100  ({completed}/{total} tasks done)\n"
        f"\U0001F525 {streak} day streak \u2014 {badge}"
    )
    await update.message.reply_photo(photo=img, caption=caption)


async def week_score(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    day_scores = get_all_day_scores(user_id)

    today = datetime.date.today()
    week_ago = today - datetime.timedelta(days=6)
    recent = {d: v for d, v in day_scores.items() if week_ago.isoformat() <= d <= today.isoformat()}

    if not recent:
        await update.message.reply_text("No tasks logged in the last 7 days yet.")
        return

    avg = round(sum(recent.values()) / len(recent))
    img = make_week_bar_chart(day_scores)
    await update.message.reply_photo(
        photo=img,
        caption=f"\U0001F4C6 Last 7 days average: {avg}/100 (based on {len(recent)} active day(s))"
    )


async def month_score(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    day_scores = get_all_day_scores(user_id)

    year_month = datetime.date.today().strftime("%Y-%m")
    month_days = {d: v for d, v in day_scores.items() if d.startswith(year_month)}

    if not month_days:
        await update.message.reply_text("No tasks logged this month yet.")
        return

    avg = round(sum(month_days.values()) / len(month_days))
    img = make_month_heatmap(day_scores)
    await update.message.reply_photo(
        photo=img,
        caption=f"\U0001F4C6 This month's average: {avg}/100 (based on {len(month_days)} active day(s))"
    )


# ---------- DAILY REMINDERS ----------
async def reminder_callback(context: ContextTypes.DEFAULT_TYPE):
    user_id = context.job.chat_id
    await context.bot.send_message(chat_id=user_id, text="\u23F0 Reminder: check off today's tasks! /list")


def schedule_reminder(app: Application, user_id: int, hour: int, minute: int):
    for job in app.job_queue.get_jobs_by_name(f"reminder_{user_id}"):
        job.schedule_removal()
    app.job_queue.run_daily(
        reminder_callback,
        time=datetime.time(hour=hour, minute=minute),
        chat_id=user_id,
        name=f"reminder_{user_id}",
    )


async def set_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /remind <HH:MM>")
        return
    try:
        hour, minute = map(int, context.args[0].split(":"))
        assert 0 <= hour <= 23 and 0 <= minute <= 59
    except (ValueError, AssertionError):
        await update.message.reply_text("Please use 24-hour HH:MM format, e.g. /remind 20:00")
        return

    user_id = update.effective_user.id
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "INSERT INTO reminders (user_id, hour, minute) VALUES (?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET hour=excluded.hour, minute=excluded.minute",
        (user_id, hour, minute),
    )
    conn.commit()
    conn.close()

    schedule_reminder(context.application, user_id, hour, minute)
    await update.message.reply_text(f"\u23F0 Daily reminder set for {context.args[0]}.")


async def stop_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM reminders WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()

    for job in context.application.job_queue.get_jobs_by_name(f"reminder_{user_id}"):
        job.schedule_removal()

    await update.message.reply_text("Reminder turned off.")


def load_all_reminders(app: Application):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT user_id, hour, minute FROM reminders")
    rows = c.fetchall()
    conn.close()
    for user_id, hour, minute in rows:
        schedule_reminder(app, user_id, hour, minute)


# ---------- MAIN ----------
def main():
    init_db()
    threading.Thread(target=run_health_server, daemon=True).start()

    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add", add_task))
    app.add_handler(CommandHandler("list", list_tasks))
    app.add_handler(CommandHandler("done", done_task))
    app.add_handler(CommandHandler("delete", delete_task))
    app.add_handler(CommandHandler("edit", edit_task))
    app.add_handler(CommandHandler("score", score))
    app.add_handler(CommandHandler("weekscore", week_score))
    app.add_handler(CommandHandler("monthscore", month_score))
    app.add_handler(CommandHandler("remind", set_reminder))
    app.add_handler(CommandHandler("stopremind", stop_reminder))

    load_all_reminders(app)
    load_task_reminders(app)

    print("Bot is running... press Ctrl+C to stop.")
    app.run_polling()


if __name__ == "__main__":
    main()
