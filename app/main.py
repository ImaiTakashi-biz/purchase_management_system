import re
import json
import os
import hmac
import base64
import hashlib
import importlib
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any, Set, Protocol, cast
from urllib.parse import quote_plus
import yaml

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import func, nullsfirst, select, or_
from sqlalchemy.orm import Session, selectinload

from app.db.session import init_db, get_db, SessionLocal
from app.models.tables import AppUser, InventoryItem, InventoryTransaction, Item, PurchaseOrder, PurchaseOrderLine, PurchaseOrderStatus, PurchaseResult, Supplier, TransactionType, UserRole
from app.services import PurchaseOrderError, PurchaseOrderService
from pydantic import BaseModel
from zoneinfo import ZoneInfo

@dataclass(frozen=True)
class InventorySnapshot:
    item_id: int
    item_code: str
    name: str
    item_type: str
    usage: str
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
SESSION_USER_ID_KEY = "auth_user_id"
PASSWORD_HASH_SCHEME = "pbkdf2_sha256"
PASSWORD_HASH_ITERATIONS = 260000
ROLE_ADMIN = UserRole.ADMIN.value
ROLE_MANAGER = UserRole.MANAGER.value
ROLE_VIEWER = UserRole.VIEWER.value
SESSION_SECRET = os.getenv("APP_SESSION_SECRET", "purchase-management-dev-secret")
# セッションCookieの有効期限（秒）。0=ブラウザ終了まで。例: 1209600=14日間（同一端末でログイン状態を保持）
_session_max_age_raw = os.getenv("APP_SESSION_MAX_AGE", "1209600").strip()
SESSION_MAX_AGE = int(_session_max_age_raw) if _session_max_age_raw else 1209600
# SessionMiddleware に渡す値。0のときはセッションのみ（None）、それ以外は秒数
SESSION_COOKIE_MAX_AGE: Optional[int] = None if SESSION_MAX_AGE == 0 else SESSION_MAX_AGE
BOOTSTRAP_ADMIN_USERNAME = os.getenv("APP_BOOTSTRAP_ADMIN_USERNAME", "admin")
BOOTSTRAP_ADMIN_PASSWORD = os.getenv("APP_BOOTSTRAP_ADMIN_PASSWORD", "admin12345")
BOOTSTRAP_ADMIN_DISPLAY_NAME = os.getenv("APP_BOOTSTRAP_ADMIN_DISPLAY_NAME", "管理者")
LOGIN_ROUTE_PATH = "/login"
AUTH_EXEMPT_PATHS: Set[str] = {
    LOGIN_ROUTE_PATH,
    "/logout",
    "/internal/docs",
    "/internal/openapi.json",
}
# 一般ユーザー向け：ログイン不要で利用可能なパス
# - ダッシュボード・在庫一覧・履歴ページ
# - 在庫一覧から利用する API（recent-transactions / 出庫 / inline-adjust）
PUBLIC_PATHS: Set[str] = {
    "/",
    "/dashboard",
    "/inventory",
    "/history",
    "/recent-transactions",
    "/inventory/issues",
    "/inventory/inline-adjust",
    "/api/inventory/issues",
}
AUTH_EXEMPT_PREFIXES: Tuple[str, ...] = ("/static", "/internal/docs")
API_AUTH_PREFIXES: Tuple[str, ...] = (
    "/api/",
    "/purchase-orders",
    "/purchase-order-lines",
    "/inventory/inline-adjust",
    "/recent-transactions",
)
ROLE_PRIORITY: Dict[str, int] = {
    ROLE_VIEWER: 10,
    ROLE_MANAGER: 20,
    ROLE_ADMIN: 30,
}


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PASSWORD_HASH_ITERATIONS,
    )
    salt_b64 = base64.b64encode(salt).decode("ascii")
    digest_b64 = base64.b64encode(digest).decode("ascii")
    return f"{PASSWORD_HASH_SCHEME}${PASSWORD_HASH_ITERATIONS}${salt_b64}${digest_b64}"


def verify_password(password: str, encoded_password: str) -> bool:
    try:
        scheme, iterations_raw, salt_b64, digest_b64 = encoded_password.split("$", 3)
        if scheme != PASSWORD_HASH_SCHEME:
            return False
        iterations = int(iterations_raw)
        salt = base64.b64decode(salt_b64.encode("ascii"))
        expected_digest = base64.b64decode(digest_b64.encode("ascii"))
    except Exception:
        return False

    actual_digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        iterations,
    )
    return hmac.compare_digest(actual_digest, expected_digest)


def get_role_value(role: object) -> str:
    if isinstance(role, UserRole):
        return role.value
    return str(role or "").strip().lower()


def is_admin_user(user: Optional[Dict[str, Any]]) -> bool:
    if not user:
        return False
    return str(user.get("role") or "") == ROLE_ADMIN


def is_manager_or_higher_user(user: Optional[Dict[str, Any]]) -> bool:
    if not user:
        return False
    role = str(user.get("role") or "")
    return ROLE_PRIORITY.get(role, 0) >= ROLE_PRIORITY.get(ROLE_MANAGER, 0)


def _is_safe_next_path(path: str) -> bool:
    return bool(path and path.startswith("/") and not path.startswith("//"))


def _normalize_next_path(path: str) -> str:
    if _is_safe_next_path(path):
        return path
    return "/dashboard"


def _is_auth_exempt_path(path: str) -> bool:
    if path in AUTH_EXEMPT_PATHS:
        return True
    return any(path.startswith(prefix) for prefix in AUTH_EXEMPT_PREFIXES)


def _is_public_path(path: str) -> bool:
    """一般ユーザー向けの公開パスか（ログイン不要でアクセス可能）。"""
    return path in PUBLIC_PATHS


def _is_api_auth_path(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in API_AUTH_PREFIXES)


def _build_user_context(user: AppUser) -> Dict[str, Any]:
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "role": get_role_value(user.role),
    }


def load_user_context_from_session(request: Request) -> Optional[Dict[str, Any]]:
    raw_user_id = request.session.get(SESSION_USER_ID_KEY)
    try:
        user_id = int(raw_user_id)
    except (TypeError, ValueError):
        return None

    with SessionLocal() as db:
        user = db.scalar(
            select(AppUser).filter(
                AppUser.id == user_id,
                AppUser.is_active.is_(True),
            )
        )
        if not user:
            request.session.pop(SESSION_USER_ID_KEY, None)
            return None
        return _build_user_context(user)


def get_request_user(request: Request) -> Dict[str, Any]:
    user = getattr(request.state, "current_user", None)
    if not isinstance(user, dict):
        user = load_user_context_from_session(request)
        if user:
            request.state.current_user = user
    if not isinstance(user, dict):
        raise HTTPException(status_code=401, detail="ログインが必要です。")
    return user


def get_optional_user(request: Request) -> Optional[Dict[str, Any]]:
    """ログインしていればユーザー情報を返し、未ログインなら None を返す。公開パス用。"""
    user = getattr(request.state, "current_user", None)
    if not isinstance(user, dict):
        user = load_user_context_from_session(request)
        if user:
            request.state.current_user = user
    return user if isinstance(user, dict) else None


def require_role(request: Request, minimum_role: str) -> Dict[str, Any]:
    user = get_request_user(request)
    user_role = str(user.get("role") or "")
    if ROLE_PRIORITY.get(user_role, 0) < ROLE_PRIORITY.get(minimum_role, 0):
        raise HTTPException(status_code=403, detail="この操作を実行する権限がありません。")
    return user


def require_viewer_user(request: Request) -> Dict[str, Any]:
    return require_role(request, ROLE_VIEWER)


def require_manager_user(request: Request) -> Dict[str, Any]:
    return require_role(request, ROLE_MANAGER)


def require_admin_user(request: Request) -> Dict[str, Any]:
    return require_role(request, ROLE_ADMIN)


def ensure_bootstrap_admin_user() -> None:
    username = (BOOTSTRAP_ADMIN_USERNAME or "").strip()
    password = BOOTSTRAP_ADMIN_PASSWORD or ""
    display_name = (BOOTSTRAP_ADMIN_DISPLAY_NAME or "").strip() or "Administrator"
    if not username or not password:
        return

    with SessionLocal() as db:
        existing = db.scalar(select(AppUser).filter(AppUser.username == username))
        if existing:
            return
        admin_user = AppUser(
            username=username,
            display_name=display_name,
            password_hash=hash_password(password),
            role=UserRole.ADMIN,
            is_active=True,
        )
        db.add(admin_user)
        db.commit()


class KeyringModule(Protocol):
    def get_password(self, service_name: str, username: str) -> Optional[str]:
        ...

    def set_password(self, service_name: str, username: str, password: str) -> None:
        ...


def load_keyring_module() -> Optional[KeyringModule]:
    try:
        module = importlib.import_module("keyring")
    except ModuleNotFoundError:
        return None
    return cast(KeyringModule, module)


class InlineAdjustmentPayload(BaseModel):
    item_code: str
    target_quantity: int


class SupplierPayload(BaseModel):
    name: str
    contact_person: Optional[str] = ""
    mobile_number: Optional[str] = ""
    phone_number: Optional[str] = ""
    email: Optional[str] = ""
    assistant_name: Optional[str] = ""
    assistant_email: Optional[str] = ""
    fax_number: Optional[str] = ""
    notes: Optional[str] = ""


class EmailAccountPayload(BaseModel):
    display_name: str
    sender: str
    department: Optional[str] = ""


class EmailSettingsPayload(BaseModel):
    smtp_server: str
    smtp_port: int
    accounts: Dict[str, EmailAccountPayload]
    department_defaults: Dict[str, str]


class EmailAccountPasswordPayload(BaseModel):
    password: str


class ItemPayload(BaseModel):
    item_code: str
    name: Optional[str] = ""
    item_type: Optional[str] = ""
    usage: Optional[str] = ""
    department: Optional[str] = ""
    manufacturer: Optional[str] = ""
    shelf: Optional[str] = ""
    unit: Optional[str] = ""
    reorder_point: int = 0
    default_order_quantity: int = 1
    unit_price: Optional[int] = None  # 単価（円）。表示時は 1,000 形式
    account_name: Optional[str] = ""  # 科目名（購入品管理・資産計上用）
    expense_item_name: Optional[str] = ""  # 費目名（購入品管理・資産計上用）
    management_type: Optional[str] = ""  # 管理 / 管理外
    supplier_id: Optional[int] = None

class IssueRecordRequest(BaseModel):
    item_code: str
    quantity: int
    reason: Optional[str] = ""


class ReceiptRecordRequest(BaseModel):
    item_code: str
    quantity: int
    reason: Optional[str] = ""


class InventoryAdjustmentRequest(BaseModel):
    item_code: str
    delta: int
    reason: Optional[str] = ""


class PurchaseOrderLineReceiptPayload(BaseModel):
    line_id: int
    quantity: int


class ReceivePurchaseOrderPayload(BaseModel):
    lines: List[PurchaseOrderLineReceiptPayload] = []
    delivery_date: Optional[str] = None
    delivery_note_number: Optional[str] = None
    line_unit_prices: Optional[Dict[str, Optional[int]]] = None


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


