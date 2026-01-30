import re
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote_plus
import yaml

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import nullsfirst, select
from sqlalchemy.orm import Session, selectinload

from app.db.session import init_db, get_db
from app.models.tables import InventoryItem, InventoryTransaction, Item, TransactionType
from pydantic import BaseModel
from zoneinfo import ZoneInfo

@dataclass(frozen=True)
class InventorySnapshot:
    item_id: int
    item_code: str
    name: str
    item_type: str  # DBの「種類」
    usage: str  # DBの「用途」
    department: str
    manufacturer: str
    shelf: Optional[str]
    unit: str
    on_hand: int
    reorder_point: int
    last_activity: str
    last_updated: datetime
    location: str
    supplier: str

UNSET_LABEL = "未設定"
JST_ZONE = ZoneInfo("Asia/Tokyo")
SHELF_TOKENIZER = re.compile(r"(\d+)")


class InlineAdjustmentPayload(BaseModel):
    item_code: str
    target_quantity: int


class IssueRecordRequest(BaseModel):
    item_code: str
    quantity: int
    reason: Optional[str] = ""


class IssueRecordResponse(BaseModel):
    item_code: str
    on_hand: int
    reorder_point: int
    gap_label: str
    status_label: str
    status_badge: str
    status_description: str
    last_activity: str
    last_updated: str
    supplier: str
    message: str

def normalize_field(value: Optional[str]) -> str:
    if value is None:
        return ""
    return value.strip()


def display_value(value: Optional[str]) -> str:
    normalized = normalize_field(value)
    return normalized if normalized else UNSET_LABEL


def natural_shelf_key(value: str) -> Tuple:
    parts: List[Tuple[bool, object]] = []
    for part in SHELF_TOKENIZER.split(value):
        if part.isdigit():
            parts.append((True, int(part)))
        else:
            parts.append((False, part.lower()))
    return tuple(parts)


def shelf_sort_key(snapshot: InventorySnapshot) -> Tuple[bool, Tuple, str]:
    shelf = normalize_field(snapshot.shelf)
    has_shelf = bool(shelf)
    key = natural_shelf_key(shelf) if shelf else ()
    return (not has_shelf, key, snapshot.item_code)


def to_jst(value: Optional[datetime]) -> datetime:
    if not value:
        return datetime.now(JST_ZONE)
    if value.tzinfo is None:
        value = value.replace(tzinfo=JST_ZONE)
    return value.astimezone(JST_ZONE)


def load_usage_order_config(path: Path) -> Tuple[List[str], Dict[str, Dict[str, int]], Dict[str, Dict[str, Dict[str, int]]]]:
    departments: List[str] = []
    usage_order: Dict[str, Dict[str, int]] = {}
    type_order: Dict[str, Dict[str, Dict[str, int]]] = {}
    if not path.exists():
        return departments, usage_order, type_order
    raw = {}
    try:
        with path.open("r", encoding="utf-8") as stream:
            raw = yaml.safe_load(stream) or {}
    except Exception:
        return departments, usage_order, type_order
    for dept_entry in raw.get("departments") or []:
        if not isinstance(dept_entry, dict):
            continue
        dept_name = display_value(dept_entry.get("name"))
        departments.append(dept_name)
        usage_order.setdefault(dept_name, {})
        type_order.setdefault(dept_name, {})
        for usage_idx, usage_entry in enumerate(dept_entry.get("usages") or []):
            if not isinstance(usage_entry, dict):
                continue
            usage_name = display_value(usage_entry.get("name"))
            usage_order[dept_name][usage_name] = usage_idx
            type_order.setdefault(dept_name, {}).setdefault(usage_name, {})
            for type_idx, type_name in enumerate(usage_entry.get("types") or []):
                type_label = display_value(type_name)
                type_order[dept_name][usage_name][type_label] = type_idx
    return departments, usage_order, type_order


PROJECT_ROOT = Path(__file__).resolve().parents[1]
USAGE_ORDER_PATH = PROJECT_ROOT / "config" / "usage_order.yaml"
DEPARTMENT_ORDER, USAGE_ORDER, TYPE_ORDER = load_usage_order_config(USAGE_ORDER_PATH)
DEPARTMENT_ORDER_INDEX = {name: idx for idx, name in enumerate(DEPARTMENT_ORDER)}

