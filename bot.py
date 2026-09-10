"""
Телеграм-бот игры «Киллер» — версия для Railway + PostgreSQL (psycopg2).

Что изменено по сравнению с прошлой версией:
  1. Все игровые действия — обычные текстовые кнопки («🎯 Моя цель», «🔫 Убить жертву», ...),
     без слэш-команд в клавиатуре.
  2. sqlite3 полностью заменён на psycopg2 / PostgreSQL (см. db.py).
  3. Админ получает полное досье игрока ВМЕСТЕ с фотографией + может заменить фото в базе.
  4. У зарегистрированного участника в меню остаётся кнопка отмены регистрации.
  5. Добавлены новые игровые механики (см. README.md, раздел «Фишки»).
"""

import csv
import io
import logging
import os
import random
import re
from datetime import datetime, timedelta, timezone

from telegram import Update, ReplyKeyboardRemove
from telegram.constants import ChatAction
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import db
import keyboards as K

# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = {
    int(x) for x in os.getenv("ADMIN_IDS", os.getenv("ADMIN_ID", "0")).replace(" ", "").split(",") if x
}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# ---- Состояния диалогов ----
REG_FLOW = 0
KILL_CODE = 10
MSG_TEXT = 20
CANCEL_REG_CONFIRM = 30
LAST_WORDS = 40
ADMIN_PHOTO = 50
MSG_ANY_RECIPIENT = 60
MSG_ANY_SIGN = 61
MSG_ANY_TEXT = 62

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

RULES_TEXT = (
    "📖 Правила игры «Киллер»\n\n"
    "1. Игра построена на принципе честной игры. Нарушил — выбыл.\n"
    "2. Каждый участник одновременно охотник и жертва.\n"
    "3. Игра стартует для всех одновременно. Ты получаешь досье на свою жертву: "
    "фото, привычки, маршруты. В это же время кто-то охотится на тебя.\n"
    "4. Жертва считается убитой, если охотник «выстрелил» в неё из пальца один на один "
    "в закрытом помещении или на улице, где в радиусе 20 метров никого нет. "
    "При свидетелях убийство не засчитывается.\n"
    "5. После смерти жертва передаёт охотнику свой личный код. Охотник вводит код в бот "
    "и получает новую жертву.\n"
    "6. Если охотник и жертва охотятся друг на друга — обратитесь к организаторам.\n"
    "7. Игра заканчивается, когда остаются два участника, или когда истекает время. "
    "Победитель — тот, у кого больше всего убийств. При равенстве выше тот, чьи жертвы сами успели больше убить, а если и так равно — кто раньше совершил своё последнее убийство.\n"
    "8. За каждое убийство начисляются очки (награда за жертву). Очки можно потратить на анонимное письмо любому живому игроку (1 очко = 1 письмо). Своему охотнику и своей жертве можно писать бесплатно.\n"
    "9. Список выбывших игрокам не показывается — кто выбыл, знают только организатор.\n"
)

# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def gen_code(k: int = 6) -> str:
    return "".join(random.choices(CODE_ALPHABET, k=k))


def gen_unique_personal_code() -> str:
    for _ in range(50):
        code = gen_code()
        if not db.fetch_one("SELECT 1 FROM players WHERE personal_code = %s", (code,)):
            return code
    return gen_code(8)


def game_started() -> bool:
    return db.get_setting("game_started", "False") == "True"


def registration_open() -> bool:
    return db.get_setting("registration_open", "True") == "True" and not game_started()


def game_duration_days() -> int:
    try:
        return int(db.get_setting("game_duration_days", "14"))
    except (TypeError, ValueError):
        return 14


def game_start_date() -> datetime | None:
    raw = db.get_setting("game_start_date", "")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:  # значения из старой (sqlite) версии
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def killfeed_enabled() -> bool:
    return db.get_setting("killfeed", "True") == "True"


def get_player(user_id: int):
    return db.fetch_one("SELECT * FROM players WHERE user_id = %s", (user_id,))


def menu_for(user_id: int):
    """Подбирает клавиатуру под текущее состояние игрока."""
    admin = is_admin(user_id)
    player = get_player(user_id)
    if not player:
        return K.guest_menu(registration_open())
    if not game_started():
        return K.lobby_menu(admin)
    if player["is_alive"]:
        return K.game_menu(admin)
    return K.dead_menu(admin)


def alive_count() -> int:
    return db.fetch_val("SELECT COUNT(*) FROM players WHERE is_alive", default=0)


async def notify_admins(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    for admin_id in ADMIN_IDS:
        if not admin_id:
            continue
        try:
            await context.bot.send_message(chat_id=admin_id, text=text)
        except TelegramError as e:
            logger.warning("Не удалось написать админу %s: %s", admin_id, e)


async def safe_send(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str, **kwargs) -> bool:
    try:
        await context.bot.send_message(chat_id=chat_id, text=text, **kwargs)
        return True
    except TelegramError as e:
        logger.warning("Не доставлено %s: %s", chat_id, e)
        return False


async def notify_admins_message(context: ContextTypes.DEFAULT_TYPE, text: str, photo_id: str | None = None) -> None:
    """Копия письма/фото админам — с реальными именами, не анонимно."""
    for admin_id in ADMIN_IDS:
        if not admin_id:
            continue
        try:
            if photo_id:
                if len(text) <= 1000:
                    await context.bot.send_photo(chat_id=admin_id, photo=photo_id, caption=text)
                else:
                    await context.bot.send_photo(chat_id=admin_id, photo=photo_id, caption=text[:1000] + "…")
                    await context.bot.send_message(chat_id=admin_id, text=text)
            else:
                await context.bot.send_message(chat_id=admin_id, text=text)
        except TelegramError as e:
            logger.warning("Не удалось переслать письмо админу %s: %s", admin_id, e)


def pressed_menu_button(text: str) -> bool:
    return text in K.ALL_MENU_BUTTONS


async def send_dossier(context: ContextTypes.DEFAULT_TYPE, chat_id: int, photo_id: str | None,
                       text: str, reply_markup=None) -> None:
    """Отправляет фото + текст. Подпись к фото у Telegram ограничена 1024 символами,
    поэтому длинные досье уходят отдельным сообщением."""
    if photo_id:
        try:
            if len(text) <= 1000:
                await context.bot.send_photo(chat_id=chat_id, photo=photo_id, caption=text,
                                             reply_markup=reply_markup)
                return
            await context.bot.send_photo(chat_id=chat_id, photo=photo_id,
                                         caption=text[:1000] + "…")
            await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)
            return
        except TelegramError as e:
            logger.warning("Фото не отправлено (%s): %s", photo_id, e)
            text += "\n\n⚠️ Фото недоступно."
    await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)


# ---------------------------------------------------------------------------
# Тексты досье
# ---------------------------------------------------------------------------
def field(row, key, default=""):
    """Безопасное чтение поля строки (RealDictRow / любая mapping-строка)."""
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


def dossier_for_hunter(p) -> str:
    """Досье жертвы — то, что видит охотник."""
    lines = [
        "🔫 ТВОЯ ЦЕЛЬ",
        "",
        f"Имя: {p['full_name']}",
        f"Курс: {p['course'] or '—'}",
        f"Группа: {p['academic_group'] or '—'}",
        f"Соцсети: {p['social_links'] or '—'}",
        f"Корпуса: {p['buildings'] or '—'}",
        f"Общежитие/район: {p['dormitory'] or '—'}",
        f"О себе и маршрутах: {p['about_self'] or '—'}",
    ]
    if field(p, "habits"):
        lines.append(f"Привычки: {field(p, 'habits')}")
    if field(p, "hint"):
        lines.append(f"🕵️ Подсказка от организаторов: {field(p, 'hint')}")
    lines.append(f"💰 Награда за цель: {p['reward']} очк.")
    if p["is_danger"]:
        lines += ["", "⚠️ ОСОБО ОПАСЕН", f"Причина: {p['danger_reason'] or 'не указана'}"]
    lines += [
        "",
        "После убийства жертва называет свой личный код — введи его через кнопку «🔫 Убить жертву».",
    ]
    return "\n".join(lines)


def dossier_self(p) -> str:
    lines = [
        "👤 ТВОЁ ДОСЬЕ",
        "",
        f"Имя: {p['full_name']}",
        f"Курс: {p['course'] or '—'}",
        f"Группа: {p['academic_group'] or '—'}",
        f"Соцсети: {p['social_links'] or '—'}",
        f"Корпуса: {p['buildings'] or '—'}",
        f"Общежитие/район: {p['dormitory'] or '—'}",
        f"О себе: {p['about_self'] or '—'}",
    ]
    if field(p, "habits"):
        lines.append(f"Привычки: {field(p, 'habits')}")
    lines += [
        "",
        f"Статус: {'🟢 жив' if p['is_alive'] else '🔴 выбыл'}",
        f"Убийств: {p['kills']}",
        f"Очков: {p['points']}",
        f"🔐 Личный код: {p['personal_code']}",
        f"💰 Награда за твою голову: {p['reward']} очк.",
    ]
    if p["is_danger"]:
        lines += ["", "⚠️ ТЫ ОСОБО ОПАСЕН", f"Причина: {p['danger_reason']}"]
    lines += ["", "Будь осторожен — за тобой охотятся." if p["is_alive"]
              else "Ты выбыл из игры. Можешь оставить последнее слово."]
    return "\n".join(lines)


def fmt_dt(value) -> str:
    """Дата в человеческом виде; терпима к строкам и None."""
    if not value:
        return "—"
    if hasattr(value, "strftime"):
        return value.strftime("%d.%m.%Y %H:%M")
    return str(value)[:16]