class PurchaseOrderLinePayload(BaseModel):
    item_id: Optional[int] = None
    quantity: int
    note: Optional[str] = ""
    item_name_free: Optional[str] = ""
    maker: Optional[str] = ""
    supplier_id: Optional[int] = None  # 品番×仕入先選択（Phase2）
    unit_price: Optional[int] = None   # 発注時単価（item_suppliers 自動登録用）


class CreatePurchaseOrderPayload(BaseModel):
    department: Optional[str] = ""
    ordered_by_user: Optional[str] = ""
    lines: List[PurchaseOrderLinePayload]


class BulkCreatePurchaseOrdersPayload(BaseModel):
    department: Optional[str] = ""
    ordered_by_user: Optional[str] = ""
    # 一括作成時に画面で変更した数量・備考を反映するため。key=item_id(str), value={ quantity: int, note: str }
    candidate_overrides: Optional[Dict[str, Dict[str, Any]]] = None


class GenerateDocumentPayload(BaseModel):
    generated_by: Optional[str] = ""
    regenerate: bool = False


class SendEmailPayload(BaseModel):
    sent_by: Optional[str] = ""
    regenerate: bool = False


class ReplyDueDatePayload(BaseModel):
    due_date: str


class UpdatePurchaseOrderStatusPayload(BaseModel):
    status: str
    updated_by: Optional[str] = ""


class PurchaseResultUpdatePayload(BaseModel):
    """購入実績1件の更新用。未指定の項目は変更しない（null は空に更新）。"""
    delivery_date: Optional[str] = None
    supplier_id: Optional[int] = None
    delivery_note_number: Optional[str] = None
    quantity: Optional[int] = None
    unit_price: Optional[int] = None
    amount: Optional[int] = None
    purchase_month: Optional[str] = None
    account_name: Optional[str] = None
    expense_item_name: Optional[str] = None
    purchaser_name: Optional[str] = None
    note: Optional[str] = None


def model_to_dict(model: BaseModel) -> Dict[str, object]:
    if hasattr(model, "model_dump"):
        return model.model_dump()  # type: ignore[attr-defined]
    return model.dict()  # type: ignore[attr-defined]

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
    """日時を JST に変換する。タイムゾーン未指定の場合は JST として解釈（在庫取引は JST で記録する前提）。"""
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
EMAIL_SETTINGS_PATH = PROJECT_ROOT / "config" / "email_settings.json"
DEPARTMENT_ORDER, USAGE_ORDER, TYPE_ORDER = load_usage_order_config(USAGE_ORDER_PATH)
DEPARTMENT_ORDER_INDEX = {name: idx for idx, name in enumerate(DEPARTMENT_ORDER)}


def _normalize_departments(value: object) -> List[str]:
    departments: List[str] = []
    if isinstance(value, str):
        normalized = value.strip()
        if normalized:
            departments.append(normalized)
        return departments
    if isinstance(value, list):
        for entry in value:
            if not isinstance(entry, str):
                continue
            normalized = entry.strip()
            if normalized and normalized not in departments:
                departments.append(normalized)
    return departments


def _build_contacts_from_email_settings(raw: object) -> Tuple[List[str], Dict[str, str], Dict[str, List[str]]]:
    if not isinstance(raw, dict):
        return [], {}, {}

    accounts_raw = raw.get("accounts") or {}
    department_defaults_raw = raw.get("department_defaults") or {}
    account_display_names: Dict[str, str] = {}
    account_departments: Dict[str, List[str]] = {}
    if isinstance(accounts_raw, dict):
        for key, value in accounts_raw.items():
            if not isinstance(value, dict):
                continue
            account_key = str(key).strip()
            display_name = str(value.get("display_name") or "").strip()
            departments = _normalize_departments(value.get("department"))
            if not departments:
                departments = _normalize_departments(value.get("departments"))
            if account_key and display_name:
                account_display_names[account_key] = display_name
                account_departments[account_key] = departments

    contacts: List[str] = []
    defaults: Dict[str, str] = {}
    contacts_by_department: Dict[str, List[str]] = {}
    if isinstance(department_defaults_raw, dict):
        for department, account_key in department_defaults_raw.items():
            department_name = str(department).strip()
            account = str(account_key).strip()
            display_name = account_display_names.get(account, "")
            if not department_name or not display_name:
                continue
            account_departments_for_default = account_departments.get(account, [])
            if account_departments_for_default and department_name not in account_departments_for_default:
                continue
            label = f"{department_name} {display_name}".strip()
            defaults[department_name] = label
            if label not in contacts_by_department.setdefault(department_name, []):
                contacts_by_department[department_name].append(label)
            if label not in contacts:
                contacts.append(label)

    for account_key, display_name in account_display_names.items():
        departments = account_departments.get(account_key, [])
        for department_name in departments:
            label = f"{department_name} {display_name}".strip()
            if label not in contacts_by_department.setdefault(department_name, []):
                contacts_by_department[department_name].append(label)
            if label not in contacts:
                contacts.append(label)

    return contacts, defaults, contacts_by_department


def load_order_contacts(email_settings_path: Path) -> Tuple[List[str], Dict[str, str], Dict[str, List[str]]]:
    if email_settings_path.exists():
        try:
            with email_settings_path.open("r", encoding="utf-8-sig") as stream:
                settings = json.load(stream)
        except Exception:
            settings = {}
        contacts, defaults, contacts_by_department = _build_contacts_from_email_settings(settings)
        if contacts:
            return contacts, defaults, contacts_by_department

    return [], {}, {}


ORDER_CONTACTS, ORDER_CONTACT_DEFAULTS, ORDER_CONTACTS_BY_DEPARTMENT = load_order_contacts(EMAIL_SETTINGS_PATH)


def load_email_settings_config(path: Path) -> Dict[str, object]:
    default_value: Dict[str, object] = {
        "smtp_server": "",
        "smtp_port": 587,
        "accounts": {},
        "department_defaults": {},
    }
    if not path.exists():
        return default_value
    try:
        with path.open("r", encoding="utf-8-sig") as stream:
            raw = json.load(stream)
    except Exception:
        return default_value
    if not isinstance(raw, dict):
        return default_value
    return raw


def normalize_email_settings(raw: object) -> Dict[str, object]:
    if not isinstance(raw, dict):
        return {
            "smtp_server": "",
            "smtp_port": 587,
            "accounts": {},
            "department_defaults": {},
        }

    smtp_server = str(raw.get("smtp_server") or "").strip()
    try:
        smtp_port = int(raw.get("smtp_port") or 587)
    except Exception:
        smtp_port = 587

    normalized_accounts: Dict[str, Dict[str, str]] = {}
    accounts_raw = raw.get("accounts") or {}
    if isinstance(accounts_raw, dict):
        for key, value in accounts_raw.items():
            if not isinstance(value, dict):
                continue
            account_key = str(key).strip()
            if not account_key:
                continue
            display_name = str(value.get("display_name") or "").strip()
            sender = str(value.get("sender") or "").strip()
            department = str(value.get("department") or "").strip()
            if not display_name or not sender:
                continue
            normalized_accounts[account_key] = {
                "display_name": display_name,
                "sender": sender,
                "department": department,
            }

    normalized_defaults: Dict[str, str] = {}
    defaults_raw = raw.get("department_defaults") or {}
    if isinstance(defaults_raw, dict):
        for department, account_key in defaults_raw.items():
            department_name = str(department).strip()
            key = str(account_key).strip()
            if not department_name or key not in normalized_accounts:
                continue
            normalized_defaults[department_name] = key

    return {
        "smtp_server": smtp_server,
        "smtp_port": smtp_port,
        "accounts": normalized_accounts,
        "department_defaults": normalized_defaults,
    }


