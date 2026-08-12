import logging
import random
import sqlite3
from datetime import datetime, timedelta
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    ConversationHandler,
    filters
)
import os

# ---------- Путь к БД ----------
if os.path.exists("/app/data"):
    DB_PATH = "/app/data/killer_game.db"
else:
    DB_PATH = "killer_game.db"

# ---------- Конфигурация ----------
BOT_TOKEN = os.getenv("BOT_TOKEN", "8377571705:AAEO50McGhCsuWGgZnhhgmhKeoUeGCHrm8s")   # замените
ADMIN_ID = int(os.getenv("ADMIN_ID", "1513781380"))  # замените

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Состояния для регистрации
FIO, COURSE, GROUP, SOCIAL, ABOUT, BUILDINGS, DORM, PHOTO = range(8)
KILL_CONFIRMATION = range(1)

# Глобальные настройки
GAME_DURATION_DAYS = 14

# ---------- Клавиатуры ----------
def get_main_keyboard():
    """Клавиатура для обычных пользователей во время игры."""
    keyboard = [
        ["/target", "/kill"],
        ["/stats", "/me"],
        ["/msg_killer", "/msg_target"]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_registration_keyboard():
    """Клавиатура для навигации в регистрации."""
    keyboard = [
        ["⬅ Назад", "❌ Отмена"]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# ---------- Работа с БД ----------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute('''
    CREATE TABLE IF NOT EXISTS players (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        full_name TEXT,
        faculty TEXT,
        course TEXT,
        academic_group TEXT,
        social_links TEXT,
        about_self TEXT,
        buildings TEXT,
        dormitory TEXT,
        photo_id TEXT,
        habits TEXT,
        is_alive BOOLEAN DEFAULT TRUE,
        registration_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        kills INTEGER DEFAULT 0,
        personal_code TEXT UNIQUE,
        reward INTEGER DEFAULT 1,
        is_danger BOOLEAN DEFAULT 0,
        danger_reason TEXT DEFAULT ''
    )
    ''')

    cursor.execute('''
    CREATE TABLE IF NOT EXISTS targets (
        hunter_id INTEGER,
        target_id INTEGER,
        assigned_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        kill_code TEXT,
        is_active BOOLEAN DEFAULT TRUE,
        PRIMARY KEY (hunter_id, target_id),
        FOREIGN KEY (hunter_id) REFERENCES players (user_id),
        FOREIGN KEY (target_id) REFERENCES players (user_id)
    )
    ''')

    cursor.execute('''
    CREATE TABLE IF NOT EXISTS kills (
        kill_id INTEGER PRIMARY KEY AUTOINCREMENT,
        hunter_id INTEGER,
        victim_id INTEGER,
        kill_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        kill_code TEXT,
        FOREIGN KEY (hunter_id) REFERENCES players (user_id),
        FOREIGN KEY (victim_id) REFERENCES players (user_id)
    )
    ''')

    cursor.execute('''
    CREATE TABLE IF NOT EXISTS game_settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )
    ''')

    # Инициализация настроек
    cursor.execute("INSERT OR IGNORE INTO game_settings (key, value) VALUES ('reward', '1')")
    cursor.execute("INSERT OR IGNORE INTO game_settings (key, value) VALUES ('game_started', 'False')")
    cursor.execute("INSERT OR IGNORE INTO game_settings (key, value) VALUES ('game_start_date', '')")

    conn.commit()
    conn.close()

# ---------- Вспомогательные функции ----------
async def get_game_state():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM game_settings WHERE key='game_started'")
    started = cursor.fetchone()
    cursor.execute("SELECT value FROM game_settings WHERE key='game_start_date'")
    start_date = cursor.fetchone()
    conn.close()
    return {
        'started': started[0] == 'True' if started else False,
        'start_date': datetime.fromisoformat(start_date[0]) if start_date and start_date[0] else None
    }

def generate_personal_code():
    return ''.join(random.choices('ABCDEFGHJKLMNPQRSTUVWXYZ23456789', k=6))

def generate_kill_code():
    return ''.join(random.choices('ABCDEFGHJKLMNPQRSTUVWXYZ23456789', k=6))

def get_global_reward():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM game_settings WHERE key='reward'")
    row = cursor.fetchone()
    conn.close()
    return int(row[0]) if row else 1

def set_global_reward(value):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("UPDATE game_settings SET value = ? WHERE key = 'reward'", (str(value),))
    conn.commit()
    conn.close()

# ---------- Обработчики команд ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    game_state = await get_game_state()

    if game_state['started']:
        await update.message.reply_text(
            f"Игра уже началась! Ты опоздал, {user.first_name}.\n"
            "Но ты можешь следить за статистикой с помощью /stats",
            reply_markup=get_main_keyboard() if await is_player_registered(user.id) else None
        )
        return

    await update.message.reply_text(
        f"Привет, {user.first_name}!\n"
        "Это бот для игры 'Киллер'.\n\n"
        "1. Игра организуется на принципе честной игры! Каждый игрок обязуется соблюдать её правила. При их нарушении игрок выбрасывается из игры.\n"
        "2. Суть игры заключается в охоте за жертвой. Каждый участник является одновременно и охотником и жертвой.\n"
        "3. Игра начинается для всех одновременно! Вы получаете досье на свою жертву. В каждом досье находится фотография жертвы и краткое описание её привычек. Эта информация может помочь вам как охотнику выследить жертву. В то же самое время кто-то получает ваше досье и начинает охоту на вас.\n"
        "4. Жертва считается убитой, если охотник выстрелил в неё из пальца, находясь в закрытом помещении один на один, или на улице, где в радиусе 20 метров никого нет. Нельзя убивать при свидетелях - будь то участник игры или просто посторонний человек.\n"
        "5. После смерти жертва должна передать охотнику секретный пароль. Охотник должен ввести, полученный пароль в ТГ-бот и получить новую жертву.\n"
        "6. В случае если охотник и жертва охотятся друг на друга, они должны обратиться к организаторам для того, чтобы получить новую жертву.\n"
        "7. Игра заканчивается тогда, когда остаются только два участника. Либо вышло время, отведённое на игру. Побеждает охотник, который убил наибольшее количество жертв.\n\n"
        "Чтобы зарегистрироваться, используй /register"
    )

async def is_player_registered(user_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM players WHERE user_id = ?", (user_id,))
    result = cursor.fetchone() is not None
    conn.close()
    return result

async def register(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    game_state = await get_game_state()
    if game_state['started']:
        await update.message.reply_text("Регистрация закрыта, игра уже началась!")
        return ConversationHandler.END

    if await is_player_registered(user_id):
        await update.message.reply_text(
            "✅ Вы уже зарегистрированы!",
            reply_markup=get_main_keyboard() if game_state['started'] else ReplyKeyboardRemove()
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "📋 Регистрация в игре 'Киллер'.\n"
        "Пожалуйста, введи своё полное имя (ФИО или ФИ):",
        reply_markup=get_registration_keyboard()
    )
    return FIO

# --- Шаги регистрации с кнопками "Назад" и "Отмена" ---
async def handle_cancel_or_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "❌ Отмена":
        await update.message.reply_text("Регистрация отменена.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END
    elif text == "⬅ Назад":
        # Для первого шага просто отменяем
        await update.message.reply_text("Регистрация отменена (нет предыдущего шага).", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END
    return None  # продолжение

async def get_fio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text
    res = await handle_cancel_or_back(update, context)
    if res is not None:
        return res
    context.user_data['full_name'] = text
    await update.message.reply_text("Введи свой курс (например, '3 курс', 'преподаватель' или др):", reply_markup=get_registration_keyboard())
    return COURSE

async def get_course(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text
    if text == "⬅ Назад":
        await update.message.reply_text("Вернулись к вводу ФИО. Введи своё полное имя:", reply_markup=get_registration_keyboard())
        return FIO
    if text == "❌ Отмена":
        await update.message.reply_text("Регистрация отменена.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END
    context.user_data['course'] = text
    await update.message.reply_text("Введи свою академическую группу (например эиф-103/6):", reply_markup=get_registration_keyboard())
    return GROUP

async def get_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text
    if text == "⬅ Назад":
        await update.message.reply_text("Введи свой курс:", reply_markup=get_registration_keyboard())
        return COURSE
    if text == "❌ Отмена":
        await update.message.reply_text("Регистрация отменена.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END
    context.user_data['academic_group'] = text
    await update.message.reply_text("Укажи ссылки на свои соцсети (ВК, Telegram):", reply_markup=get_registration_keyboard())
    return SOCIAL

async def get_social(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text
    if text == "⬅ Назад":
        await update.message.reply_text("Введи свою группу:", reply_markup=get_registration_keyboard())
        return GROUP
    if text == "❌ Отмена":
        await update.message.reply_text("Регистрация отменена.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END
    context.user_data['social_links'] = text
    await update.message.reply_text(
        "Расскажи немного о себе: где ты обычно обитаешь, твой примерный маршрут на день, любимые места.\n"
        "Это поможет охотнику тебя найти.",
        reply_markup=get_registration_keyboard()
    )
    return ABOUT

async def get_about(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text
    if text == "⬅ Назад":
        await update.message.reply_text("Укажи соцсети:", reply_markup=get_registration_keyboard())
        return SOCIAL
    if text == "❌ Отмена":
        await update.message.reply_text("Регистрация отменена.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END
    context.user_data['about_self'] = text
    await update.message.reply_text("В каких корпусах у тебя обычно проходят пары?", reply_markup=get_registration_keyboard())
    return BUILDINGS

async def get_buildings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text
    if text == "⬅ Назад":
        await update.message.reply_text("Расскажи о себе:", reply_markup=get_registration_keyboard())
        return ABOUT
    if text == "❌ Отмена":
        await update.message.reply_text("Регистрация отменена.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END
    context.user_data['buildings'] = text
    await update.message.reply_text(
        "Ты живешь в общаге? Если да, укажи корпус.\n"
        "Если нет, напиши 'нет' или укажи примерный район проживания.",
        reply_markup=get_registration_keyboard()
    )
    return DORM

async def get_dorm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text
    if text == "⬅ Назад":
        await update.message.reply_text("В каких корпусах пары?", reply_markup=get_registration_keyboard())
        return BUILDINGS
    if text == "❌ Отмена":
        await update.message.reply_text("Регистрация отменена.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END
    context.user_data['dormitory'] = text
    await update.message.reply_text("Теперь загрузи своё фото (оно будет в досье для охотника).", reply_markup=ReplyKeyboardRemove())
    return PHOTO

async def get_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    photo_file = await update.message.photo[-1].get_file()
    context.user_data['photo_id'] = photo_file.file_id

    personal_code = generate_personal_code()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("SELECT user_id FROM players WHERE user_id = ?", (update.effective_user.id,))
    if cursor.fetchone():
        await update.message.reply_text("Ты уже зарегистрирован!", reply_markup=ReplyKeyboardRemove())
        conn.close()
        return ConversationHandler.END

    cursor.execute('''
        INSERT INTO players 
        (user_id, username, full_name, course, academic_group, social_links, about_self, buildings, dormitory, photo_id, personal_code)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        update.effective_user.id,
        update.effective_user.username,
        context.user_data['full_name'],
        context.user_data['course'],
        context.user_data['academic_group'],
        context.user_data['social_links'],
        context.user_data['about_self'],
        context.user_data['buildings'],
        context.user_data['dormitory'],
        context.user_data['photo_id'],
        personal_code
    ))
    conn.commit()
    conn.close()

    game_state = await get_game_state()
    keyboard = get_main_keyboard() if game_state['started'] else ReplyKeyboardRemove()

    await update.message.reply_text(
        f"✅ Регистрация завершена! Ты в игре.\n"
        f"🔐 Твой личный секретный код: {personal_code}\n\n"
        "Запомни его! Ты должен будешь передать его охотнику, если он тебя убьёт.\n"
        "Ожидай начала. Когда игра начнется, ты получишь свою первую цель.\n\n"
        "Ты можешь проверить свои данные с помощью /me",
        reply_markup=keyboard
    )

    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"📝 Новый участник!\nИмя: {context.user_data['full_name']}\nГруппа: {context.user_data['academic_group']}\nКод: {personal_code}\nID: {update.effective_user.id}"
        )
    except Exception as e:
        logger.error(f"Не удалось отправить уведомление админу: {e}")

    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Регистрация отменена.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

