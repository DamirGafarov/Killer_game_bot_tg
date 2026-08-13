"""
Локальный тест игровой логики (назначение целей, убийства) без PostgreSQL.
Подменяет функции модуля db на sqlite-совместимую реализацию.
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

    # цепочка до 2 живых -> авто-финал
    await bot.register_kill(ctx, hunter_id=1, victim_id=3, code="CODE3")
    await bot.check_game_over(ctx)
    assert bot.alive_count() == 3 and bot.game_started()
    await bot.register_kill(ctx, hunter_id=1, victim_id=4, code="CODE4")
    await bot.check_game_over(ctx)
    assert bot.alive_count() == 2
    assert not bot.game_started(), "игра должна завершиться при 2 выживших"
    print("✓ автозавершение при двух выживших")

    top = db.fetch_all("SELECT full_name, points FROM players ORDER BY points DESC LIMIT 1")
    assert top[0]["full_name"] == "Аня" and top[0]["points"] == 9, dict(top[0])
    print("✓ очки:", dict(top[0]))

    # досье строятся без ошибок
    p = bot.get_player(1)
    assert "ТВОЯ ЦЕЛЬ" in bot.dossier_for_hunter(p)
    assert "ТВОЁ ДОСЬЕ" in bot.dossier_self(p)
    assert "ПОЛНАЯ КАРТОЧКА" in bot.dossier_admin(p)
    print("✓ тексты досье (охотник / игрок / админ)")

    # kill-feed и уведомления ушли
    assert any("Ещё один выбыл" in t for _, t in ctx.bot.sent)
    assert any("Тебя устранили" in t for _, t in ctx.bot.sent)
    print("✓ уведомления: килл-фид, жертва, админ")
    print("\nВСЕ ТЕСТЫ ПРОЙДЕНЫ")


if __name__ == "__main__":
    asyncio.run(main())
