"""
Локальный тест игровой логики (назначение целей, убийства, топ киллеров, письма)
без PostgreSQL. Подменяет функции модуля db на sqlite-совместимую реализацию.
Запуск: python test_flow.py
"""
import asyncio
import os
import re
import sqlite3

os.environ.setdefault("DATABASE_URL", "postgresql://x/y")
os.environ.setdefault("BOT_TOKEN", "1:x")
os.environ.setdefault("ADMIN_IDS", "999")

import db  # noqa: E402
import bot  # noqa: E402
import keyboards as K  # noqa: E402

CONN = sqlite3.connect(":memory:")
CONN.row_factory = sqlite3.Row


def tr(sql: str) -> str:
    sql = sql.replace("%s", "?")
    sql = sql.replace("BIGINT", "INTEGER").replace("SERIAL PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT")
    sql = sql.replace("TIMESTAMPTZ", "TIMESTAMP").replace("NOW()", "CURRENT_TIMESTAMP")
    sql = sql.replace("ILIKE", "LIKE").replace("NULLS LAST", "")
    sql = re.sub(r"INTERVAL '[^']+'", "'-1 day'", sql)
    sql = sql.replace("IS_ALIVE", "is_alive")
    return sql


def _cur(sql, params=()):
    c = CONN.cursor()
    c.execute(tr(sql), params)
    return c


db.fetch_one = lambda sql, params=(): _cur(sql, params).fetchone()
db.fetch_all = lambda sql, params=(): _cur(sql, params).fetchall()


def _fetch_val(sql, params=(), default=None):
    row = _cur(sql, params).fetchone()
    return row[0] if row else default


def _execute(sql, params=()):
    c = _cur(sql, params)
    CONN.commit()
    return c.rowcount


db.fetch_val = _fetch_val
db.execute = _execute
db.get_setting = lambda key, default=None: _fetch_val(
    "SELECT value FROM game_settings WHERE key = ?", (key,), default)


def _set_setting(key, value):
    _execute("INSERT INTO game_settings (key, value) VALUES (?, ?) "
             "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, str(value)))


db.set_setting = _set_setting
# TOP_KILLERS_SQL использует коррелированный подзапрос — sqlite это тоже понимает после tr()
bot.TOP_KILLERS_SQL = tr(bot.TOP_KILLERS_SQL)


def init():
    for stmt in db.SCHEMA:
        if "CREATE UNIQUE INDEX" in stmt:
            continue
        CONN.execute(tr(stmt))
    CONN.execute("CREATE UNIQUE INDEX t_pair ON targets(hunter_id, target_id) WHERE is_active")
    for k, v in db.DEFAULT_SETTINGS.items():
        _set_setting(k, v)
    CONN.commit()


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))

    async def send_photo(self, chat_id, photo, caption=None, **kw):
        self.sent.append((chat_id, "PHOTO:" + (caption or "")))


class FakeCtx:
    def __init__(self):
        self.bot = FakeBot()
        self.user_data = {}


