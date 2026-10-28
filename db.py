"""Quản lý cơ sở dữ liệu SQLite bất đồng bộ (ACID Transaction) cho hệ thống Rút Ví Trả Sau Shopee (SPayLater)."""
import asyncio
from contextlib import asynccontextmanager
import aiosqlite

from config import (
    ADMIN_GROUP_ID,
    DB_PATH,
    DEFAULT_FEE_PERCENT,
    GARENA_ACCOUNT,
    GARENA_PASSWORD,
)

_conn: aiosqlite.Connection | None = None
_tx_lock = asyncio.Lock()

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;

CREATE TABLE IF NOT EXISTS users (
    telegram_id         BIGINT PRIMARY KEY,
    username            VARCHAR(64) DEFAULT '',
    full_name           VARCHAR(128) DEFAULT '',
    balance             BIGINT DEFAULT 0,
    bank_name           VARCHAR(64) DEFAULT '',
    bank_account_number VARCHAR(64) DEFAULT '',
    bank_account_name   VARCHAR(128) DEFAULT '',
    total_withdrawn     BIGINT DEFAULT 0,
    total_orders        INT DEFAULT 0,
    is_banned           BOOLEAN DEFAULT 0,
    custom_fee          FLOAT DEFAULT NULL,
    created_at          TIMESTAMP DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS orders (
    order_id            VARCHAR(16) PRIMARY KEY,
    telegram_id         BIGINT NOT NULL REFERENCES users(telegram_id),
    amount_face         BIGINT NOT NULL,
    fee_percent         FLOAT NOT NULL,
    amount_received     BIGINT NOT NULL,
    so_expected         BIGINT DEFAULT 0,
    so_before           BIGINT DEFAULT 0,
    so_after            BIGINT DEFAULT 0,
    so_verified         INT DEFAULT 0,
    so_notified_admin   INT DEFAULT 0,
    shopee_qr_url       TEXT DEFAULT '',
    bill_image_file_id  TEXT DEFAULT '',
    bank_info_text      TEXT DEFAULT '',
    bank_code           VARCHAR(32) DEFAULT '',
    bank_account        VARCHAR(64) DEFAULT '',
    bank_holder         VARCHAR(128) DEFAULT '',
    status              VARCHAR(32) DEFAULT 'WAITING_SCAN',
    reject_reason       TEXT DEFAULT '',
    qr_chat_id          BIGINT DEFAULT 0,
    qr_msg_id           BIGINT DEFAULT 0,
    expired_at          TIMESTAMP NOT NULL,
    created_at          TIMESTAMP DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS withdrawals (
    wd_id               VARCHAR(16) PRIMARY KEY,
    telegram_id         BIGINT NOT NULL REFERENCES users(telegram_id),
    amount              BIGINT NOT NULL,
    bank_info_text      TEXT DEFAULT '',
    bank_code           VARCHAR(32) DEFAULT '',
    bank_account        VARCHAR(64) DEFAULT '',
    bank_holder         VARCHAR(128) DEFAULT '',
    status              VARCHAR(32) DEFAULT 'PENDING',
    reject_reason       TEXT DEFAULT '',
    created_at          TIMESTAMP DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_orders_user_status ON orders(telegram_id, status);
CREATE INDEX IF NOT EXISTS idx_orders_status_exp ON orders(status, expired_at);
CREATE INDEX IF NOT EXISTS idx_withdrawals_user_status ON withdrawals(telegram_id, status);

CREATE TABLE IF NOT EXISTS system_config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


async def get_db() -> aiosqlite.Connection:
    global _conn
    if _conn is None:
        _conn = await aiosqlite.connect(DB_PATH)
        _conn.row_factory = aiosqlite.Row
        await _conn.executescript(SCHEMA)
        await _conn.commit()
    return _conn


async def init_db():
    conn = await get_db()
    # Tự động nâng cấp bảng users và orders nếu DB cũ chưa có cột mới
    user_cols = [
        ("balance", "BIGINT DEFAULT 0"),
    ]
    for col_name, col_def in user_cols:
        try:
            await conn.execute(f"ALTER TABLE users ADD COLUMN {col_name} {col_def}")
        except Exception:
            pass

    order_cols = [
        ("so_expected", "BIGINT DEFAULT 0"),
        ("so_before", "BIGINT DEFAULT 0"),
        ("so_after", "BIGINT DEFAULT 0"),
        ("so_verified", "INT DEFAULT 0"),
        ("so_notified_admin", "INT DEFAULT 0"),
    ]
    for col_name, col_def in order_cols:
        try:
            await conn.execute(f"ALTER TABLE orders ADD COLUMN {col_name} {col_def}")
        except Exception:
            pass

    defaults = {
        "default_fee_percent": str(DEFAULT_FEE_PERCENT),
        "garena_account": GARENA_ACCOUNT,
        "garena_password": GARENA_PASSWORD,
        "current_so_balance": "0",
        "auto_verify_so_on_bill": "1",
        "is_maintenance": "0",
        "admin_group_id": str(ADMIN_GROUP_ID),
        "crawler_mode": "auto",
    }
    for k, v in defaults.items():
        await conn.execute(
            "INSERT OR IGNORE INTO system_config(key, value) VALUES (?, ?)",
            (k, str(v)),
        )
    await conn.execute(
        "UPDATE system_config SET value=? WHERE key='garena_account' AND (value='' OR value IS NULL)",
        (GARENA_ACCOUNT,),
    )
    await conn.commit()


async def close_db():
    global _conn
    if _conn is not None:
        await _conn.close()
        _conn = None


@asynccontextmanager
async def tx():
    """ACID Transaction (BEGIN IMMEDIATE) tránh Race Condition."""
    conn = await get_db()
    async with _tx_lock:
        await conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise


async def fetchone(sql: str, params: tuple = ()) -> dict | None:
    conn = await get_db()
    async with conn.execute(sql, params) as cur:
        row = await cur.fetchone()
        return dict(row) if row else None


async def fetchall(sql: str, params: tuple = ()) -> list[dict]:
    conn = await get_db()
    async with conn.execute(sql, params) as cur:
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def fetchval(sql: str, params: tuple = (), default=None):
    conn = await get_db()
    async with conn.execute(sql, params) as cur:
        row = await cur.fetchone()
        return row[0] if row and row[0] is not None else default


async def execute(sql: str, params: tuple = ()) -> int:
    conn = await get_db()
    async with conn.execute(sql, params) as cur:
        await conn.commit()
        return cur.lastrowid