SAMPLE_INVENTORY: List[InventorySnapshot] = [
    InventorySnapshot(
        item_id=1,
        item_code='16167',
        name='工具刃物セット（研削用）',
        item_type='刃物',
        usage='品質検査',
        department='生産部',
        manufacturer='株式会社マジマ機工',
        shelf='棚A-01',
        unit='本',
        on_hand=12,
        reorder_point=20,
        last_activity='出庫 - 品質検査ラインへ',
        last_updated=datetime(2026, 1, 28, 16, 12),
        location='棚A-01',
        supplier='株式会社マジマ機工',
    ),
    InventorySnapshot(
        item_id=2,
        item_code='18745',
        name='電動ドライバーユニット',
        item_type='電動工具',
        usage='管理保全',
        department='DIP部',
        manufacturer='東光電子',
        shelf='棚B-04',
        unit='台',
        on_hand=36,
        reorder_point=30,
        last_activity='調整 - 管理外品から移管',
        last_updated=datetime(2026, 1, 29, 9, 45),
        location='棚B-04',
        supplier='東光電子',
    ),
    InventorySnapshot(
        item_id=3,
        item_code='20590',
        name='拡張治具セット',
        item_type='治具',
        usage='評価試験',
        department='品質保証部',
        manufacturer='パナチ',
        shelf='棚C-11',
        unit='セット',
        on_hand=5,
        reorder_point=8,
        last_activity='出庫 - 評価試験',
        last_updated=datetime(2026, 1, 30, 7, 58),
        location='棚C-11',
        supplier='パナチ',
    ),
    InventorySnapshot(
        item_id=4,
        item_code='31201',
        name='消耗品（研磨布）',
        item_type='消耗品',
        usage='生産補充',
        department='生産部',
        manufacturer='マジマ機工',
        shelf='棚D-02',
        unit='袋',
        on_hand=48,
        reorder_point=40,
        last_activity='入庫 - 仕入先納品',
        last_updated=datetime(2026, 1, 29, 15, 24),
        location='棚D-02',
        supplier='マジマ機工',
    ),
    InventorySnapshot(
        item_id=5,
        item_code='40218',
        name='清掃キットXL',
        item_type='清掃用品',
        usage='清掃',
        department='総務部',
        manufacturer='サニクリーン',
        shelf='棚E-06',
        unit='個',
        on_hand=22,
        reorder_point=22,
        last_activity='調整 - 使用部署変更',
        last_updated=datetime(2026, 1, 27, 18, 5),
        location='棚E-06',
        supplier='サニクリーン',
    ),
]

RECENT_TRANSACTIONS: List[Dict[str, str]] = [
    {
        'item': '工具刃物セット（研削用）',
        'department': '生産部',
        'type': '出庫',
        'delta': '-2 本',
        'date': '2026/01/29 14:10',
        'note': '品質検査ラインで使用',
    },
    {
        'item': '電動ドライバーユニット',
        'department': 'DIP部',
        'type': '調整',
        'delta': '+3 台',
        'date': '2026/01/29 11:50',
        'note': '管理外品との統合',
    },
    {
        'item': '拡張治具セット',
        'department': '品質保証部',
        'type': '入庫',
        'delta': '+4 セット',
        'date': '2026/01/28 09:30',
        'note': '評価試験用補充',
    },
    {
        'item': '清掃キットXL',
        'department': '総務部',
        'type': '調整',
        'delta': '+2 個',
        'date': '2026/01/27 18:05',
        'note': '使用部署変更に伴う補充',
    },
]

NAV_LINKS = [
    {'label': 'ダッシュボード', 'href': '#', 'active': False},
    {'label': '在庫管理', 'href': '/inventory', 'active': True},
    {'label': '発注管理', 'href': '#', 'active': False},
    {'label': '納品管理', 'href': '#', 'active': False},
]

DELIVERIES_TODAY = 4

app = FastAPI(
    title='購入品一元管理システム',
    description='社内向け在庫・購買ダッシュボード',
    docs_url='/internal/docs',
    redoc_url=None,
    openapi_url='/internal/openapi.json',
)

@app.on_event("startup")
def on_startup() -> None:
    init_db()

