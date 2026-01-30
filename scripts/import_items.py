import csv
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(PROJECT_ROOT))

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import SessionLocal, init_db
from app.models.tables import (
    InventoryItem,
    InventoryTransaction,
    Item,
    Supplier,
    TransactionType,
)


SEED_DIR = Path("data") / "seed"
ITEMS_CSV = SEED_DIR / "仕入品マスタ.csv"
SUPPLIERS_CSV = SEED_DIR / "仕入先マスタ.csv"


def safe_int(value: str) -> int:
    if not value:
        return 0
    cleaned = "".join(ch for ch in value if (ch.isdigit() or ch in "-."))
    if not cleaned:
        return 0
    try:
        if "." in cleaned:
            return int(float(cleaned))
        return int(cleaned)
    except ValueError:
        return 0


def parse_datetime(value: str) -> Optional[datetime]:
    if not value:
        return None
    value = value.strip()
    for fmt in ("%Y/%m/%d %H:%M", "%Y/%m/%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def read_column(row: dict[str, str], key: str) -> str:
    raw = row.get(key)
    if raw is None:
        raw = row.get(key.strip(), "")
    return raw.strip()


def iter_csv_rows(path: Path) -> Iterator[dict]:
    for encoding in ("utf-8-sig", "cp932"):
        try:
            with path.open("r", encoding=encoding, newline="") as stream:
                for row in csv.DictReader(stream):
                    yield row
            return
        except UnicodeDecodeError:
            continue
    raise UnicodeDecodeError(f"unable to decode {path}")


def import_suppliers(session: Session) -> None:
    if not SUPPLIERS_CSV.exists():
        print(f"SUPPLIER CSV not found: {SUPPLIERS_CSV}")
        return
    for row in iter_csv_rows(SUPPLIERS_CSV):
        name = read_column(row, "仕入先名")
        if not name:
            continue
        supplier = session.scalar(select(Supplier).filter(Supplier.name == name))
        data = {
            "name": name,
            "contact_person": read_column(row, "営業担当者名"),
            "mobile_number": read_column(row, "携帯番号"),
            "phone_number": read_column(row, "会社電話番号"),
            "email": read_column(row, "メール"),
            "assistant_name": read_column(row, "アシスタント名"),
            "assistant_email": read_column(row, "メールCC"),
            "fax_number": read_column(row, "FAX番号"),
            "notes": read_column(row, "備考"),
        }
        if supplier:
            for key, value in data.items():
                setattr(supplier, key, value)
        else:
            session.add(Supplier(**data))


def import_items(session: Session) -> None:
    if not ITEMS_CSV.exists():
        print(f"ITEM CSV not found: {ITEMS_CSV}")
        return
    for row in iter_csv_rows(ITEMS_CSV):
        item_code = read_column(row, "品番")
        if not item_code:
            continue
        usage = read_column(row, "用途")
        item_type = read_column(row, "種類")
        department = read_column(row, "部署名")
        manufacturer = read_column(row, "メーカー名")
        shelf_value = read_column(row, "棚番") or None
        reorder_point = safe_int(read_column(row, "発注点"))
        stock_qty = safe_int(read_column(row, "在庫数"))
        supplier_name = read_column(row, "仕入先名")
        supplier = None
        if supplier_name:
            supplier = session.scalar(select(Supplier).filter(Supplier.name == supplier_name))

        item = session.scalar(select(Item).filter(Item.item_code == item_code))
        if item:
            if not item.unit:
                item.unit = "個"
            item.usage = usage
            item.item_type = item_type
            item.department = department
            item.manufacturer = manufacturer
            item.shelf = shelf_value
            item.reorder_point = reorder_point
            item.supplier = supplier
        else:
            name = item_type or manufacturer or item_code
            item = Item(
                item_code=item_code,
                name=name,
                item_type=item_type,
                usage=usage,
                department=department,
                manufacturer=manufacturer,
                shelf=shelf_value,
                unit="個",
                reorder_point=reorder_point,
                supplier=supplier,
            )
            session.add(item)
            session.flush()

        inventory = session.scalar(
            select(InventoryItem).filter(InventoryItem.item_id == item.id)
        )
        if inventory:
            inventory.quantity_on_hand = stock_qty
        else:
            session.add(
                InventoryItem(
                    item_id=item.id,
                    quantity_on_hand=stock_qty,
                )
            )
            session.flush()

        has_tx = session.scalar(
            select(InventoryTransaction)
            .filter(InventoryTransaction.item_id == item.id)
            .limit(1)
        )
        if not has_tx:
            occurred_at = (
                parse_datetime(read_column(row, "入庫日"))
                or parse_datetime(read_column(row, "出庫日"))
                or datetime.now(timezone.utc)
            )
            session.add(
                InventoryTransaction(
                    item_id=item.id,
                    tx_type=TransactionType.RECEIPT,
                    delta=stock_qty,
                    reason="CSV在庫マスタ取込",
                    note=usage or item_type or "",
                    occurred_at=occurred_at,
                    created_by="seed-script",
                )
            )


def main() -> None:
    init_db()
    with SessionLocal() as session:
        import_suppliers(session)
        import_items(session)
        session.commit()


if __name__ == "__main__":
    main()
