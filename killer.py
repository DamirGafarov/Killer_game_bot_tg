import logging
import random
import sqlite3
from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    ConversationHandler,
    filters
)
import os
BOT_TOKEN = os.getenv("BOT_TOKEN", "8377571705:AAEO50McGhCsuWGgZnhhgmhKeoUeGCHrm8s")
ADMIN_ID = int(os.getenv("ADMIN_ID", "1513781380"))

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Состояния для регистрации
REGISTER, UPLOAD_PHOTO, ADD_HABITS = range(3)
KILL_CONFIRMATION = range(1)

# Настройки игры
GAME_DURATION_DAYS = 14
ADMIN_ID = 1513781380  # (узнать через @userinfobot)
FIO, COURSE, GROUP, SOCIAL, ABOUT, BUILDINGS, DORM, PHOTO = range(8)
# Инициализация базы данных
def init_db():
    conn = sqlite3.connect('killer_game.db')
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
        personal_code TEXT UNIQUE
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

    conn.commit()
    conn.close()

# Вспомогательная функция для получения состояния игры
async def get_game_state():
    conn = sqlite3.connect('killer_game.db')
    cursor = conn.cursor()

    cursor.execute("SELECT value FROM game_settings WHERE key='game_started'")
    started = cursor.fetchone()
    cursor.execute("SELECT value FROM game_settings WHERE key='game_start_date'")
    start_date = cursor.fetchone()

    conn.close()

    return {
        'started': started[0] == 'True' if started else False,
        'start_date': datetime.fromisoformat(start_date[0]) if start_date else None
    }

# Генерация кода для убийства
def generate_personal_code():
    return ''.join(random.choices('ABCDEFGHJKLMNPQRSTUVWXYZ23456789', k=6))

def generate_kill_code():
    return ''.join(random.choices('ABCDEFGHJKLMNPQRSTUVWXYZ23456789', k=6))
# ------------------------------------------------------------
# Обработчики команд
# ------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    game_state = await get_game_state()

    if game_state['started']:
        await update.message.reply_text(
            f"Игра уже началась! Ты опоздал, {user.first_name}.\n"
            "Но ты можешь следить за статистикой с помощью /stats"
        )
        return

    await update.message.reply_text(
    f"Привет, {user.first_name}!\n"
    "Это бот для игры 'Киллер'.\n\n"
    "1. Игра организуется на принципе честной игры! Каждый игрок обязуется соблюдать её правила. При их нарушении игрок выбрасывается из игры.\n\n"
    "2. Суть игры заключается в охоте за жертвой. Каждый участник является одновременно и охотником и жертвой.\n"
    "3. Игра начинается для всех одновременно! Вы получаете досье на свою жертву. В каждом досье находится фотография жертвы и краткое описание её привычек. Эта информация может помочь вам как охотнику выследить жертву. В то же самое время кто-то получает ваше досье и начинает охоту на вас.\n"
    "4. Жертва считается убитой, если охотник выстрелил в неё из пальца, находясь в закрытом помещении один на один, или на улице, где в радиусе 20 метров никого нет. Нельзя убивать при свидетелях - будь то участник игры или просто посторонний человек.\n"
    "5. После смерти жертва должна передать охотнику секретный пароль. Охотник должен ввести, полученный пароль в ТГ-бот и получить новую жертву.\n"
    "6. В случае если охотник и жертва охотятся друг на друга, они должны обратиться к организаторам для того, чтобы получить новую жертву.\n"
    "7. Игра заканчивается тогда, когда остаются только два участника. Либо вышло время, отведённое на игру. Побеждает охотник, который убил наибольшее количество жертв.\n\n"
    "Чтобы зарегистрироваться, используй /register"
)


