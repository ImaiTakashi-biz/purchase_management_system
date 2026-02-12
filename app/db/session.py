from pathlib import Path
from typing import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import Base

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "purchase.db"
SQLALCHEMY_DATABASE_URL = f"sqlite:///{DB_PATH}"

engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={"check_same_thread": False},
    future=True,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine, future=True)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _table_exists(conn: Connection, table_name: str) -> bool:
    row = conn.execute(
        text("SELECT name FROM sqlite_master WHERE type='table' AND name = :name"),
        {"name": table_name},
    ).fetchone()
    return row is not None


def _table_columns(conn: Connection, table_name: str) -> list[str]:
    if not _table_exists(conn, table_name):
        return []
    rows = conn.execute(text(f"PRAGMA table_info({table_name})")).fetchall()
    return [row[1] for row in rows]


def _ensure_column(conn: Connection, table_name: str, column_name: str, ddl: str) -> None:
    columns = _table_columns(conn, table_name)
    if column_name in columns:
        return
    conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {ddl}"))


def _migrate_legacy_purchase_order_tables(conn: Connection) -> None:
    legacy_tables = conn.execute(
        text(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND ("
            "name LIKE 'purchase_orders_legacy_%' OR "
            "name LIKE 'purchase_order_lines_legacy_%' OR "
            "name LIKE 'purchase_order_histories_legacy_%'"
            ")"
        )
    ).fetchall()
    for row in legacy_tables:
        conn.execute(text(f"DROP TABLE IF EXISTS {row[0]}"))

    if not _table_exists(conn, "purchase_orders"):
        return

    columns = set(_table_columns(conn, "purchase_orders"))
    required_columns = {"supplier_id", "department", "ordered_by_user", "status", "issued_date"}
    legacy_markers = {"order_number", "expected_date"}
    is_legacy = bool(columns & legacy_markers) or not required_columns.issubset(columns)

    if not is_legacy:
        return

    for table_name in ("purchase_order_lines", "purchase_order_histories", "purchase_orders"):
        if _table_exists(conn, table_name):
            conn.execute(text(f"DROP TABLE IF EXISTS {table_name}"))


def init_db() -> None:
    with engine.connect() as conn:
        _migrate_legacy_purchase_order_tables(conn)
        conn.commit()

    Base.metadata.create_all(bind=engine)

    with engine.connect() as conn:
        _ensure_column(conn, "items", "management_type", "VARCHAR(32)")
        _ensure_column(conn, "items", "default_order_quantity", "INTEGER NOT NULL DEFAULT 1")
        _ensure_column(conn, "items", "unit_price", "INTEGER")
        _ensure_column(conn, "items", "account_name", "VARCHAR(128)")
        _ensure_column(conn, "items", "expense_item_name", "VARCHAR(128)")
        _ensure_column(conn, "suppliers", "mobile_number", "VARCHAR(64)")
        _ensure_column(conn, "suppliers", "phone_number", "VARCHAR(64)")
        _ensure_column(conn, "suppliers", "email_cc", "VARCHAR(256)")
        _ensure_column(conn, "suppliers", "assistant_name", "VARCHAR(128)")
        _ensure_column(conn, "suppliers", "assistant_email", "VARCHAR(256)")
        _ensure_column(conn, "suppliers", "fax_number", "VARCHAR(64)")
        _ensure_column(conn, "suppliers", "notes", "TEXT")
        _ensure_column(conn, "purchase_order_lines", "received_quantity", "INTEGER NOT NULL DEFAULT 0")
        # 既存の items.supplier_id + unit_price を item_suppliers に1件ずつ投入（重複は無視）
        if _table_exists(conn, "item_suppliers"):
            conn.execute(
                text(
                    "INSERT OR IGNORE INTO item_suppliers (item_id, supplier_id, unit_price) "
                    "SELECT id, supplier_id, unit_price FROM items WHERE supplier_id IS NOT NULL"
                )
            )
        conn.execute(
            text(
                "UPDATE suppliers "
                "SET assistant_email = email_cc "
                "WHERE (assistant_email IS NULL OR TRIM(assistant_email) = '') "
                "AND (email_cc IS NOT NULL AND TRIM(email_cc) <> '')"
            )
        )
        conn.execute(
            text(
                "UPDATE purchase_order_lines "
                "SET received_quantity = 0 "
                "WHERE received_quantity IS NULL"
            )
        )
        conn.commit()