def save_email_settings_config(path: Path, settings: Dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(settings, stream, ensure_ascii=False, indent=2)
    temp_path.replace(path)


def get_purchase_order_service(db: Session) -> PurchaseOrderService:
    return PurchaseOrderService(db=db, templates=templates, project_root=PROJECT_ROOT)

SAMPLE_INVENTORY: List[InventorySnapshot] = [
    InventorySnapshot(
        item_id=1,
        item_code='16167',
        name='ボーリングバイト',
        item_type='工具',
        usage='加工',
        department='生産部',
        manufacturer='まじま機工',
        shelf='A-01',
        unit='本',
        on_hand=12,
        reorder_point=20,
        last_activity='出庫',
        last_updated=datetime(2026, 1, 28, 16, 12),
        location='A-01',
        supplier='まじま機工',
    ),
    InventorySnapshot(
        item_id=2,
        item_code='21346',
        name='ネジ切りバイト',
        item_type='工具',
        usage='加工',
        department='品質保証部',
        manufacturer='まじま機工',
        shelf='A-02',
        unit='本',
        on_hand=4,
        reorder_point=5,
        last_activity='出庫',
        last_updated=datetime(2026, 1, 29, 9, 45),
        location='A-02',
        supplier='まじま機工',
    ),
    InventorySnapshot(
        item_id=3,
        item_code='440617',
        name='クイックルハンディ取替用シート',
        item_type='清掃用品',
        usage='清掃',
        department='総務部',
        manufacturer='花王',
        shelf='C-11',
        unit='袋',
        on_hand=5,
        reorder_point=8,
        last_activity='調整',
        last_updated=datetime(2026, 1, 30, 10, 30),
        location='C-11',
        supplier='岩瀬産業(株)',
    ),
]

RECENT_TRANSACTIONS = [
    {
        'item': 'ボーリングバイト',
        'department': '生産部',
        'type': '出庫',
        'delta': '-2 本',
        'date': '2026/01/29 14:10',
        'note': '現場使用',
    },
    {
        'item': 'ネジ切りバイト',
        'department': '品質保証部',
        'type': '調整',
        'delta': '+1 本',
        'date': '2026/01/29 11:50',
        'note': '棚卸差分補正',
    },
    {
        'item': 'クイックルハンディ取替用シート',
        'department': '総務部',
        'type': '出庫',
        'delta': '-1 袋',
        'date': '2026/01/28 09:30',
        'note': '清掃で使用',
    },
]

BASE_NAV_LINKS = [
    {'label': 'ダッシュボード', 'href': '/dashboard'},
    {'label': '在庫管理', 'href': '/inventory'},
    {'label': '発注管理', 'href': '/orders'},
    {'label': '入出庫管理', 'href': '/logistics'},
    {'label': '購入品管理', 'href': '/purchase-results'},
    {'label': 'データ管理', 'href': '/manage/suppliers'},
    {'label': '履歴', 'href': '/history'},
]


def build_nav_links(active_href: str, current_user: Optional[Dict[str, Any]] = None) -> List[Dict[str, object]]:
    """画面上部のナビゲーションリンク一覧を構築する。

    - 左側: 共通メニュー（ダッシュボード、在庫管理、発注管理など）
    - 右側: 管理者ログイン / ログアウト
      - 管理者ログインは常に表示（管理者・担当者がここからログインできるようにする）
      - ログアウトはログイン済みの場合のみ表示
    """
    links: List[Dict[str, object]] = []
    for link in BASE_NAV_LINKS:
        if link["href"] == "/manage/suppliers" and not is_manager_or_higher_user(current_user):
            # データ管理は manager 以上のみ表示
            continue
        if link["href"] in ("/orders", "/logistics", "/purchase-results") and not is_manager_or_higher_user(current_user):
            # 発注管理・入出庫管理・購入品管理は manager 以上のみ表示
            continue
        links.append(
            {
                "label": link["label"],
                "href": link["href"],
                "active": link["href"] == active_href,
                "right": False,
            }
        )

    # 右側ナビゲーション（管理者ログイン／ログアウト）
    # 管理者ログインリンクは常に表示し、管理者や担当者がここからログインできるようにする
    links.append(
        {
            "label": "管理者ログイン",
            "href": LOGIN_ROUTE_PATH,
            "active": active_href == LOGIN_ROUTE_PATH,
            "right": True,  # 最初の右側リンクとして右寄せ用クラスを付与する
        }
    )

    # ログイン済みの場合のみログアウトを表示（管理者ログインの右側）
    if current_user:
        links.append(
            {
                "label": "ログアウト",
                "href": "/logout",
                "active": False,
                "right": False,
            }
        )

    return links

def count_deliveries_due_today(db: Session) -> int:
    """本日が回答納期かつ入荷待ちの発注件数を返す（DB実データのみ）。"""
    today = datetime.now(JST_ZONE).date()
    subq = (
        select(PurchaseOrderLine.purchase_order_id)
        .select_from(PurchaseOrderLine)
        .join(PurchaseOrder, PurchaseOrder.id == PurchaseOrderLine.purchase_order_id)
        .where(PurchaseOrderLine.vendor_reply_due_date == today)
        .where(PurchaseOrder.status.in_([PurchaseOrderStatus.SENT.value, PurchaseOrderStatus.WAITING.value]))
        .distinct()
    )
    count = db.scalar(select(func.count()).select_from(subq.subquery())) or 0
    return int(count)


def load_item_order_status_map(db: Session, item_ids: List[int]) -> Dict[int, Tuple[str, str]]:
    """
    品目ごとの発注状況を返す。key=item_id, value=(表示用ステータス, 納期YYYY/MM/DD)。
    発注依頼済=DRAFT/CONFIRMED、入荷待ち=SENT/WAITING。複数行ある場合は入荷待ちを優先し、納期は最も早い日付。
    """
    if not item_ids:
        return {}
    open_statuses = [
        PurchaseOrderStatus.DRAFT.value,
        PurchaseOrderStatus.CONFIRMED.value,
        PurchaseOrderStatus.SENT.value,
        PurchaseOrderStatus.WAITING.value,
    ]
    stmt = (
        select(
            PurchaseOrderLine.item_id,
            PurchaseOrder.status,
            PurchaseOrderLine.vendor_reply_due_date,
        )
        .select_from(PurchaseOrderLine)
        .join(PurchaseOrder, PurchaseOrder.id == PurchaseOrderLine.purchase_order_id)
        .where(PurchaseOrder.status.in_(open_statuses))
        .where(PurchaseOrderLine.item_id.in_(item_ids))
    )
    rows = db.execute(stmt).all()
    # item_id -> (is_waiting: bool, earliest_due: date|None)
    by_item: Dict[int, Tuple[bool, Optional[date]]] = {}
    for item_id, order_status, due_date in rows:
        if item_id is None:
            continue
        is_waiting = order_status in (PurchaseOrderStatus.SENT.value, PurchaseOrderStatus.WAITING.value)
        prev = by_item.get(item_id)
        if prev is None:
            by_item[item_id] = (is_waiting, due_date)
        else:
            prev_waiting, prev_due = prev
            earliest = prev_due
            if due_date is not None and (earliest is None or due_date < earliest):
                earliest = due_date
            by_item[item_id] = (prev_waiting or is_waiting, earliest)
    result: Dict[int, Tuple[str, str]] = {}
    for iid, (is_waiting, due) in by_item.items():
        status_ja = "入荷待ち" if is_waiting else "発注依頼済"
        due_str = due.strftime("%Y/%m/%d") if due else ""
        result[iid] = (status_ja, due_str)
    return result


app = FastAPI(
    title='購入品一元管理システム',
    description='社内向け在庫・発注管理アプリ',
    docs_url='/internal/docs',
    redoc_url=None,
    openapi_url='/internal/openapi.json',
)

@app.on_event("startup")
def on_startup() -> None:
    init_db()
    ensure_bootstrap_admin_user()

BASE_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = BASE_DIR / 'web' / 'templates'
STATIC_DIR = BASE_DIR / 'web' / 'static'

# 発注ステータスの日本語表示用（バッジなど）
PURCHASE_ORDER_STATUS_JA = {
    PurchaseOrderStatus.DRAFT.value: "下書き",
    PurchaseOrderStatus.CONFIRMED.value: "確定",
    PurchaseOrderStatus.SENT.value: "送信済",
    PurchaseOrderStatus.WAITING.value: "入荷待ち",
    PurchaseOrderStatus.RECEIVED.value: "納品済",
    PurchaseOrderStatus.CANCELLED.value: "取消",
}

def _filter_status_ja(value: str) -> str:
    """発注ステータスを日本語ラベルに変換するJinjaフィルタ用"""
    return PURCHASE_ORDER_STATUS_JA.get(str(value), str(value))

templates = Jinja2Templates(directory=TEMPLATE_DIR)
templates.env.filters['urlencode'] = lambda value: quote_plus(str(value))
templates.env.filters['status_ja'] = _filter_status_ja
app.mount('/static', StaticFiles(directory=STATIC_DIR), name='static')


class AuthContextMiddleware:
    """認証コンテキストを付与し、要認証パスでは未ログイン時はログインへリダイレクトする。"""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        path = request.url.path

        # 認証完全免除（ログイン画面・ログアウト・静的・API docs）
        if _is_auth_exempt_path(path):
            user = load_user_context_from_session(request)
            if user:
                scope.setdefault("state", {})["current_user"] = user
            await self.app(scope, receive, send)
            return

        # 一般ユーザー向け公開パス（ダッシュボード・在庫一覧など）はログイン不要
        if _is_public_path(path):
            user = load_user_context_from_session(request)
            if user:
                scope.setdefault("state", {})["current_user"] = user
            await self.app(scope, receive, send)
            return

        # 上記以外はログイン必須
        user = load_user_context_from_session(request)
        if not user:
            if _is_api_auth_path(path):
                response = JSONResponse(status_code=401, content={"detail": "ログインが必要です。"})
            else:
                next_path = path
                if request.url.query:
                    next_path = f"{next_path}?{request.url.query}"
                redirect_next = quote_plus(next_path)
                response = RedirectResponse(url=f"{LOGIN_ROUTE_PATH}?next={redirect_next}", status_code=303)
            await response(scope, receive, send)
            return

        scope.setdefault("state", {})["current_user"] = user
        await self.app(scope, receive, send)


app.add_middleware(AuthContextMiddleware)
# max_age: セッションCookieの有効期限。None=ブラウザ終了まで。>0で同一端末でログイン状態を保持（例: 14日）
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    same_site="lax",
    max_age=SESSION_COOKIE_MAX_AGE,
)


@app.get(LOGIN_ROUTE_PATH, response_class=HTMLResponse, include_in_schema=False)
def login_page(
    request: Request,
    next: str = Query("/dashboard"),
) -> HTMLResponse:
    current_user = load_user_context_from_session(request)
    next_path = _normalize_next_path(next)
    if current_user:
        return RedirectResponse(url=next_path, status_code=303)
    context = {
        "request": request,
        "next_path": next_path,
        "error": "",
    }
    return templates.TemplateResponse("login.html", context)


@app.post(LOGIN_ROUTE_PATH, response_class=HTMLResponse, include_in_schema=False)
def login_submit(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    next_path: str = Form("/dashboard"),
) -> HTMLResponse:
    normalized_username = username.strip()
    normalized_next = _normalize_next_path(next_path)
    if not normalized_username or not password:
        context = {
            "request": request,
            "next_path": normalized_next,
            "error": "ユーザー名とパスワードを入力してください。",
        }
        return templates.TemplateResponse("login.html", context, status_code=400)

    with SessionLocal() as db:
        user = db.scalar(
            select(AppUser).filter(
                AppUser.username == normalized_username,
                AppUser.is_active.is_(True),
            )
        )
        if not user or not verify_password(password, user.password_hash):
            context = {
                "request": request,
                "next_path": normalized_next,
                "error": "ユーザー名またはパスワードが正しくありません。",
            }
            return templates.TemplateResponse("login.html", context, status_code=401)

    request.session[SESSION_USER_ID_KEY] = user.id
    return RedirectResponse(url=normalized_next, status_code=303)


@app.get("/logout", include_in_schema=False)
def logout(request: Request) -> RedirectResponse:
    request.session.pop(SESSION_USER_ID_KEY, None)
    return RedirectResponse(url=LOGIN_ROUTE_PATH, status_code=303)

