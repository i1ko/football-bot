"""Football Alicante Community bot."""

import asyncio
import calendar
import logging
import os
import random
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, time, timedelta, timezone
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, ChatMemberHandler, CommandHandler, MessageHandler, PollAnswerHandler, filters

from fun_text import FORTUNES, LATE_JOKES, MATCH_DAY_MESSAGES, POLL_REMINDERS

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
GROUP_ID = -1004320321125
MAP_URL = "https://maps.google.com/maps?q=38.304481,-0.525636&ll=38.304481,-0.525636&z=16"
RULES_URL = "https://t.me/footballalicante/165"
DB_PATH = Path(__file__).with_name("football_data.sqlite3")
PARKING_REPLY = "Можеш приїхати навіть на фурі  місця всім вистачить 😄"
LOCATION_REPLY = f"Ось наша локація:\n{MAP_URL}"
SCHEDULE_REPLY = ("Збираємось двічі на тиждень.\n\n"
                  "У будні  у середу або четвер, зазвичай увечері приблизно о 20:00 / 20:30.\n\n"
                  "У неділю граємо зранку, зараз орієнтир  09:30.\n\n"
                  "Зазвичай граємо 1,5—2 години.")
POLL_OPTIONS = ["✅ Буду", "❌ Не буду", "🤔 50/50"]
AUTO_REPLY_CATEGORIES = {
    PARKING_REPLY: "PARKING",
    LOCATION_REPLY: "LOCATION",
    SCHEDULE_REPLY: "SCHEDULE",
}
logger = logging.getLogger(__name__)
report_task = None
fun_task = None
GAME_ZONE = ZoneInfo("Europe/Madrid")


def now():
    return datetime.now(timezone.utc)


def stamp(value):
    return value.astimezone(timezone.utc).isoformat()


def plus_two_months(value):
    index = value.year * 12 + value.month + 1
    year, month0 = divmod(index, 12)
    month = month0 + 1
    return value.replace(year=year, month=month, day=min(value.day, calendar.monthrange(year, month)[1]))


@contextmanager
def db_open():
    db = sqlite3.connect(DB_PATH)
    db.execute("PRAGMA foreign_keys = ON")
    try:
        with db:
            yield db
    finally:
        db.close()


