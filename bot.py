"""
Telegram-бот с утренней сводкой.

Что делает:
- при первом запуске (/start) сам расспрашивает пользователя обо всём нужном
  (город, часовой пояс, время подъёма, сколько часов спать, время присылки
  сводки, время в пути дом->колледж и работа->колледж, работает ли
  пользователь и по каким дням, расписание пар на каждый день недели);
- каждое утро в заданное время присылает сводку: погода, совет по одежде,
  планы на день, расписание пар с точным временем начала/конца и перерывами,
  время выхода из дома или с работы, время отхода ко сну.

Настройка — см. README.md.
"""

import logging
import os
from datetime import datetime, time as dtime, timedelta, date
from zoneinfo import ZoneInfo, available_timezones

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import storage
from weather import fetch_weather, clothing_advice, WeatherError

load_dotenv()

BOT_TOKEN = os.environ.get("BOT_TOKEN")
WEATHER_API_KEY = os.environ.get("WEATHER_API_KEY")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

WEEKDAYS_RU = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]

# ---- состояния диалога онбординга ----
(
    CITY,
    TIMEZONE,
    WAKE_TIME,
    SLEEP_HOURS,
    DIGEST_TIME,
    COMMUTE_HOME,
    HAS_WORK,
    COMMUTE_WORK,
    WORK_DAY,
    SCHEDULE_DAY,
) = range(10)


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def parse_hhmm(text: str) -> str | None:
    text = text.strip()
    try:
        t = datetime.strptime(text, "%H:%M").time()
        return t.strftime("%H:%M")
    except ValueError:
        return None


def parse_day_schedule(text: str) -> list | None:
    """Разбирает сообщение вида:
    9:00-10:30 Матанализ
    10:45-12:15 Физика
    Пустой день обозначается "-".
    Возвращает список пар (start, end, name) или None, если формат неверный.
    """
    text = text.strip()
    if text == "-":
        return []
    classes = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if " " in line:
            time_part, name = line.split(" ", 1)
        else:
            time_part, name = line, "Пара"
        if "-" not in time_part:
            return None
        start_s, _, end_s = time_part.partition("-")
        start = parse_hhmm(start_s)
        end = parse_hhmm(end_s)
        if start is None or end is None:
            return None
        classes.append({"start": start, "end": end, "name": name.strip()})
    classes.sort(key=lambda c: c["start"])
    return classes


def time_obj(hhmm: str) -> dtime:
    return datetime.strptime(hhmm, "%H:%M").time()


def combine_today(hhmm: str) -> datetime:
    return datetime.combine(date.today(), time_obj(hhmm))


def fmt_minutes(m: int) -> str:
    h, mm = divmod(int(m), 60)
    if h and mm:
        return f"{h} ч {mm} мин"
    if h:
        return f"{h} ч"
    return f"{mm} мин"


# ---------------------------------------------------------------------------
# Онбординг
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["draft"] = {}
    existing = storage.get_user(update.effective_chat.id)
    if existing:
        await update.message.reply_text(
            "У тебя уже есть настройки. Если хочешь пройти опрос заново и "
            "переписать всё (город, расписание и т.д.) — жми /reset, затем /start.\n"
            "Посмотреть текущие настройки — /settings. Список команд — /help."
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "Привет! Я буду каждое утро присылать тебе сводку: погода, совет что "
        "надеть, планы на день, расписание пар, когда выходить из дома/с "
        "работы и во сколько лечь спать.\n\n"
        "Сначала немного настроек. В любой момент можно прервать через /cancel.\n\n"
        "В каком городе ты находишься? (для погоды)"
    )
    return CITY


async def city_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    city = update.message.text.strip()
    if not WEATHER_API_KEY:
        await update.message.reply_text(
            "⚠️ У бота не задан WEATHER_API_KEY, поэтому не могу проверить город "
            "прямо сейчас, но сохраню как есть."
        )
        context.user_data["draft"]["city"] = city
    else:
        try:
            fetch_weather(city, WEATHER_API_KEY)
        except WeatherError as e:
            await update.message.reply_text(f"{e}\nПопробуй ввести город ещё раз.")
            return CITY
        context.user_data["draft"]["city"] = city

    await update.message.reply_text(
        "Отлично. Теперь укажи свой часовой пояс в формате IANA, например "
        "Europe/Warsaw, Europe/Moscow, Asia/Almaty.\n"
        "Если не знаешь — просто напиши название своего города на английском, "
        "но надёжнее взять точное значение из списка часовых поясов."
    )
    return TIMEZONE