def calculate_status(snapshot: InventorySnapshot) -> Tuple[str, str, str]:
    if snapshot.reorder_point <= 0:
        return (
            '正常',
            'bg-emerald-50 text-emerald-700 border-emerald-100',
            '発注点が未設定です',
        )
    if snapshot.on_hand <= snapshot.reorder_point:
        return (
            '不足',
            'bg-red-50 text-red-600 border-red-100',
            '至急発注が必要です',
        )
    buffer = max(5, snapshot.reorder_point // 2)
    if snapshot.on_hand <= snapshot.reorder_point + buffer:
        return (
            '注意',
            'bg-amber-50 text-amber-600 border-amber-100',
            '発注点に近づいています',
        )
    return (
        '正常',
        'bg-emerald-50 text-emerald-700 border-emerald-100',
        '在庫は安定しています',
    )

def build_inventory_row(
    snapshot: InventorySnapshot,
    order_map: Optional[Dict[int, Tuple[str, str]]] = None,
) -> Dict[str, Any]:
    label, badge, description = calculate_status(snapshot)
    gap = snapshot.on_hand - snapshot.reorder_point
    order_status_display = ""
    order_due_display = ""
    if order_map and snapshot.item_id in order_map:
        order_status_display, order_due_display = order_map[snapshot.item_id]
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
        'order_status_display': order_status_display,
        'order_due_display': order_due_display,
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


def count_low_stock_by_department(suggestions: List[Dict[str, object]]) -> Dict[str, int]:
    counter: Dict[str, int] = {}
    for suggestion in suggestions:
        department = normalize_field(suggestion.get('department', '')) or UNSET_LABEL
        counter[department] = counter.get(department, 0) + 1
    return counter


def load_low_stock_suggestions(db: Session) -> List[Dict[str, object]]:
    snapshots = load_inventory_snapshots(db)
    return build_low_stock_suggestions_from_snapshots(snapshots)


def build_low_stock_suggestions_from_snapshots(
    snapshots: List[InventorySnapshot],
) -> List[Dict[str, object]]:
    suggestions: List[Dict[str, object]] = []
    for snapshot in snapshots:
        if snapshot.reorder_point <= 0:
            continue
        gap = snapshot.on_hand - snapshot.reorder_point
        if gap > 0:
            continue
        suggestions.append(
            {
                'item_id': snapshot.item_id,
                'item_code': snapshot.item_code,
                'name': snapshot.name,
                'department': snapshot.department or UNSET_LABEL,
                'on_hand': snapshot.on_hand,
                'reorder_point': snapshot.reorder_point,
                'gap': gap,
                'gap_label': '{:+,d}'.format(gap),
                'supplier': snapshot.supplier or UNSET_LABEL,
            }
        )
    return suggestions


def build_orders_url(department: str = "") -> str:
    params = []
    final_department = normalize_field(department)
    if final_department:
        params.append(('department', final_department))
    if not params:
        return '/orders'
    return '/orders?' + '&'.join(f'{name}={quote_plus(value)}' for name, value in params)


def build_inventory_status_payload(
    item: Item,
    inventory_item: InventoryItem,
    last_tx: Optional[InventoryTransaction],
    db: Optional[Session] = None,
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
    order_status_display = ""
    order_due_display = ""
    if db is not None:
        order_map = load_item_order_status_map(db, [item.id])
        if item.id in order_map:
            order_status_display, order_due_display = order_map[item.id]
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
        "order_status_display": order_status_display,
        "order_due_display": order_due_display,
    }


def load_inventory_snapshots(db: Session) -> List[InventorySnapshot]:
    # 在庫一覧には管理対象のみ表示する
    stmt = (
        select(Item)
        .where(or_(Item.management_type == "管理", Item.management_type.is_(None)))
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
                "item": tx.item.name if tx.item else "荳肴・蜩∫岼",
                "department": tx.item.department if tx.item and tx.item.department else "譛ｪ險ｭ螳夐Κ鄂ｲ",
                "type": summary.split("繝ｻ")[0] if summary else "",
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


def count_today_movement_transactions(db: Session) -> Dict[str, int]:
    now = datetime.now(JST_ZONE)
    today = now.date()
    stmt = (
        select(InventoryTransaction)
        .order_by(InventoryTransaction.occurred_at.desc())
        .limit(1000)
    )
    txs = db.scalars(stmt).all()
    receipt_count = 0
    issue_count = 0
    adjust_count = 0
    for tx in txs:
        occurred_day = to_jst(tx.occurred_at).date()
        if occurred_day != today:
            continue
        tx_type = tx.tx_type.value if isinstance(tx.tx_type, TransactionType) else str(tx.tx_type)
        if tx_type == TransactionType.RECEIPT.value:
            receipt_count += 1
        elif tx_type == TransactionType.ISSUE.value:
            issue_count += 1
        else:
            adjust_count += 1
    return {
        "receipt_count": receipt_count,
        "issue_count": issue_count,
        "adjust_count": adjust_count,
    }


def load_pending_receipt_orders(db: Session, limit: int = 100) -> List[Dict[str, Any]]:
    service = get_purchase_order_service(db)
    orders = service.list_orders()
    pending_statuses = {PurchaseOrderStatus.SENT.value, PurchaseOrderStatus.WAITING.value}
    pending = [order for order in orders if str(order.get("status") or "") in pending_statuses]
    pending.sort(key=lambda row: int(row.get("id") or 0), reverse=True)
    if limit > 0:
        pending = pending[:limit]
    for order in pending:
        lines = order.get("lines") or []
        order["line_count"] = len(lines)
        order["total_quantity"] = sum(int(line.get("quantity") or 0) for line in lines)
        order["received_quantity_total"] = sum(int(line.get("received_quantity") or 0) for line in lines)
        order["remaining_quantity_total"] = sum(int(line.get("remaining_quantity") or 0) for line in lines)
    return pending


def load_recent_adjustment_transactions(db: Session, limit: int = 20) -> List[Dict[str, str]]:
    stmt = (
        select(InventoryTransaction)
        .filter(InventoryTransaction.tx_type == TransactionType.ADJUST)
        .order_by(InventoryTransaction.occurred_at.desc())
        .limit(limit)
        .options(selectinload(InventoryTransaction.item))
    )
    txs = db.scalars(stmt).all()
    rows: List[Dict[str, str]] = []
    for tx in txs:
        occurred_at = to_jst(tx.occurred_at).strftime("%Y/%m/%d %H:%M")
        item_name = tx.item.name if tx.item else "未設定品目"
        item_code = tx.item.item_code if tx.item else ""
        delta = f"{'+' if tx.delta >= 0 else ''}{tx.delta}"
        rows.append(
            {
                "date": occurred_at,
                "item": item_name,
                "item_code": item_code,
                "delta": delta,
                "reason": tx.reason or "",
                "created_by": tx.created_by or "",
            }
        )
    return rows


@app.get("/recent-transactions")
def recent_transactions(
    db: Session = Depends(get_db),
    limit: int = Query(4, ge=1, le=20),
    current_user: Optional[Dict[str, Any]] = Depends(get_optional_user),
) -> Dict[str, List[Dict[str, str]]]:
    """直近トランザクション。未ログインでも取得可能（一般ユーザー向けダッシュボード・在庫で利用）。DBの実データのみ返す。"""
    transactions = load_recent_transactions(db, limit)
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
    return RedirectResponse(url='/dashboard')


@app.get('/dashboard', response_class=HTMLResponse)
def dashboard_page(
    request: Request,
    db: Session = Depends(get_db),
    current_user: Optional[Dict[str, Any]] = Depends(get_optional_user),
) -> HTMLResponse:
    snapshots = load_inventory_snapshots(db)
    if not snapshots:
        snapshots = SAMPLE_INVENTORY

    low_stock_suggestions = load_low_stock_suggestions(db)
    if not low_stock_suggestions:
        low_stock_suggestions = build_low_stock_suggestions_from_snapshots(snapshots)
    low_stock_suggestions = sorted(
        low_stock_suggestions,
        key=lambda row: (
            int(row.get("gap") or 0),
            str(row.get("department") or ""),
            str(row.get("item_code") or ""),
        ),
    )
    low_stock_top = low_stock_suggestions[:8]
    low_stock_by_department = sorted(
        count_low_stock_by_department(low_stock_suggestions).items(),
        key=lambda row: (-int(row[1]), str(row[0])),
    )

    service = get_purchase_order_service(db)
    orders = service.list_orders()
    status_order = [
        PurchaseOrderStatus.DRAFT.value,
        PurchaseOrderStatus.CONFIRMED.value,
        PurchaseOrderStatus.SENT.value,
        PurchaseOrderStatus.WAITING.value,
        PurchaseOrderStatus.RECEIVED.value,
    ]
    status_labels = {
        PurchaseOrderStatus.DRAFT.value: "下書き",
        PurchaseOrderStatus.CONFIRMED.value: "確定",
        PurchaseOrderStatus.SENT.value: "送信済",
        PurchaseOrderStatus.WAITING.value: "入荷待ち",
        PurchaseOrderStatus.RECEIVED.value: "納品済",
    }
    status_counts_map = {status: 0 for status in status_order}
    for order in orders:
        status = str(order.get("status") or "")
        if status in status_counts_map:
            status_counts_map[status] += 1
    order_status_cards = [
        {
            "status": status,
            "label": status_labels.get(status, status),
            "count": status_counts_map.get(status, 0),
        }
        for status in status_order
    ]

    pending_orders_all = load_pending_receipt_orders(db, limit=0)
    pending_orders_top = pending_orders_all[:6]
    pending_remaining_qty = sum(int(order.get("remaining_quantity_total") or 0) for order in pending_orders_all)

    today_counts = count_today_movement_transactions(db)
    movement_total = (
        int(today_counts.get("receipt_count") or 0)
        + int(today_counts.get("issue_count") or 0)
        + int(today_counts.get("adjust_count") or 0)
    )

    recent_transactions = load_recent_transactions(db, limit=8)

    kpi_cards = [
        {
            "label": "管理品目数",
            "value": len(snapshots),
            "note": "在庫管理対象アイテム",
            "icon": "inventory_2",
        },
        {
            "label": "要発注品目",
            "value": len(low_stock_suggestions),
            "note": "発注点以下（不足含む）",
            "icon": "warning",
        },
        {
            "label": "入庫待ち発注",
            "value": len(pending_orders_all),
            "note": f"残数量 {pending_remaining_qty}",
            "icon": "local_shipping",
        },
        {
            "label": "本日移動件数",
            "value": movement_total,
            "note": "入庫・出庫・調整の合計",
            "icon": "sync_alt",
        },
    ]

    context = {
        "request": request,
        "nav_links": build_nav_links('/dashboard', current_user),
        "kpi_cards": kpi_cards,
        "low_stock_items": low_stock_top,
        "low_stock_by_department": low_stock_by_department[:6],
        "order_status_cards": order_status_cards,
        "order_total_count": len(orders),
        "pending_orders": pending_orders_top,
        "pending_order_total": len(pending_orders_all),
        "today_counts": today_counts,
        "recent_transactions": recent_transactions,
        "build_orders_url": build_orders_url,
        "now": datetime.now(JST_ZONE),
        "current_user": current_user,
    }
    return templates.TemplateResponse('dashboard.html', context)

@app.get('/inventory', response_class=HTMLResponse)
def inventory_index(
    request: Request,
    q: str = Query('', alias='q'),
    category: str = Query('', alias='category'),
    usage: str = Query('', alias='usage'),
    department: str = Query('', alias='department'),
    message: str = Query('', alias='message'),
    db: Session = Depends(get_db),
    current_user: Optional[Dict[str, Any]] = Depends(get_optional_user),
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
    order_map = load_item_order_status_map(db, [s.item_id for s in filtered_snapshots])
    rows = [build_inventory_row(snapshot, order_map) for snapshot in filtered_snapshots]

    total_items = len(snapshots)
    attention_count = sum(1 for snapshot in snapshots if calculate_status(snapshot)[0] != '正常')
    low_stock_count = sum(1 for snapshot in snapshots if snapshot.on_hand <= snapshot.reorder_point)
    deliveries_due_today = count_deliveries_due_today(db)

    kpi_cards = [
        {
            'label': '管理品目数',
            'value': total_items,
            'note': '仕入品マスタ登録件数',
            'icon': 'inventory_2',
        },
        {
            'label': '注意対象',
            'value': attention_count,
            'note': '不足または警告状態',
            'icon': 'priority_high',
        },
        {
            'label': '在庫不足',
            'value': low_stock_count,
            'note': '発注点以下の品目数',
            'icon': 'warning',
        },
        {
            'label': '本日入荷予定',
            'value': deliveries_due_today,
            'note': '本日が回答納期の入荷待ち発注件数',
            'icon': 'local_shipping',
        },
    ]

    alert_message = message.strip()
    context = {
        'request': request,
        'nav_links': build_nav_links('/inventory', current_user),
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
        'now': datetime.now(JST_ZONE),
        'build_inventory_url': build_inventory_url,
        'alert_message': alert_message,
        'current_user': current_user,
    }
    return templates.TemplateResponse('inventory_list.html', context)


@app.get('/logistics', response_class=HTMLResponse)
def logistics_page(
    request: Request,
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(require_manager_user),
) -> HTMLResponse:
    snapshots = load_inventory_snapshots(db)
    item_options = [
        {
            "item_code": snapshot.item_code,
            "name": snapshot.name,
            "department": snapshot.department,
            "on_hand": snapshot.on_hand,
            "unit": snapshot.unit,
            "supplier": snapshot.supplier,
        }
        for snapshot in snapshots
    ]
    pending_orders = load_pending_receipt_orders(db, limit=100)
    recent_adjustments = load_recent_adjustment_transactions(db, limit=20)
    counts = count_today_movement_transactions(db)
    pending_line_count = sum(int(order.get("line_count") or 0) for order in pending_orders)
    pending_remaining_qty = sum(int(order.get("remaining_quantity_total") or 0) for order in pending_orders)
    kpi_cards = [
        {
            "label": "入庫待ち発注",
            "value": len(pending_orders),
            "note": "発注メール送信後の待機件数",
            "icon": "local_shipping",
        },
        {
            "label": "入庫待ち明細",
            "value": pending_line_count,
            "note": "入庫計上待ちの明細行数",
            "icon": "receipt_long",
        },
        {
            "label": "入庫残数量",
            "value": pending_remaining_qty,
            "note": "未入荷の残数合計",
            "icon": "inventory",
        },
        {
            "label": "本日の入庫計上",
            "value": counts["receipt_count"],
            "note": "在庫反映済み入庫件数",
            "icon": "move_to_inbox",
        },
    ]
    context = {
        "request": request,
        "nav_links": build_nav_links('/logistics', current_user),
        "kpi_cards": kpi_cards,
        "item_options": item_options,
        "item_options_json": json.dumps(item_options),
        "pending_orders": pending_orders,
        "recent_adjustments": recent_adjustments,
        "now": datetime.now(JST_ZONE),
        "current_user": current_user,
    }
    return templates.TemplateResponse('logistics.html', context)


@app.post('/api/logistics/inbound/{order_id}/receive')
def receive_purchase_order_to_inventory(
    order_id: int,
    payload: ReceivePurchaseOrderPayload,
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(require_manager_user),
) -> Dict[str, object]:
    service = get_purchase_order_service(db)
    updated_by = str(current_user.get("display_name") or current_user.get("username") or "system")
    line_receipts: Dict[int, int] = {}
    for row in payload.lines:
        line_receipts[int(row.line_id)] = int(row.quantity)
    line_unit_prices: Optional[Dict[int, Optional[int]]] = None
    if payload.line_unit_prices:
        line_unit_prices = {int(k): v for k, v in payload.line_unit_prices.items()}
    try:
        result = service.receive_order_partial(
            order_id=order_id,
            updated_by=updated_by,
            line_receipts=line_receipts or None,
            delivery_date=payload.delivery_date,
            delivery_note_number=payload.delivery_note_number,
            line_unit_prices=line_unit_prices,
        )
    except PurchaseOrderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    status = str(result.get("status") or PurchaseOrderStatus.WAITING.value)
    fully_received = bool(result.get("fully_received"))
    message = (
        f"PO #{order_id} の入庫計上が完了しました。"
        if fully_received
        else f"PO #{order_id} を分納入庫として計上しました。残数は引き続き入庫待ちです。"
    )
    return {
        "purchase_order_id": int(result.get("purchase_order_id") or order_id),
        "status": status,
        "fully_received": fully_received,
        "message": message,
    }


@app.post('/inventory/issues')
def inventory_issue(
    item_code: str = Form(...),
    quantity: int = Form(...),
    reason: str = Form(""),
    created_by: str = Form(""),
    next_url: str = Form("/inventory"),
    db: Session = Depends(get_db),
    current_user: Optional[Dict[str, Any]] = Depends(get_optional_user),
) -> RedirectResponse:
    if quantity <= 0:
        raise HTTPException(status_code=400, detail="出庫数は1以上で指定してください。")
    item = db.scalar(select(Item).filter(Item.item_code == item_code))
    if not item:
        raise HTTPException(status_code=404, detail="対象品目が見つかりません。")
    inventory_item = db.scalar(select(InventoryItem).filter(InventoryItem.item_id == item.id))
    if not inventory_item:
        raise HTTPException(status_code=400, detail="在庫情報が見つかりません。")
    if inventory_item.quantity_on_hand < quantity:
        raise HTTPException(status_code=400, detail="在庫数が不足しています。")

    inventory_item.quantity_on_hand -= quantity
    created_by_value = created_by or (
        str(current_user.get("username") or "system") if current_user else "system"
    )
    tx = InventoryTransaction(
        item_id=item.id,
        tx_type=TransactionType.ISSUE,
        delta=-quantity,
        reason=reason or "",
        note="",
        occurred_at=datetime.now(JST_ZONE),
        created_by=created_by_value,
    )
    db.add(tx)
    db.commit()
    separator = '&' if '?' in next_url else '?'
    message = f"{item.name} ({item.item_code}) を {quantity} 出庫しました。"
    return RedirectResponse(url=f"{next_url}{separator}message={quote_plus(message)}", status_code=303)


@app.post('/api/inventory/receipts', response_model=IssueRecordResponse)
def api_inventory_receipt(
    payload: ReceiptRecordRequest,
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(require_manager_user),
) -> IssueRecordResponse:
    _ = current_user
    if payload.quantity <= 0:
        raise HTTPException(status_code=400, detail="数量は1以上で指定してください。")
    item = db.scalar(select(Item).filter(Item.item_code == payload.item_code))
    if not item:
        raise HTTPException(status_code=404, detail="対象品目が見つかりません。")
    inventory_item = db.scalar(select(InventoryItem).filter(InventoryItem.item_id == item.id))
    if not inventory_item:
        raise HTTPException(status_code=400, detail="在庫情報が見つかりません。")

    inventory_item.quantity_on_hand += payload.quantity
    tx = InventoryTransaction(
        item_id=item.id,
        tx_type=TransactionType.RECEIPT,
        delta=payload.quantity,
        reason=payload.reason or "",
        note="",
        occurred_at=datetime.now(JST_ZONE),
        created_by=str(current_user.get("username") or "system"),
    )
    db.add(tx)
    db.commit()
    response_data = build_inventory_status_payload(item, inventory_item, tx, db=db)
    response_data["message"] = f"{item.name} ({item.item_code}) を {payload.quantity} 入庫しました。"
    return IssueRecordResponse(**response_data)


@app.post('/api/inventory/issues', response_model=IssueRecordResponse)
def api_inventory_issue(
    payload: IssueRecordRequest,
    db: Session = Depends(get_db),
    current_user: Optional[Dict[str, Any]] = Depends(get_optional_user),
) -> IssueRecordResponse:
    if payload.quantity <= 0:
        raise HTTPException(status_code=400, detail="数量は1以上で指定してください。")
    item = db.scalar(select(Item).filter(Item.item_code == payload.item_code))
    if not item:
        raise HTTPException(status_code=404, detail="対象品目が見つかりません。")
    inventory_item = db.scalar(select(InventoryItem).filter(InventoryItem.item_id == item.id))
    if not inventory_item:
        raise HTTPException(status_code=400, detail="在庫情報が見つかりません。")
    if inventory_item.quantity_on_hand < payload.quantity:
        raise HTTPException(status_code=400, detail="在庫数が不足しています。")

    inventory_item.quantity_on_hand -= payload.quantity
    created_by_value = str(current_user.get("username") or "system") if current_user else "system"
    tx = InventoryTransaction(
        item_id=item.id,
        tx_type=TransactionType.ISSUE,
        delta=-payload.quantity,
        reason=payload.reason or "",
        note="",
        occurred_at=datetime.now(JST_ZONE),
        created_by=created_by_value,
    )
    db.add(tx)
    db.commit()
    response_data = build_inventory_status_payload(item, inventory_item, tx, db=db)
    response_data["message"] = f"{item.name} ({item.item_code}) を {payload.quantity} 出庫しました。"
    return IssueRecordResponse(**response_data)


@app.post('/api/inventory/adjustments', response_model=IssueRecordResponse)
def api_inventory_adjustment(
    payload: InventoryAdjustmentRequest,
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(require_manager_user),
) -> IssueRecordResponse:
    if payload.delta == 0:
        raise HTTPException(status_code=400, detail="調整数は0以外で指定してください。")
    item = db.scalar(select(Item).filter(Item.item_code == payload.item_code))
    if not item:
        raise HTTPException(status_code=404, detail="対象品目が見つかりません。")
    inventory_item = db.scalar(select(InventoryItem).filter(InventoryItem.item_id == item.id))
    if not inventory_item:
        raise HTTPException(status_code=400, detail="在庫情報が見つかりません。")

    after_quantity = (inventory_item.quantity_on_hand or 0) + payload.delta
    if after_quantity < 0:
        raise HTTPException(status_code=400, detail="調整後在庫が0未満になるため実行できません。")

    inventory_item.quantity_on_hand = after_quantity
    tx = InventoryTransaction(
        item_id=item.id,
        tx_type=TransactionType.ADJUST,
        delta=payload.delta,
        reason=(payload.reason or "").strip() or "在庫調整",
        note="入出庫管理",
        occurred_at=datetime.now(JST_ZONE),
        created_by=str(current_user.get("username") or "system"),
    )
    db.add(tx)
    db.commit()
    response_data = build_inventory_status_payload(item, inventory_item, tx, db=db)
    response_data["message"] = f"{item.name} ({item.item_code}) を {payload.delta:+d} 調整しました。"
    return IssueRecordResponse(**response_data)


@app.post('/inventory/adjust')
def inventory_adjust(
    item_code: str = Form(...),
    delta: int = Form(...),
    next_url: str = Form("/inventory"),
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(require_manager_user),
) -> RedirectResponse:
    _ = current_user
    if delta == 0:
        raise HTTPException(status_code=400, detail="調整数量を指定してください。")
    item = db.scalar(select(Item).filter(Item.item_code == item_code))
    if not item:
        raise HTTPException(status_code=404, detail="対象品目が見つかりません。")
    inventory_item = db.scalar(select(InventoryItem).filter(InventoryItem.item_id == item.id))
    if not inventory_item:
        raise HTTPException(status_code=400, detail="在庫情報が見つかりません。")

    inventory_item.quantity_on_hand += delta
    if inventory_item.quantity_on_hand < 0:
        raise HTTPException(status_code=400, detail="在庫数を0未満にはできません。")

    tx = InventoryTransaction(
        item_id=item.id,
        tx_type=TransactionType.ADJUST,
        delta=delta,
        reason="在庫一括調整",
        note="管理画面更新",
        occurred_at=datetime.now(JST_ZONE),
        created_by="system",
    )
    db.add(tx)
    db.commit()
    separator = '&' if '?' in next_url else '?'
    message = f"{item.name} を {delta:+d} 調整しました。"
    return RedirectResponse(url=f"{next_url}{separator}message={quote_plus(message)}", status_code=303)


@app.post('/inventory/inline-adjust')
def inventory_inline_adjust(
    payload: InlineAdjustmentPayload,
    db: Session = Depends(get_db),
    current_user: Optional[Dict[str, Any]] = Depends(get_optional_user),
) -> Dict[str, object]:
    if payload.target_quantity < 0:
        raise HTTPException(status_code=400, detail="在庫数量は0以上で指定してください。")
    item = db.scalar(select(Item).filter(Item.item_code == payload.item_code))
    if not item:
        raise HTTPException(status_code=404, detail="指定された品目が見つかりません。")
    inventory_item = db.scalar(select(InventoryItem).filter(InventoryItem.item_id == item.id))
    if not inventory_item:
        raise HTTPException(status_code=400, detail="在庫情報が未登録です。")

    before_quantity = inventory_item.quantity_on_hand or 0
    inventory_item.quantity_on_hand = payload.target_quantity
    delta = payload.target_quantity - before_quantity
    if delta != 0:
        tx = InventoryTransaction(
            item_id=item.id,
            tx_type=TransactionType.ADJUST,
            delta=delta,
            reason="在庫一括更新",
            note="一覧から更新",
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
    return build_inventory_status_payload(item, inventory_item, last_tx, db=db)


def build_manage_sections(current_user: Dict[str, Any], active_key: str) -> List[Dict[str, object]]:
    sections: List[Dict[str, object]] = [
        {"key": "suppliers", "label": "仕入先", "href": "/manage/suppliers"},
        {"key": "items", "label": "仕入品", "href": "/manage/items"},
    ]
    if is_admin_user(current_user):
        sections.append({"key": "email", "label": "メール設定", "href": "/manage/email-settings"})
    for section in sections:
        section["active"] = section["key"] == active_key
    return sections


def serialize_items_for_manage(items: List[Item]) -> List[Dict[str, object]]:
    return [
        {
            "id": item.id,
            "item_code": item.item_code,
            "name": item.name,
            "item_type": item.item_type or "",
            "usage": item.usage or "",
            "department": item.department or "",
            "manufacturer": item.manufacturer or "",
            "shelf": item.shelf or "",
            "unit": item.unit or "",
            "reorder_point": item.reorder_point or 0,
            "default_order_quantity": getattr(item, "default_order_quantity", 1) or 1,
            "unit_price": getattr(item, "unit_price", None),
            "account_name": getattr(item, "account_name", None) or "",
            "expense_item_name": getattr(item, "expense_item_name", None) or "",
            "management_type": item.management_type or "",
            "supplier_id": item.supplier_id,
            "supplier_name": item.supplier.name if item.supplier else "",
        }
        for item in items
    ]


@app.get('/manage/data', include_in_schema=False)
def manage_data(
    current_user: Dict[str, Any] = Depends(require_manager_user),
) -> RedirectResponse:
    _ = current_user
    return RedirectResponse(url='/manage/suppliers', status_code=303)


@app.get('/manage/suppliers', response_class=HTMLResponse)
def manage_suppliers(
    request: Request,
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(require_manager_user),
) -> HTMLResponse:
    suppliers = db.scalars(select(Supplier).order_by(Supplier.name.asc())).all()
    context = {
        'request': request,
        'nav_links': build_nav_links('/manage/suppliers', current_user),
        'manage_sections': build_manage_sections(current_user, 'suppliers'),
        'suppliers': suppliers,
        'now': datetime.now(JST_ZONE),
        'current_user': current_user,
    }
    return templates.TemplateResponse('manage_suppliers.html', context)


def _distinct_item_values(items: List[Item]) -> Dict[str, List[str]]:
    """仕入品一覧からカテゴリ・用途・部署・メーカー・棚番・品番の既存値リストを重複排除・ソートして返す。"""
    codes: Set[str] = set()
    types: Set[str] = set()
    usages: Set[str] = set()
    departments: Set[str] = set()
    manufacturers: Set[str] = set()
    shelves: Set[str] = set()
    for item in items:
        if item.item_code and item.item_code.strip():
            codes.add(item.item_code.strip())
        if item.item_type and item.item_type.strip():
            types.add(item.item_type.strip())
        if item.usage and item.usage.strip():
            usages.add(item.usage.strip())
        if item.department and item.department.strip():
            departments.add(item.department.strip())
        if item.manufacturer and item.manufacturer.strip():
            manufacturers.add(item.manufacturer.strip())
        if item.shelf and item.shelf.strip():
            shelves.add(item.shelf.strip())
    return {
        "item_codes": sorted(codes),
        "item_types": sorted(types),
        "usages": sorted(usages),
        "departments": sorted(departments),
        "manufacturers": sorted(manufacturers),
        "shelves": sorted(shelves),
    }


@app.get('/manage/items', response_class=HTMLResponse)
def manage_items(
    request: Request,
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(require_manager_user),
) -> HTMLResponse:
    suppliers = db.scalars(select(Supplier).order_by(Supplier.name.asc())).all()
    items = (
        db.scalars(select(Item).options(selectinload(Item.supplier)).order_by(Item.item_code.asc()))
        .all()
    )
    distinct = _distinct_item_values(items)
    context = {
        'request': request,
        'nav_links': build_nav_links('/manage/suppliers', current_user),
        'manage_sections': build_manage_sections(current_user, 'items'),
        'suppliers': suppliers,
        'items_data': serialize_items_for_manage(items),
        'distinct_item_codes': distinct["item_codes"],
        'distinct_item_types': distinct["item_types"],
        'distinct_usages': distinct["usages"],
        'distinct_departments': distinct["departments"],
        'distinct_manufacturers': distinct["manufacturers"],
        'distinct_shelves': distinct["shelves"],
        'now': datetime.now(JST_ZONE),
        'current_user': current_user,
    }
    return templates.TemplateResponse('manage_items.html', context)


def _query_purchase_results_filtered(
    db: Session,
    delivery_date_from: Optional[str] = None,
    delivery_date_to: Optional[str] = None,
    purchase_month: Optional[str] = None,
    supplier_id: Optional[int] = None,
    item_code: Optional[str] = None,
):
    stmt = (
        select(PurchaseResult)
        .options(selectinload(PurchaseResult.item), selectinload(PurchaseResult.supplier))
        .order_by(PurchaseResult.delivery_date.desc().nulls_last(), PurchaseResult.id.desc())
    )
    if delivery_date_from and delivery_date_from.strip():
        try:
            stmt = stmt.where(PurchaseResult.delivery_date >= date.fromisoformat(delivery_date_from.strip()))
        except ValueError:
            pass
    if delivery_date_to and delivery_date_to.strip():
        try:
            stmt = stmt.where(PurchaseResult.delivery_date <= date.fromisoformat(delivery_date_to.strip()))
        except ValueError:
            pass
    if purchase_month and purchase_month.strip() and len(purchase_month.strip()) == 4:
        stmt = stmt.where(PurchaseResult.purchase_month == purchase_month.strip())
    if supplier_id is not None:
        stmt = stmt.where(PurchaseResult.supplier_id == supplier_id)
    if item_code and item_code.strip():
        stmt = stmt.join(Item, PurchaseResult.item_id == Item.id).where(
            or_(Item.item_code.ilike(f"%{item_code.strip()}%"), Item.name.ilike(f"%{item_code.strip()}%"))
        )
    return db.scalars(stmt).unique().all()


@app.get('/purchase-results', response_class=HTMLResponse)
def purchase_results_page(
    request: Request,
    delivery_date_from: Optional[str] = Query(None),
    delivery_date_to: Optional[str] = Query(None),
    purchase_month: Optional[str] = Query(None),
    supplier_id: Optional[int] = Query(None),
    item_code: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(require_manager_user),
) -> HTMLResponse:
    results = _query_purchase_results_filtered(
        db,
        delivery_date_from=delivery_date_from,
        delivery_date_to=delivery_date_to,
        purchase_month=purchase_month,
        supplier_id=supplier_id,
        item_code=item_code,
    )
    suppliers = db.scalars(select(Supplier).order_by(Supplier.name.asc())).all()
    rows: List[Dict[str, Any]] = []
    for r in results:
        rows.append({
            "id": r.id,
            "delivery_date": r.delivery_date.isoformat() if r.delivery_date else "",
            "supplier_id": r.supplier_id,
            "supplier_name": r.supplier.name if r.supplier else "",
            "delivery_note_number": r.delivery_note_number or "",
            "item_code": r.item.item_code if r.item else "",
            "item_name": r.item.name if r.item else "",
            "quantity": r.quantity,
            "unit_price": r.unit_price,
            "amount": r.amount,
            "purchase_month": r.purchase_month or "",
            "account_name": r.account_name or "",
            "expense_item_name": r.expense_item_name or "",
            "purchaser_name": r.purchaser_name or "",
            "note": r.note or "",
        })
    context = {
        "request": request,
        "nav_links": build_nav_links("/purchase-results", current_user),
        "results": rows,
        "suppliers": suppliers,
        "filters": {
            "delivery_date_from": delivery_date_from or "",
            "delivery_date_to": delivery_date_to or "",
            "purchase_month": purchase_month or "",
            "supplier_id": supplier_id,
            "item_code": item_code or "",
        },
        "now": datetime.now(JST_ZONE),
        "current_user": current_user,
    }
    return templates.TemplateResponse("purchase_results.html", context)


@app.get('/purchase-results/csv')
def purchase_results_csv(
    delivery_date_from: Optional[str] = Query(None),
    delivery_date_to: Optional[str] = Query(None),
    purchase_month: Optional[str] = Query(None),
    supplier_id: Optional[int] = Query(None),
    item_code: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(require_manager_user),
) -> Response:
    results = _query_purchase_results_filtered(
        db,
        delivery_date_from=delivery_date_from,
        delivery_date_to=delivery_date_to,
        purchase_month=purchase_month,
        supplier_id=supplier_id,
        item_code=item_code,
    )
    import csv
    import io
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "納入日", "購入先", "納品書番号", "品番", "品名", "数量", "単価", "金額",
        "購入月", "科目名", "費目名", "購入者", "備考",
    ])
    for r in results:
        writer.writerow([
            r.delivery_date.isoformat() if r.delivery_date else "",
            r.supplier.name if r.supplier else "",
            r.delivery_note_number or "",
            r.item.item_code if r.item else "",
            r.item.name if r.item else "",
            r.quantity,
            r.unit_price if r.unit_price is not None else "",
            r.amount if r.amount is not None else "",
            r.purchase_month or "",
            r.account_name or "",
            r.expense_item_name or "",
            r.purchaser_name or "",
            r.note or "",
        ])
    body = buf.getvalue()
    return Response(
        content=body.encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=purchase_results.csv"},
    )


@app.patch('/api/purchase-results/{result_id}')
def update_purchase_result(
    result_id: int,
    payload: PurchaseResultUpdatePayload,
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(require_manager_user),
) -> Dict[str, object]:
    """購入実績1件を更新する。manager 以上のみ実行可能。"""
    _ = current_user
    row = db.get(PurchaseResult, result_id)
    if not row:
        raise HTTPException(status_code=404, detail="購入実績が見つかりません。")
    if payload.delivery_date is not None:
        if payload.delivery_date.strip() == "":
            row.delivery_date = None
        else:
            try:
                row.delivery_date = date.fromisoformat(payload.delivery_date.strip())
            except ValueError:
                raise HTTPException(status_code=400, detail="納入日の形式が不正です。")
    if payload.supplier_id is not None:
        sup = db.get(Supplier, payload.supplier_id)
        if not sup:
            raise HTTPException(status_code=400, detail="指定した仕入先が存在しません。")
        row.supplier_id = payload.supplier_id
    if payload.delivery_note_number is not None:
        row.delivery_note_number = (payload.delivery_note_number.strip() or None)
    if payload.quantity is not None:
        if payload.quantity < 0:
            raise HTTPException(status_code=400, detail="数量は0以上で指定してください。")
        row.quantity = payload.quantity
    if payload.unit_price is not None:
        row.unit_price = payload.unit_price if payload.unit_price >= 0 else None
    if payload.amount is not None:
        row.amount = payload.amount if payload.amount >= 0 else None
    if payload.purchase_month is not None:
        row.purchase_month = (payload.purchase_month.strip() or None)
    if payload.account_name is not None:
        row.account_name = (payload.account_name.strip() or None)
    if payload.expense_item_name is not None:
        row.expense_item_name = (payload.expense_item_name.strip() or None)
    if payload.purchaser_name is not None:
        row.purchaser_name = (payload.purchaser_name.strip() or None)
    if payload.note is not None:
        row.note = (payload.note.strip() or None)
    db.commit()
    db.refresh(row)
    return {"ok": True, "id": row.id}


@app.get('/manage/purchase-results', response_class=RedirectResponse)
def redirect_manage_purchase_results_to_purchase_results(
    request: Request,
    delivery_date_from: Optional[str] = Query(None),
    delivery_date_to: Optional[str] = Query(None),
    purchase_month: Optional[str] = Query(None),
    supplier_id: Optional[int] = Query(None),
    item_code: Optional[str] = Query(None),
) -> RedirectResponse:
    """旧URL: データ管理内の購入品管理 → 独立ページへリダイレクト"""
    params = []
    if delivery_date_from:
        params.append(f"delivery_date_from={quote_plus(delivery_date_from)}")
    if delivery_date_to:
        params.append(f"delivery_date_to={quote_plus(delivery_date_to)}")
    if purchase_month:
        params.append(f"purchase_month={quote_plus(purchase_month)}")
    if supplier_id is not None:
        params.append(f"supplier_id={supplier_id}")
    if item_code:
        params.append(f"item_code={quote_plus(item_code)}")
    qs = "&".join(params)
    url = "/purchase-results" + ("?" + qs if qs else "")
    return RedirectResponse(url=url, status_code=302)


@app.get('/manage/purchase-results/csv', response_class=RedirectResponse)
def redirect_manage_purchase_results_csv(request: Request) -> RedirectResponse:
    """旧URL: CSV → 新URLへリダイレクト（クエリは維持）"""
    qs = request.url.query
    url = "/purchase-results/csv" + ("?" + qs if qs else "")
    return RedirectResponse(url=url, status_code=302)


@app.get('/manage/email-settings', response_class=HTMLResponse)
def manage_email_settings_page(
    request: Request,
    current_user: Dict[str, Any] = Depends(require_admin_user),
) -> HTMLResponse:
    context = {
        'request': request,
        'nav_links': build_nav_links('/manage/suppliers', current_user),
        'manage_sections': build_manage_sections(current_user, 'email'),
        'now': datetime.now(JST_ZONE),
        'current_user': current_user,
    }
    return templates.TemplateResponse('manage_email_settings.html', context)


@app.get('/api/email-settings')
def get_email_settings(
    current_user: Dict[str, Any] = Depends(require_admin_user),
) -> Dict[str, object]:
    _ = current_user
    settings = normalize_email_settings(load_email_settings_config(EMAIL_SETTINGS_PATH))
    accounts = settings.get("accounts") if isinstance(settings.get("accounts"), dict) else {}
    password_registered: Dict[str, bool] = {}
    keyring_available = True
    try:
        keyring_module = load_keyring_module()
        if not keyring_module:
            raise ModuleNotFoundError("keyring")
    except ModuleNotFoundError:
        keyring_available = False
    else:
        for account_key, account in accounts.items():
            sender = ""
            if isinstance(account, dict):
                sender = str(account.get("sender") or "").strip()
            try:
                password_registered[account_key] = bool(
                    sender and keyring_module.get_password("purchase_order_app", sender)
                )
            except Exception:
                password_registered[account_key] = False

    return {
        **settings,
        "password_registered": password_registered,
        "keyring_available": keyring_available,
    }


@app.put('/api/email-settings')
def update_email_settings(
    payload: EmailSettingsPayload,
    current_user: Dict[str, Any] = Depends(require_admin_user),
) -> Dict[str, object]:
    _ = current_user
    raw = model_to_dict(payload)
    settings = normalize_email_settings(raw)
    smtp_server = str(settings.get("smtp_server") or "").strip()
    smtp_port = int(settings.get("smtp_port") or 0)
    accounts = settings.get("accounts") if isinstance(settings.get("accounts"), dict) else {}
    defaults = settings.get("department_defaults") if isinstance(settings.get("department_defaults"), dict) else {}
    if not smtp_server:
        raise HTTPException(status_code=400, detail="smtp_server は必須です。")
    if smtp_port <= 0:
        raise HTTPException(status_code=400, detail="smtp_port は1以上で指定してください。")
    if not accounts:
        raise HTTPException(status_code=400, detail="accounts は1件以上必要です。")
    for department_name, account_key in defaults.items():
        account_value = accounts.get(account_key)
        if not isinstance(account_value, dict):
            continue
        account_department = str(account_value.get("department") or "").strip()
        if account_department and account_department != str(department_name).strip():
            raise HTTPException(
                status_code=400,
                detail=f"部署デフォルト不整合: {department_name} の既定アカウント {account_key} の部署は {account_department} です。",
            )

    save_email_settings_config(EMAIL_SETTINGS_PATH, settings)
    return {"saved": True}


@app.post('/api/email-settings/accounts/{account_key}/password')
def set_email_account_password(
    account_key: str,
    payload: EmailAccountPasswordPayload,
    current_user: Dict[str, Any] = Depends(require_admin_user),
) -> Dict[str, object]:
    _ = current_user
    normalized_key = (account_key or '').strip()
    if not normalized_key:
        raise HTTPException(status_code=400, detail='account_key が不正です。')
    password = (payload.password or '').strip()
    if not password:
        raise HTTPException(status_code=400, detail='パスワードを入力してください。')

    settings = normalize_email_settings(load_email_settings_config(EMAIL_SETTINGS_PATH))
    accounts = settings.get('accounts') if isinstance(settings.get('accounts'), dict) else {}
    account = accounts.get(normalized_key)
    if not isinstance(account, dict):
        raise HTTPException(status_code=404, detail='対象アカウントが見つかりません。')
    sender = str(account.get('sender') or '').strip()
    if not sender:
        raise HTTPException(status_code=400, detail='送信元メールアドレスが未設定です。')

    keyring_module = load_keyring_module()
    if not keyring_module:
        raise HTTPException(status_code=400, detail='keyring がインストールされていません。')

    try:
        keyring_module.set_password('purchase_order_app', sender, password)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f'パスワード保存に失敗しました: {exc}') from exc

    return {'saved': True, 'account_key': normalized_key, 'sender': sender}


@app.get('/orders', response_class=HTMLResponse)
def orders_page(
    request: Request,
    department: str = Query('', alias='department'),
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(require_manager_user),
) -> HTMLResponse:
    selected_department = normalize_field(department)
    snapshots = load_inventory_snapshots(db)
    sidebar_structure = build_sidebar_structure(snapshots)
    # 部署ごとの発注を前提とするため、未選択時は先頭の部署へリダイレクト
    if not selected_department and sidebar_structure:
        first_dept = (sidebar_structure[0].get('name') or '').strip()
        if first_dept:
            return RedirectResponse(
                url=f"/orders?department={quote_plus(first_dept)}",
                status_code=302,
            )
    service = get_purchase_order_service(db)
    suggestions = service.build_low_stock_candidates(selected_department)
    low_stock_count = len(suggestions)
    existing_orders = service.list_orders(selected_department)
    draft_count = sum(1 for order in existing_orders if order.get('status') == PurchaseOrderStatus.DRAFT.value)
    waiting_count = sum(1 for order in existing_orders if order.get('status') == PurchaseOrderStatus.WAITING.value)
    order_contacts_all, order_contact_defaults, order_contacts_by_department = load_order_contacts(EMAIL_SETTINGS_PATH)
    order_contacts = (
        order_contacts_by_department.get(selected_department, [])
        if selected_department
        else order_contacts_all
    )
    highlight_cards = [
        {
            'label': '在庫不足候補',
            'value': low_stock_count,
            'note': '発注対象の候補件数',
            'icon': 'warning',
        },
        {
            'label': '発注下書き',
            'value': draft_count,
            'note': '注文書未確定',
            'icon': 'description',
        },
        {
            'label': '入荷待ち',
            'value': waiting_count,
            'note': '納期回答転記済み / 納品待ち',
            'icon': 'schedule',
        },
    ]
    context = {
        'request': request,
        'nav_links': build_nav_links('/orders', current_user),
        'sidebar_structure': sidebar_structure,
        'low_stock_suggestions': suggestions,
        'existing_orders': existing_orders,
        'build_orders_url': build_orders_url,
        'selected_department': selected_department,
        'highlight_cards': highlight_cards,
        'purchase_order_statuses': [status.value for status in PurchaseOrderStatus],
        'order_contacts': order_contacts,
        'default_order_contact': order_contact_defaults.get(selected_department, ''),
        'now': datetime.now(JST_ZONE),
        'current_user': current_user,
    }
    return templates.TemplateResponse('orders.html', context)


@app.post('/purchase-orders')
def create_purchase_order(
    payload: CreatePurchaseOrderPayload,
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(require_manager_user),
) -> Dict[str, object]:
    _ = current_user
    service = get_purchase_order_service(db)
    try:
        result = service.create_order(
            lines=[model_to_dict(line) for line in payload.lines],
            ordered_by_user=normalize_field(payload.ordered_by_user),
            department=normalize_field(payload.department),
        )
    except PurchaseOrderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return result


@app.post('/purchase-orders/bulk-from-low-stock')
def create_bulk_purchase_orders(
    payload: BulkCreatePurchaseOrdersPayload,
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(require_manager_user),
) -> Dict[str, object]:
    _ = current_user
    service = get_purchase_order_service(db)
    try:
        result = service.create_bulk_orders_from_low_stock(
            ordered_by_user=normalize_field(payload.ordered_by_user),
            department=normalize_field(payload.department),
            candidate_overrides=payload.candidate_overrides,
        )
    except PurchaseOrderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return result


@app.get('/purchase-orders/{order_id}/document-preview')
def preview_purchase_order_document(
    order_id: int,
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(require_manager_user),
) -> Response:
    _ = current_user
    service = get_purchase_order_service(db)
    try:
        pdf_bytes = service.get_document_preview_pdf(order_id)
    except PurchaseOrderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="PO_{order_id}_preview.pdf"'},
    )