BASE_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = BASE_DIR / 'web' / 'templates'
STATIC_DIR = BASE_DIR / 'web' / 'static'

templates = Jinja2Templates(directory=TEMPLATE_DIR)
templates.env.filters['urlencode'] = lambda value: quote_plus(str(value))
app.mount('/static', StaticFiles(directory=STATIC_DIR), name='static')

def calculate_status(snapshot: InventorySnapshot) -> Tuple[str, str, str]:
    if snapshot.reorder_point <= 0:
        return (
            '十分',
            'bg-emerald-50 text-emerald-700 border-emerald-100',
            '発注点が未設定',
        )
    if snapshot.on_hand <= snapshot.reorder_point:
        return (
            '不足',
            'bg-red-50 text-red-600 border-red-100',
            '直ちに再発注',
        )
    buffer = max(5, snapshot.reorder_point // 2)
    if snapshot.on_hand <= snapshot.reorder_point + buffer:
        return (
            '要注意',
            'bg-amber-50 text-amber-600 border-amber-100',
            '発注点に近づいています',
        )
    return (
        '十分',
        'bg-emerald-50 text-emerald-700 border-emerald-100',
        '在庫は安定',
    )

def build_inventory_row(snapshot: InventorySnapshot) -> Dict[str, str]:
    label, badge, description = calculate_status(snapshot)
    gap = snapshot.on_hand - snapshot.reorder_point
    return {
        'item_code': snapshot.item_code,
        'name': snapshot.name,
        'item_type': display_value(snapshot.item_type),
        'usage': display_value(snapshot.usage),
        'department': snapshot.department,
        'manufacturer': snapshot.manufacturer,
        'shelf': snapshot.shelf,
        'unit': snapshot.unit,
        'on_hand': '{:,}'.format(snapshot.on_hand),
        'on_hand_value': snapshot.on_hand,
        'reorder_point': '{:,}'.format(snapshot.reorder_point),
        'last_activity': snapshot.last_activity,
        'last_updated': snapshot.last_updated.strftime('%Y/%m/%d %H:%M'),
        'location': snapshot.location,
        'supplier': snapshot.supplier,
        'item_id': snapshot.item_id,
        'status_label': label,
        'status_badge': badge,
        'status_description': description,
        'stock_gap': gap,
        'stock_gap_label': '{:+,d}'.format(gap),
    }

def build_sidebar_structure(snapshots: List[InventorySnapshot]) -> List[Dict[str, List[Dict[str, int]]]]:
    structure: Dict[str, Dict[str, int]] = {}
    for snapshot in snapshots:
        department = snapshot.department
        usage = display_value(snapshot.usage)
        if department not in structure:
            structure[department] = {}
        structure[department][usage] = structure[department].get(usage, 0) + 1
    def department_key(dept_name: str) -> Tuple[int, str]:
        idx = DEPARTMENT_ORDER_INDEX.get(dept_name)
        if idx is not None:
            return (0, idx)
        return (1, dept_name)
    def usage_key(dept_name: str, usage_name: str) -> Tuple[int, str]:
        order_map = USAGE_ORDER.get(dept_name, {})
        idx = order_map.get(usage_name)
        if idx is not None:
            return (0, idx)
        return (1, usage_name)
    sorted_departments = sorted(structure.items(), key=lambda kv: department_key(kv[0]))
    result: List[Dict[str, List[Dict[str, int]]]] = []
    for department, categories in sorted_departments:
        ordered_categories = sorted(categories.items(), key=lambda kv: usage_key(department, kv[0]))
        result.append(
            {
                'name': department,
                'categories': [
                    {'name': usage, 'count': count} for usage, count in ordered_categories
                ],
            }
        )
    return result


def build_category_options(snapshots: List[InventorySnapshot]) -> List[str]:
    return sorted({display_value(snapshot.item_type) for snapshot in snapshots})


def build_type_options(
    snapshots: List[InventorySnapshot], usage: str, department: str
) -> List[str]:
    relevant_snapshots = snapshots
    if department:
        relevant_snapshots = [
            snapshot for snapshot in snapshots if snapshot.department == department
        ]
    if usage:
        candidates = [
            display_value(snapshot.item_type)
            for snapshot in relevant_snapshots
            if display_value(snapshot.usage) == usage
        ]
    else:
        candidates = [display_value(snapshot.item_type) for snapshot in relevant_snapshots]
    unique = sorted(set(candidates))
    def type_key(value: str) -> Tuple[int, str]:
        order_map = TYPE_ORDER.get(department or "", {}).get(usage or "", {})
        idx = order_map.get(value)
        if idx is not None:
            return (0, idx)
        return (1, value)
    return sorted(unique, key=type_key)


def describe_transaction(tx: Optional[InventoryTransaction]) -> Tuple[str, datetime, str]:
    if not tx:
        return "履歴なし", to_jst(None), ""
    label_map = {
        TransactionType.RECEIPT: "入庫",
        TransactionType.ISSUE: "出庫",
        TransactionType.ADJUST: "調整",
    }
    label = label_map.get(tx.tx_type, tx.tx_type.value if isinstance(tx.tx_type, TransactionType) else str(tx.tx_type))
    details = [label]
    if tx.note:
        details.append(tx.note)
    elif tx.reason:
        details.append(tx.reason)
    summary = "・".join(details)
    occurred = to_jst(tx.occurred_at or tx.created_at)
    supplier = tx.note or tx.reason or ""
    return summary, occurred, supplier


def build_inventory_status_payload(
    item: Item,
    inventory_item: InventoryItem,
    last_tx: Optional[InventoryTransaction],
) -> Dict[str, object]:
    last_activity, last_updated, last_supplier = describe_transaction(last_tx)
    supplier_label = (
        item.supplier.name if item.supplier else last_supplier or normalize_field(item.manufacturer)
    )
    shelf_value = normalize_field(item.shelf)
    snapshot = InventorySnapshot(
        item_id=item.id,
        item_code=item.item_code,
        name=item.name,
        item_type=display_value(item.item_type),
        usage=display_value(item.usage),
        department=display_value(item.department),
        manufacturer=normalize_field(item.manufacturer),
        shelf=shelf_value or None,
        unit=item.unit or "",
        on_hand=inventory_item.quantity_on_hand or 0,
        reorder_point=item.reorder_point or 0,
        last_activity=last_activity,
        last_updated=last_updated,
        location=shelf_value,
        supplier=supplier_label or "",
    )
    status_label, status_badge, status_description = calculate_status(snapshot)
    gap = snapshot.on_hand - snapshot.reorder_point
    return {
        "item_code": item.item_code,
        "on_hand": snapshot.on_hand,
        "reorder_point": snapshot.reorder_point,
        "gap_label": '{:+,d}'.format(gap),
        "status_label": status_label,
        "status_badge": status_badge,
        "status_description": status_description,
        "last_activity": last_activity,
        "last_updated": last_updated.strftime('%Y/%m/%d %H:%M'),
        "supplier": supplier_label or "",
    }


def load_inventory_snapshots(db: Session) -> List[InventorySnapshot]:
    stmt = (
        select(Item)
        .options(selectinload(Item.inventory_item))
        .options(selectinload(Item.inventory_transactions))
        .options(selectinload(Item.supplier))
        .order_by(
            Item.shelf.asc().nullsfirst(),
            Item.item_code.asc(),
        )
    )
    items = db.scalars(stmt).all()
    snapshots: List[InventorySnapshot] = []
    for item in items:
        inventory = item.inventory_item
        on_hand = inventory.quantity_on_hand if inventory else 0
        last_tx = item.inventory_transactions[0] if item.inventory_transactions else None
        last_activity, last_updated, last_tx_supplier = describe_transaction(last_tx)
        item_type_value = display_value(item.item_type)
        usage_value = display_value(item.usage)
        department_value = display_value(item.department)
        shelf_value = normalize_field(item.shelf)
        manufacturer_value = normalize_field(item.manufacturer)
        supplier_label = (
            item.supplier.name
            if item.supplier
            else last_tx_supplier or manufacturer_value
        )
        snapshots.append(
            InventorySnapshot(
                item_id=item.id,
                item_code=item.item_code,
                name=item.name,
                item_type=item_type_value,
                usage=usage_value,
                department=department_value,
                manufacturer=manufacturer_value,
                shelf=shelf_value or None,
                unit=item.unit or "",
                on_hand=on_hand,
                reorder_point=item.reorder_point or 0,
                last_activity=last_activity,
                last_updated=last_updated,
                location=shelf_value,
                supplier=supplier_label or "",
            )
        )
    return snapshots


def load_recent_transactions(db: Session, limit: int = 4) -> List[Dict[str, str]]:
    stmt = (
        select(InventoryTransaction)
        .order_by(InventoryTransaction.occurred_at.desc())
        .limit(limit)
        .options(selectinload(InventoryTransaction.item))
    )
    txs = db.scalars(stmt).all()
    recent: List[Dict[str, str]] = []
    for tx in txs:
        summary, occurred_at, _ = describe_transaction(tx)
        recent.append(
            {
                "item": tx.item.name if tx.item else "不明品目",
                "department": tx.item.department if tx.item and tx.item.department else "未設定部署",
                "type": summary.split("・")[0] if summary else "",
                "shelf": tx.item.shelf if tx.item else "",
                "item_code": tx.item.item_code if tx.item else "",
                "manufacturer": tx.item.manufacturer if tx.item else "",
                "summary": summary,
                "delta": f"{'+' if tx.delta >= 0 else ''}{tx.delta} {tx.item.unit if tx.item and tx.item.unit else ''}".strip(),
                "date": occurred_at.strftime("%Y/%m/%d %H:%M"),
                "note": tx.note or tx.reason or "",
            }
        )
    return recent


@app.get("/recent-transactions")
def recent_transactions(
    db: Session = Depends(get_db),
    limit: int = Query(4, ge=1, le=20),
) -> Dict[str, List[Dict[str, str]]]:
    transactions = load_recent_transactions(db, limit)
    if not transactions:
        transactions = RECENT_TRANSACTIONS
    return {"transactions": transactions}


def filter_inventory(
    snapshots: List[InventorySnapshot],
    keyword: str,
    category: str,
    usage: str,
    department: str,
) -> List[InventorySnapshot]:
    normalized_keyword = keyword.lower()
    normalized_category = normalize_field(category)
    normalized_usage = normalize_field(usage)
    normalized_department = normalize_field(department)
    result: List[InventorySnapshot] = []
    for snapshot in snapshots:
        snapshot_type = display_value(snapshot.item_type)
        snapshot_usage = display_value(snapshot.usage)
        snapshot_department = normalize_field(snapshot.department)
        if normalized_category and snapshot_type != normalized_category:
            continue
        if normalized_usage and snapshot_usage != normalized_usage:
            continue
        if normalized_department and snapshot_department != normalized_department:
            continue
        if normalized_keyword:
            haystack = ' '.join([
                snapshot.item_code,
                snapshot.name,
                snapshot_type,
                snapshot_usage,
                snapshot_department,
            ]).lower()
            if normalized_keyword not in haystack:
                continue
        result.append(snapshot)
    return result


def build_inventory_url(
    keyword: str,
    category: str,
    current_usage: str,
    *,
    q: Optional[str] = None,
    category_override: Optional[str] = None,
    usage: Optional[str] = None,
    department: Optional[str] = None,
    department_override: Optional[str] = None,
) -> str:
    params: List[Tuple[str, str]] = []
    final_q = keyword if q is None else q
    if final_q:
        params.append(('q', final_q))
    final_category = category if category_override is None else category_override
    if final_category:
        params.append(('category', final_category))
    final_usage = current_usage if usage is None else usage
    final_department = department if department_override is None else department_override
    final_department = normalize_field(final_department)
    if final_usage:
        params.append(('usage', final_usage))
    if final_department:
        params.append(('department', final_department))
    if not params:
        return '/inventory'
    return '/inventory?' + '&'.join(f'{name}={quote_plus(value)}' for name, value in params)

@app.get('/', include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url='/inventory')

@app.get('/inventory', response_class=HTMLResponse)
def inventory_index(
    request: Request,
    q: str = Query('', alias='q'),
    category: str = Query('', alias='category'),
    usage: str = Query('', alias='usage'),
    department: str = Query('', alias='department'),
    message: str = Query('', alias='message'),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    keyword = q.strip()
    selected_category = normalize_field(category)
    selected_usage = normalize_field(usage)
    selected_department = normalize_field(department)

    snapshots = load_inventory_snapshots(db)
    if not snapshots:
        snapshots = SAMPLE_INVENTORY

    filtered_snapshots = filter_inventory(
        snapshots,
        keyword,
        selected_category,
        selected_usage,
        selected_department,
    )
    filtered_snapshots = sorted(filtered_snapshots, key=shelf_sort_key)
    rows = [build_inventory_row(snapshot) for snapshot in filtered_snapshots]

    total_items = len(snapshots)
    attention_count = sum(1 for snapshot in snapshots if calculate_status(snapshot)[0] != '十分')
    low_stock_count = sum(1 for snapshot in snapshots if snapshot.on_hand <= snapshot.reorder_point)
    recent_transactions = load_recent_transactions(db)
    if not recent_transactions:
        recent_transactions = RECENT_TRANSACTIONS

    kpi_cards = [
        {
            'label': '総品目数',
            'value': total_items,
            'note': '仕入品マスタをCSV同期済',
            'icon': 'inventory_2',
        },
        {
            'label': '要注意在庫',
            'value': attention_count,
            'note': '警告／不足を含む',
            'icon': 'priority_high',
        },
        {
            'label': '在庫不足',
            'value': low_stock_count,
            'note': '発注点以下の品目',
            'icon': 'warning',
        },
        {
            'label': '本日納品予定',
            'value': DELIVERIES_TODAY,
            'note': 'マジマ機工・東光電子',
            'icon': 'local_shipping',
        },
    ]

    alert_message = message.strip()
    context = {
        'request': request,
        'nav_links': NAV_LINKS,
        'sidebar_structure': build_sidebar_structure(snapshots),
        'category_options': build_category_options(snapshots),
        'selected_category': selected_category,
        'keyword': keyword,
        'selected_usage': selected_usage,
        'selected_department': selected_department,
        'type_options': build_type_options(snapshots, selected_usage, selected_department),
        'filtered_items': rows,
        'filtered_count': len(rows),
        'total_items': total_items,
        'kpi_cards': kpi_cards,
        'recent_transactions': recent_transactions,
        'now': datetime.now(JST_ZONE),
        'build_inventory_url': build_inventory_url,
        'alert_message': alert_message,
    }
    return templates.TemplateResponse('inventory_list.html', context)


@app.post('/inventory/issues')
def inventory_issue(
    item_code: str = Form(...),
    quantity: int = Form(...),
    reason: str = Form(""),
    created_by: str = Form(""),
    next_url: str = Form("/inventory"),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    if quantity <= 0:
        raise HTTPException(status_code=400, detail="出庫数は 1 以上で指定してください。")
    item = db.scalar(select(Item).filter(Item.item_code == item_code))
    if not item:
        raise HTTPException(status_code=404, detail="該当品目が存在しません。")
    inventory = db.scalar(select(Item).filter(Item.id == item.id).options(selectinload(Item.inventory_item)))
    inventory_item = db.scalar(
        select(InventoryItem).filter(InventoryItem.item_id == item.id)
    )
    if not inventory_item:
        raise HTTPException(status_code=400, detail="在庫情報が見つかりません。")
    if inventory_item.quantity_on_hand < quantity:
        raise HTTPException(status_code=400, detail="在庫数が不足しています。")
    inventory_item.quantity_on_hand -= quantity
    tx = InventoryTransaction(
        item_id=item.id,
        tx_type=TransactionType.ISSUE,
        delta=-quantity,
        reason=reason or "",
        note="",
        occurred_at=datetime.now(JST_ZONE),
        created_by=created_by or "system",
    )
    db.add(tx)
    db.commit()
    separator = '&' if '?' in next_url else '?'
    message = f"{item.name}（{item.item_code}）を{quantity}件出庫しました。"
    return RedirectResponse(
        url=f"{next_url}{separator}message={quote_plus(message)}",
        status_code=303,
    )


@app.post('/api/inventory/issues', response_model=IssueRecordResponse)
def api_inventory_issue(
    payload: IssueRecordRequest,
    db: Session = Depends(get_db),
) -> IssueRecordResponse:
    """シンプルな持出し記録 API（ユーザー情報なし）。"""
    if payload.quantity <= 0:
        raise HTTPException(status_code=400, detail="数量は 1 以上で指定してください。")
    item = db.scalar(select(Item).filter(Item.item_code == payload.item_code))
    if not item:
        raise HTTPException(status_code=404, detail="該当品目が存在しません。")
    inventory_item = db.scalar(
        select(InventoryItem).filter(InventoryItem.item_id == item.id)
    )
    if not inventory_item:
        raise HTTPException(status_code=400, detail="在庫情報が未登録です。")
    if inventory_item.quantity_on_hand < payload.quantity:
        raise HTTPException(status_code=400, detail="在庫数が不足しています。")
    inventory_item.quantity_on_hand -= payload.quantity
    tx = InventoryTransaction(
        item_id=item.id,
        tx_type=TransactionType.ISSUE,
        delta=-payload.quantity,
        reason=payload.reason or "",
        note="",
        occurred_at=datetime.now(JST_ZONE),
        created_by="system",
    )
    db.add(tx)
    db.commit()
    response_data = build_inventory_status_payload(item, inventory_item, tx)
    response_data["message"] = f"{item.name}（{item.item_code}）を{payload.quantity}件出庫しました。"
    return IssueRecordResponse(**response_data)


@app.post('/inventory/adjust')
def inventory_adjust(
    item_code: str = Form(...),
    delta: int = Form(...),
    next_url: str = Form("/inventory"),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    if delta == 0:
        raise HTTPException(status_code=400, detail="変更量を指定してください。")
    item = db.scalar(select(Item).filter(Item.item_code == item_code))
    if not item:
        raise HTTPException(status_code=404, detail="該当品目が存在しません。")
    inventory_item = db.scalar(
        select(InventoryItem).filter(InventoryItem.item_id == item.id)
    )
    if not inventory_item:
        raise HTTPException(status_code=400, detail="在庫情報が見つかりません。")
    inventory_item.quantity_on_hand += delta
    if inventory_item.quantity_on_hand < 0:
        raise HTTPException(status_code=400, detail="在庫数が 0 未満になっています。")
    tx = InventoryTransaction(
        item_id=item.id,
        tx_type=TransactionType.ADJUST,
        delta=delta,
        reason="在庫一覧ワンクリック調整",
        note="運用画面操作",
        occurred_at=datetime.now(JST_ZONE),
        created_by="system",
    )
    db.add(tx)
    db.commit()
    separator = '&' if '?' in next_url else '?'
    message = f"{item.name} を{delta:+d}件調整しました。"
    return RedirectResponse(
        url=f"{next_url}{separator}message={quote_plus(message)}",
        status_code=303,
    )

@app.post('/inventory/inline-adjust')
def inventory_inline_adjust(
    payload: InlineAdjustmentPayload,
    db: Session = Depends(get_db),
) -> Dict[str, object]:
    if payload.target_quantity < 0:
        raise HTTPException(status_code=400, detail="在庫数量は0以上の整数で指定してください")
    item = db.scalar(select(Item).filter(Item.item_code == payload.item_code))
    if not item:
        raise HTTPException(status_code=404, detail="指定された品番が存在しません")
    inventory_item = db.scalar(
        select(InventoryItem).filter(InventoryItem.item_id == item.id)
    )
    if not inventory_item:
        raise HTTPException(status_code=400, detail="在庫情報が未登録です")
    before_quantity = inventory_item.quantity_on_hand or 0
    inventory_item.quantity_on_hand = payload.target_quantity
    delta = payload.target_quantity - before_quantity
    if delta != 0:
        tx = InventoryTransaction(
            item_id=item.id,
            tx_type=TransactionType.ADJUST,
            delta=delta,
            reason="在庫一覧画面からの更新",
            note="即時更新",
            occurred_at=datetime.now(JST_ZONE),
            created_by="system",
        )
        db.add(tx)
    db.commit()
    last_tx_stmt = (
        select(InventoryTransaction)
        .filter(InventoryTransaction.item_id == item.id)
        .order_by(InventoryTransaction.occurred_at.desc())
        .limit(1)
    )
    last_tx = db.scalar(last_tx_stmt)
    return build_inventory_status_payload(item, inventory_item, last_tx)