async def main():
    init()
    names = {1: "Аня", 2: "Борис", 3: "Вера", 4: "Гоша", 5: "Дима"}
    for uid, name in names.items():
        _execute(
            "INSERT INTO players (user_id, full_name, personal_code, photo_id, reward) VALUES (?,?,?,?,?)",
            (uid, name, f"CODE{uid}", f"photo{uid}", uid),
        )
    ids = list(names)
    for i, h in enumerate(ids):
        _execute("INSERT INTO targets (hunter_id, target_id, kill_code, is_active) VALUES (?,?,?,1)",
                 (h, ids[(i + 1) % len(ids)], "X"))
    db.set_setting("game_started", "True")

    ctx = FakeCtx()
    assert bot.game_started()
    assert bot.alive_count() == 5

    # 1 убивает 2 -> должен унаследовать цель 3
    await bot.register_kill(ctx, hunter_id=1, victim_id=2, code="CODE2")
    hunter = bot.get_player(1)
    assert hunter["kills"] == 1, hunter["kills"]
    assert hunter["points"] == 2, hunter["points"]  # награда за игрока 2 == 2
    assert bot.get_player(2)["is_alive"] == 0
    t = db.fetch_all("SELECT target_id FROM targets WHERE hunter_id=1 AND is_active")
    assert [r["target_id"] for r in t] == [3], t
    assert bot.alive_count() == 4
    print("✓ убийство и наследование цели")

    # Килл-фид игрокам НЕ рассылается — они не должны знать, КТО выбыл (имя).
    # Сообщение с именем выбывшего допустимо только админу (id=999).
    # Сообщение «Твоя цель выбыла» без имени — нормально, его исключаем из проверки.
    player_msgs = [t for cid, t in ctx.bot.sent if cid != 999 and isinstance(t, str)]
    leak = [
        t for t in player_msgs
        if "выбыл" in t
        and "твоя цель" not in t.lower()
        and "тебя устранили" not in t.lower()
        and "ты выбыл" not in t.lower()
    ]
    assert not leak, f"игрокам не должно приходить имя выбывшего: {leak}"
    assert any("Тебя устранили" in t for _, t in ctx.bot.sent)
    admin_msgs_kill = [t for cid, t in ctx.bot.sent if cid == 999]
    assert any("Убийство" in t or "Выбыл" in t for t in admin_msgs_kill), "админ должен знать об убийстве"
    print("✓ килл-фид скрыт от игроков, жертва уведомлена лично, админ видит всё")

    # цепочка до 2 живых -> авто-финал
    await bot.register_kill(ctx, hunter_id=1, victim_id=3, code="CODE3")
    await bot.check_game_over(ctx)
    assert bot.alive_count() == 3 and bot.game_started()
    await bot.register_kill(ctx, hunter_id=1, victim_id=4, code="CODE4")
    await bot.check_game_over(ctx)
    assert bot.alive_count() == 2
    assert not bot.game_started(), "игра должна завершиться при 2 выживших"
    print("✓ автозавершение при двух выживших")

    # Топ киллеров: Аня (id=1) убила 3 раза, должна быть на первом месте
    top = db.fetch_all(bot.TOP_KILLERS_SQL, (5,))
    assert top[0]["full_name"] == "Аня" and top[0]["kills"] == 3, dict(top[0])
    print("✓ топ киллеров (сортировка по числу убийств):", dict(top[0]))

    # досье строятся без ошибок
    p = bot.get_player(1)
    assert "ТВОЯ ЦЕЛЬ" in bot.dossier_for_hunter(p)
    assert "ТВОЁ ДОСЬЕ" in bot.dossier_self(p)
    assert "ПОЛНАЯ КАРТОЧКА" in bot.dossier_admin(p)
    print("✓ тексты досье (охотник / игрок / админ)")

    # --- Платное письмо любому живому игроку ---
    # Аня (1) имеет 9 очков (2+3+4), пишет живому игроку 5 (Дима) за 1 очко
    balance_before = bot.get_player(1)["points"]
    ctx2 = FakeCtx()
    ctx2.user_data["msg_any_to"] = 5
    ctx2.user_data["msg_any_to_name"] = "Дима"
    ctx2.user_data["msg_any_sign"] = "killer"

    class FakeMsg:
        def __init__(self, text, sink):
            self.text = text
            self.photo = None
            self.caption = None
            self._sink = sink
        async def reply_text(self, text, **kw):
            self._sink.append(("reply", text))

    class FakeUpdate:
        def __init__(self, uid, text, sink):
            self.effective_user = type("U", (), {"id": uid})()
            self.message = FakeMsg(text, sink)

    replies2 = []
    upd = FakeUpdate(1, "Тайное сообщение", replies2)
    await bot.msg_any_send(upd, ctx2)
    balance_after = bot.get_player(1)["points"]
    assert balance_after == balance_before - 1, (balance_before, balance_after)
    assert any("Платное анонимное письмо" in t for _, t in ctx2.bot.sent)
    admin_msgs = [t for cid, t in ctx2.bot.sent if cid == 999]
    assert any("Копия платного письма" in t and "Аня" in t and "Дима" in t for t in admin_msgs), \
        "админ должен получить копию с реальными именами"
    print("✓ платное письмо: списание очка + доставка + копия админу с реальными именами")

    # Нехватка средств
    _execute("UPDATE players SET points = 0 WHERE user_id = 1")
    ctx3 = FakeCtx()
    ctx3.user_data["msg_any_to"] = 5
    ctx3.user_data["msg_any_to_name"] = "Дима"
    ctx3.user_data["msg_any_sign"] = "victim"
    replies3 = []
    upd3 = FakeUpdate(1, "Ещё письмо", replies3)
    await bot.msg_any_send(upd3, ctx3)
    assert any("Не хватает средств" in t for _, t in replies3)
    print("✓ при нехватке очков покупка блокируется с понятным сообщением")

    print("\nВСЕ ТЕСТЫ ПРОЙДЕНЫ")


if __name__ == "__main__":
    asyncio.run(main())