@app.post('/purchase-orders/{order_id}/document')
def generate_purchase_order_document(
    order_id: int,
    payload: GenerateDocumentPayload,
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(require_manager_user),
) -> Dict[str, object]:
    _ = current_user
    service = get_purchase_order_service(db)
    try:
        return service.generate_document(
            order_id=order_id,
            generated_by=normalize_field(payload.generated_by) or "system",
            regenerate=payload.regenerate,
        )
    except PurchaseOrderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get('/purchase-orders/{order_id}/email-preview')
def purchase_order_email_preview(
    order_id: int,
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(require_manager_user),
) -> Dict[str, object]:
    _ = current_user
    service = get_purchase_order_service(db)
    try:
        return service.get_email_preview(order_id)
    except PurchaseOrderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post('/purchase-orders/{order_id}/send-email')
def send_purchase_order_email(
    order_id: int,
    payload: SendEmailPayload,
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(require_manager_user),
) -> Dict[str, object]:
    _ = current_user
    service = get_purchase_order_service(db)
    try:
        return service.send_email(
            order_id=order_id,
            sent_by=normalize_field(payload.sent_by) or "system",
            regenerate=payload.regenerate,
        )
    except PurchaseOrderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post('/purchase-order-lines/{line_id}/reply-due-date')
def update_purchase_order_line_due_date(
    line_id: int,
    payload: ReplyDueDatePayload,
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(require_manager_user),
) -> Dict[str, object]:
    _ = current_user
    try:
        parsed_date = date.fromisoformat(payload.due_date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="回答納期は YYYY-MM-DD 形式で指定してください。") from exc

    service = get_purchase_order_service(db)
    try:
        return service.update_reply_due_date(line_id=line_id, due_date=parsed_date)
    except PurchaseOrderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post('/purchase-orders/{order_id}/status')