def dossier_admin(p) -> str:
    """Полная техническая карточка для организатора."""
    reg = fmt_dt(field(p, "registration_date", None))
    death = fmt_dt(field(p, "death_date", None))
    hunter = db.fetch_one(
        "SELECT p.full_name, p.user_id FROM targets t JOIN players p ON p.user_id = t.hunter_id "
        "WHERE t.target_id = %s AND t.is_active",
        (p["user_id"],),
    )
    victims = db.fetch_all(
        "SELECT p.full_name, p.user_id FROM targets t JOIN players p ON p.user_id = t.target_id "
        "WHERE t.hunter_id = %s AND t.is_active",
        (p["user_id"],),
    )
    lines = [
        "🗂 ПОЛНАЯ КАРТОЧКА ИГРОКА",
        "",
        f"user_id: {p['user_id']}",
        f"username: @{p['username']}" if p["username"] else "username: —",
        f"ФИО: {p['full_name']}",
        f"Курс: {p['course'] or '—'}",
        f"Группа: {p['academic_group'] or '—'}",
        f"Соцсети: {p['social_links'] or '—'}",
        f"О себе: {p['about_self'] or '—'}",
        f"Корпуса: {p['buildings'] or '—'}",
        f"Общежитие: {p['dormitory'] or '—'}",
        f"Привычки: {p['habits'] or '—'}",
        f"Подсказка: {p['hint'] or '—'}",
        f"Фото в базе: {'есть' if p['photo_id'] else 'НЕТ'}",
        "",
        f"Статус: {'жив' if p['is_alive'] else 'выбыл'}",
        f"Убийств: {p['kills']} | Очков: {p['points']}",
        f"Личный код: {p['personal_code']}",
        f"Награда за него: {p['reward']}",
        f"Особо опасен: {'ДА — ' + (p['danger_reason'] or '') if p['is_danger'] else 'нет'}",
        f"Регистрация: {reg}",
        f"Смерть: {death}",
        f"Убит игроком: {p['killed_by'] or '—'}",
        f"Последнее слово: {p['last_words'] or '—'}",
        "",
        "Охотится на него: " + (f"{hunter['full_name']} / {hunter['user_id']}" if hunter else "—"),
        "Его цели: " + (", ".join(f"{v['full_name']} / {v['user_id']}" for v in victims) if victims else "—"),
        "",
        f"Заменить фото: /set_photo {p['user_id']}",
        f"Правка поля: /edit_player {p['user_id']} <поле> <значение>",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# /start, помощь, правила
# ---------------------------------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    player = get_player(user.id)

    if player:
        if game_started():
            text = (
                f"С возвращением, {user.first_name}. Игра идёт.\n"
                "Открой «🎯 Моя цель», чтобы увидеть досье жертвы."
            )
        else:
            text = (
                f"Привет, {user.first_name}. Ты уже в списке участников.\n"
                "Ждём старта игры. Пока можешь проверить своё досье или отменить регистрацию."
            )
    else:
        text = (
            f"Привет, {user.first_name}.\n"
            "Это бот игры «Киллер».\n\n" + RULES_TEXT + "\n"
            + ("Нажми «📝 Зарегистрироваться», чтобы попасть в игру."
               if registration_open() else "Регистрация сейчас закрыта.")
        )
    await update.message.reply_text(text, reply_markup=menu_for(user.id))


async def cmd_rules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(RULES_TEXT, reply_markup=menu_for(update.effective_user.id))


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    text = (
        "❓ Как пользоваться ботом\n\n"
        "Всё управление — кнопками внизу экрана:\n"
        f"• {K.BTN_TARGET} — досье твоей жертвы с фото\n"
        f"• {K.BTN_KILL} — ввести код убитой жертвы\n"
        f"• {K.BTN_ME} — твоя анкета и личный код\n"
        f"• {K.BTN_STATS} — статистика игры\n"
        f"• {K.BTN_TOP} — топ игроков по числу убийств\n"
        f"• {K.BTN_MSG_KILLER} / {K.BTN_MSG_TARGET} — анонимная записка своему охотнику/жертве, бесплатно, можно с фото\n"
        f"• {K.BTN_MSG_ANY} — анонимное письмо любому живому игроку за 1 очко (только текст)\n"
        f"• {K.BTN_CANCEL_REG} — выйти из игры до её начала\n\n"
        "Кто выбыл из игры — видно только организаторам.\n\n"
        "Если клавиатура пропала — отправь /start."
    )
    if is_admin(user_id):
        text += "\n\nАдминские команды: /admin"
    await update.message.reply_text(text, reply_markup=menu_for(user_id))


# ---------------------------------------------------------------------------
# РЕГИСТРАЦИЯ
# ---------------------------------------------------------------------------
REG_STEPS = [
    ("full_name", "Введи своё полное имя (ФИО или ФИ):", False),
    ("course", "Введи свой курс (например «3 курс», «магистратура», «преподаватель»):", False),
    ("academic_group", "Введи академическую группу (например ЭИФ-103/6):", False),
    ("social_links", "Укажи ссылки на соцсети (ВК, Telegram):", False),
    ("about_self", "Расскажи о себе: где обычно бываешь, примерный маршрут дня, любимые места.\n"
                   "Это поможет охотнику тебя найти:", False),
    ("buildings", "В каких корпусах у тебя обычно проходят пары?", False),
    ("dormitory", "Живёшь в общежитии? Если да — укажи корпус. Если нет — район проживания:", False),
    ("habits", "Приметы и привычки: как выглядишь, что носишь, где пьёшь кофе.\n"
               "Можно пропустить:", True),
    ("photo_id", "Загрузи своё фото — оно попадёт в досье охотника.", False),
]


async def ask_reg_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    idx = context.user_data.get("reg_idx", 0)
    key, question, skippable = REG_STEPS[idx]
    prefix = f"Шаг {idx + 1} из {len(REG_STEPS)}\n"
    if key == "photo_id":
        await update.message.reply_text(prefix + question, reply_markup=K.registration_nav())
    else:
        await update.message.reply_text(prefix + question, reply_markup=K.registration_nav(skippable))
    return REG_FLOW


async def reg_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    if game_started():
        await update.message.reply_text(
            "Игра уже началась — регистрация закрыта. Следи за статистикой.",
            reply_markup=menu_for(user_id),
        )
        return ConversationHandler.END
    if not registration_open():
        await update.message.reply_text(
            "Регистрация временно закрыта организаторами.", reply_markup=menu_for(user_id)
        )
        return ConversationHandler.END
    if get_player(user_id):
        await update.message.reply_text(
            "✅ Ты уже зарегистрирован. Если хочешь выйти — нажми «🚫 Отменить регистрацию».",
            reply_markup=menu_for(user_id),
        )
        return ConversationHandler.END

    context.user_data["reg_idx"] = 0
    context.user_data["reg_data"] = {}
    await update.message.reply_text(
        "📋 Регистрация в игре «Киллер».\n"
        "Кнопкой «⬅ Назад» можно вернуться к предыдущему вопросу, «❌ Отмена» — прервать.",
    )
    return await ask_reg_step(update, context)


async def reg_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    idx = context.user_data.get("reg_idx", 0)
    key, _question, skippable = REG_STEPS[idx]

    if text == K.BTN_CANCEL:
        return await reg_cancel(update, context)

    if text == K.BTN_BACK:
        if idx == 0:
            return await reg_cancel(update, context)
        context.user_data["reg_idx"] = idx - 1
        return await ask_reg_step(update, context)

    if key == "photo_id":
        await update.message.reply_text(
            "Нужна именно фотография. Пришли её как фото (не файлом).",
            reply_markup=K.registration_nav(),
        )
        return REG_FLOW

    if text == K.BTN_SKIP:
        if not skippable:
            await update.message.reply_text("Этот вопрос пропустить нельзя.")
            return REG_FLOW
        context.user_data["reg_data"][key] = ""
    else:
        if not text:
            await update.message.reply_text("Пустой ответ. Напиши что-нибудь.")
            return REG_FLOW
        context.user_data["reg_data"][key] = text[:800]

    context.user_data["reg_idx"] = idx + 1
    return await ask_reg_step(update, context)


async def reg_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    idx = context.user_data.get("reg_idx", 0)
    key = REG_STEPS[idx][0]
    if key != "photo_id":
        await update.message.reply_text("Фото понадобится позже, сейчас ответь текстом.")
        return REG_FLOW

    context.user_data["reg_data"]["photo_id"] = update.message.photo[-1].file_id
    return await reg_finish(update, context)


async def reg_finish(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    data = context.user_data.get("reg_data", {})

    if get_player(user.id):
        await update.message.reply_text("Ты уже зарегистрирован.", reply_markup=menu_for(user.id))
        return ConversationHandler.END

    personal_code = gen_unique_personal_code()
    db.execute(
        """
        INSERT INTO players
            (user_id, username, full_name, course, academic_group, social_links,
             about_self, buildings, dormitory, habits, photo_id, personal_code, reward)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (user_id) DO NOTHING
        """,
        (
            user.id,
            user.username,
            data.get("full_name"),
            data.get("course"),
            data.get("academic_group"),
            data.get("social_links"),
            data.get("about_self"),
            data.get("buildings"),
            data.get("dormitory"),
            data.get("habits", ""),
            data.get("photo_id"),
            personal_code,
            int(db.get_setting("reward", "1") or 1),
        ),
    )

    await update.message.reply_text(
        "✅ Регистрация завершена, ты в игре.\n\n"
        f"🔐 Твой личный секретный код: {personal_code}\n\n"
        "Запомни его: этот код ты назовёшь охотнику, когда он тебя «убьёт».\n"
        "Никому не сообщай его просто так — назвал код, значит выбыл.\n\n"
        "Ждём старта. Кнопка «🚫 Отменить регистрацию» остаётся доступной до начала игры.",
        reply_markup=menu_for(user.id),
    )

    total = db.fetch_val("SELECT COUNT(*) FROM players", default=0)
    await notify_admins(
        context,
        "📝 Новый участник\n"
        f"Имя: {data.get('full_name')}\n"
        f"Группа: {data.get('academic_group')}\n"
        f"Курс: {data.get('course')}\n"
        f"Код: {personal_code}\n"
        f"ID: {user.id}\n"
        f"Всего участников: {total}\n\n"
        f"Посмотреть досье с фото: /view_profile {user.id}",
    )
    context.user_data.pop("reg_data", None)
    context.user_data.pop("reg_idx", None)
    return ConversationHandler.END


async def reg_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("reg_data", None)
    context.user_data.pop("reg_idx", None)
    await update.message.reply_text(
        "Регистрация прервана. Начать заново — кнопка «📝 Зарегистрироваться».",
        reply_markup=menu_for(update.effective_user.id),
    )
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# ОТМЕНА РЕГИСТРАЦИИ (пункт 4)
# ---------------------------------------------------------------------------
async def cancel_reg_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    player = get_player(user_id)
    if not player:
        await update.message.reply_text("Ты и так не зарегистрирован.", reply_markup=menu_for(user_id))
        return ConversationHandler.END
    if game_started():
        await update.message.reply_text(
            "Игра уже началась — выйти самостоятельно нельзя. Напиши организаторам.",
            reply_markup=menu_for(user_id),
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "Точно отменить регистрацию? Анкета и фото будут удалены из базы.\n"
        "Зарегистрироваться снова можно будет, пока игра не началась.",
        reply_markup=K.confirm_cancel_reg(),
    )
    return CANCEL_REG_CONFIRM


async def cancel_reg_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    text = (update.message.text or "").strip()

    if text != K.BTN_CANCEL_REG_YES:
        await update.message.reply_text("Ок, ты остаёшься в игре.", reply_markup=menu_for(user_id))
        return ConversationHandler.END

    player = get_player(user_id)
    db.execute("DELETE FROM targets WHERE hunter_id = %s OR target_id = %s", (user_id, user_id))
    db.execute("DELETE FROM players WHERE user_id = %s", (user_id,))

    await update.message.reply_text(
        "🚫 Регистрация отменена, анкета удалена.\n"
        "Захочешь вернуться — нажми «📝 Зарегистрироваться».",
        reply_markup=menu_for(user_id),
    )
    if player:
        await notify_admins(
            context,
            f"❗️ Игрок отменил регистрацию: {player['full_name']} (ID {user_id}).\n"
            f"Осталось участников: {db.fetch_val('SELECT COUNT(*) FROM players', default=0)}",
        )
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# ЦЕЛЬ / ДОСЬЕ / СТАТИСТИКА
# ---------------------------------------------------------------------------
async def show_target(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    player = get_player(user_id)
    if not player:
        await update.message.reply_text("Ты не в игре.", reply_markup=menu_for(user_id))
        return
    if not game_started():
        await update.message.reply_text("Игра ещё не началась.", reply_markup=menu_for(user_id))
        return
    if not player["is_alive"]:
        await update.message.reply_text("Ты выбыл — целей больше нет.", reply_markup=menu_for(user_id))
        return

    targets = db.fetch_all(
        "SELECT p.* FROM targets t JOIN players p ON p.user_id = t.target_id "
        "WHERE t.hunter_id = %s AND t.is_active ORDER BY t.assigned_date",
        (user_id,),
    )
    if not targets:
        await update.message.reply_text(
            "У тебя пока нет активной цели. Организаторы скоро её назначат.",
            reply_markup=menu_for(user_id),
        )
        return

    if len(targets) > 1:
        await update.message.reply_text(f"⚡️ У тебя {len(targets)} активных цели.")

    for t in targets:
        await send_dossier(context, user_id, t["photo_id"], dossier_for_hunter(t))


async def show_me(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    player = get_player(user_id)
    if not player:
        await update.message.reply_text(
            "Ты не зарегистрирован. Нажми «📝 Зарегистрироваться».", reply_markup=menu_for(user_id)
        )
        return
    await send_dossier(context, user_id, player["photo_id"], dossier_self(player),
                       reply_markup=menu_for(user_id))


async def show_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    total = db.fetch_val("SELECT COUNT(*) FROM players", default=0)
    alive = alive_count()
    kills = db.fetch_val("SELECT COUNT(*) FROM kills", default=0)

    lines = ["📊 Статистика игры", ""]
    if game_started():
        start = game_start_date()
        lines.append("Статус: игра идёт")
        if start:
            end = start + timedelta(days=game_duration_days())
            left = end - datetime.now(timezone.utc)
            hours = max(0, int(left.total_seconds() // 3600))
            lines.append(f"До финала: {hours // 24} дн. {hours % 24} ч.")
    else:
        lines.append("Статус: игра не начата" + (" (регистрация открыта)" if registration_open() else ""))

    lines += [
        f"Участников: {total}",
        f"Живых: {alive}",
        f"Выбыло: {total - alive}",
        f"Всего убийств: {kills}",
    ]
    today = db.fetch_val("SELECT COUNT(*) FROM kills WHERE kill_date > NOW() - INTERVAL '24 hours'", default=0)
    lines.append(f"Убийств за сутки: {today}")
    await update.message.reply_text("\n".join(lines), reply_markup=menu_for(user_id))


TOP_KILLERS_SQL = """
    SELECT p.user_id, p.full_name, p.kills, p.points, p.last_kill_date,
           COALESCE((
               SELECT SUM(v.kills) FROM players v WHERE v.killed_by = p.user_id
           ), 0) AS victims_kills_sum
    FROM players p
    ORDER BY p.kills DESC, victims_kills_sum DESC,
             p.last_kill_date ASC NULLS LAST, p.full_name
    LIMIT %s
"""


async def show_top(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    rows = db.fetch_all(TOP_KILLERS_SQL, (15,))
    if not rows:
        await update.message.reply_text("Пока никого нет.", reply_markup=menu_for(user_id))
        return
    medals = ["🥇", "🥈", "🥉"]
    lines = ["🏆 Топ киллеров", ""]
    for i, r in enumerate(rows, 1):
        mark = medals[i - 1] if i <= 3 else f"{i}."
        lines.append(f"{mark} {r['full_name']} — {r['kills']} уб. ({r['points']} очк.)")
    await update.message.reply_text("\n".join(lines), reply_markup=menu_for(user_id))


async def show_graveyard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кладбище видно ТОЛЬКО организаторам — игрокам не положено знать,
    кто выбыл из игры."""
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("Эта информация недоступна во время игры.", reply_markup=menu_for(user_id))
        return
    rows = db.fetch_all(
        "SELECT p.full_name, p.death_date, p.last_words, k.full_name AS killer "
        "FROM players p LEFT JOIN players k ON k.user_id = p.killed_by "
        "WHERE NOT p.is_alive ORDER BY p.death_date DESC NULLS LAST LIMIT 50"
    )
    if not rows:
        await update.message.reply_text("🪦 Кладбище пусто. Пока все живы.", reply_markup=menu_for(user_id))
        return
    lines = ["🪦 Кладбище (видно только админам)", ""]
    for r in rows:
        when = fmt_dt(r["death_date"])
        line = f"• {r['full_name']} — {when}"
        if r["killer"]:
            line += f", охотник: {r['killer']}"
        lines.append(line)
        if r["last_words"]:
            lines.append(f"   🕯 «{r['last_words']}»")
    await update.message.reply_text("\n".join(lines), reply_markup=menu_for(user_id))


# ---------------------------------------------------------------------------
# УБИЙСТВО
# ---------------------------------------------------------------------------
async def kill_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    player = get_player(user_id)
    if not player or not game_started():
        await update.message.reply_text("Игра не идёт.", reply_markup=menu_for(user_id))
        return ConversationHandler.END
    if not player["is_alive"]:
        await update.message.reply_text("Ты выбыл из игры.", reply_markup=menu_for(user_id))
        return ConversationHandler.END

    targets = db.fetch_all(
        "SELECT p.user_id, p.personal_code, p.full_name, p.reward "
        "FROM targets t JOIN players p ON p.user_id = t.target_id "
        "WHERE t.hunter_id = %s AND t.is_active AND p.is_alive",
        (user_id,),
    )
    if not targets:
        await update.message.reply_text("У тебя нет активной цели.", reply_markup=menu_for(user_id))
        return ConversationHandler.END

    context.user_data["kill_targets"] = [
        (t["user_id"], (t["personal_code"] or "").upper(), t["full_name"], t["reward"]) for t in targets
    ]
    context.user_data["kill_attempts"] = 0
    await update.message.reply_text(
        "Введи личный код жертвы, который она назвала после «убийства»:",
        reply_markup=K.cancel_only(),
    )
    return KILL_CODE


async def kill_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    text = (update.message.text or "").strip()

    if text == K.BTN_CANCEL or pressed_menu_button(text):
        await update.message.reply_text("Отменено.", reply_markup=menu_for(user.id))
        return ConversationHandler.END

    entered = text.upper().replace(" ", "")
    targets = context.user_data.get("kill_targets", [])
    victim = next((t for t in targets if t[1] == entered), None)

    if not victim:
        context.user_data["kill_attempts"] = context.user_data.get("kill_attempts", 0) + 1
        if context.user_data["kill_attempts"] >= 5:
            await update.message.reply_text(
                "Слишком много неверных попыток. Попробуй позже.", reply_markup=menu_for(user.id)
            )
            await notify_admins(
                context, f"⚠️ {user.id} ввёл 5 неверных кодов убийства подряд."
            )
            return ConversationHandler.END
        await update.message.reply_text(
            "❌ Неверный код. Проверь и введи ещё раз (или «❌ Отмена»).",
            reply_markup=K.cancel_only(),
        )
        return KILL_CODE

    victim_id, _code, victim_name, victim_reward = victim
    await register_kill(context, hunter_id=user.id, victim_id=victim_id, code=entered)

    hunter = get_player(user.id)
    await update.message.reply_text(
        f"🎯 Цель устранена: {victim_name}.\n"
        f"Получено очков: {victim_reward}. Всего у тебя: {hunter['points']} очк. "
        f"({hunter['kills']} уб.)\n\n"
        "Открой «🎯 Моя цель» — там уже новое досье.",
        reply_markup=menu_for(user.id),
    )
    context.user_data.pop("kill_targets", None)
    await check_game_over(context)
    return ConversationHandler.END


async def register_kill(context: ContextTypes.DEFAULT_TYPE, hunter_id: int, victim_id: int, code: str) -> None:
    """Фиксирует убийство, перевыдаёт цели, рассылает уведомления."""
    victim = get_player(victim_id)
    if not victim or not victim["is_alive"]:
        return
    reward = victim["reward"] or 1

    db.execute(
        "INSERT INTO kills (hunter_id, victim_id, kill_code, points) VALUES (%s,%s,%s,%s)",
        (hunter_id, victim_id, code, reward),
    )
    db.execute(
        "UPDATE players SET kills = kills + 1, points = points + %s, last_kill_date = NOW() "
        "WHERE user_id = %s",
        (reward, hunter_id),
    )
    db.execute(
        "UPDATE players SET is_alive = FALSE, death_date = NOW(), killed_by = %s WHERE user_id = %s",
        (hunter_id, victim_id),
    )

    # Цели жертвы освобождаются
    victim_targets = [
        r["target_id"]
        for r in db.fetch_all(
            "SELECT target_id FROM targets WHERE hunter_id = %s AND is_active", (victim_id,)
        )
    ]
    db.execute("UPDATE targets SET is_active = FALSE WHERE hunter_id = %s", (victim_id,))

    # Охотники, у которых жертва была целью (включая нашего)
    other_hunters = [
        r["hunter_id"]
        for r in db.fetch_all(
            "SELECT hunter_id FROM targets WHERE target_id = %s AND is_active", (victim_id,)
        )
    ]
    db.execute("UPDATE targets SET is_active = FALSE WHERE target_id = %s", (victim_id,))

    hunter = get_player(hunter_id)

    # Наш охотник наследует цель жертвы
    new_target_id = assign_target(hunter_id, preferred=victim_targets[0] if victim_targets else None)
    if new_target_id is None:
        await notify_admins(context, f"⚠️ Игроку {hunter_id} не удалось назначить новую цель автоматически.")

    # Остальным охотникам жертвы — новые цели
    for other in other_hunters:
        if other == hunter_id:
            continue
        got = assign_target(other)
        if got:
            await safe_send(context, other, "🔄 Твоя цель выбыла из игры. Назначена новая — открой «🎯 Моя цель».")
        else:
            await notify_admins(context, f"⚠️ Охотнику {other} не назначена новая цель.")

    # Жертве
    await safe_send(
        context,
        victim_id,
        "💀 Тебя устранили. Ты выбыл из игры.\n"
        f"Итог: {victim['kills']} убийств, {victim['points']} очков.\n\n"
        "Можешь оставить последнее слово — оно появится на кладбище.",
    )
    try:
        await context.bot.send_message(
            chat_id=victim_id, text="Меню обновлено.", reply_markup=menu_for(victim_id)
        )
    except TelegramError:
        pass

    # Килл-фид игрокам больше не рассылается — они не должны знать, кто выбыл.
    # Админы всегда получают сводку об убийстве ниже, независимо от killfeed_enabled().
    await notify_admins(
        context,
        "🔪 Убийство\n"
        f"Охотник: {hunter['full_name']} ({hunter_id})\n"
        f"Жертва: {victim['full_name']} ({victim_id})\n"
        f"Очков за жертву: {reward}\n"
        f"Живых осталось: {alive_count()}",
    )


def assign_target(hunter_id: int, preferred: int | None = None) -> int | None:
    """Назначает охотнику новую активную цель. Возвращает target_id или None."""
    def ok(candidate: int) -> bool:
        if not candidate or candidate == hunter_id:
            return False
        p = get_player(candidate)
        if not p or not p["is_alive"]:
            return False
        exists = db.fetch_one(
            "SELECT 1 FROM targets WHERE hunter_id = %s AND target_id = %s AND is_active",
            (hunter_id, candidate),
        )
        return not exists

    candidate = preferred if preferred and ok(preferred) else None

    if candidate is None:
        # свободные игроки: живые, на которых пока никто не охотится
        free = db.fetch_all(
            "SELECT p.user_id FROM players p "
            "WHERE p.is_alive AND p.user_id <> %s "
            "AND NOT EXISTS (SELECT 1 FROM targets t WHERE t.target_id = p.user_id AND t.is_active)",
            (hunter_id,),
        )
        pool = [r["user_id"] for r in free if ok(r["user_id"])]
        if not pool:
            others = db.fetch_all(
                "SELECT user_id FROM players WHERE is_alive AND user_id <> %s", (hunter_id,)
            )
            pool = [r["user_id"] for r in others if ok(r["user_id"])]
        if pool:
            candidate = random.choice(pool)

    if candidate is None:
        return None

    db.execute(
        "INSERT INTO targets (hunter_id, target_id, kill_code, is_active) VALUES (%s,%s,%s,TRUE) "
        "ON CONFLICT DO NOTHING",
        (hunter_id, candidate, gen_code()),
    )
    return candidate


async def check_game_over(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not game_started():
        return
    if alive_count() <= 2:
        await finish_game(context, reason="В игре осталось два участника")


async def finish_game(context: ContextTypes.DEFAULT_TYPE, reason: str = "") -> None:
    rows = db.fetch_all(TOP_KILLERS_SQL, (10000,))
    db.set_setting("game_started", "False")
    db.execute("UPDATE targets SET is_active = FALSE")

    lines = ["🏁 Игра окончена."]
    if reason:
        lines.append(reason)
    lines += ["", "Итоговая таблица (по числу убийств):"]
    medals = ["🥇", "🥈", "🥉"]
    for i, r in enumerate(rows, 1):
        mark = medals[i - 1] if i <= 3 else f"{i}."
        lines.append(f"{mark} {r['full_name']} — {r['kills']} уб. ({r['points']} очк.)")
    result = "\n".join(lines)

    for i, r in enumerate(rows, 1):
        personal = f"🏁 Игра окончена.\nТвоё место: {i} из {len(rows)}\nУбийств: {r['kills']} ({r['points']} очк.)\n\n"
        await safe_send(context, r["user_id"], personal + result, reply_markup=ReplyKeyboardRemove())
    await notify_admins(context, result)


# ---------------------------------------------------------------------------
# АНОНИМНЫЕ СООБЩЕНИЯ
# ---------------------------------------------------------------------------
async def msg_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Бесплатные письма своему охотнику/жертве. Могут содержать фото."""
    user_id = update.effective_user.id
    text = (update.message.text or "")
    to_killer = K.BTN_MSG_KILLER in text or "msg_killer" in text

    if not game_started():
        await update.message.reply_text("Игра ещё не идёт.", reply_markup=menu_for(user_id))
        return ConversationHandler.END

    if to_killer:
        row = db.fetch_one("SELECT hunter_id FROM targets WHERE target_id = %s AND is_active", (user_id,))
        if not row:
            await update.message.reply_text("На тебя сейчас никто не охотится.", reply_markup=menu_for(user_id))
            return ConversationHandler.END
        context.user_data["msg_to"] = [row["hunter_id"]]
        context.user_data["msg_dir"] = "to_killer"
        prompt = "Напиши анонимную записку своему охотнику (можно с фото):"
    else:
        rows = db.fetch_all("SELECT target_id FROM targets WHERE hunter_id = %s AND is_active", (user_id,))
        if not rows:
            await update.message.reply_text("У тебя нет активной цели.", reply_markup=menu_for(user_id))
            return ConversationHandler.END
        context.user_data["msg_to"] = [r["target_id"] for r in rows]
        context.user_data["msg_dir"] = "to_target"
        prompt = "Напиши анонимную записку своей жертве (можно с фото):"

    await update.message.reply_text(prompt, reply_markup=K.cancel_only())
    return MSG_TEXT


async def msg_send(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    text = (update.message.text or update.message.caption or "").strip()
    if text == K.BTN_CANCEL or (not update.message.photo and pressed_menu_button(text)):
        await update.message.reply_text("Отменено.", reply_markup=menu_for(user_id))
        return ConversationHandler.END
    if len(text) > 800:
        text = text[:800]

    photo_id = update.message.photo[-1].file_id if update.message.photo else None
    if not text and not photo_id:
        await update.message.reply_text("Нужен текст или фото. Попробуй ещё раз.")
        return MSG_TEXT

    direction = context.user_data.get("msg_dir")
    header = "📩 Анонимная записка от твоей жертвы:" if direction == "to_killer" \
        else "📩 Анонимная записка от твоего охотника:"
    sender = get_player(user_id)
    sender_name = field(sender, "full_name", str(user_id)) if sender else str(user_id)

    delivered = 0
    for chat_id in context.user_data.get("msg_to", []):
        body = f"{header}\n\n{text}" if text else header
        if photo_id:
            ok = bool(await safe_send_photo(context, chat_id, photo_id, body))
        else:
            ok = await safe_send(context, chat_id, body)
        if ok:
            delivered += 1
        db.execute(
            "INSERT INTO anon_messages (from_id, to_id, direction, body, photo_id, is_paid) "
            "VALUES (%s,%s,%s,%s,%s,FALSE)",
            (user_id, chat_id, direction, text, photo_id),
        )
        recipient = get_player(chat_id)
        recipient_name = field(recipient, "full_name", str(chat_id)) if recipient else str(chat_id)
        admin_copy = (
            "✉️ Копия письма (бесплатно, не анонимно)\n"
            f"От: {sender_name} ({user_id})\n"
            f"Кому: {recipient_name} ({chat_id})\n"
            f"Направление: {'жертва → киллер' if direction == 'to_killer' else 'киллер → жертва'}\n\n"
            f"{text}"
        )
        await notify_admins_message(context, admin_copy, photo_id=photo_id)

    await update.message.reply_text(
        "✅ Записка доставлена." if delivered else "❌ Не удалось доставить записку.",
        reply_markup=menu_for(user_id),
    )
    context.user_data.pop("msg_to", None)
    context.user_data.pop("msg_dir", None)
    return ConversationHandler.END


async def safe_send_photo(context: ContextTypes.DEFAULT_TYPE, chat_id: int, photo_id: str, caption: str) -> bool:
    try:
        if len(caption) <= 1000:
            await context.bot.send_photo(chat_id=chat_id, photo=photo_id, caption=caption)
        else:
            await context.bot.send_photo(chat_id=chat_id, photo=photo_id, caption=caption[:1000] + "…")
            await context.bot.send_message(chat_id=chat_id, text=caption)
        return True
    except TelegramError as e:
        logger.warning("Фото не доставлено %s: %s", chat_id, e)
        return False


# ---------------------------------------------------------------------------
# ПЛАТНОЕ ПИСЬМО ЛЮБОМУ ЖИВОМУ ИГРОКУ (1 очко = 1 письмо)
# ---------------------------------------------------------------------------
MSG_ANY_PRICE = 1


async def msg_any_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    player = get_player(user_id)
    if not player or not game_started():
        await update.message.reply_text("Игра не идёт.", reply_markup=menu_for(user_id))
        return ConversationHandler.END
    if not player["is_alive"]:
        await update.message.reply_text("Ты выбыл из игры.", reply_markup=menu_for(user_id))
        return ConversationHandler.END

    points = field(player, "points", 0) or 0
    if points < MSG_ANY_PRICE:
        await update.message.reply_text(
            f"❌ Не хватает средств. Твой баланс: {points} очк. Цена письма: {MSG_ANY_PRICE} очк.",
            reply_markup=menu_for(user_id),
        )
        return ConversationHandler.END

    players = db.fetch_all(
        "SELECT user_id, full_name FROM players WHERE is_alive AND user_id <> %s ORDER BY full_name",
        (user_id,),
    )
    if not players:
        await update.message.reply_text("Нет других живых игроков.", reply_markup=menu_for(user_id))
        return ConversationHandler.END

    context.user_data["msg_any_list"] = [(r["user_id"], r["full_name"]) for r in players]
    lines = [f"💰 Письмо любому игроку стоит {MSG_ANY_PRICE} очк. Твой баланс: {points} очк.",
             "", "Выбери получателя — напиши номер или имя из списка:", ""]
    for i, (_, name) in enumerate(context.user_data["msg_any_list"], 1):
        lines.append(f"{i}. {name}")
    await update.message.reply_text("\n".join(lines), reply_markup=K.cancel_only())
    return MSG_ANY_RECIPIENT


async def msg_any_pick_recipient(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    text = (update.message.text or "").strip()
    if text == K.BTN_CANCEL:
        await update.message.reply_text("Отменено.", reply_markup=menu_for(user_id))
        return ConversationHandler.END

    options = context.user_data.get("msg_any_list", [])
    chosen = None
    if text.isdigit():
        idx = int(text) - 1
        if 0 <= idx < len(options):
            chosen = options[idx]
    if chosen is None:
        matches = [o for o in options if o[1].lower() == text.lower()]
        if not matches:
            matches = [o for o in options if text.lower() in o[1].lower()]
        if len(matches) == 1:
            chosen = matches[0]

    if chosen is None:
        await update.message.reply_text("Не нашёл такого игрока однозначно. Попробуй ввести номер из списка.")
        return MSG_ANY_RECIPIENT

    context.user_data["msg_any_to"] = chosen[0]
    context.user_data["msg_any_to_name"] = chosen[1]
    await update.message.reply_text(
        f"Получатель: {chosen[1]}.\nОт чьего имени отправить письмо?",
        reply_markup=K.sign_choice(),
    )
    return MSG_ANY_SIGN


async def msg_any_pick_sign(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    text = (update.message.text or "").strip()
    if text == K.BTN_CANCEL:
        await update.message.reply_text("Отменено.", reply_markup=menu_for(user_id))
        return ConversationHandler.END
    if text not in (K.BTN_SIGN_KILLER, K.BTN_SIGN_VICTIM):
        await update.message.reply_text("Выбери одну из двух кнопок.", reply_markup=K.sign_choice())
        return MSG_ANY_SIGN

    context.user_data["msg_any_sign"] = "killer" if text == K.BTN_SIGN_KILLER else "victim"
    await update.message.reply_text(
        "Теперь напиши текст письма (только текст, без фото):",
        reply_markup=K.cancel_only(),
    )
    return MSG_ANY_TEXT


async def msg_any_send(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    text = (update.message.text or "").strip()
    if text == K.BTN_CANCEL or pressed_menu_button(text):
        await update.message.reply_text("Отменено.", reply_markup=menu_for(user_id))
        return ConversationHandler.END
    if not text:
        await update.message.reply_text("Нужен текст. Попробуй ещё раз.")
        return MSG_ANY_TEXT
    if len(text) > 800:
        text = text[:800]

    to_id = context.user_data.get("msg_any_to")
    sign = context.user_data.get("msg_any_sign")
    if not to_id:
        await update.message.reply_text("Что-то пошло не так. Начни занова через «💰 Письмо игроку».", reply_markup=menu_for(user_id))
        return ConversationHandler.END

    # Повторная проверка баланса непосредственно перед списанием — вдруг уже потратил в другом окне
    fresh = get_player(user_id)
    points = field(fresh, "points", 0) or 0
    if points < MSG_ANY_PRICE:
        await update.message.reply_text(
            f"❌ Не хватает средств. Твой баланс: {points} очк.",
            reply_markup=menu_for(user_id),
        )
        return ConversationHandler.END

    rows_updated = db.execute(
        "UPDATE players SET points = points - %s WHERE user_id = %s AND points >= %s",
        (MSG_ANY_PRICE, user_id, MSG_ANY_PRICE),
    )
    if not rows_updated:
        await update.message.reply_text("❌ Не хватает средств.", reply_markup=menu_for(user_id))
        return ConversationHandler.END

    sign_label = "🔫 от киллера" if sign == "killer" else "💀 от жертвы"
    body = f"💰 Платное анонимное письмо ({sign_label}):\n\n{text}"
    delivered = await safe_send(context, to_id, body)

    db.execute(
        "INSERT INTO anon_messages (from_id, to_id, direction, body, is_paid, sign) "
        "VALUES (%s,%s,'to_any',%s,TRUE,%s)",
        (user_id, to_id, text, sign),
    )

    sender = get_player(user_id)
    sender_name = field(sender, "full_name", str(user_id)) if sender else str(user_id)
    to_name = context.user_data.get("msg_any_to_name", str(to_id))
    admin_copy = (
        "💰 Копия платного письма (не анонимно)\n"
        f"От: {sender_name} ({user_id})\n"
        f"Кому: {to_name} ({to_id})\n"
        f"Подпись получателю: {sign_label}\n"
        f"Списано: {MSG_ANY_PRICE} очк.\n\n"
        f"{text}"
    )
    await notify_admins_message(context, admin_copy)

    await update.message.reply_text(
        "✅ Письмо отправлено." if delivered else "⚠️ Очко списано, но доставить не удалось.",
        reply_markup=menu_for(user_id),
    )
    context.user_data.pop("msg_any_list", None)
    context.user_data.pop("msg_any_to", None)
    context.user_data.pop("msg_any_to_name", None)
    context.user_data.pop("msg_any_sign", None)
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# ПОСЛЕДНЕЕ СЛОВО
# ---------------------------------------------------------------------------
async def last_words_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    player = get_player(user_id)
    if not player:
        await update.message.reply_text("Ты не в игре.", reply_markup=menu_for(user_id))
        return ConversationHandler.END
    if player["is_alive"]:
        await update.message.reply_text("Ты ещё жив. Последнее слово рановато.", reply_markup=menu_for(user_id))
        return ConversationHandler.END
    await update.message.reply_text(
        "Напиши своё последнее слово (появится на кладбище, до 200 символов):",
        reply_markup=K.cancel_only(),
    )
    return LAST_WORDS


async def last_words_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    text = (update.message.text or "").strip()
    if text == K.BTN_CANCEL or pressed_menu_button(text):
        await update.message.reply_text("Отменено.", reply_markup=menu_for(user_id))
        return ConversationHandler.END
    db.execute("UPDATE players SET last_words = %s WHERE user_id = %s", (text[:200], user_id))
    await update.message.reply_text("🕯 Записано. Организаторы увидят это в кладбище.", reply_markup=menu_for(user_id))
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# АДМИНКА
# ---------------------------------------------------------------------------
def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *a, **kw):
        if not is_admin(update.effective_user.id):
            await update.message.reply_text("Команда только для организаторов.")
            return
        return await func(update, context, *a, **kw)
    return wrapper


ADMIN_HELP = (
    "🛠 Панель организатора\n\n"
    "Игра:\n"
    "/start_game — раздать цели и начать\n"
    "/end_game — завершить и разослать итоги\n"
    "/reset_game — полный сброс данных\n"
    "/set_time <дни> — длительность игры\n"
    "/open_reg, /close_reg — регистрация\n"
    "/killfeed on|off — уведомление админов о каждом убийстве (игрокам не рассылается)\n"
    "/status — техстатус\n\n"
    "Игроки:\n"
    "/list_players — список\n"
    "/view_profile <id|часть имени> — досье с фото + полная карточка\n"
    "/set_photo <id> — заменить фотографию в базе\n"
    "/edit_player <id> <поле> <значение>\n"
    "/add_hint <id> <текст> — подсказка охотнику\n"
    "/set_reward_player <id> <очки>\n"
    "/set_global_reward <очки>\n"
    "/set_danger <id> <причина>, /remove_danger <id>\n"
    "/add_player <id> <имя>, /remove_player <id>\n"
    "/revive <id> — вернуть в игру\n\n"
    "Цепочка охоты:\n"
    "/show_targets — кто на кого охотится\n"
    "/reassign <hunter_id> <target_id>\n"
    "/check_pairs — найти взаимные охоты\n"
    "/armageddon — каждому вторую цель\n"
    "/broadcast <текст> — рассылка всем\n"
    "/export — CSV с игроками"
)


@admin_only
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(ADMIN_HELP, reply_markup=menu_for(update.effective_user.id))


@admin_only
async def start_game(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if game_started():
        await update.message.reply_text("Игра уже идёт.")
        return
    players = [r["user_id"] for r in db.fetch_all("SELECT user_id FROM players WHERE is_alive")]
    if len(players) < 3:
        await update.message.reply_text("Нужно минимум 3 участника.")
        return
    no_photo = db.fetch_all("SELECT user_id, full_name FROM players WHERE photo_id IS NULL")
    if no_photo:
        names = ", ".join(f"{p['full_name']} ({p['user_id']})" for p in no_photo)
        await update.message.reply_text(f"⚠️ Без фото: {names}\nДобавь через /set_photo <id> или продолжай.")

    random.shuffle(players)
    db.execute("UPDATE targets SET is_active = FALSE")
    with db.get_cursor(dict_cursor=False) as cur:
        for i, hunter in enumerate(players):
            target = players[(i + 1) % len(players)]
            cur.execute(
                "INSERT INTO targets (hunter_id, target_id, kill_code, is_active) "
                "VALUES (%s,%s,%s,TRUE) ON CONFLICT DO NOTHING",
                (hunter, target, gen_code()),
            )

    db.set_setting("game_started", "True")
    db.set_setting("game_start_date", datetime.now(timezone.utc).isoformat())
    db.set_setting("registration_open", "False")

    for pid in players:
        await safe_send(
            context,
            pid,
            "🎮 Игра началась.\n"
            f"Продолжительность: {game_duration_days()} дн.\n"
            "Твоя цель уже назначена — нажми «🎯 Моя цель».\n"
            "Удачной охоты. И помни: за тобой тоже идут.",
            reply_markup=K.game_menu(is_admin(pid)),
        )
    await update.message.reply_text(f"Игра начата. Участников: {len(players)}")


@admin_only
async def end_game(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await finish_game(context, reason="Игра остановлена организатором")
    await update.message.reply_text("Игра завершена, итоги разосланы.")


@admin_only
async def reset_game(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args or args[0] != "CONFIRM":
        await update.message.reply_text(
            "Это удалит ВСЕ данные игроков.\nПодтверди: /reset_game CONFIRM"
        )
        return
    db.execute("DELETE FROM kills")
    db.execute("DELETE FROM targets")
    db.execute("DELETE FROM anon_messages")
    db.execute("DELETE FROM players")
    for key, value in db.DEFAULT_SETTINGS.items():
        db.set_setting(key, value)
    await update.message.reply_text("Игра сброшена, база очищена.")


@admin_only
async def list_players(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = db.fetch_all(
        "SELECT user_id, full_name, academic_group, is_alive, kills, points, photo_id, is_danger "
        "FROM players ORDER BY is_alive DESC, points DESC"
    )
    if not rows:
        await update.message.reply_text("Игроков нет.")
        return
    alive = [r for r in rows if r["is_alive"]]
    dead = [r for r in rows if not r["is_alive"]]

    def fmt(r):
        marks = ""
        if not r["photo_id"]:
            marks += " 📵"
        if r["is_danger"]:
            marks += " ⚠️"
        return f"• {r['full_name']} ({r['academic_group'] or '—'}) — {r['points']} очк./{r['kills']} уб. — id {r['user_id']}{marks}"

    text = f"👥 Игроки: {len(rows)}\n\n🟢 Живые ({len(alive)}):\n" + "\n".join(fmt(r) for r in alive)
    if dead:
        text += f"\n\n🔴 Выбыли ({len(dead)}):\n" + "\n".join(fmt(r) for r in dead)
    for chunk in [text[i:i + 3800] for i in range(0, len(text), 3800)]:
        await update.message.reply_text(chunk)


def find_player(query: str):
    query = query.strip()
    if query.isdigit():
        p = db.fetch_one("SELECT * FROM players WHERE user_id = %s", (int(query),))
        if p:
            return p
    if query.startswith("@"):
        p = db.fetch_one("SELECT * FROM players WHERE lower(username) = %s", (query[1:].lower(),))
        if p:
            return p
    return db.fetch_one(
        "SELECT * FROM players WHERE full_name ILIKE %s ORDER BY full_name LIMIT 1",
        (f"%{query}%",),
    )


@admin_only
async def view_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Пункт 3: полная информация + фото + предпросмотр досье охотника."""
    if not context.args:
        await update.message.reply_text("Использование: /view_profile <user_id | @username | часть имени>")
        return
    player = find_player(" ".join(context.args))
    if not player:
        await update.message.reply_text("Игрок не найден.")
        return

    chat_id = update.effective_chat.id
    await context.bot.send_chat_action(chat_id, ChatAction.TYPING)

    # 1) Фото + досье в том виде, в котором его увидит охотник
    preview = "👁 ТАК ЭТО ВИДИТ ОХОТНИК\n\n" + dossier_for_hunter(player)
    if not player["photo_id"]:
        preview += f"\n\n⚠️ Фото в базе НЕТ. Загрузить: /set_photo {player['user_id']}"
    await send_dossier(context, chat_id, player["photo_id"], preview)

    # 2) Полная техническая карточка
    await context.bot.send_message(chat_id, dossier_admin(player))


@admin_only
async def set_photo_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Пункт 3: замена фотографии игрока в базе."""
    if not context.args:
        await update.message.reply_text("Использование: /set_photo <user_id | часть имени>")
        return ConversationHandler.END
    player = find_player(" ".join(context.args))
    if not player:
        await update.message.reply_text("Игрок не найден.")
        return ConversationHandler.END

    context.user_data["photo_target"] = player["user_id"]
    hint = "Текущее фото ниже. " if player["photo_id"] else "Фото сейчас нет. "
    if player["photo_id"]:
        try:
            await context.bot.send_photo(
                chat_id=update.effective_chat.id, photo=player["photo_id"],
                caption=f"Текущее фото: {player['full_name']}",
            )
        except TelegramError:
            hint = "Текущее фото не открывается. "
    await update.message.reply_text(
        f"{hint}Пришли новое фото для {player['full_name']} (id {player['user_id']}).\n"
        "Можно также прислать текстом file_id. Отмена — «❌ Отмена».",
        reply_markup=K.cancel_only(),
    )
    return ADMIN_PHOTO


@admin_only
async def set_photo_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    target_id = context.user_data.get("photo_target")
    if not target_id:
        return ConversationHandler.END

    if update.message.photo:
        file_id = update.message.photo[-1].file_id
    elif update.message.document and (update.message.document.mime_type or "").startswith("image/"):
        file_id = update.message.document.file_id
    else:
        text = (update.message.text or "").strip()
        if text == K.BTN_CANCEL or pressed_menu_button(text):
            await update.message.reply_text("Отменено.", reply_markup=menu_for(update.effective_user.id))
            return ConversationHandler.END
        if len(text) < 20:
            await update.message.reply_text("Пришли фотографию или корректный file_id.")
            return ADMIN_PHOTO
        file_id = text

    db.execute("UPDATE players SET photo_id = %s WHERE user_id = %s", (file_id, target_id))
    player = get_player(target_id)
    try:
        await context.bot.send_photo(
            chat_id=update.effective_chat.id,
            photo=file_id,
            caption=f"✅ Фото обновлено в базе: {player['full_name']} (id {target_id}).\n"
                    "Так его увидит охотник.",
        )
    except TelegramError as e:
        await update.message.reply_text(f"Фото сохранено, но предпросмотр не удался: {e}")

    await safe_send(context, target_id, "🖼 Организаторы обновили фотографию в твоём досье.")
    context.user_data.pop("photo_target", None)
    await update.message.reply_text("Готово.", reply_markup=menu_for(update.effective_user.id))
    return ConversationHandler.END


@admin_only
async def edit_player(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    allowed = [
        "full_name", "course", "academic_group", "social_links", "about_self",
        "buildings", "dormitory", "habits", "hint", "photo_id", "personal_code",
        "reward", "danger_reason", "username",
    ]
    if len(context.args) < 3:
        await update.message.reply_text(
            "Использование: /edit_player <user_id> <поле> <значение>\n"
            "Поля: " + ", ".join(allowed)
        )
        return
    try:
        user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("user_id должен быть числом.")
        return
    field = context.args[1].lower()
    if field not in allowed:
        await update.message.reply_text("Недопустимое поле. Доступно: " + ", ".join(allowed))
        return
    value = " ".join(context.args[2:])
    if field == "reward":
        try:
            value = int(value)
        except ValueError:
            await update.message.reply_text("reward должен быть числом.")
            return
    affected = db.execute(f"UPDATE players SET {field} = %s WHERE user_id = %s", (value, user_id))
    await update.message.reply_text(
        f"✅ Поле {field} обновлено." if affected else "❌ Игрок не найден."
    )


@admin_only
async def add_hint(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) < 2:
        await update.message.reply_text("Использование: /add_hint <user_id> <текст подсказки>")
        return
    try:
        user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("user_id должен быть числом.")
        return
    hint = " ".join(context.args[1:])
    affected = db.execute("UPDATE players SET hint = %s WHERE user_id = %s", (hint, user_id))
    if affected:
        hunter = db.fetch_one("SELECT hunter_id FROM targets WHERE target_id = %s AND is_active", (user_id,))
        if hunter:
            await safe_send(context, hunter["hunter_id"], f"🕵️ Новая подсказка по твоей цели:\n{hint}")
        await update.message.reply_text("✅ Подсказка добавлена.")
    else:
        await update.message.reply_text("❌ Игрок не найден.")


@admin_only
async def set_global_reward(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text(f"Текущая базовая награда: {db.get_setting('reward', '1')}\n"
                                        "Использование: /set_global_reward <число>")
        return
    try:
        value = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Нужно число.")
        return
    db.set_setting("reward", str(value))
    db.execute("UPDATE players SET reward = %s WHERE is_alive", (value,))
    await update.message.reply_text(f"✅ Базовая награда: {value} (применена ко всем живым).")


@admin_only
async def set_reward_player(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) < 2:
        await update.message.reply_text("Использование: /set_reward_player <user_id> <очки>")
        return
    try:
        user_id, reward = int(context.args[0]), int(context.args[1])
    except ValueError:
        await update.message.reply_text("user_id и очки — числа.")
        return
    affected = db.execute("UPDATE players SET reward = %s WHERE user_id = %s", (reward, user_id))
    if affected:
        hunter = db.fetch_one("SELECT hunter_id FROM targets WHERE target_id = %s AND is_active", (user_id,))
        if hunter:
            await safe_send(context, hunter["hunter_id"], f"💰 Награда за твою цель изменена: {reward} очк.")
        await update.message.reply_text(f"✅ Награда игрока {user_id}: {reward}.")
    else:
        await update.message.reply_text("❌ Игрок не найден.")


@admin_only
async def set_danger(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) < 2:
        await update.message.reply_text("Использование: /set_danger <user_id> <причина>")
        return
    try:
        user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("user_id должен быть числом.")
        return
    reason = " ".join(context.args[1:])
    affected = db.execute(
        "UPDATE players SET is_danger = TRUE, danger_reason = %s WHERE user_id = %s", (reason, user_id)
    )
    if affected:
        await safe_send(context, user_id, f"⚠️ Тебе присвоен статус ОСОБО ОПАСЕН.\nПричина: {reason}")
        await update.message.reply_text("✅ Статус установлен.")
    else:
        await update.message.reply_text("❌ Игрок не найден.")


@admin_only
async def remove_danger(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Использование: /remove_danger <user_id>")
        return
    try:
        user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("user_id должен быть числом.")
        return
    affected = db.execute(
        "UPDATE players SET is_danger = FALSE, danger_reason = '' WHERE user_id = %s", (user_id,)
    )
    await update.message.reply_text("✅ Статус снят." if affected else "❌ Игрок не найден.")


@admin_only
async def add_player(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) < 2:
        await update.message.reply_text("Использование: /add_player <user_id> <ФИО> [группа]")
        return
    try:
        user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("user_id должен быть числом.")
        return
    full_name = context.args[1]
    group = context.args[2] if len(context.args) > 2 else "—"
    code = gen_unique_personal_code()
    db.execute(
        "INSERT INTO players (user_id, full_name, academic_group, personal_code, is_alive) "
        "VALUES (%s,%s,%s,%s,TRUE) ON CONFLICT (user_id) DO NOTHING",
        (user_id, full_name, group, code),
    )
    await update.message.reply_text(f"✅ {full_name} добавлен. Личный код: {code}\nФото: /set_photo {user_id}")


@admin_only
async def remove_player(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Использование: /remove_player <user_id>")
        return
    try:
        user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("user_id должен быть числом.")
        return
    player = get_player(user_id)
    if not player:
        await update.message.reply_text("Игрок не найден.")
        return

    hunters = [r["hunter_id"] for r in db.fetch_all(
        "SELECT hunter_id FROM targets WHERE target_id = %s AND is_active", (user_id,))]
    victim_targets = [r["target_id"] for r in db.fetch_all(
        "SELECT target_id FROM targets WHERE hunter_id = %s AND is_active", (user_id,))]

    db.execute("DELETE FROM targets WHERE hunter_id = %s OR target_id = %s", (user_id, user_id))
    db.execute("DELETE FROM players WHERE user_id = %s", (user_id,))

    report = [f"Игрок {player['full_name']} ({user_id}) удалён."]
    for h in hunters:
        preferred = victim_targets[0] if victim_targets else None
        new_t = assign_target(h, preferred)
        if new_t:
            await safe_send(context, h, "🔄 Твоя цель покинула игру. Назначена новая — открой «🎯 Моя цель».")
            report.append(f"Охотнику {h} назначена цель {new_t}.")
        else:
            report.append(f"⚠️ Охотнику {h} цель не назначена.")
    await safe_send(context, user_id, "Организаторы исключили тебя из игры.", reply_markup=ReplyKeyboardRemove())
    await update.message.reply_text("\n".join(report))


@admin_only
async def revive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Использование: /revive <user_id>")
        return
    try:
        user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("user_id должен быть числом.")
        return
    affected = db.execute(
        "UPDATE players SET is_alive = TRUE, death_date = NULL, killed_by = NULL WHERE user_id = %s",
        (user_id,),
    )
    if not affected:
        await update.message.reply_text("Игрок не найден.")
        return
    new_t = assign_target(user_id)
    await safe_send(context, user_id, "✨ Организаторы вернули тебя в игру. Открой «🎯 Моя цель».",
                    reply_markup=menu_for(user_id))
    await update.message.reply_text(f"✅ Игрок возвращён. Цель: {new_t or 'не назначена'}")


@admin_only
async def show_targets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = db.fetch_all(
        "SELECT h.full_name AS hunter, h.user_id AS hid, v.full_name AS victim, v.user_id AS vid, "
        "v.is_alive AS v_alive "
        "FROM targets t JOIN players h ON h.user_id = t.hunter_id "
        "JOIN players v ON v.user_id = t.target_id WHERE t.is_active ORDER BY h.full_name"
    )
    if not rows:
        await update.message.reply_text("Активных охот нет.")
        return
    lines = ["🔍 Текущие охоты:", ""]
    for r in rows:
        mark = "" if r["v_alive"] else " (цель мертва!)"
        lines.append(f"• {r['hunter']} [{r['hid']}] → {r['victim']} [{r['vid']}]{mark}")
    text = "\n".join(lines)
    for chunk in [text[i:i + 3800] for i in range(0, len(text), 3800)]:
        await update.message.reply_text(chunk)


@admin_only
async def reassign(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) < 2:
        await update.message.reply_text("Использование: /reassign <hunter_id> <target_id>")
        return
    try:
        hunter_id, target_id = int(context.args[0]), int(context.args[1])
    except ValueError:
        await update.message.reply_text("Нужны числовые id.")
        return
    if hunter_id == target_id:
        await update.message.reply_text("Нельзя охотиться на себя.")
        return
    if not get_player(hunter_id) or not get_player(target_id):
        await update.message.reply_text("Один из игроков не найден.")
        return
    db.execute("UPDATE targets SET is_active = FALSE WHERE hunter_id = %s", (hunter_id,))
    db.execute(
        "INSERT INTO targets (hunter_id, target_id, kill_code, is_active) VALUES (%s,%s,%s,TRUE) "
        "ON CONFLICT DO NOTHING",
        (hunter_id, target_id, gen_code()),
    )
    await safe_send(context, hunter_id, "🔄 Организаторы назначили тебе новую цель. Открой «🎯 Моя цель».")
    await update.message.reply_text("✅ Цель переназначена.")


@admin_only
async def check_pairs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = db.fetch_all(
        "SELECT a.hunter_id AS x, a.target_id AS y FROM targets a "
        "JOIN targets b ON a.hunter_id = b.target_id AND a.target_id = b.hunter_id "
        "WHERE a.is_active AND b.is_active AND a.hunter_id < a.target_id"
    )
    if not rows:
        await update.message.reply_text("✅ Взаимных охот нет.")
        return
    lines = ["⚠️ Найдены взаимные охоты (игроки охотятся друг на друга):", ""]
    for r in rows:
        a, b = get_player(r["x"]), get_player(r["y"])
        lines.append(f"• {a['full_name']} [{a['user_id']}] ↔ {b['full_name']} [{b['user_id']}]")
    lines.append("\nИсправить: /reassign <hunter_id> <target_id>")
    await update.message.reply_text("\n".join(lines))


@admin_only
async def armageddon(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    hunters = [r["user_id"] for r in db.fetch_all("SELECT user_id FROM players WHERE is_alive")]
    added = 0
    for hunter_id in hunters:
        current = [r["target_id"] for r in db.fetch_all(
            "SELECT target_id FROM targets WHERE hunter_id = %s AND is_active", (hunter_id,))]
        if len(current) >= 2:
            continue
        got = assign_target(hunter_id)
        if got:
            added += 1
            await safe_send(context, hunter_id, "☄️ АРМАГЕДДОН. Тебе выдана вторая цель — открой «🎯 Моя цель».")
    await update.message.reply_text(f"✅ Армагеддон: добавлено {added} доп. целей.")


@admin_only
async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = " ".join(context.args)
    if not text:
        await update.message.reply_text("Использование: /broadcast <текст>")
        return
    rows = db.fetch_all("SELECT user_id FROM players")
    ok = 0
    for r in rows:
        if await safe_send(context, r["user_id"], f"📢 Сообщение от организаторов:\n\n{text}"):
            ok += 1
    await update.message.reply_text(f"Рассылка: {ok} из {len(rows)}.")


@admin_only
async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    total = db.fetch_val("SELECT COUNT(*) FROM players", default=0)
    alive = alive_count()
    kills = db.fetch_val("SELECT COUNT(*) FROM kills", default=0)
    active = db.fetch_val("SELECT COUNT(*) FROM targets WHERE is_active", default=0)
    no_photo = db.fetch_val("SELECT COUNT(*) FROM players WHERE photo_id IS NULL", default=0)
    orphan = db.fetch_val(
        "SELECT COUNT(*) FROM players p WHERE p.is_alive AND NOT EXISTS "
        "(SELECT 1 FROM targets t WHERE t.hunter_id = p.user_id AND t.is_active)", default=0
    )
    lines = [
        "🕹 Техстатус",
        f"Игра: {'идёт' if game_started() else 'не начата'}",
        f"Регистрация: {'открыта' if registration_open() else 'закрыта'}",
        f"Килл-фид: {'вкл' if killfeed_enabled() else 'выкл'}",
        f"Игроков: {total} | живых: {alive}",
        f"Убийств: {kills} | активных охот: {active}",
        f"Без фото: {no_photo} | живых без цели: {orphan}",
        f"Длительность: {game_duration_days()} дн.",
    ]
    start = game_start_date()
    if game_started() and start:
        left = start + timedelta(days=game_duration_days()) - datetime.now(timezone.utc)
        lines.append(f"Осталось: {max(0, int(left.total_seconds() // 3600))} ч.")
    await update.message.reply_text("\n".join(lines))


@admin_only
async def set_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text(f"Длительность: {game_duration_days()} дн.\nИзменить: /set_time <дни>")
        return
    try:
        days = int(context.args[0])
        if days < 1:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Нужно целое число дней больше нуля.")
        return
    db.set_setting("game_duration_days", str(days))
    await update.message.reply_text(f"✅ Длительность игры: {days} дн.")


@admin_only
async def toggle_reg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    open_it = update.message.text.startswith("/open_reg")
    db.set_setting("registration_open", "True" if open_it else "False")
    await update.message.reply_text("✅ Регистрация " + ("открыта." if open_it else "закрыта."))


@admin_only
async def toggle_killfeed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    arg = (context.args[0].lower() if context.args else "")
    if arg not in ("on", "off"):
        await update.message.reply_text(f"Килл-фид: {'вкл' if killfeed_enabled() else 'выкл'}\n"
                                        "Использование: /killfeed on|off")
        return
    db.set_setting("killfeed", "True" if arg == "on" else "False")
    await update.message.reply_text("✅ Килл-фид " + ("включён." if arg == "on" else "выключен."))


@admin_only
async def export_csv(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = db.fetch_all(
        "SELECT user_id, username, full_name, course, academic_group, social_links, "
        "dormitory, buildings, is_alive, kills, points, personal_code, reward, "
        "is_danger, registration_date FROM players ORDER BY points DESC"
    )
    if not rows:
        await update.message.reply_text("Нет данных.")
        return
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()), delimiter=";")
    writer.writeheader()
    for r in rows:
        writer.writerow(dict(r))
    data = io.BytesIO(buf.getvalue().encode("utf-8-sig"))
    data.name = f"killer_players_{datetime.now():%Y%m%d_%H%M}.csv"
    await context.bot.send_document(chat_id=update.effective_chat.id, document=data,
                                    caption="📄 Экспорт игроков")


# ---------------------------------------------------------------------------
# ФОНОВЫЕ ЗАДАЧИ
# ---------------------------------------------------------------------------
async def job_check_deadline(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Автоматически завершает игру по истечении срока."""
    if not game_started():
        return
    start = game_start_date()
    if not start:
        return
    end = start + timedelta(days=game_duration_days())
    now = datetime.now(timezone.utc)
    if now >= end:
        await finish_game(context, reason="Время игры истекло")
        return
    left = end - now
    if timedelta(hours=23) < left <= timedelta(hours=24):
        for r in db.fetch_all("SELECT user_id FROM players WHERE is_alive"):
            await safe_send(context, r["user_id"], "⏳ До конца игры сутки. Последний рывок.")


async def job_daily_digest(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ежедневная сводка живым игрокам."""
    if not game_started():
        return
    alive = alive_count()
    kills = db.fetch_val("SELECT COUNT(*) FROM kills WHERE kill_date > NOW() - INTERVAL '24 hours'", default=0)
    leader = db.fetch_one("SELECT full_name, kills FROM players ORDER BY kills DESC, last_kill_date ASC NULLS LAST LIMIT 1")
    text = (
        "🗞 Сводка за сутки\n"
        f"Убийств: {kills}\n"
        f"В живых: {alive}\n"
        + (f"Лидер: {leader['full_name']} — {leader['kills']} уб." if leader and leader["kills"] else "")
    )
    for r in db.fetch_all("SELECT user_id FROM players WHERE is_alive"):
        await safe_send(context, r["user_id"], text)


# ---------------------------------------------------------------------------
# Прочее
# ---------------------------------------------------------------------------
async def unknown_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Не понял. Пользуйся кнопками внизу экрана.",
        reply_markup=menu_for(update.effective_user.id),
    )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Ошибка обработчика: %s", context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("Что-то сломалось. Попробуй ещё раз или напиши организаторам.")
        except TelegramError:
            pass


async def post_init(application: Application) -> None:
    await application.bot.set_my_commands([
        ("start", "Меню и правила"),
        ("register", "Зарегистрироваться"),
        ("target", "Моя цель"),
        ("kill", "Ввести код жертвы"),
        ("me", "Моё досье"),
        ("stats", "Статистика"),
        ("top", "Топ игроков"),
        ("rules", "Правила"),
        ("help", "Помощь"),
    ])


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def build_application() -> Application:
    if not BOT_TOKEN:
        raise RuntimeError("Не задана переменная окружения BOT_TOKEN")

    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()

    def btn(label: str):
        return filters.Regex(f"^{re.escape(label)}$")

    # --- Регистрация ---
    reg_conv = ConversationHandler(
        entry_points=[
            CommandHandler("register", reg_start),
            MessageHandler(btn(K.BTN_REGISTER), reg_start),
        ],
        states={
            REG_FLOW: [
                MessageHandler(filters.PHOTO, reg_photo),
                MessageHandler(filters.TEXT & ~filters.COMMAND, reg_text),
            ],
        },
        fallbacks=[CommandHandler("cancel", reg_cancel)],
        name="registration",
    )

    # --- Отмена регистрации ---
    cancel_reg_conv = ConversationHandler(
        entry_points=[
            MessageHandler(btn(K.BTN_CANCEL_REG), cancel_reg_start),
            CommandHandler("cancel_registration", cancel_reg_start),
        ],
        states={
            CANCEL_REG_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, cancel_reg_confirm)],
        },
        fallbacks=[],
        name="cancel_registration",
    )

    # --- Убийство ---
    kill_conv = ConversationHandler(
        entry_points=[
            CommandHandler("kill", kill_start),
            MessageHandler(btn(K.BTN_KILL), kill_start),
        ],
        states={KILL_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, kill_code)]},
        fallbacks=[CommandHandler("cancel", reg_cancel)],
        name="kill",
    )

    # --- Анонимные записки (бесплатно, своему охотнику/жертве, можно с фото) ---
    msg_conv = ConversationHandler(
        entry_points=[
            MessageHandler(btn(K.BTN_MSG_KILLER), msg_start),
            MessageHandler(btn(K.BTN_MSG_TARGET), msg_start),
            CommandHandler("msg_killer", msg_start),
            CommandHandler("msg_target", msg_start),
        ],
        states={MSG_TEXT: [MessageHandler((filters.TEXT | filters.PHOTO) & ~filters.COMMAND, msg_send)]},
        fallbacks=[],
        name="anon_messages",
    )

    # --- Платное письмо любому живому игроку (1 очко) ---
    msg_any_conv = ConversationHandler(
        entry_points=[
            MessageHandler(btn(K.BTN_MSG_ANY), msg_any_start),
            CommandHandler("msg_any", msg_any_start),
        ],
        states={
            MSG_ANY_RECIPIENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, msg_any_pick_recipient)],
            MSG_ANY_SIGN: [MessageHandler(filters.TEXT & ~filters.COMMAND, msg_any_pick_sign)],
            MSG_ANY_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, msg_any_send)],
        },
        fallbacks=[],
        name="msg_any",
    )

    # --- Последнее слово ---
    last_words_conv = ConversationHandler(
        entry_points=[
            MessageHandler(btn(K.BTN_LAST_WORDS), last_words_start),
            CommandHandler("last_words", last_words_start),
        ],
        states={LAST_WORDS: [MessageHandler(filters.TEXT & ~filters.COMMAND, last_words_save)]},
        fallbacks=[],
        name="last_words",
    )

    # --- Замена фото админом ---
    photo_conv = ConversationHandler(
        entry_points=[CommandHandler("set_photo", set_photo_start)],
        states={
            ADMIN_PHOTO: [
                MessageHandler(filters.PHOTO | filters.Document.IMAGE, set_photo_save),
                MessageHandler(filters.TEXT & ~filters.COMMAND, set_photo_save),
            ]
        },
        fallbacks=[CommandHandler("cancel", reg_cancel)],
        name="admin_set_photo",
    )

    for conv in (reg_conv, cancel_reg_conv, kill_conv, msg_conv, msg_any_conv, last_words_conv, photo_conv):
        app.add_handler(conv)

    # --- Базовые команды и кнопки ---
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("rules", cmd_rules))
    app.add_handler(CommandHandler("target", show_target))
    app.add_handler(CommandHandler("me", show_me))
    app.add_handler(CommandHandler("stats", show_stats))
    app.add_handler(CommandHandler("top", show_top))
    app.add_handler(CommandHandler("graveyard", show_graveyard))  # только для админов, проверка внутри show_graveyard

    app.add_handler(MessageHandler(btn(K.BTN_TARGET), show_target))
    app.add_handler(MessageHandler(btn(K.BTN_ME), show_me))
    app.add_handler(MessageHandler(btn(K.BTN_STATS), show_stats))
    app.add_handler(MessageHandler(btn(K.BTN_TOP), show_top))
    app.add_handler(MessageHandler(btn(K.BTN_RULES), cmd_rules))
    app.add_handler(MessageHandler(btn(K.BTN_HELP), cmd_help))
    app.add_handler(MessageHandler(btn(K.BTN_ADMIN), admin_panel))

    # --- Админ ---
    admin_handlers = {
        "admin": admin_panel,
        "start_game": start_game,
        "end_game": end_game,
        "reset_game": reset_game,
        "list_players": list_players,
        "view_profile": view_profile,
        "edit_player": edit_player,
        "add_hint": add_hint,
        "set_global_reward": set_global_reward,
        "set_reward_player": set_reward_player,
        "set_danger": set_danger,
        "remove_danger": remove_danger,
        "add_player": add_player,
        "remove_player": remove_player,
        "revive": revive,
        "show_targets": show_targets,
        "reassign": reassign,
        "check_pairs": check_pairs,
        "armageddon": armageddon,
        "broadcast": broadcast,
        "status": status,
        "set_time": set_time,
        "open_reg": toggle_reg,
        "close_reg": toggle_reg,
        "killfeed": toggle_killfeed,
        "export": export_csv,
    }
    for name, handler in admin_handlers.items():
        app.add_handler(CommandHandler(name, handler))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unknown_text))
    app.add_error_handler(on_error)

    if app.job_queue:
        app.job_queue.run_repeating(job_check_deadline, interval=1800, first=60)
        app.job_queue.run_repeating(job_daily_digest, interval=86400, first=3600)

    return app


def main() -> None:
    db.init_db()
    app = build_application()
    logger.info("Бот запущен. Админы: %s", ADMIN_IDS)
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
