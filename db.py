"""
Слой работы с PostgreSQL (psycopg2) для игры «Киллер».
Полностью заменяет sqlite3.
"""

import os
import logging
from contextlib import contextmanager

import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Подключение
# ---------------------------------------------------------------------------
# Railway автоматически подставляет DATABASE_URL, если в проекте есть PostgreSQL.
# Локально можно положить её в переменные окружения или .env
DATABASE_URL = (
    os.getenv("DATABASE_URL")
    or os.getenv("POSTGRES_URL")
    or os.getenv("DATABASE_PUBLIC_URL")
)

if not DATABASE_URL:
    raise RuntimeError(
        "Не найдена переменная окружения DATABASE_URL. "
        "На Railway добавьте плагин PostgreSQL и переменную "
        "DATABASE_URL=${{Postgres.DATABASE_URL}} в сервис бота."
    )

# Railway иногда отдаёт postgres:// — psycopg2 хочет postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

_POOL: ThreadedConnectionPool | None = None


def init_pool(minconn: int = 1, maxconn: int = 10) -> None:
    global _POOL
    if _POOL is None:
        _POOL = ThreadedConnectionPool(
            minconn,
            maxconn,
            dsn=DATABASE_URL,
            connect_timeout=10,
            keepalives=1,
            keepalives_idle=30,
            keepalives_interval=10,
            keepalives_count=5,
        )
        logger.info("Пул подключений к PostgreSQL создан")


def close_pool() -> None:
    global _POOL
    if _POOL is not None:
        _POOL.closeall()
        _POOL = None


@contextmanager
def get_conn():
    """Выдаёт соединение из пула и возвращает его обратно."""
    if _POOL is None:
        init_pool()
    conn = _POOL.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _POOL.putconn(conn)


@contextmanager
def get_cursor(dict_cursor: bool = True):
    """
    Контекстный менеджер курсора.
    dict_cursor=True -> обращение к полям по имени: row["full_name"].
    """
    with get_conn() as conn:
        factory = psycopg2.extras.RealDictCursor if dict_cursor else None
        cur = conn.cursor(cursor_factory=factory)
        try:
            yield cur
        finally:
            cur.close()


# ---------------------------------------------------------------------------
# Удобные хелперы (сахар вместо ручного открытия курсора)
# ---------------------------------------------------------------------------
def fetch_one(sql: str, params: tuple = ()):
    with get_cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def fetch_all(sql: str, params: tuple = ()):
    with get_cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def fetch_val(sql: str, params: tuple = (), default=None):
    with get_cursor(dict_cursor=False) as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
        return row[0] if row else default


def execute(sql: str, params: tuple = ()) -> int:
    """Возвращает количество затронутых строк."""
    with get_cursor(dict_cursor=False) as cur:
        cur.execute(sql, params)
        return cur.rowcount


def execute_returning(sql: str, params: tuple = ()):
    with get_cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


# ---------------------------------------------------------------------------
# Схема
# ---------------------------------------------------------------------------
SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS players (
        user_id           BIGINT PRIMARY KEY,
        username          TEXT,
        full_name         TEXT,
        faculty           TEXT DEFAULT '',
        course            TEXT,
        academic_group    TEXT,
        social_links      TEXT,
        about_self        TEXT,
        buildings         TEXT,
        dormitory         TEXT,
        photo_id          TEXT,
        habits            TEXT DEFAULT '',
        hint              TEXT DEFAULT '',
        is_alive          BOOLEAN NOT NULL DEFAULT TRUE,
        registration_date TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        death_date        TIMESTAMPTZ,
        kills             INTEGER NOT NULL DEFAULT 0,
        points            INTEGER NOT NULL DEFAULT 0,
        personal_code     TEXT UNIQUE,
        reward            INTEGER NOT NULL DEFAULT 1,
        is_danger         BOOLEAN NOT NULL DEFAULT FALSE,
        danger_reason     TEXT NOT NULL DEFAULT '',
        last_words        TEXT DEFAULT '',
        killed_by         BIGINT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS targets (
        id            SERIAL PRIMARY KEY,
        hunter_id     BIGINT NOT NULL REFERENCES players(user_id) ON DELETE CASCADE,
        target_id     BIGINT NOT NULL REFERENCES players(user_id) ON DELETE CASCADE,
        assigned_date TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        kill_code     TEXT,
        is_active     BOOLEAN NOT NULL DEFAULT TRUE
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS targets_active_pair_idx
        ON targets (hunter_id, target_id) WHERE is_active
    """,
    """
    CREATE TABLE IF NOT EXISTS kills (
        kill_id   SERIAL PRIMARY KEY,
        hunter_id BIGINT REFERENCES players(user_id) ON DELETE SET NULL,
        victim_id BIGINT REFERENCES players(user_id) ON DELETE SET NULL,
        kill_date TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        kill_code TEXT,
        points    INTEGER NOT NULL DEFAULT 1
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS game_settings (
        key   TEXT PRIMARY KEY,
        value TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS anon_messages (
        id         SERIAL PRIMARY KEY,
        from_id    BIGINT,
        to_id      BIGINT,
        direction  TEXT,
        body       TEXT,
        sent_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
]

DEFAULT_SETTINGS = {
    "reward": "1",
    "game_started": "False",
    "game_start_date": "",
    "game_duration_days": "14",
    "registration_open": "True",
    "killfeed": "True",
}


def init_db() -> None:
    init_pool()
    with get_cursor(dict_cursor=False) as cur:
        for stmt in SCHEMA:
            cur.execute(stmt)
        # Мягкие миграции: если таблица создавалась ранней версией — добавим колонки
        migrations = [
            "ALTER TABLE players ADD COLUMN IF NOT EXISTS hint TEXT DEFAULT ''",
            "ALTER TABLE players ADD COLUMN IF NOT EXISTS points INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE players ADD COLUMN IF NOT EXISTS last_words TEXT DEFAULT ''",
            "ALTER TABLE players ADD COLUMN IF NOT EXISTS killed_by BIGINT",
            "ALTER TABLE players ADD COLUMN IF NOT EXISTS death_date TIMESTAMPTZ",
            "ALTER TABLE players ADD COLUMN IF NOT EXISTS habits TEXT DEFAULT ''",
            "ALTER TABLE kills ADD COLUMN IF NOT EXISTS points INTEGER NOT NULL DEFAULT 1",
        ]
        for stmt in migrations:
            try:
                cur.execute(stmt)
            except Exception as e:  # pragma: no cover
                logger.warning("Миграция пропущена (%s): %s", stmt, e)

        for key, value in DEFAULT_SETTINGS.items():
            cur.execute(
                "INSERT INTO game_settings (key, value) VALUES (%s, %s) "
                "ON CONFLICT (key) DO NOTHING",
                (key, value),
            )
    logger.info("Схема БД инициализирована")


# ---------------------------------------------------------------------------
# Настройки игры
# ---------------------------------------------------------------------------
def get_setting(key: str, default: str | None = None) -> str | None:
    val = fetch_val("SELECT value FROM game_settings WHERE key = %s", (key,))
    return val if val is not None else default


def set_setting(key: str, value: str) -> None:
    execute(
        "INSERT INTO game_settings (key, value) VALUES (%s, %s) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (key, str(value)),
    )