def update_purchase_order_status(
    order_id: int,
    payload: UpdatePurchaseOrderStatusPayload,
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(require_manager_user),
) -> Dict[str, object]:
    _ = current_user
    target_status = (payload.status or "").strip().upper()
    if target_status in {PurchaseOrderStatus.WAITING.value, PurchaseOrderStatus.RECEIVED.value}:
        raise HTTPException(
            status_code=400,
            detail="入庫待ち・納品計上は発注一覧から変更できません。入出庫管理ページで実行してください。",
        )
    service = get_purchase_order_service(db)
    try:
        return service.update_order_status(
            order_id=order_id,
            target_status=target_status,
            updated_by=normalize_field(payload.updated_by) or "system",
        )
    except PurchaseOrderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get('/history', response_class=HTMLResponse)
def history_page(
    request: Request,
    db: Session = Depends(get_db),
    current_user: Optional[Dict[str, Any]] = Depends(get_optional_user),
) -> HTMLResponse:
    transactions = load_recent_transactions(db, limit=50)
    context = {
        'request': request,
        'nav_links': build_nav_links('/history', current_user),
        'recent_transactions': transactions,
        'now': datetime.now(JST_ZONE),
        'current_user': current_user,
    }
    return templates.TemplateResponse('history.html', context)


@app.post('/api/suppliers')
def create_supplier(
    payload: SupplierPayload,
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(require_manager_user),
) -> Dict[str, object]:
    _ = current_user
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail='仕入先名は必須です。')
    existing = db.scalar(select(Supplier).filter(Supplier.name == name))
    if existing:
        raise HTTPException(status_code=400, detail='同名の仕入先が既に存在します。')

    supplier = Supplier(
        name=name,
        contact_person=payload.contact_person.strip() or None,
        mobile_number=payload.mobile_number.strip() or None,
        phone_number=payload.phone_number.strip() or None,
        email=payload.email.strip() or None,
        assistant_name=payload.assistant_name.strip() or None,
        assistant_email=payload.assistant_email.strip() or None,
        fax_number=payload.fax_number.strip() or None,
        notes=payload.notes.strip() or None,
    )
    db.add(supplier)
    db.commit()
    db.refresh(supplier)
    return {'id': supplier.id, 'name': supplier.name}