# ---------- Административные команды (все с DB_PATH) ----------
async def start_game(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Эта команда только для организаторов!")
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("SELECT user_id FROM players WHERE is_alive=1")
    players = [row[0] for row in cursor.fetchall()]

    if len(players) < 3:
        await update.message.reply_text("Для начала игры нужно минимум 3 участника!")
        conn.close()
        return

    random.shuffle(players)

    for i in range(len(players)):
        hunter = players[i]
        target = players[(i + 1) % len(players)]
        kill_code = generate_kill_code()

        cursor.execute(
            "INSERT INTO targets (hunter_id, target_id, kill_code) VALUES (?, ?, ?)",
            (hunter, target, kill_code)
        )

    start_date = datetime.now()
    cursor.execute(
        "INSERT OR REPLACE INTO game_settings (key, value) VALUES (?, ?)",
        ('game_started', 'True')
    )
    cursor.execute(
        "INSERT OR REPLACE INTO game_settings (key, value) VALUES (?, ?)",
        ('game_start_date', start_date.isoformat())
    )

    conn.commit()
    conn.close()

    for player_id in players:
        try:
            await context.bot.send_message(
                chat_id=player_id,
                text="🎮 Игра началась! Твоя цель уже ждет тебя. Используй /target чтобы увидеть свою цель!",
                reply_markup=get_main_keyboard()
            )
        except Exception as e:
            logger.error(f"Не удалось отправить сообщение игроку {player_id}: {e}")

    await update.message.reply_text(f"Игра начата! Участников: {len(players)}")

async def end_game(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Эта команда только для организаторов!")
        return
    await end_game_logic(context.bot)
    await update.message.reply_text("Игра завершена. Результаты разосланы.")

async def end_game_logic(bot):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
    SELECT user_id, full_name, kills FROM players WHERE is_alive = 1 ORDER BY kills DESC
    ''')
    winners = cursor.fetchall()
    cursor.execute("UPDATE game_settings SET value = 'False' WHERE key = 'game_started'")
    conn.commit()
    conn.close()

    message = "🏆 Игра окончена! Результаты:\n\n"
    for i, (user_id, name, kills) in enumerate(winners, 1):
        message += f"{i}. {name} - {kills} убийств\n"
        try:
            await bot.send_message(
                chat_id=user_id,
                text=f"🎉 Поздравляем! Ты в топ-{i}!\nТвой результат: {kills} убийств.",
                reply_markup=ReplyKeyboardRemove()
            )
        except Exception as e:
            logger.error(f"Не удалось отправить сообщение победителю {user_id}: {e}")

    try:
        await bot.send_message(chat_id=ADMIN_ID, text=message)
    except Exception as e:
        logger.error(f"Не удалось отправить результаты админу: {e}")

async def reset_game(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Эта команда только для организаторов!")
        return
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM players")
    cursor.execute("DELETE FROM targets")
    cursor.execute("DELETE FROM kills")
    cursor.execute("DELETE FROM game_settings")
    conn.commit()
    conn.close()
    await update.message.reply_text("Игра сброшена. Все данные удалены.")

async def list_players(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Эта команда только для организаторов!")
        return
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, full_name, faculty, is_alive, kills FROM players")
    players = cursor.fetchall()
    conn.close()

    if not players:
        await update.message.reply_text("Нет зарегистрированных игроков.")
        return

    alive = [p for p in players if p[3]]
    dead = [p for p in players if not p[3]]

    text = "👥 Список игроков:\n\n"
    text += "🟢 Живые:\n"
    for p in alive:
        text += f"• {p[1]} ({p[2]}) — убийств: {p[4]}\n"
    text += "\n🔴 Мёртвые:\n"
    for p in dead:
        text += f"• {p[1]} ({p[2]}) — убийств: {p[4]}\n"

    await update.message.reply_text(text)

async def add_player(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Эта команда только для организаторов!")
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Использование: /add_player <user_id> <полное имя> [факультет]")
        return
    try:
        user_id = int(args[0])
        full_name = args[1]
        faculty = args[2] if len(args) > 2 else "Не указан"
    except ValueError:
        await update.message.reply_text("Неверный формат. user_id должен быть числом.")
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR IGNORE INTO players (user_id, full_name, faculty, is_alive) VALUES (?, ?, ?, 1)",
        (user_id, full_name, faculty)
    )
    conn.commit()
    conn.close()
    await update.message.reply_text(f"Игрок {full_name} (ID {user_id}) добавлен.")

async def remove_player(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Эта команда только для организаторов!")
        return
    args = context.args
    if not args:
        await update.message.reply_text("Использование: /remove_player <user_id>")
        return
    try:
        user_id = int(args[0])
    except ValueError:
        await update.message.reply_text("user_id должен быть числом.")
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT full_name FROM players WHERE user_id = ?", (user_id,))
    player = cursor.fetchone()
    if not player:
        await update.message.reply_text(f"Игрок с ID {user_id} не найден.")
        conn.close()
        return
    player_name = player[0]

    # ... (логика переназначения целей — оставим как было, но с DB_PATH)
    cursor.execute("SELECT target_id FROM targets WHERE hunter_id = ? AND is_active = 1", (user_id,))
    old_target_row = cursor.fetchone()
    old_target_id = old_target_row[0] if old_target_row else None

    cursor.execute("SELECT hunter_id FROM targets WHERE target_id = ? AND is_active = 1", (user_id,))
    hunter_row = cursor.fetchone()
    hunter_id = hunter_row[0] if hunter_row else None

    if hunter_id and old_target_id:
        cursor.execute("SELECT is_alive FROM players WHERE user_id = ?", (old_target_id,))
        old_target_alive = cursor.fetchone()
        if old_target_alive and old_target_alive[0]:
            new_kill_code = generate_personal_code()
            cursor.execute(
                "INSERT INTO targets (hunter_id, target_id, kill_code, is_active) VALUES (?, ?, ?, 1)",
                (hunter_id, old_target_id, new_kill_code)
            )
            await update.message.reply_text(f"🔄 Игроку {hunter_id} переназначена цель {old_target_id}.")
        else:
            await update.message.reply_text(f"⚠️ Цель {old_target_id} мертва. Используйте ручное назначение.")
    elif hunter_id and not old_target_id:
        await update.message.reply_text(f"⚠️ У удалённого не было цели. Охотник {hunter_id} без цели.")
    elif not hunter_id and old_target_id:
        await update.message.reply_text(f"⚠️ На удалённого никто не охотился. Цель {old_target_id} без охотника.")
    else:
        await update.message.reply_text("Удаляемый игрок не участвовал в цепочке охоты.")

    cursor.execute("DELETE FROM targets WHERE hunter_id = ? OR target_id = ?", (user_id, user_id))
    cursor.execute("DELETE FROM players WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

    await update.message.reply_text(f"Игрок {player_name} (ID {user_id}) удалён.")

async def set_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Эта команда только для организаторов!")
        return
    global GAME_DURATION_DAYS
    args = context.args
    if not args:
        await update.message.reply_text(f"Текущая длительность игры: {GAME_DURATION_DAYS} дней.\nИспользуйте /set_time <дни>")
        return
    try:
        days = int(args[0])
        if days < 1:
            await update.message.reply_text("Длительность должна быть положительным числом.")
            return
    except ValueError:
        await update.message.reply_text("Введите число дней.")
        return
    GAME_DURATION_DAYS = days
    await update.message.reply_text(f"Длительность игры установлена на {days} дней.")

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Эта команда только для организаторов!")
        return
    message_text = ' '.join(context.args)
    if not message_text:
        await update.message.reply_text("Укажите текст для рассылки: /broadcast <текст>")
        return
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM players")
    players = cursor.fetchall()
    conn.close()
    success = 0
    for (user_id,) in players:
        try:
            await context.bot.send_message(chat_id=user_id, text=f"📢 Сообщение от организатора:\n{message_text}")
            success += 1
        except Exception as e:
            logger.error(f"Не удалось отправить сообщение {user_id}: {e}")
    await update.message.reply_text(f"Рассылка завершена. Отправлено {success} из {len(players)} сообщений.")

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Эта команда только для организаторов!")
        return
    game_state = await get_game_state()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM players")
    total = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM players WHERE is_alive=1")
    alive = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM kills")
    kills = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM targets WHERE is_active=1")
    active_targets = cursor.fetchone()[0]
    conn.close()
    text = (
        f"🕹 **Технический статус игры**\n"
        f"Игра {'активна' if game_state['started'] else 'не начата'}\n"
        f"Всего игроков: {total}\n"
        f"Выживших: {alive}\n"
        f"Всего убийств: {kills}\n"
        f"Активных целей: {active_targets}\n"
        f"Длительность: {GAME_DURATION_DAYS} дней\n"
    )
    if game_state['started'] and game_state['start_date']:
        end_date = game_state['start_date'] + timedelta(days=GAME_DURATION_DAYS)
        time_left = end_date - datetime.now()
        text += f"Время до окончания: {max(0, time_left.days)} дней\n"
    await update.message.reply_text(text)

# ---------- Новые админ-команды ----------
async def view_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Только для админа.")
        return
    args = context.args
    if not args:
        await update.message.reply_text("Использование: /view_profile <user_id или часть имени>")
        return
    query = ' '.join(args)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    try:
        user_id = int(query)
        cursor.execute("SELECT * FROM players WHERE user_id = ?", (user_id,))
    except ValueError:
        cursor.execute("SELECT * FROM players WHERE full_name LIKE ?", (f"%{query}%",))
    player = cursor.fetchone()
    conn.close()
    if not player:
        await update.message.reply_text("Игрок не найден.")
        return
    fields = ['user_id', 'username', 'full_name', 'faculty', 'course', 'academic_group',
              'social_links', 'about_self', 'buildings', 'dormitory', 'photo_id', 'habits',
              'is_alive', 'registration_date', 'kills', 'personal_code', 'reward', 'is_danger', 'danger_reason']
    msg = "📋 **Анкета игрока**\n\n"
    for i, field in enumerate(fields):
        value = player[i] if i < len(player) else '—'
        msg += f"**{field}:** {value}\n"
    await update.message.reply_text(msg, parse_mode='Markdown')

async def show_targets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Только для админа.")
        return
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT p1.full_name, p2.full_name
        FROM targets t
        JOIN players p1 ON t.hunter_id = p1.user_id
        JOIN players p2 ON t.target_id = p2.user_id
        WHERE t.is_active = 1
    ''')
    rows = cursor.fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("Нет активных целей.")
        return
    msg = "🔍 **Текущие охоты:**\n\n"
    for hunter, target in rows:
        msg += f"• {hunter} → {target}\n"
    await update.message.reply_text(msg, parse_mode='Markdown')

async def armageddon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Только для админа.")
        return
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT DISTINCT hunter_id FROM targets WHERE is_active = 1')
    hunters = [row[0] for row in cursor.fetchall()]
    added = 0
    for hunter_id in hunters:
        cursor.execute('SELECT target_id FROM targets WHERE hunter_id = ? AND is_active = 1 LIMIT 1', (hunter_id,))
        row = cursor.fetchone()
        if not row:
            continue
        primary_target = row[0]
        cursor.execute('SELECT target_id FROM targets WHERE hunter_id = ? AND is_active = 1 LIMIT 1', (primary_target,))
        row2 = cursor.fetchone()
        if not row2:
            continue
        secondary_target = row2[0]
        cursor.execute('SELECT 1 FROM targets WHERE hunter_id = ? AND target_id = ? AND is_active = 1', (hunter_id, secondary_target))
        if cursor.fetchone():
            continue
        cursor.execute('INSERT INTO targets (hunter_id, target_id, kill_code, is_active) VALUES (?, ?, ?, 1)',
                       (hunter_id, secondary_target, generate_kill_code()))
        added += 1
    conn.commit()
    conn.close()
    await update.message.reply_text(f"✅ Армагедон активирован! Добавлено {added} дополнительных целей.")

async def edit_player(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Только для админа.")
        return
    args = context.args
    if len(args) < 3:
        await update.message.reply_text("Использование: /edit_player <user_id> <поле> <новое_значение>")
        return
    try:
        user_id = int(args[0])
    except ValueError:
        await update.message.reply_text("user_id должен быть числом.")
        return
    field = args[1].lower()
    new_value = ' '.join(args[2:])
    allowed_fields = ['full_name', 'course', 'academic_group', 'social_links', 'about_self',
                      'buildings', 'dormitory', 'photo_id', 'habits', 'personal_code', 'reward', 'danger_reason']
    if field not in allowed_fields:
        await update.message.reply_text(f"Допустимые поля: {', '.join(allowed_fields)}")
        return
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(f"UPDATE players SET {field} = ? WHERE user_id = ?", (new_value, user_id))
    conn.commit()
    affected = cursor.rowcount
    conn.close()
    if affected:
        await update.message.reply_text(f"✅ Поле {field} обновлено для user_id {user_id}.")
    else:
        await update.message.reply_text("❌ Игрок не найден.")

async def set_global_reward_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Только для админа.")
        return
    args = context.args
    if not args:
        await update.message.reply_text("Использование: /set_global_reward <число>")
        return
    try:
        reward = int(args[0])
    except ValueError:
        await update.message.reply_text("Введите число.")
        return
    set_global_reward(reward)
    await update.message.reply_text(f"✅ Глобальная награда за убийство установлена: {reward}.")

async def set_reward_player(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Установить индивидуальную награду для игрока."""
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Только для админа.")
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Использование: /set_reward_player <user_id> <число>")
        return
    try:
        user_id = int(args[0])
        reward = int(args[1])
    except ValueError:
        await update.message.reply_text("user_id и награда должны быть числами.")
        return
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("UPDATE players SET reward = ? WHERE user_id = ?", (reward, user_id))
    conn.commit()
    affected = cursor.rowcount
    conn.close()
    if affected:
        await update.message.reply_text(f"✅ Награда для игрока {user_id} установлена: {reward}.")
    else:
        await update.message.reply_text("❌ Игрок не найден.")

async def set_danger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Установить статус 'ОСОБО ОПАСЕН' с причиной."""
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Только для админа.")
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Использование: /set_danger <user_id> <причина>")
        return
    try:
        user_id = int(args[0])
    except ValueError:
        await update.message.reply_text("user_id должен быть числом.")
        return
    reason = ' '.join(args[1:])
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("UPDATE players SET is_danger = 1, danger_reason = ? WHERE user_id = ?", (reason, user_id))
    conn.commit()
    affected = cursor.rowcount
    conn.close()
    if affected:
        await update.message.reply_text(f"✅ Игрок {user_id} помечен как ОСОБО ОПАСЕН.\nПричина: {reason}")
    else:
        await update.message.reply_text("❌ Игрок не найден.")

async def remove_danger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Снять статус 'ОСОБО ОПАСЕН'."""
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Только для админа.")
        return
    args = context.args
    if not args:
        await update.message.reply_text("Использование: /remove_danger <user_id>")
        return
    try:
        user_id = int(args[0])
    except ValueError:
        await update.message.reply_text("user_id должен быть числом.")
        return
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("UPDATE players SET is_danger = 0, danger_reason = '' WHERE user_id = ?", (user_id,))
    conn.commit()
    affected = cursor.rowcount
    conn.close()
    if affected:
        await update.message.reply_text(f"✅ Статус ОСОБО ОПАСЕН снят для игрока {user_id}.")
    else:
        await update.message.reply_text("❌ Игрок не найден.")

# ---------- Пользовательские команды ----------
async def msg_killer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT hunter_id FROM targets WHERE target_id = ? AND is_active = 1', (user_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        await update.message.reply_text("❌ У вас нет активного киллера (или вы уже мертвы).")
        return
    killer_id = row[0]
    text = ' '.join(context.args)
    if not text:
        await update.message.reply_text("Напишите сообщение: /msg_killer <текст>")
        return
    try:
        await context.bot.send_message(
            chat_id=killer_id,
            text=f"📩 Сообщение от вашей жертвы (анонимно):\n{text}"
        )
        await update.message.reply_text("✅ Сообщение отправлено вашему киллеру.")
    except Exception as e:
        logger.error(f"Не удалось отправить сообщение киллеру: {e}")
        await update.message.reply_text("❌ Не удалось отправить сообщение.")

async def msg_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT target_id FROM targets WHERE hunter_id = ? AND is_active = 1', (user_id,))
    rows = cursor.fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("❌ У вас нет активной цели.")
        return
    text = ' '.join(context.args)
    if not text:
        await update.message.reply_text("Напишите сообщение: /msg_target <текст>")
        return
    for (target_id,) in rows:
        try:
            await context.bot.send_message(
                chat_id=target_id,
                text=f"📩 Сообщение от вашего охотника (анонимно):\n{text}"
            )
        except Exception as e:
            logger.error(f"Не удалось отправить сообщение жертве {target_id}: {e}")
    await update.message.reply_text("✅ Сообщение отправлено вашей жертве.")

# ---------- Команда /target (показывает все цели с меткой опасности) ----------
async def show_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    game_state = await get_game_state()
    if not game_state['started']:
        await update.message.reply_text("Игра еще не началась!")
        return
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT p.full_name, p.course, p.academic_group, p.social_links, p.about_self,
               p.buildings, p.dormitory, p.photo_id, p.is_danger, p.danger_reason
        FROM targets t
        JOIN players p ON t.target_id = p.user_id
        WHERE t.hunter_id = ? AND t.is_active = 1
    ''', (user.id,))
    targets = cursor.fetchall()
    conn.close()
    if not targets:
        await update.message.reply_text("У тебя нет активных целей.")
        return
    for target in targets:
        (full_name, course, group, social, about, buildings, dorm, photo_id, is_danger, danger_reason) = target
        danger_text = ""
        if is_danger:
            danger_text = f"\n⚠️ **ОСОБО ОПАСЕН**\nПричина: {danger_reason}"
        target_info = (
            f"🔫 **Твоя цель:**\n\n"
            f"**Имя:** {full_name}\n"
            f"**Курс:** {course}\n"
            f"**Группа:** {group}\n"
            f"**Соцсети:** {social}\n"
            f"**О себе:** {about}\n"
            f"**Корпуса:** {buildings}\n"
            f"**Общежитие/район:** {dorm}\n"
            f"{danger_text}\n\n"
            f"Когда встретишь цель, она должна сообщить тебе свой личный код.\n"
            f"Введи его командой /kill после убийства."
        )
        await context.bot.send_photo(
            chat_id=user.id,
            photo=photo_id,
            caption=target_info,
            parse_mode='Markdown'
        )

# ---------- Команда /me (показывает своё досье с меткой опасности и наградой) ----------
async def show_me(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT full_name, course, academic_group, social_links, about_self, buildings, dormitory,
               photo_id, is_alive, kills, personal_code, reward, is_danger, danger_reason
        FROM players WHERE user_id = ?
    ''', (user.id,))
    player = cursor.fetchone()
    conn.close()
    if not player:
        await update.message.reply_text("❌ Ты не зарегистрирован в игре. Используй /register")
        return
    (full_name, course, group, social, about, buildings, dorm, photo_id, is_alive, kills, personal_code, reward, is_danger, danger_reason) = player

    danger_text = ""
    if is_danger:
        danger_text = f"\n⚠️ **ОСОБО ОПАСЕН**\nПричина: {danger_reason}"

    message = (
        f"👤 Твоё досье:\n\n"
        f"Имя: {full_name}\n"
        f"Курс: {course}\n"
        f"Группа: {group}\n"
        f"Соцсети: {social}\n"
        f"О себе: {about}\n"
        f"Корпуса: {buildings}\n"
        f"Общежитие/район: {dorm}\n"
        f"Статус: {'жив' if is_alive else 'мёртв'}\n"
        f"Убийств: {kills}\n"
        f"Твой личный код: {personal_code}\n"
        f"Твоя награда: {reward}\n"
        f"{danger_text}\n\n"
    )
    if is_alive:
        message += "Будь осторожен, за тобой могут охотиться!"
    else:
        message += "Ты уже мёртв в этой игре. Жди следующей!"

    if photo_id:
        try:
            await context.bot.send_photo(chat_id=user.id, photo=photo_id, caption=message)
        except Exception as e:
            await update.message.reply_text(message + "\n\n⚠️ Фото не загрузилось")
    else:
        await update.message.reply_text(message)

# ---------- Команда /stats ----------
async def show_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT full_name, kills FROM players ORDER BY kills DESC LIMIT 10')
    top_players = cursor.fetchall()
    cursor.execute('SELECT COUNT(*) FROM players')
    total_players = cursor.fetchone()[0]
    cursor.execute('SELECT COUNT(*) FROM players WHERE is_alive = 1')
    alive_players = cursor.fetchone()[0]
    cursor.execute('SELECT COUNT(*) FROM kills')
    total_kills = cursor.fetchone()[0]
    game_state = await get_game_state()
    conn.close()
    message = "📊 Статистика игры:\n\n"
    message += f"Игра {'начата' if game_state['started'] else 'не начата'}\n"
    if game_state['started']:
        time_left = game_state['start_date'] + timedelta(days=GAME_DURATION_DAYS) - datetime.now()
        message += f"Осталось времени: {max(0, time_left.days)} дней\n"
    message += f"\nУчастников: {total_players}\n"
    message += f"Выживших: {alive_players}\n"
    message += f"Всего убийств: {total_kills}\n\n"
    message += "🏆 Топ игроков:\n"
    for i, (name, kills) in enumerate(top_players, 1):
        message += f"{i}. {name} - {kills} убийств\n"
    await update.message.reply_text(message)

# ---------- Логика убийства с индивидуальной наградой ----------
async def kill_target(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    game_state = await get_game_state()
    if not game_state['started']:
        await update.message.reply_text("Игра еще не началась!")
        return ConversationHandler.END

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT target_id, personal_code
        FROM targets t
        JOIN players p ON t.target_id = p.user_id
        WHERE t.hunter_id = ? AND t.is_active = 1
    ''', (user.id,))
    targets = cursor.fetchall()
    conn.close()
    if not targets:
        await update.message.reply_text("У тебя нет активных целей!")
        return ConversationHandler.END

    context.user_data['targets'] = targets
    await update.message.reply_text("Введи личный код жертвы, которую хочешь убить:")
    return KILL_CONFIRMATION

async def confirm_kill(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    entered_code = update.message.text.strip().upper()
    targets = context.user_data.get('targets', [])
    if not targets:
        await update.message.reply_text("Что-то пошло не так. Попробуй снова.")
        return ConversationHandler.END

    victim_id = None
    for target_id, code in targets:
        if code == entered_code:
            victim_id = target_id
            break

    if not victim_id:
        await update.message.reply_text("Неверный код! Попробуй еще раз.")
        return KILL_CONFIRMATION

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("SELECT full_name, is_alive, reward FROM players WHERE user_id = ?", (victim_id,))
    victim = cursor.fetchone()
    if not victim or not victim[1]:
        await update.message.reply_text("Эта цель уже мертва.")
        conn.close()
        return ConversationHandler.END

    victim_name = victim[0]
    victim_reward = victim[2] if victim[2] is not None else 1

    # Регистрируем убийство
    cursor.execute('INSERT INTO kills (hunter_id, victim_id, kill_code) VALUES (?, ?, ?)',
                   (user.id, victim_id, entered_code))
    cursor.execute('UPDATE players SET kills = kills + 1 WHERE user_id = ?', (user.id,))
    cursor.execute('UPDATE players SET is_alive = 0 WHERE user_id = ?', (victim_id,))
    cursor.execute('DELETE FROM targets WHERE hunter_id = ? AND target_id = ?', (user.id, victim_id))

    # Добавляем цель убитого (если есть)
    cursor.execute('SELECT target_id FROM targets WHERE hunter_id = ? AND is_active = 1 LIMIT 1', (victim_id,))
    new_target_row = cursor.fetchone()
    if new_target_row:
        new_target_id = new_target_row[0]
        cursor.execute('SELECT 1 FROM targets WHERE hunter_id = ? AND target_id = ? AND is_active = 1', (user.id, new_target_id))
        if not cursor.fetchone():
            cursor.execute('INSERT INTO targets (hunter_id, target_id, kill_code, is_active) VALUES (?, ?, ?, 1)',
                           (user.id, new_target_id, generate_kill_code()))

    conn.commit()
    conn.close()

    await update.message.reply_text(
        f"🎯 Ты успешно убил {victim_name}! Получена награда: {victim_reward}."
    )

    # Проверка окончания игры
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM players WHERE is_alive = 1")
    alive_count = cursor.fetchone()[0]
    conn.close()
    if alive_count <= 2:
        await end_game_logic(context.bot)

    return ConversationHandler.END

# ---------- MAIN ----------
def main():
    application = Application.builder().token(BOT_TOKEN).build()
    init_db()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('register', register)],
        states={
            FIO: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_fio)],
            COURSE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_course)],
            GROUP: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_group)],
            SOCIAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_social)],
            ABOUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_about)],
            BUILDINGS: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_buildings)],
            DORM: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_dorm)],
            PHOTO: [MessageHandler(filters.PHOTO, get_photo)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )

    kill_handler = ConversationHandler(
        entry_points=[CommandHandler('kill', kill_target)],
        states={
            KILL_CONFIRMATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, confirm_kill)],
        },
        fallbacks=[],
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(conv_handler)
    application.add_handler(kill_handler)
    application.add_handler(CommandHandler("target", show_target))
    application.add_handler(CommandHandler("stats", show_stats))
    application.add_handler(CommandHandler("me", show_me))

    # Админ-команды
    application.add_handler(CommandHandler("start_game", start_game))
    application.add_handler(CommandHandler("end_game", end_game))
    application.add_handler(CommandHandler("reset_game", reset_game))
    application.add_handler(CommandHandler("list_players", list_players))
    application.add_handler(CommandHandler("add_player", add_player))
    application.add_handler(CommandHandler("remove_player", remove_player))
    application.add_handler(CommandHandler("set_time", set_time))
    application.add_handler(CommandHandler("broadcast", broadcast))
    application.add_handler(CommandHandler("status", status))

    # Новые админ-команды
    application.add_handler(CommandHandler("view_profile", view_profile))
    application.add_handler(CommandHandler("show_targets", show_targets))
    application.add_handler(CommandHandler("armageddon", armageddon))
    application.add_handler(CommandHandler("edit_player", edit_player))
    application.add_handler(CommandHandler("set_global_reward", set_global_reward_command))
    application.add_handler(CommandHandler("set_reward_player", set_reward_player))
    application.add_handler(CommandHandler("set_danger", set_danger))
    application.add_handler(CommandHandler("remove_danger", remove_danger))

    # Пользовательские команды
    application.add_handler(CommandHandler("msg_killer", msg_killer))
    application.add_handler(CommandHandler("msg_target", msg_target))

    application.run_polling()

if __name__ == "__main__":
    main()