async def timezone_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    tz_name = update.message.text.strip()
    if tz_name not in available_timezones():
        await update.message.reply_text(
            "Не узнаю такой часовой пояс. Нужен формат вида Europe/Warsaw. "
            "Попробуй ещё раз."
        )
        return TIMEZONE
    context.user_data["draft"]["timezone"] = tz_name
    await update.message.reply_text("Во сколько ты обычно встаёшь? Формат ЧЧ:ММ, например 07:00")
    return WAKE_TIME


async def wake_time_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    t = parse_hhmm(update.message.text)
    if t is None:
        await update.message.reply_text("Не понял время. Формат ЧЧ:ММ, например 07:00")
        return WAKE_TIME
    context.user_data["draft"]["wake_time"] = t
    await update.message.reply_text("Сколько часов сна тебе нужно, чтобы выспаться? Например: 8")
    return SLEEP_HOURS


async def sleep_hours_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        hours = float(update.message.text.strip().replace(",", "."))
        if not (3 <= hours <= 14):
            raise ValueError
    except ValueError:
        await update.message.reply_text("Введи число часов от 3 до 14, например 8 или 7.5")
        return SLEEP_HOURS
    context.user_data["draft"]["sleep_hours"] = hours
    await update.message.reply_text(
        "Во сколько присылать тебе утреннюю сводку? Формат ЧЧ:ММ, например 06:30 "
        "(лучше немного раньше, чем ты встаёшь, либо сразу после подъёма)."
    )
    return DIGEST_TIME


async def digest_time_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    t = parse_hhmm(update.message.text)
    if t is None:
        await update.message.reply_text("Не понял время. Формат ЧЧ:ММ, например 06:30")
        return DIGEST_TIME
    context.user_data["draft"]["digest_time"] = t
    await update.message.reply_text("Сколько минут занимает дорога из дома до колледжа?")
    return COMMUTE_HOME


async def commute_home_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        minutes = int(update.message.text.strip())
        if not (0 <= minutes <= 300):
            raise ValueError
    except ValueError:
        await update.message.reply_text("Введи целое число минут, например 25")
        return COMMUTE_HOME
    context.user_data["draft"]["commute_home_college"] = minutes
    await update.message.reply_text("Ты работаешь (кроме учёбы)? Ответь \"да\" или \"нет\".")
    return HAS_WORK


async def has_work_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    ans = update.message.text.strip().lower()
    if ans not in ("да", "нет"):
        await update.message.reply_text("Ответь, пожалуйста, \"да\" или \"нет\".")
        return HAS_WORK
    context.user_data["draft"]["has_work"] = ans == "да"
    if ans == "да":
        await update.message.reply_text("Сколько минут занимает дорога с работы до колледжа?")
        return COMMUTE_WORK
    context.user_data["draft"]["commute_work_college"] = None
    context.user_data["draft"]["work_schedule"] = {}
    return await start_schedule_loop(update, context)


async def commute_work_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        minutes = int(update.message.text.strip())
        if not (0 <= minutes <= 300):
            raise ValueError
    except ValueError:
        await update.message.reply_text("Введи целое число минут, например 15")
        return COMMUTE_WORK
    context.user_data["draft"]["commute_work_college"] = minutes
    context.user_data["draft"]["work_schedule"] = {}
    context.user_data["draft"]["work_day_index"] = 0
    return await ask_work_day(update, context)