@app.put('/api/suppliers/{supplier_id}')
def update_supplier(
    supplier_id: int,
    payload: SupplierPayload,
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(require_manager_user),
) -> Dict[str, object]:
    _ = current_user
    supplier = db.scalar(select(Supplier).filter(Supplier.id == supplier_id))
    if not supplier:
        raise HTTPException(status_code=404, detail='仕入先が見つかりません。')

    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail='仕入先名は必須です。')

    duplicate = db.scalar(select(Supplier).filter(Supplier.name == name, Supplier.id != supplier_id))
    if duplicate:
        raise HTTPException(status_code=400, detail='同名の仕入先が既に存在します。')

    supplier.name = name
    supplier.contact_person = payload.contact_person.strip() or None
    supplier.mobile_number = payload.mobile_number.strip() or None
    supplier.phone_number = payload.phone_number.strip() or None
    supplier.email = payload.email.strip() or None
    supplier.assistant_name = payload.assistant_name.strip() or None
    supplier.assistant_email = payload.assistant_email.strip() or None
    supplier.fax_number = payload.fax_number.strip() or None
    supplier.notes = payload.notes.strip() or None
    db.commit()
    return {'id': supplier.id, 'name': supplier.name}


@app.delete('/api/suppliers/{supplier_id}')
def delete_supplier(
    supplier_id: int,
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(require_manager_user),
) -> Dict[str, object]:
    """仕入先を削除する。品目または発注に紐づいている場合は削除不可。"""
    _ = current_user
    supplier = db.scalar(select(Supplier).filter(Supplier.id == supplier_id))
    if not supplier:
        raise HTTPException(status_code=404, detail='仕入先が見つかりません。')
    items_count = db.scalar(select(func.count(Item.id)).where(Item.supplier_id == supplier_id))
    orders_count = db.scalar(select(func.count(PurchaseOrder.id)).where(PurchaseOrder.supplier_id == supplier_id))
    if (items_count or 0) > 0 or (orders_count or 0) > 0:
        raise HTTPException(
            status_code=400,
            detail='この仕入先は品目または発注に紐づいているため削除できません。',
        )
    db.delete(supplier)
    db.commit()
    return {'id': supplier_id, 'message': '仕入先を削除しました。'}