def init_db():
    with db_open() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS members (
                user_id INTEGER PRIMARY KEY, first_seen TEXT NOT NULL,
                display_name TEXT NOT NULL, is_bot INTEGER NOT NULL,
                username TEXT
            );
            CREATE TABLE IF NOT EXISTS polls (
                poll_id TEXT PRIMARY KEY, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS participation (
                poll_id TEXT NOT NULL REFERENCES polls(poll_id),
                user_id INTEGER NOT NULL, PRIMARY KEY (poll_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY, value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS cards (
                user_id INTEGER PRIMARY KEY REFERENCES members(user_id),
                yellow_at TEXT NOT NULL, yellow_until TEXT NOT NULL,
                red_at TEXT, red_until TEXT
            );
            CREATE TABLE IF NOT EXISTS info_requests (
                user_id INTEGER NOT NULL, category TEXT NOT NULL,
                stage INTEGER NOT NULL, blocked_until TEXT,
                PRIMARY KEY (user_id, category)
            );
            CREATE TABLE IF NOT EXISTS football_commands (
                chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
                PRIMARY KEY (chat_id, message_id)
            );
            CREATE TABLE IF NOT EXISTS poll_fun (
                poll_id TEXT PRIMARY KEY REFERENCES polls(poll_id),
                poll_message_id INTEGER NOT NULL,
                reminder_due TEXT NOT NULL, reminder_sent_at TEXT,
                match_at TEXT, match_due TEXT, match_sent_at TEXT
            );
            CREATE TABLE IF NOT EXISTS fun_cooldowns (
                user_id INTEGER NOT NULL, category TEXT NOT NULL,
                until TEXT NOT NULL,
                PRIMARY KEY (user_id, category)
            );
            CREATE TABLE IF NOT EXISTS fortune_history (
                user_id INTEGER PRIMARY KEY, last_index INTEGER NOT NULL
            );
        """)
        if "username" not in {row[1] for row in db.execute("PRAGMA table_info(members)")}:
            db.execute("ALTER TABLE members ADD COLUMN username TEXT")
        db.execute("INSERT OR IGNORE INTO settings VALUES ('period_start', ?)", (stamp(now()),))


def remember(user, seen=None, reset_join=False):
    if user is None:
        return
    username = user.username.casefold() if getattr(user, "username", None) else None
    with db_open() as db:
        if username:
            db.execute("UPDATE members SET username = NULL WHERE username = ? AND user_id != ?",
                       (username, user.id))
        db.execute(
            """INSERT INTO members(user_id, first_seen, display_name, is_bot, username)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
               display_name=excluded.display_name, is_bot=excluded.is_bot,
               username=excluded.username""",
            (user.id, stamp(seen or now()), user.full_name, int(user.is_bot), username),
        )
        if reset_join:
            db.execute("UPDATE members SET first_seen = ? WHERE user_id = ?", (stamp(seen or now()), user.id))


def norm(value):
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", value.casefold())).strip()


PARK = r"(?:парков\w*|паркинг\w*|паркув\w*|припарков\w*|припаркув\w*)"
PARK_PLACE = r"(?:парковк\w*|паркинг\w*|паркуванн\w*|парковочн\w*)"
CAR = r"(?:машин\w*|авто\w*|автомобил\w*|автомобіл\w*)"
FIELD = r"(?:поле|поля|полі|футбол\w*|игр\w*|гр[аиі]\w*)"
PLAY = r"(?:игра\w*|грає\w*|грати|собира\w*|збира\w*)"


def matches(text, patterns):
    return any(re.search(pattern, text) for pattern in patterns)


def auto_reply(value):
    text = norm(value)
    if len(text.split()) < 2 or value.lstrip().startswith("/"):
        return None
    if matches(text, (
        rf"\b(?:как|що|что|як|чи|есть ли|имеется ли)\b(?:\s+\w+){{0,4}}\s+{PARK_PLACE}\b",
        rf"\b(?:где|де|куда|куди)\b(?:\s+\w+){{0,5}}\s+(?:{PARK}|(?:постав\w*|залиш\w*|остав\w*)\s+{CAR})\b",
        rf"\b(?:можно|можна)\b(?:\s+\w+){{0,4}}\s+(?:припарков\w*|припаркув\w*|постав\w*\s+{CAR})\b",
        rf"\b(?:есть|є|будет|буде)\b(?:\s+\w+){{0,3}}\s+(?:место|місце)\s+(?:для|під)\s+{CAR}\b",
    )):
        return PARKING_REPLY
    if matches(text, (
        rf"\b(?:где|де)\b(?:\s+\w+){{0,4}}\s+(?:поле|поля|игра\w*|гра\w*|играете|граєте)\b",
        rf"\b(?:куда|куди|как|як)\b(?:\s+\w+){{0,3}}\s+(?:ехать|їхати|доехать|доїхати|попасть|дістатися)\b(?:\s+\w+){{0,4}}\s+{FIELD}\b",
        rf"\b(?:скинь\w*|пришл\w*|отправ\w*|надішл\w*|дайте|дай|подскаж\w*|підкаж\w*)\b(?:\s+\w+){{0,3}}\s+(?:локац\w*|гео|адрес\w*|адреса)\b(?:\s+\w+){{0,3}}\s+{FIELD}\b",
        rf"\b(?:какая|яка|где|де)\s+(?:у вас\s+)?(?:локац\w*|адрес\w*|адреса|место|місце)\b(?:\s+\w+){{0,3}}\s+{FIELD}\b",
    )):
        return LOCATION_REPLY
    if matches(text, (
        rf"\b(?:когда|коли|как часто|як часто|во сколько|о котрій)\b(?:\s+\w+){{0,4}}\s+{PLAY}\b",
        rf"\b(?:по каким|по яких|в какие|у які)\s+дн\w*\b(?:\s+\w+){{0,4}}\s+{PLAY}\b",
        rf"\b(?:сколько|скільки)\b(?:\s+\w+){{0,3}}\s+(?:длится|триває|идет|йде)\s+(?:игра|гра)\b",
        rf"\b(?:когда|коли)\s+(?:будет|буде)?\s*(?:следующ\w*|наступн\w*)\s+(?:игра|гра)\b",
        rf"\b(?:какое|какий|який|яке)\b(?:\s+\w+){{0,3}}\s+(?:расписани\w*|розклад\w*|график\w*)\b",
    )):
        return SCHEDULE_REPLY
    return None


async def welcome(update, context):
    message = update.message
    if message is None or message.chat_id != GROUP_ID:
        return
    for user in message.new_chat_members:
        if user.id == context.bot.id:
            continue
        remember(user, message.date, reset_join=True)
        mention = f"@{escape(user.username)}" if user.username else user.mention_html()
        text = (f"👋 Вітаємо, {mention}, у Football Alicante Community!\n\n"
                f'📍 <a href="{MAP_URL.replace("&", "&amp;")}">Місце, де ми граємо</a>\n'
                f'📋 <a href="{RULES_URL}">Правила групи</a>')
        try:
            await message.reply_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        except TelegramError:
            logger.exception("Failed to welcome user %s", user.id)


async def member_changed(update, context):
    change = update.chat_member
    if change and change.chat.id == GROUP_ID:
        member = change.new_chat_member
        if member.status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR,
                             ChatMemberStatus.OWNER, ChatMemberStatus.RESTRICTED):
            remember(member.user, change.date, reset_join=change.old_chat_member.status in (
                ChatMemberStatus.LEFT, ChatMemberStatus.BANNED))


WEEKDAYS = {
    "понеділок": 0, "понедельник": 0,
    "вівторок": 1, "вторник": 1,
    "середа": 2, "среда": 2, "середу": 2, "среду": 2,
    "четвер": 3, "четверг": 3,
    "п'ятниця": 4, "пятница": 4, "пятниця": 4,
    "субота": 5, "суббота": 5,
    "неділя": 6, "воскресенье": 6,
}
TIME_PATTERN = r"(?P<hour>[01]?\d|2[0-3]):(?P<minute>[0-5]\d)(?:\s*[-–—]\s*(?:[01]?\d|2[0-3]):[0-5]\d)?"


def game_time_from_title(title, created_at):
    """Only interpret an unambiguous weekday or full date with a start time."""
    value = title.casefold().strip().replace("’", "'")
    weekday_words = "|".join(re.escape(word) for word in WEEKDAYS)
    prefix = r"(?:футбол\s+)?(?:у\s+|в\s+)?"
    day = re.fullmatch(rf"{prefix}(?P<day>{weekday_words})\s+(?:о\s+)?{TIME_PATTERN}", value)
    dated = re.fullmatch(rf"(?:футбол\s+)?(?P<date>\d{{1,2}}[./]\d{{1,2}}[./]\d{{4}})\s+(?:о\s+)?{TIME_PATTERN}", value)
    match = day or dated
    if match is None:
        return None
    local_created = created_at.astimezone(GAME_ZONE)
    try:
        if day:
            days_ahead = (WEEKDAYS[day.group("day")] - local_created.weekday()) % 7
            game_date = local_created.date() + timedelta(days=days_ahead)
        else:
            parts = re.split(r"[./]", dated.group("date"))
            game_date = datetime(int(parts[2]), int(parts[1]), int(parts[0])).date()
        game = datetime.combine(game_date, time(int(match.group("hour")), int(match.group("minute"))), GAME_ZONE)
        if day and game <= local_created:
            game += timedelta(days=7)
        if game <= local_created:
            return None
        return game.astimezone(timezone.utc)
    except ValueError:
        return None


def match_day_due(game_at):
    local_game = game_at.astimezone(GAME_ZONE)
    due = datetime.combine(local_game.date(), time(7, 30), GAME_ZONE)
    if due >= local_game:
        due = local_game - timedelta(hours=1)
    return due.astimezone(timezone.utc) if due.date() == local_game.date() else None


def take_fun_cooldown(user_id, category, hours, at=None):
    at = at or now()
    with db_open() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT until FROM fun_cooldowns WHERE user_id = ? AND category = ?",
            (user_id, category),
        ).fetchone()
        if row and datetime.fromisoformat(row[0]) > at:
            return False
        db.execute(
            """INSERT INTO fun_cooldowns(user_id, category, until) VALUES (?, ?, ?)
               ON CONFLICT(user_id, category) DO UPDATE SET until = excluded.until""",
            (user_id, category, stamp(at + timedelta(hours=hours))),
        )
        return True


def pick_fortune(user_id):
    with db_open() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT last_index FROM fortune_history WHERE user_id = ?", (user_id,)).fetchone()
        if row is None:
            index = random.randrange(len(FORTUNES))
        else:
            index = random.randrange(len(FORTUNES) - 1)
            if index >= row[0]:
                index += 1
        db.execute(
            """INSERT INTO fortune_history(user_id, last_index) VALUES (?, ?)
               ON CONFLICT(user_id) DO UPDATE SET last_index = excluded.last_index""",
            (user_id, index),
        )
    return FORTUNES[index]


def says_they_are_late(value):
    if "?" in value:
        return False
    text = norm(value)
    if re.search(r"\b(?:якщо|если|не)\b(?:\s+\w+){0,2}\s+(?:опоздаю|запізнюсь|запізнюся|задержусь|задерживаюсь|затримаюсь|затримуюсь)\b", text):
        return False
    return bool(re.search(
        r"\b(?:опоздаю|запізнюсь|запізнюся|задержусь|задерживаюсь|затримаюсь|затримуюсь)\b"
        r"|\bбуду\s+(?:позже|пізніше|(?:хвилин|минут)\s+через\s+\d+)\b",
        text,
    ))


async def football(update, context):
    message = update.effective_message
    if (message is None or message.chat_id != GROUP_ID or message.from_user is None
            or message.from_user.is_bot):
        return
    with db_open() as db:
        registered = db.execute(
            "INSERT OR IGNORE INTO football_commands(chat_id, message_id) VALUES (?, ?)",
            (message.chat_id, message.message_id),
        ).rowcount
    if not registered:
        return
    remember(message.from_user, message.date)
    title = re.sub(r"^/football(?:@\w+)?", "", message.text or "", count=1, flags=re.I).strip()
    if not title:
        action = info_request_action(message.from_user.id, "FOOTBALL_HELP")
        if action == "answer":
            await message.reply_text("Приклад: /football Неділя 9:30-11:30")
        elif action == "warn":
            await message.reply_text("🖕")
        return
    try:
        sent = await context.bot.send_poll(
            chat_id=GROUP_ID, question=title, options=POLL_OPTIONS,
            is_anonymous=False, allows_multiple_answers=False,
            reply_to_message_id=message.message_id,
        )
    except TelegramError:
        logger.exception("Failed to create football poll")
        await message.reply_text("Не вдалося створити голосування. Спробуйте ще раз пізніше.")
        return
    game_at = game_time_from_title(title, sent.date)
    game_due = match_day_due(game_at) if game_at else None
    with db_open() as db:
        db.execute("INSERT OR IGNORE INTO polls VALUES (?, ?)", (sent.poll.id, stamp(sent.date)))
        db.execute(
            """INSERT OR IGNORE INTO poll_fun
               (poll_id, poll_message_id, reminder_due, reminder_sent_at,
                match_at, match_due, match_sent_at)
               VALUES (?, ?, ?, NULL, ?, ?, NULL)""",
            (sent.poll.id, sent.message_id, stamp(sent.date + timedelta(hours=24)),
             stamp(game_at) if game_at else None, stamp(game_due) if game_due else None),
        )


async def card_target(message, context, usage, allow_reply=True, unknown_text=None):
    """Resolve a known user from the command argument, or from a replied message."""
    parts = (message.text or "").split(maxsplit=1)
    raw = parts[1].strip().split()[0] if len(parts) > 1 and parts[1].strip() else None
    if raw:
        username = raw.removeprefix("@").casefold()
        if not re.fullmatch(r"[a-zA-Z0-9_]{5,32}", username):
            await message.reply_text(usage)
            return None
        with db_open() as db:
            row = db.execute("SELECT user_id, username FROM members WHERE username = ? AND is_bot = 0",
                             (username,)).fetchone()
        if row is None:
            # Older databases did not store usernames. Refresh only known user IDs.
            with db_open() as db:
                known_ids = [item[0] for item in db.execute(
                    "SELECT user_id FROM members WHERE is_bot = 0 AND username IS NULL"
                )]
            for known_id in known_ids:
                try:
                    member = await context.bot.get_chat_member(GROUP_ID, known_id)
                except TelegramError:
                    continue
                remember(member.user)
                if member.user.username and member.user.username.casefold() == username:
                    row = (member.user.id, username)
                    break
        if row is None:
            await message.reply_text(
                unknown_text.format(username=username) if unknown_text
                else f"Не знайшов @{username} у базі учасників."
            )
            return None
        return row
    replied = message.reply_to_message if allow_reply else None
    if replied and replied.from_user and not replied.from_user.is_bot:
        remember(replied.from_user, replied.date)
        with db_open() as db:
            row = db.execute("SELECT user_id, username FROM members WHERE user_id = ?",
                             (replied.from_user.id,)).fetchone()
        return row
    await message.reply_text(usage)
    return None


async def fortune(update, context):
    message = update.effective_message
    if (message is None or message.chat_id != GROUP_ID or message.from_user is None
            or message.from_user.is_bot):
        return
    remember(message.from_user, message.date)
    if not (message.text or "").split(maxsplit=1)[1:]:
        await message.reply_text("Використання: /fortune @username")
        return
    if not take_fun_cooldown(message.from_user.id, "FORTUNE", 2):
        return
    target = await card_target(
        message, context, "Використання: /fortune @username", allow_reply=False,
        unknown_text="Не знайшов @{username} серед відомих учасників.",
    )
    if target is None:
        return
    _, username = target
    await message.reply_text(pick_fortune(message.from_user.id).format(user=f"@{username}"))


async def admin_command_allowed(message, context, denial_text="Ця команда доступна тільки адміністраторам."):
    if message is None or message.chat_id != GROUP_ID or message.from_user is None:
        return False
    try:
        member = await context.bot.get_chat_member(GROUP_ID, message.from_user.id)
    except TelegramError:
        logger.exception("Failed to check administrator status for %s", message.from_user.id)
        await message.reply_text("Не вдалося перевірити права адміністратора. Спробуйте пізніше.")
        return False
    if member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
        await message.reply_text(denial_text)
        return False
    return True


def card_state(user_id, at):
    with db_open() as db:
        row = db.execute("SELECT yellow_until, red_until FROM cards WHERE user_id = ?", (user_id,)).fetchone()
    if row is None:
        return None, None
    yellow = datetime.fromisoformat(row[0]) if row[0] else None
    red = datetime.fromisoformat(row[1]) if row[1] else None
    return yellow if yellow and yellow > at else None, red if red and red > at else None


def info_request_action(user_id, category, at=None):
    """Return answer, warn, or ignore for this user's information category."""
    at = at or now()
    with db_open() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT stage, blocked_until FROM info_requests WHERE user_id = ? AND category = ?",
            (user_id, category),
        ).fetchone()
        if row is None or (row[0] == 2 and datetime.fromisoformat(row[1]) <= at):
            db.execute(
                """INSERT INTO info_requests(user_id, category, stage, blocked_until)
                   VALUES (?, ?, 1, NULL)
                   ON CONFLICT(user_id, category) DO UPDATE SET stage = 1, blocked_until = NULL""",
                (user_id, category),
            )
            return "answer"
        if row[0] == 1:
            db.execute(
                "UPDATE info_requests SET stage = 2, blocked_until = ? WHERE user_id = ? AND category = ?",
                (stamp(at + timedelta(hours=24)), user_id, category),
            )
            return "warn"
        return "ignore"


def non_tagging_name(username, display_name):
    # A word joiner keeps @ visually intact without creating a Telegram mention.
    name = f"@\u2060{username}" if username else display_name
    return name.replace("@", "@\u2060") if not username else name


async def yellow_list(message):
    at = now()
    with db_open() as db:
        rows = db.execute(
            """SELECT m.username, m.display_name, c.yellow_at, c.yellow_until
               FROM cards c JOIN members m ON m.user_id = c.user_id
               WHERE c.yellow_until > ?
               ORDER BY c.yellow_at, m.user_id""",
            (stamp(at),),
        ).fetchall()
    if not rows:
        await message.reply_text("🟨 Активних жовтих карток немає.")
        return
    entries = []
    for username, display_name, issued, until in rows:
        entries.append(
            f"{non_tagging_name(username, display_name)}\n"
            f"Видана: {datetime.fromisoformat(issued):%d.%m.%Y}\n"
            f"Діє до: {datetime.fromisoformat(until):%d.%m.%Y}"
        )
    await message.reply_text("🟨 Активні жовті картки:\n\n" + "\n\n".join(entries))


async def yellow(update, context):
    message = update.effective_message
    if message is None or message.chat_id != GROUP_ID or message.from_user is None or message.from_user.is_bot:
        return
    if not (message.text or "").split(maxsplit=1)[1:]:
        action = info_request_action(message.from_user.id, "YELLOW_LIST")
        if action == "answer":
            await yellow_list(message)
        elif action == "warn":
            await message.reply_text("🖕")
        return
    if not await admin_command_allowed(
        message, context, "Видавати жовті картки можуть тільки адміністратори."
    ):
        return
    target = await card_target(message, context, "Використання: /yellow @username")
    if target is None:
        return
    user_id, username = target
    label = f"@{username}" if username else message.reply_to_message.from_user.mention_html()
    issued = now()
    yellow_until, red_until = card_state(user_id, issued)
    if red_until:
        response = f"🟥 Червона картка для {label}"
    elif yellow_until:
        with db_open() as db:
            db.execute("UPDATE cards SET red_at = ?, red_until = ? WHERE user_id = ?",
                       (stamp(issued), stamp(plus_two_months(issued)), user_id))
        response = f"🟥 Червона картка для {label}"
    else:
        with db_open() as db:
            db.execute(
                """INSERT INTO cards(user_id, yellow_at, yellow_until, red_at, red_until)
                   VALUES (?, ?, ?, NULL, NULL)
                   ON CONFLICT(user_id) DO UPDATE SET
                   yellow_at=excluded.yellow_at, yellow_until=excluded.yellow_until,
                   red_at=NULL, red_until=NULL""",
                (user_id, stamp(issued), stamp(plus_two_months(issued))),
            )
        response = f"🟨 Жовта картка для {label}"
    await message.reply_text(response, parse_mode=ParseMode.HTML)


async def clearcards(update, context):
    message = update.effective_message
    if not await admin_command_allowed(message, context):
        return
    target = await card_target(message, context, "Використання: /clearcards @username", allow_reply=False)
    if target is None:
        return
    user_id, username = target
    with db_open() as db:
        db.execute("DELETE FROM cards WHERE user_id = ?", (user_id,))
    if username:
        label = non_tagging_name(username, "")
    else:
        label = non_tagging_name(None, message.reply_to_message.from_user.full_name)
    await message.reply_text(f"Усі картки {label} анульовано.")


async def cards(update, context):
    message = update.effective_message
    if not await admin_command_allowed(message, context):
        return
    target = await card_target(message, context, "Використання: /cards @username")
    if target is None:
        return
    user_id, username = target
    label = f"@{username}" if username else message.reply_to_message.from_user.mention_html()
    yellow_until, red_until = card_state(user_id, now())
    lines = [label]
    if yellow_until or red_until:
        lines.append(f"🟨 Активних жовтих: {int(yellow_until is not None)}")
        if yellow_until:
            lines.append(f"Діє до: {yellow_until:%d.%m.%Y}")
        if red_until:
            lines.append(f"🟥 Активна червона картка до: {red_until:%d.%m.%Y}")
    else:
        lines.append("Карток немає.")
    await message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def poll_answer(update, context):
    answer = update.poll_answer
    if answer is None or answer.user is None or not answer.option_ids:
        return
    with db_open() as db:
        result = db.execute("SELECT 1 FROM polls WHERE poll_id = ?", (answer.poll_id,)).fetchone()
        if result:
            db.execute("INSERT OR IGNORE INTO participation VALUES (?, ?)", (answer.poll_id, answer.user.id))
    if result:
        remember(answer.user)


def claim_due_fun_event(at=None):
    """Claim one due message before sending, so a Restart cannot resend it."""
    at = at or now()
    with db_open() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            """SELECT poll_id, poll_message_id, reminder_due, match_at
               FROM poll_fun WHERE reminder_sent_at IS NULL AND reminder_due <= ?
               ORDER BY reminder_due LIMIT 1""",
            (stamp(at),),
        ).fetchone()
        if row:
            db.execute("UPDATE poll_fun SET reminder_sent_at = ? WHERE poll_id = ?", (stamp(at), row[0]))
            return "reminder", row
        row = db.execute(
            """SELECT poll_id, poll_message_id, match_due, match_at
               FROM poll_fun WHERE match_sent_at IS NULL AND match_due IS NOT NULL AND match_due <= ?
               ORDER BY match_due LIMIT 1""",
            (stamp(at),),
        ).fetchone()
        if row:
            db.execute("UPDATE poll_fun SET match_sent_at = ? WHERE poll_id = ?", (stamp(at), row[0]))
            return "match", row
    return None


async def send_due_fun_event(application, at=None):
    at = at or now()
    claimed = claim_due_fun_event(at)
    if claimed is None:
        return False
    kind, (poll_id, message_id, due_text, match_text) = claimed
    game_at = datetime.fromisoformat(match_text) if match_text else None
    if kind == "reminder":
        due = datetime.fromisoformat(due_text)
        if at > due + timedelta(hours=6) or (game_at and at >= game_at):
            return True
        phrase = random.choice(POLL_REMINDERS)
    else:
        if (game_at is None or at >= game_at
                or at.astimezone(GAME_ZONE).date() != game_at.astimezone(GAME_ZONE).date()):
            return True
        phrase = random.choice(MATCH_DAY_MESSAGES)
    try:
        await application.bot.send_message(GROUP_ID, phrase, reply_to_message_id=message_id)
    except TelegramError:
        logger.exception("Failed to send %s for poll %s", kind, poll_id)
    return True


async def fun_loop(application):
    while True:
        try:
            await send_due_fun_event(application)
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Fun message timer failed; will retry")
            await asyncio.sleep(60)


async def ordinary_message(update, context):
    message = update.message
    if (message is None or message.chat_id != GROUP_ID or message.from_user is None
            or message.from_user.is_bot):
        return
    remember(message.from_user, message.date)
    reply = auto_reply(message.text or "")
    if reply:
        action = info_request_action(message.from_user.id, AUTO_REPLY_CATEGORIES[reply])
        if action == "answer":
            await message.reply_text(reply, disable_web_page_preview=True)
        elif action == "warn":
            await message.reply_text("🖕")
        return
    if says_they_are_late(message.text or "") and take_fun_cooldown(message.from_user.id, "LATE", 4):
        await message.reply_text(random.choice(LATE_JOKES))


def period_start():
    with db_open() as db:
        return datetime.fromisoformat(db.execute(
            "SELECT value FROM settings WHERE key = 'period_start'"
        ).fetchone()[0])


async def publish_report(application, start, end):
    with db_open() as db:
        rows = db.execute(
            """SELECT m.user_id, m.display_name, m.first_seen,
                      COUNT(DISTINCT poll.poll_id)
               FROM members m
               LEFT JOIN participation p ON p.user_id = m.user_id
               LEFT JOIN polls poll ON poll.poll_id = p.poll_id
                   AND poll.created_at >= ? AND poll.created_at < ?
               WHERE m.is_bot = 0
               GROUP BY m.user_id""",
            (stamp(start), stamp(end)),
        ).fetchall()
    groups = {0: [], 1: []}
    for user_id, name, first_seen, count in rows:
        if (now() - datetime.fromisoformat(first_seen)).total_seconds() < 14 * 86400 or count >= 2:
            continue
        try:
            member = await application.bot.get_chat_member(GROUP_ID, user_id)
        except TelegramError:
            logger.exception("Cannot check membership for user %s", user_id)
            continue
        if member.status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
            continue
        if member.status == ChatMemberStatus.RESTRICTED and not member.is_member:
            continue
        groups[count].append(f'<a href="tg://user?id={user_id}">{escape(name)}</a>')
    if groups[0] or groups[1]:
        parts = ["🕵 Підсумки за останні два місяці."]
        if groups[0]:
            parts.append("👻 0 голосувань:\n" + "\n".join(groups[0]))
        if groups[1]:
            parts.append("🤏 1 голосування:\n" + "\n".join(groups[1]))
        parts.append("Хлопці, ви грати плануєте? 😄\nЧи ви тут спецагенти  читаєте нашу переписку й передаєте інформацію іспанській поліції? 👀")
    else:
        parts = ["Підсумки за останні два місяці.",
                 "Усі активні учасники нормально брали участь у голосуваннях 😎",
                 "Схоже, цього разу чистка не потрібна. Продовжуємо грати ⚽"]
    await application.bot.send_message(GROUP_ID, "\n\n".join(parts), parse_mode=ParseMode.HTML)


async def report_loop(application):
    while True:
        try:
            start = period_start()
            end = plus_two_months(start)
            if now() >= end:
                await publish_report(application, start, end)
                with db_open() as db:
                    db.execute("UPDATE settings SET value = ? WHERE key = 'period_start' AND value = ?",
                               (stamp(end), stamp(start)))
                continue
            await asyncio.sleep(min(max((end - now()).total_seconds(), 1), 3600))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Two-month report failed; will retry")
            await asyncio.sleep(300)


async def post_init(application):
    global report_task, fun_task
    init_db()
    report_task = asyncio.create_task(report_loop(application))
    fun_task = asyncio.create_task(fun_loop(application))


async def post_shutdown(application):
    for task in (report_task, fun_task):
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


def main():
    if not TOKEN:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN before starting the bot.")
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    application = Application.builder().token(TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()
    application.add_handler(CommandHandler("football", football))
    application.add_handler(CommandHandler("fortune", fortune))
    application.add_handler(CommandHandler("yellow", yellow))
    application.add_handler(CommandHandler("clearcards", clearcards))
    application.add_handler(CommandHandler("cards", cards))
    application.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome))
    application.add_handler(ChatMemberHandler(member_changed, ChatMemberHandler.CHAT_MEMBER))
    application.add_handler(PollAnswerHandler(poll_answer))
    # Exclude bot-command entities anywhere in the message, including quoted command examples.
    application.add_handler(MessageHandler(filters.TEXT & ~filters.Command(only_start=False), ordinary_message))
    logger.info("Starting bot for chat %s", GROUP_ID)
    application.run_polling(
        allowed_updates=[Update.MESSAGE, Update.CHAT_MEMBER, Update.POLL_ANSWER],
        drop_pending_updates=False, bootstrap_retries=-1,
    )


if __name__ == "__main__":
    main()