async def ask_work_day(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    idx = context.user_data["draft"]["work_day_index"]
    if idx >= 7:
        return await start_schedule_loop(update, context)
    await update.message.reply_text(
        f"{WEEKDAYS_RU[idx]}: во сколько заканчивается работа перед учёбой в этот день?\n"
        "Формат ЧЧ:ММ, либо \"-\", если в этот день перед учёбой работы нет."
    )
    return WORK_DAY


async def work_day_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    idx = context.user_data["draft"]["work_day_index"]
    if text == "-":
        context.user_data["draft"]["work_schedule"][str(idx)] = None
    else:
        t = parse_hhmm(text)
        if t is None:
            await update.message.reply_text("Формат ЧЧ:ММ или \"-\". Попробуй ещё раз.")
            return WORK_DAY
        context.user_data["draft"]["work_schedule"][str(idx)] = t
    context.user_data["draft"]["work_day_index"] = idx + 1
    return await ask_work_day(update, context)


async def start_schedule_loop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["draft"]["schedule"] = {}
    context.user_data["draft"]["schedule_day_index"] = 0
    await update.message.reply_text(
        "Теперь расписание пар на каждый день недели.\n"
        "Присылай каждую пару отдельной строкой в формате:\n"
        "9:00-10:30 Матанализ\n10:45-12:15 Физика\n\n"
        "Если в какой-то день пар нет — просто отправь \"-\"."
    )
    return await ask_schedule_day(update, context)


async def ask_schedule_day(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    idx = context.user_data["draft"]["schedule_day_index"]
    if idx >= 7:
        return await finish_onboarding(update, context)
    await update.message.reply_text(f"Расписание на {WEEKDAYS_RU[idx]}:")
    return SCHEDULE_DAY


async def schedule_day_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    idx = context.user_data["draft"]["schedule_day_index"]
    classes = parse_day_schedule(update.message.text)
    if classes is None:
        await update.message.reply_text(
            "Не разобрал формат. Пример:\n9:00-10:30 Матанализ\n10:45-12:15 Физика\n"
            "Или \"-\", если пар нет."
        )
        return SCHEDULE_DAY
    context.user_data["draft"]["schedule"][str(idx)] = classes
    context.user_data["draft"]["schedule_day_index"] = idx + 1
    return await ask_schedule_day(update, context)


async def finish_onboarding(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    draft = context.user_data["draft"]
    draft.setdefault("plans", {})
    storage.save_user(update.effective_chat.id, draft)
    schedule_daily_job(context.application, update.effective_chat.id, draft)
    await update.message.reply_text(
        "Готово! Настройки сохранены. Каждый день в "
        f"{draft['digest_time']} ({draft['timezone']}) буду присылать сводку.\n\n"
        "Полезные команды:\n"
        "/today — прислать сводку прямо сейчас\n"
        "/addplan текст — добавить пункт в планы на сегодня\n"
        "/plans — показать планы на сегодня\n"
        "/clearplans — очистить планы на сегодня\n"
        "/settings — посмотреть текущие настройки\n"
        "/reset — стереть настройки и начать заново"
    )
    context.user_data.pop("draft", None)
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("draft", None)
    await update.message.reply_text("Настройка прервана. Можно начать заново командой /start.")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Сборка и отправка утренней сводки
# ---------------------------------------------------------------------------

def build_digest_text(chat_id) -> str:
    user = storage.get_user(chat_id)
    if not user:
        return "Настройки не найдены. Напиши /start, чтобы их задать."

    tz = ZoneInfo(user["timezone"])
    now = datetime.now(tz)
    weekday_idx = now.weekday()
    today_str = now.strftime("%Y-%m-%d")

    lines = [f"Доброе утро! ☀️", f"📅 {now.strftime('%d.%m.%Y')}, {WEEKDAYS_RU[weekday_idx]}", ""]

    # погода
    if WEATHER_API_KEY:
        try:
            w = fetch_weather(user["city"], WEATHER_API_KEY)
            lines.append(
                f"🌤 Погода в {user['city']}: {w['temp']}°C "
                f"(ощущается как {w['feels_like']}°C), {w['description']}"
            )
            lines.append(f"👕 {clothing_advice(w)}")
        except WeatherError as e:
            lines.append(f"🌤 Погода недоступна: {e}")
    else:
        lines.append("🌤 Погода недоступна: не задан WEATHER_API_KEY.")
    lines.append("")

    # планы на день
    plans = user.get("plans", {}).get(today_str, [])
    lines.append("📝 Планы на сегодня:")
    if plans:
        for p in plans:
            lines.append(f"  • {p}")
    else:
        lines.append("  (пока ничего не добавлено — /addplan текст)")
    lines.append("")

    # расписание пар
    classes = user.get("schedule", {}).get(str(weekday_idx), [])
    lines.append("🎓 Расписание пар:")
    if classes:
        prev_end = None
        for c in classes:
            if prev_end is not None:
                gap = (
                    datetime.combine(date.today(), time_obj(c["start"]))
                    - datetime.combine(date.today(), time_obj(prev_end))
                ).total_seconds() / 60
                if gap > 0:
                    lines.append(f"     — перемена {fmt_minutes(gap)}")
            lines.append(f"  {c['start']}–{c['end']}  {c['name']}")
            prev_end = c["end"]
    else:
        lines.append("  Сегодня пар нет 🎉")
    lines.append("")

    # когда выходить
    if classes:
        first_start = classes[0]["start"]
        work_end = user.get("work_schedule", {}).get(str(weekday_idx))
        if work_end:
            leave_dt = datetime.combine(date.today(), time_obj(first_start)) - timedelta(
                minutes=user.get("commute_work_college") or 0
            )
            lines.append(
                f"🏢 Работа заканчивается в {work_end}. "
                f"Выйти с работы в колледж: {leave_dt.strftime('%H:%M')}"
            )
        else:
            leave_dt = datetime.combine(date.today(), time_obj(first_start)) - timedelta(
                minutes=user.get("commute_home_college") or 0
            )
            lines.append(f"🚶 Выйти из дома: {leave_dt.strftime('%H:%M')}")
    lines.append("")

    # сон
    wake_dt = datetime.combine(date.today(), time_obj(user["wake_time"]))
    bedtime_dt = wake_dt - timedelta(hours=user["sleep_hours"])
    lines.append(
        f"😴 Подъём в {user['wake_time']}. Чтобы выспаться "
        f"({user['sleep_hours']} ч), ложись около {bedtime_dt.strftime('%H:%M')} накануне вечером."
    )

    return "\n".join(lines)


async def send_digest_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = context.job.chat_id
    try:
        text = build_digest_text(chat_id)
        await context.bot.send_message(chat_id=chat_id, text=text)
    except Exception:
        log.exception("Не удалось отправить сводку для %s", chat_id)


def schedule_daily_job(application: Application, chat_id, user: dict) -> None:
    jq = application.job_queue
    for job in jq.get_jobs_by_name(f"digest_{chat_id}"):
        job.schedule_removal()
    t = time_obj(user["digest_time"]).replace(tzinfo=ZoneInfo(user["timezone"]))
    jq.run_daily(
        send_digest_job,
        time=t,
        days=(0, 1, 2, 3, 4, 5, 6),
        chat_id=chat_id,
        name=f"digest_{chat_id}",
    )


async def restore_jobs(application: Application) -> None:
    for chat_id, user in storage.all_users().items():
        try:
            schedule_daily_job(application, int(chat_id), user)
        except Exception:
            log.exception("Не удалось восстановить задачу для %s", chat_id)


# ---------------------------------------------------------------------------
# Обычные команды (вне онбординга)
# ---------------------------------------------------------------------------

async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(build_digest_text(update.effective_chat.id))


async def addplan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Использование: /addplan текст плана")
        return
    user = storage.get_user(update.effective_chat.id)
    if not user:
        await update.message.reply_text("Сначала настрой бота через /start.")
        return
    tz = ZoneInfo(user["timezone"])
    today_str = datetime.now(tz).strftime("%Y-%m-%d")
    storage.add_plan(update.effective_chat.id, today_str, " ".join(context.args))
    await update.message.reply_text("Добавил в планы на сегодня ✅")


async def plans_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = storage.get_user(update.effective_chat.id)
    if not user:
        await update.message.reply_text("Сначала настрой бота через /start.")
        return
    tz = ZoneInfo(user["timezone"])
    today_str = datetime.now(tz).strftime("%Y-%m-%d")
    plans = storage.get_plans(update.effective_chat.id, today_str)
    if not plans:
        await update.message.reply_text("На сегодня пока ничего не запланировано.")
        return
    await update.message.reply_text("Планы на сегодня:\n" + "\n".join(f"• {p}" for p in plans))


async def clearplans_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = storage.get_user(update.effective_chat.id)
    if not user:
        await update.message.reply_text("Сначала настрой бота через /start.")
        return
    tz = ZoneInfo(user["timezone"])
    today_str = datetime.now(tz).strftime("%Y-%m-%d")
    storage.clear_plans(update.effective_chat.id, today_str)
    await update.message.reply_text("Планы на сегодня очищены.")


async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = storage.get_user(update.effective_chat.id)
    if not user:
        await update.message.reply_text("Настройки не заданы. Напиши /start.")
        return
    text = [
        f"Город: {user['city']}",
        f"Часовой пояс: {user['timezone']}",
        f"Подъём: {user['wake_time']}",
        f"Нужно спать: {user['sleep_hours']} ч",
        f"Сводка присылается в: {user['digest_time']}",
        f"Дорога дом→колледж: {user['commute_home_college']} мин",
    ]
    if user.get("has_work"):
        text.append(f"Дорога работа→колледж: {user['commute_work_college']} мин")
        work_days = [
            f"  {WEEKDAYS_RU[int(k)]}: до {v}"
            for k, v in sorted(user.get("work_schedule", {}).items(), key=lambda x: int(x[0]))
            if v
        ]
        if work_days:
            text.append("Рабочие дни перед учёбой:\n" + "\n".join(work_days))
    text.append("")
    text.append("Расписание пар:")
    for idx in range(7):
        classes = user.get("schedule", {}).get(str(idx), [])
        if classes:
            joined = ", ".join(f"{c['start']}-{c['end']} {c['name']}" for c in classes)
        else:
            joined = "нет пар"
        text.append(f"  {WEEKDAYS_RU[idx]}: {joined}")
    await update.message.reply_text("\n".join(text))


async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    storage.delete_user(update.effective_chat.id)
    for job in context.application.job_queue.get_jobs_by_name(f"digest_{update.effective_chat.id}"):
        job.schedule_removal()
    await update.message.reply_text("Настройки удалены. Напиши /start, чтобы настроить бота заново.")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "/start — первичная настройка\n"
        "/today — прислать сводку прямо сейчас\n"
        "/addplan текст — добавить пункт в планы на сегодня\n"
        "/plans — показать планы на сегодня\n"
        "/clearplans — очистить планы на сегодня\n"
        "/settings — показать текущие настройки\n"
        "/reset — стереть все настройки\n"
        "/cancel — прервать текущий диалог настройки"
    )


# ---------------------------------------------------------------------------
# Запуск
# ---------------------------------------------------------------------------

def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Не задан BOT_TOKEN. Смотри README.md.")

    application = ApplicationBuilder().token(BOT_TOKEN).post_init(restore_jobs).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, city_step)],
            TIMEZONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, timezone_step)],
            WAKE_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, wake_time_step)],
            SLEEP_HOURS: [MessageHandler(filters.TEXT & ~filters.COMMAND, sleep_hours_step)],
            DIGEST_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, digest_time_step)],
            COMMUTE_HOME: [MessageHandler(filters.TEXT & ~filters.COMMAND, commute_home_step)],
            HAS_WORK: [MessageHandler(filters.TEXT & ~filters.COMMAND, has_work_step)],
            COMMUTE_WORK: [MessageHandler(filters.TEXT & ~filters.COMMAND, commute_work_step)],
            WORK_DAY: [MessageHandler(filters.TEXT & ~filters.COMMAND, work_day_step)],
            SCHEDULE_DAY: [MessageHandler(filters.TEXT & ~filters.COMMAND, schedule_day_step)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    application.add_handler(conv)
    application.add_handler(CommandHandler("today", today_cmd))
    application.add_handler(CommandHandler("addplan", addplan_cmd))
    application.add_handler(CommandHandler("plans", plans_cmd))
    application.add_handler(CommandHandler("clearplans", clearplans_cmd))
    application.add_handler(CommandHandler("settings", settings_cmd))
    application.add_handler(CommandHandler("reset", reset_cmd))
    application.add_handler(CommandHandler("help", help_cmd))

    log.info("Бот запущен, жду сообщений...")
    application.run_polling()


if __name__ == "__main__":
    main()