def _resolve_supplier(db: Session, supplier_id: Optional[int]) -> Optional[Supplier]:
    if supplier_id is None:
        return None
    return db.scalar(select(Supplier).filter(Supplier.id == supplier_id))


def _effective_item_name(payload: ItemPayload) -> str:
    name = (payload.name or '').strip()
    if name:
        return name
    if payload.item_code.strip():
        return payload.item_code.strip()
    if (payload.item_type or '').strip():
        return payload.item_type.strip()
    return '品目未設定'


@app.post('/api/items')
def create_item(
    payload: ItemPayload,
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(require_manager_user),
) -> Dict[str, object]:
    _ = current_user
    code = payload.item_code.strip()
    if not code:
        raise HTTPException(status_code=400, detail='品番は必須です。')

    existing = db.scalar(select(Item).filter(Item.item_code == code))
    if existing:
        raise HTTPException(status_code=400, detail='同一品番が既に存在します。')

    supplier = _resolve_supplier(db, payload.supplier_id)
    management_type = (payload.management_type or '').strip()
    if management_type not in ('管理', '管理外'):
        management_type = '管理'

    item = Item(
        item_code=code,
        name=_effective_item_name(payload),
        item_type=payload.item_type.strip() or None,
        usage=payload.usage.strip() or None,
        department=payload.department.strip() or None,
        manufacturer=payload.manufacturer.strip() or None,
        shelf=payload.shelf.strip() or None,
        unit=payload.unit.strip() or None,
        reorder_point=max(0, payload.reorder_point),
        default_order_quantity=max(1, payload.default_order_quantity),
        unit_price=payload.unit_price if payload.unit_price is not None else None,
        account_name=(payload.account_name or "").strip() or None,
        expense_item_name=(payload.expense_item_name or "").strip() or None,
        management_type=management_type,
        supplier_id=supplier.id if supplier else None,
    )
    db.add(item)
    db.commit()
    db.refresh(item)

    inventory_item = InventoryItem(item_id=item.id, quantity_on_hand=0)
    db.add(inventory_item)
    db.commit()
    return {'id': item.id, 'item_code': item.item_code}


@app.put('/api/items/{item_id}')
def update_item(
    item_id: int,
    payload: ItemPayload,
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(require_manager_user),
) -> Dict[str, object]:
    _ = current_user
    item = db.scalar(select(Item).filter(Item.id == item_id))
    if not item:
        raise HTTPException(status_code=404, detail='仕入品が見つかりません。')

    code = payload.item_code.strip()
    if not code:
        raise HTTPException(status_code=400, detail='品番は必須です。')

    if code != item.item_code:
        duplicate = db.scalar(select(Item).filter(Item.item_code == code, Item.id != item_id))
        if duplicate:
            raise HTTPException(status_code=400, detail='同一品番が既に存在します。')
        item.item_code = code

    supplier = _resolve_supplier(db, payload.supplier_id)
    item.name = _effective_item_name(payload)
    item.item_type = payload.item_type.strip() or None
    item.usage = payload.usage.strip() or None
    item.department = payload.department.strip() or None
    item.manufacturer = payload.manufacturer.strip() or None
    item.shelf = payload.shelf.strip() or None
    item.unit = payload.unit.strip() or None
    item.reorder_point = max(0, payload.reorder_point)
    item.default_order_quantity = max(1, payload.default_order_quantity)
    item.unit_price = payload.unit_price if payload.unit_price is not None else None
    item.account_name = (payload.account_name or "").strip() or None
    item.expense_item_name = (payload.expense_item_name or "").strip() or None
    management_type = (payload.management_type or '').strip()
    if management_type in ('管理', '管理外'):
        item.management_type = management_type
    elif not item.management_type:
        item.management_type = '管理'
    item.supplier_id = supplier.id if supplier else None
    db.commit()
    return {'id': item.id, 'item_code': item.item_code}


@app.delete('/api/items/{item_id}')
def delete_item(
    item_id: int,
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(require_manager_user),
) -> Dict[str, object]:
    """仕入品を削除する。発注明細に紐づいている場合は削除不可。在庫・在庫履歴は連鎖削除される。"""
    _ = current_user
    item = db.scalar(select(Item).filter(Item.id == item_id))
    if not item:
        raise HTTPException(status_code=404, detail='仕入品が見つかりません。')
    lines_count = db.scalar(select(func.count(PurchaseOrderLine.id)).where(PurchaseOrderLine.item_id == item_id))
    if (lines_count or 0) > 0:
        raise HTTPException(
            status_code=400,
            detail='この品目は発注明細に紐づいているため削除できません。',
        )
    db.delete(item)
    db.commit()
    return {'id': item_id, 'message': '仕入品を削除しました。'}


@app.get('/api/items')
def search_items(
    q: str = Query('', alias='q'),
    limit: int = Query(20, ge=1, le=50),
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(require_manager_user),
) -> Dict[str, List[Dict[str, object]]]:
    _ = current_user
    stmt = select(Item).options(selectinload(Item.supplier))
    if q:
        pattern = f"%{q}%"
        stmt = stmt.filter(or_(Item.item_code.ilike(pattern), Item.name.ilike(pattern)))
    stmt = stmt.order_by(Item.item_code.asc()).limit(limit)
    items = db.scalars(stmt).all()

    results = [
        {
            'id': item.id,
            'item_code': item.item_code,
            'name': item.name,
            'item_type': item.item_type or '',
            'usage': item.usage or '',
            'department': item.department or '',
            'manufacturer': item.manufacturer or '',
            'shelf': item.shelf or '',
            'unit': item.unit or '',
            'reorder_point': item.reorder_point or 0,
            'default_order_quantity': getattr(item, 'default_order_quantity', 1) or 1,
            'unit_price': getattr(item, 'unit_price', None),
            'account_name': getattr(item, 'account_name', None) or '',
            'expense_item_name': getattr(item, 'expense_item_name', None) or '',
            'management_type': item.management_type or '',
            'supplier_id': item.supplier_id,
            'supplier_name': item.supplier.name if item.supplier else '',
        }
        for item in items
    ]
    return {'items': results}
