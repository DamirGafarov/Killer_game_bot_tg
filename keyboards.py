"""Все клавиатуры и подписи кнопок. Кнопки — обычный текст, без слэшей."""

from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove

# --- Игровые кнопки ---
BTN_TARGET = "🎯 Моя цель"
BTN_KILL = "🔫 Убить жертву"
BTN_ME = "👤 Моё досье"
BTN_STATS = "📊 Статистика"
BTN_TOP = "🏆 Топ киллеров"
BTN_MSG_KILLER = "✉️ Письмо киллеру"
BTN_MSG_TARGET = "✉️ Письмо жертве"
BTN_MSG_ANY = "💰 Письмо игроку (1 очко)"
BTN_RULES = "📖 Правила"
BTN_HELP = "❓ Помощь"
BTN_LAST_WORDS = "🕯 Последнее слово"

# --- Регистрация ---
BTN_REGISTER = "📝 Зарегистрироваться"
BTN_CANCEL_REG = "🚫 Отменить регистрацию"
BTN_CANCEL_REG_YES = "✅ Да, удалить мою анкету"
BTN_CANCEL_REG_NO = "↩️ Нет, я остаюсь в игре"
BTN_BACK = "⬅ Назад"
BTN_CANCEL = "❌ Отмена"
BTN_SKIP = "⏭ Пропустить"

# --- Подписи для платного анонимного письма "любому игроку" ---
BTN_SIGN_KILLER = "🔫 От киллера"
BTN_SIGN_VICTIM = "💀 От жертвы"

# --- Админ ---
BTN_ADMIN = "🛠 Админ-панель"

ALL_MENU_BUTTONS = [
    BTN_TARGET, BTN_KILL, BTN_ME, BTN_STATS, BTN_TOP,
    BTN_MSG_KILLER, BTN_MSG_TARGET, BTN_MSG_ANY, BTN_RULES, BTN_HELP, BTN_LAST_WORDS,
    BTN_REGISTER, BTN_CANCEL_REG, BTN_ADMIN,
]


def kb(rows) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def remove_kb() -> ReplyKeyboardRemove:
    return ReplyKeyboardRemove()


def guest_menu(registration_open: bool = True) -> ReplyKeyboardMarkup:
    """Клавиатура для незарегистрированного пользователя."""
    rows = []
    if registration_open:
        rows.append([BTN_REGISTER])
    rows.append([BTN_RULES, BTN_STATS])
    rows.append([BTN_HELP])
    return kb(rows)


def lobby_menu(is_admin: bool = False) -> ReplyKeyboardMarkup:
    """Меню зарегистрированного игрока ДО старта игры.
    Кнопка отмены регистрации остаётся доступной."""
    rows = [
        [BTN_ME, BTN_RULES],
        [BTN_STATS, BTN_CANCEL_REG],
        [BTN_HELP],
    ]
    if is_admin:
        rows.append([BTN_ADMIN])
    return kb(rows)


def game_menu(is_admin: bool = False) -> ReplyKeyboardMarkup:
    """Меню живого игрока во время игры. Кладбища здесь нет намеренно —
    игроки не должны знать, кто выбыл."""
    rows = [
        [BTN_TARGET, BTN_KILL],
        [BTN_ME, BTN_STATS],
        [BTN_MSG_KILLER, BTN_MSG_TARGET],
        [BTN_MSG_ANY, BTN_TOP],
        [BTN_RULES, BTN_HELP],
    ]
    if is_admin:
        rows.append([BTN_ADMIN])
    return kb(rows)


def dead_menu(is_admin: bool = False) -> ReplyKeyboardMarkup:
    """Меню выбывшего игрока. Кладбища тоже нет — выбывший не должен
    видеть общий список погибших."""
    rows = [
        [BTN_STATS, BTN_TOP],
        [BTN_ME, BTN_LAST_WORDS],
        [BTN_RULES, BTN_HELP],
    ]
    if is_admin:
        rows.append([BTN_ADMIN])
    return kb(rows)


def registration_nav(with_skip: bool = False) -> ReplyKeyboardMarkup:
    rows = [[BTN_BACK, BTN_CANCEL]]
    if with_skip:
        rows.insert(0, [BTN_SKIP])
    return kb(rows)


def cancel_only() -> ReplyKeyboardMarkup:
    return kb([[BTN_CANCEL]])


def confirm_cancel_reg() -> ReplyKeyboardMarkup:
    return kb([[BTN_CANCEL_REG_YES], [BTN_CANCEL_REG_NO]])


def sign_choice() -> ReplyKeyboardMarkup:
    """Выбор подписи отправителя для платного анонимного письма."""
    return kb([[BTN_SIGN_KILLER, BTN_SIGN_VICTIM], [BTN_CANCEL]])