async def register(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    game_state = await get_game_state()
    if game_state['started']:
        await update.message.reply_text("Регистрация закрыта, игра уже началась!")
        return ConversationHandler.END

    await update.message.reply_text(
        "📋 Регистрация в игре 'Киллер'.\n"
        "Пожалуйста, введи своё полное имя (ФИО или ФИ):"
    )
    return FIO

# 1. ФИО
async def get_fio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['full_name'] = update.message.text
    await update.message.reply_text("Введи свой курс (например, '3 курс', 'преподаватель' или др):")
    return COURSE

# 2. Курс
async def get_course(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['course'] = update.message.text
    await update.message.reply_text("Введи свою академическую группу (например эиф-103/6):")
    return GROUP

# 3. Группа
async def get_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['academic_group'] = update.message.text
    await update.message.reply_text(
        "Укажи ссылки на свои соцсети (ВК, Telegram)\n"
    )
    return SOCIAL

# 4. Соцсети
async def get_social(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['social_links'] = update.message.text
    await update.message.reply_text(
        "Расскажи немного о себе: где ты обычно обитаешь, твой примерный маршрут на день, любимые места.\n"
        "Это поможет охотнику тебя найти."
    )
    return ABOUT

# 5. О себе
async def get_about(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['about_self'] = update.message.text
    await update.message.reply_text(
        "В каких корпусах у тебя обычно проходят пары?"
    )
    return BUILDINGS

# 6. Корпуса
async def get_buildings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['buildings'] = update.message.text
    await update.message.reply_text(
        "Ты живешь в общаге? Если да, укажи корпус.\n"
        "Если нет, напиши 'нет' или укажи примерный район проживания."
    )
    return DORM

# 7. Общежитие
async def get_dorm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['dormitory'] = update.message.text
    await update.message.reply_text(
        "Теперь загрузи своё фото (оно будет в досье для охотника)."
    )
    return PHOTO

# 8. Фото и завершение регистрации
async def get_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    photo_file = await update.message.photo[-1].get_file()
    context.user_data['photo_id'] = photo_file.file_id

    # Генерация личного кода
    personal_code = generate_personal_code()

    # Сохранение в БД
    conn = sqlite3.connect('killer_game.db')
    cursor = conn.cursor()

    # Проверка на повторную регистрацию
    cursor.execute("SELECT user_id FROM players WHERE user_id = ?", (update.effective_user.id,))
    if cursor.fetchone():
        await update.message.reply_text("Ты уже зарегистрирован! Если хочешь обновить данные, сначала обратись к администратору.")
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

    # Отправка подтверждения
    await update.message.reply_text(
        f"✅ Регистрация завершена! Ты в игре.\n"
        f"🔐 Твой личный секретный код: {personal_code}\n\n"
        "Запомни его! Ты должен будешь передать его охотнику, если он тебя убьёт.\n"
        "Ожидай начала. Когда игра начнется, ты получишь свою первую цель.\n\n"
        "Ты можешь проверить свои данные с помощью /me"
    )

    # Уведомление админу
    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"📝 Новый участник!\nИмя: {context.user_data['full_name']}\nГруппа: {context.user_data['academic_group']}\nКод: {personal_code}\nID: {update.effective_user.id}"
        )
    except Exception as e:
        logger.error(f"Не удалось отправить уведомление админу: {e}")

    return ConversationHandler.END

# Функция отмены
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Регистрация отменена.")
    return ConversationHandler.END

# Обновлённая команда /target – показывает полное досье
async def show_target(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    game_state = await get_game_state()

    if not game_state['started']:
        await update.message.reply_text("Игра еще не началась!")
        return

    conn = sqlite3.connect('killer_game.db')
    cursor = conn.cursor()

    cursor.execute('''
        SELECT p.full_name, p.course, p.academic_group, p.social_links, p.about_self,
               p.buildings, p.dormitory, p.photo_id
        FROM targets t
        JOIN players p ON t.target_id = p.user_id
        WHERE t.hunter_id = ? AND t.is_active = 1
    ''', (user.id,))

    target = cursor.fetchone()
    if not target:
        await update.message.reply_text("У тебя нет активной цели.")
        conn.close()
        return

    (full_name, course, group, social, about, buildings, dorm, photo_id) = target

    target_info = (
        f"🔫 **Твоя цель:**\n\n"
        f"**Имя:** {full_name}\n"
        f"**Курс:** {course}\n"
        f"**Группа:** {group}\n"
        f"**Соцсети:** {social}\n"
        f"**О себе:** {about}\n"
        f"**Корпуса:** {buildings}\n"
        f"**Общежитие/район:** {dorm}\n\n"
        f"Когда встретишь цель, она должна сообщить тебе свой личный код.\n"
        f"Введи его командой /kill после убийства."
    )

    await context.bot.send_photo(
        chat_id=user.id,
        photo=photo_id,
        caption=target_info
    )
    conn.close()

# ------------------------------------------------------------
# Административные команды
# ------------------------------------------------------------

async def start_game(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Эта команда только для организаторов!")
        return

    conn = sqlite3.connect('killer_game.db')
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
            '''
            INSERT INTO targets (hunter_id, target_id, kill_code)
            VALUES (?, ?, ?)
            ''',
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

    # Оповещаем всех игроков
    for player_id in players:
        try:
            await context.bot.send_message(
                chat_id=player_id,
                text="🎮 Игра началась! Твоя цель уже ждет тебя. Используй /target чтобы увидеть свою цель!"
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
    conn = sqlite3.connect('killer_game.db')
    cursor = conn.cursor()

    cursor.execute('''
    SELECT user_id, full_name, kills FROM players WHERE is_alive = 1 ORDER BY kills DESC
    ''')
    winners = cursor.fetchall()

    cursor.execute('''
    UPDATE game_settings SET value = 'False' WHERE key = 'game_started'
    ''')
    conn.commit()
    conn.close()

    message = "🏆 Игра окончена! Результаты:\n\n"
    for i, (user_id, name, kills) in enumerate(winners, 1):
        message += f"{i}. {name} - {kills} убийств\n"
        try:
            await bot.send_message(
                chat_id=user_id,
                text=f"🎉 Поздравляем! Ты в топ-{i}!\n"
                     f"Твой результат: {kills} убийств."
            )
        except Exception as e:
            logger.error(f"Не удалось отправить сообщение победителю {user_id}: {e}")

    try:
        await bot.send_message(
            chat_id=ADMIN_ID,
            text=message
        )
    except Exception as e:
        logger.error(f"Не удалось отправить результаты админу: {e}")

async def reset_game(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Эта команда только для организаторов!")
        return

    conn = sqlite3.connect('killer_game.db')
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

    conn = sqlite3.connect('killer_game.db')
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

async def assign_targets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Эта команда только для организаторов!")
        return

    conn = sqlite3.connect('killer_game.db')
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM players WHERE is_alive=1")
    players = [row[0] for row in cursor.fetchall()]

    if len(players) < 2:
        await update.message.reply_text("Недостаточно живых игроков для назначения целей.")
        conn.close()
        return

    # Удаляем старые активные цели
    cursor.execute("DELETE FROM targets")
    random.shuffle(players)

    for i in range(len(players)):
        hunter = players[i]
        target = players[(i + 1) % len(players)]
        kill_code = generate_kill_code()
        cursor.execute(
            "INSERT INTO targets (hunter_id, target_id, kill_code) VALUES (?, ?, ?)",
            (hunter, target, kill_code)
        )

    conn.commit()
    conn.close()
    await update.message.reply_text("Цели переназначены.")

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

    conn = sqlite3.connect('killer_game.db')
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

    conn = sqlite3.connect('killer_game.db')
    cursor = conn.cursor()

    cursor.execute("SELECT full_name FROM players WHERE user_id = ?", (user_id,))
    player = cursor.fetchone()
    if not player:
        await update.message.reply_text(f"Игрок с ID {user_id} не найден.")
        conn.close()
        return
    player_name = player[0]

    # Цель удаляемого
    cursor.execute("SELECT target_id FROM targets WHERE hunter_id = ? AND is_active = 1", (user_id,))
    old_target_row = cursor.fetchone()
    old_target_id = old_target_row[0] if old_target_row else None

    # Охотник на удаляемого
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
            await update.message.reply_text(f"⚠️ Цель {old_target_id} мертва. Используйте /assign_targets.")
    elif hunter_id and not old_target_id:
        await update.message.reply_text(f"⚠️ У удалённого не было цели. Охотник {hunter_id} без цели. Используйте /assign_targets.")
    elif not hunter_id and old_target_id:
        await update.message.reply_text(f"⚠️ На удалённого никто не охотился. Цель {old_target_id} без охотника. Используйте /assign_targets.")
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

    global GAME_DURATION_DAYS   # ← объявление глобальной переменной в начале

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

    conn = sqlite3.connect('killer_game.db')
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
    conn = sqlite3.connect('killer_game.db')
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

    await update.message.reply_text(text, parse_mode='Markdown')

# ------------------------------------------------------------
# Игровые команды
# ------------------------------------------------------------


async def kill_target(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    game_state = await get_game_state()

    if not game_state['started']:
        await update.message.reply_text("Игра еще не началась!")
        return ConversationHandler.END

    conn = sqlite3.connect('killer_game.db')
    cursor = conn.cursor()

    # Проверяем, есть ли у охотника активная цель
    cursor.execute('''
    SELECT target_id FROM targets 
    WHERE hunter_id = ? AND is_active = 1
    ''', (user.id,))
    target = cursor.fetchone()

    if not target:
        await update.message.reply_text("У тебя нет активной цели!")
        conn.close()
        return ConversationHandler.END

    context.user_data['target_id'] = target[0]
    await update.message.reply_text(
        "Введи личный код, который тебе сообщила жертва:"
    )
    return KILL_CONFIRMATION

async def confirm_kill(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    entered_code = update.message.text.strip().upper()
    target_id = context.user_data.get('target_id')

    if not target_id:
        await update.message.reply_text("Что-то пошло не так. Попробуй снова.")
        return ConversationHandler.END

    conn = sqlite3.connect('killer_game.db')
    cursor = conn.cursor()

    # Проверяем, что цель жива и код совпадает
    cursor.execute('''
    SELECT full_name, personal_code, is_alive FROM players WHERE user_id = ?
    ''', (target_id,))
    victim = cursor.fetchone()

    if not victim or not victim[2]:
        await update.message.reply_text("Эта цель уже мертва или не существует.")
        conn.close()
        return ConversationHandler.END

    victim_name, correct_code, is_alive = victim
    if entered_code != correct_code:
        await update.message.reply_text("Неверный код! Попробуй еще раз.")
        conn.close()
        return KILL_CONFIRMATION

    # Код верный — регистрируем убийство
    # 1. Записываем убийство
    cursor.execute('''
    INSERT INTO kills (hunter_id, victim_id, kill_code)
    VALUES (?, ?, ?)
    ''', (user.id, target_id, correct_code))

    # 2. Увеличиваем счётчик убийств охотника
    cursor.execute('''
    UPDATE players SET kills = kills + 1 WHERE user_id = ?
    ''', (user.id,))

    # 3. Помечаем жертву мёртвой
    cursor.execute('''
    UPDATE players SET is_alive = 0 WHERE user_id = ?
    ''', (target_id,))

    # 4. Деактивируем текущую цель охотника
    cursor.execute('''
    UPDATE targets SET is_active = 0 WHERE hunter_id = ? AND target_id = ?
    ''', (user.id, target_id))

    # 5. Находим цель убитого (новую цель для охотника)
    cursor.execute('''
    SELECT target_id, kill_code FROM targets 
    WHERE hunter_id = ? AND is_active = 1
    ''', (target_id,))

    new_target = cursor.fetchone()

    if new_target:
        new_target_id, new_kill_code = new_target
        cursor.execute('''
        INSERT INTO targets (hunter_id, target_id, kill_code)
        VALUES (?, ?, ?)
        ''', (user.id, new_target_id, new_kill_code))

        # Деактивируем старую цель убитого
        cursor.execute('''
        UPDATE targets SET is_active = 0 WHERE hunter_id = ? AND target_id = ?
        ''', (target_id, new_target_id))

    conn.commit()

    # Уведомляем жертву
    try:
        await context.bot.send_message(
            chat_id=target_id,
            text=f"💀 Ты был убит игроком {user.full_name}! Игра для тебя окончена."
        )
    except Exception as e:
        logger.error(f"Не удалось отправить сообщение убитому игроку {target_id}: {e}")

    # Отвечаем охотнику
    if new_target:
        cursor.execute('''
        SELECT full_name, photo_id FROM players WHERE user_id = ?
        ''', (new_target_id,))
        target_info = cursor.fetchone()

        await update.message.reply_text(
            f"🎯 Ты успешно убил {victim_name}!\n"
            f"Теперь твоя новая цель: {target_info[0]}\n"
            "Используй /target чтобы увидеть досье."
        )

        await context.bot.send_photo(
            chat_id=user.id,
            photo=target_info[1],
            caption=f"Твоя новая цель: {target_info[0]}"
        )
    else:
        await update.message.reply_text(
            f"🎯 Ты успешно убил {victim_name}!\n"
            "Кажется, ты последний выживший! Поздравляю с победой!"
        )

    # Проверяем окончание игры
    cursor.execute('''
    SELECT COUNT(*) FROM players WHERE is_alive = 1
    ''')
    alive_count = cursor.fetchone()[0]

    if alive_count <= 2:
        await end_game_logic(context.bot)

    conn.close()
    return ConversationHandler.END

async def show_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    conn = sqlite3.connect('killer_game.db')
    cursor = conn.cursor()

    cursor.execute('''
    SELECT full_name, kills FROM players ORDER BY kills DESC LIMIT 10
    ''')
    top_players = cursor.fetchall()

    cursor.execute('''
    SELECT COUNT(*) FROM players
    ''')
    total_players = cursor.fetchone()[0]

    cursor.execute('''
    SELECT COUNT(*) FROM players WHERE is_alive = 1
    ''')
    alive_players = cursor.fetchone()[0]

    cursor.execute('''
    SELECT COUNT(*) FROM kills
    ''')
    total_kills = cursor.fetchone()[0]

    game_state = await get_game_state()

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
    conn.close()

async def show_me(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user

    conn = sqlite3.connect('killer_game.db')
    cursor = conn.cursor()

    cursor.execute('''
        SELECT full_name, course, academic_group, social_links, about_self, buildings, dormitory, photo_id, is_alive, kills, personal_code
        FROM players WHERE user_id = ?
    ''', (user.id,))

    player = cursor.fetchone()

    if not player:
        await update.message.reply_text("Ты не зарегистрирован в игре. Используй /register")
        conn.close()
        return

    (full_name, course, group, social, about, buildings, dorm, photo_id, is_alive, kills, personal_code) = player

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
        f"Твой личный код: ||{personal_code}||\n\n"
    )
    if is_alive:
        message += "Будь осторожен, за тобой могут охотиться!"
    else:
        message += "Ты уже мёртв в этой игре. Жди следующей!"

    await context.bot.send_photo(
        chat_id=user.id,
        photo=photo_id,
        caption=message,
        parse_mode='Markdown'
    )

    conn.close()

# ------------------------------------------------------------
# Основная функция
# ------------------------------------------------------------
def main() -> None:
    # Создаём Application
    application = Application.builder().token(BOT_TOKEN).build()

    # Инициализация базы данных
    init_db()

    # Регистрация
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

    # Обработчик убийства
    kill_handler = ConversationHandler(
        entry_points=[CommandHandler('kill', kill_target)],
        states={
            KILL_CONFIRMATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, confirm_kill)],
        },
        fallbacks=[],
    )

    # Регистрация обработчиков
    application.add_handler(CommandHandler("start", start))
    application.add_handler(conv_handler)
    application.add_handler(kill_handler)
    application.add_handler(CommandHandler("target", show_target))
    application.add_handler(CommandHandler("stats", show_stats))
    application.add_handler(CommandHandler("me", show_me))

    # Административные команды
    application.add_handler(CommandHandler("start_game", start_game))
    application.add_handler(CommandHandler("end_game", end_game))
    application.add_handler(CommandHandler("reset_game", reset_game))
    application.add_handler(CommandHandler("list_players", list_players))
    application.add_handler(CommandHandler("assign_targets", assign_targets))
    application.add_handler(CommandHandler("add_player", add_player))
    application.add_handler(CommandHandler("remove_player", remove_player))
    application.add_handler(CommandHandler("set_time", set_time))
    application.add_handler(CommandHandler("broadcast", broadcast))
    application.add_handler(CommandHandler("status", status))

    # Запуск бота
    application.run_polling()

if __name__ == '__main__':
    main()