from datetime import datetime
from enum import Enum
from zoneinfo import ZoneInfo

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    Enum as sqlalchemyEnum,
    func,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from app.db.base import Base


class TransactionType(str, Enum):
    RECEIPT = "receipt"
    ISSUE = "issue"
    ADJUST = "adjust"


class UserRole(str, Enum):
    ADMIN = "admin"
    MANAGER = "manager"
    VIEWER = "viewer"


class Supplier(Base):
    __tablename__ = "suppliers"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(256), unique=True, nullable=False, index=True)
    contact_person = Column(String(128), nullable=True)
    mobile_number = Column(String(64), nullable=True)
    phone_number = Column(String(64), nullable=True)
    email = Column(String(256), nullable=True)
    email_cc = Column(String(256), nullable=True)
    assistant_name = Column(String(128), nullable=True)
    assistant_email = Column(String(256), nullable=True)
    fax_number = Column(String(64), nullable=True)
    notes = Column(Text, nullable=True)

    items = relationship("Item", back_populates="supplier")
    item_suppliers = relationship(
        "ItemSupplier",
        back_populates="supplier",
        cascade="all, delete-orphan",
    )
    purchase_orders = relationship("PurchaseOrder", back_populates="supplier")
    purchase_results = relationship("PurchaseResult", back_populates="supplier")
    unit_price_history = relationship("UnitPriceHistory", back_populates="supplier")


class Item(Base):
    __tablename__ = "items"

    id = Column(Integer, primary_key=True, index=True)
    item_code = Column(String(64), unique=True, nullable=False, index=True)
    name = Column(String(256), nullable=False)
    item_type = Column(String(128), nullable=True)
    usage = Column(String(128), nullable=True)
    department = Column(String(128), nullable=True)
    manufacturer = Column(String(256), nullable=True)
    shelf = Column(String(64), nullable=True)
    unit = Column(String(32), nullable=True)
    reorder_point = Column(Integer, default=0, nullable=False)
    default_order_quantity = Column(Integer, default=1, nullable=False)
    unit_price = Column(Integer, nullable=True)  # 単価（円）。表示時は 1,000 のようにカンマ区切り
    account_name = Column(String(128), nullable=True)  # 科目名（購入品管理・資産計上用）
    expense_item_name = Column(String(128), nullable=True)  # 費目名（購入品管理・資産計上用）
    supplier_id = Column(ForeignKey("suppliers.id"), nullable=True)
    management_type = Column(String(32), nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    inventory_item = relationship(
        "InventoryItem",
        back_populates="item",
        uselist=False,
        cascade="all, delete-orphan",
    )
    inventory_transactions = relationship(
        "InventoryTransaction",
        back_populates="item",
        order_by="InventoryTransaction.occurred_at.desc()",
        cascade="all, delete-orphan",
    )
    supplier = relationship("Supplier", back_populates="items")
    item_suppliers = relationship(
        "ItemSupplier",
        back_populates="item",
        cascade="all, delete-orphan",
    )
    purchase_order_lines = relationship("PurchaseOrderLine", back_populates="item")
    purchase_results = relationship("PurchaseResult", back_populates="item")
    unit_price_history = relationship(
        "UnitPriceHistory", back_populates="item", cascade="all, delete-orphan"
    )


class ItemSupplier(Base):
    """品番×仕入先ごとの単価。発注時の「この品番をこの仕入先で」の選択と単価の参照元。"""
    __tablename__ = "item_suppliers"
    __table_args__ = (UniqueConstraint("item_id", "supplier_id", name="uq_item_suppliers_item_supplier"),)

    id = Column(Integer, primary_key=True, index=True)
    item_id = Column(ForeignKey("items.id"), nullable=False)
    supplier_id = Column(ForeignKey("suppliers.id"), nullable=False)
    unit_price = Column(Integer, nullable=True)  # 単価（円）
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    item = relationship("Item", back_populates="item_suppliers")
    supplier = relationship("Supplier", back_populates="item_suppliers")


class InventoryItem(Base):
    __tablename__ = "inventory_items"
    __table_args__ = (UniqueConstraint("item_id", name="uq_inventory_items_item_id"),)

    id = Column(Integer, primary_key=True, index=True)
    item_id = Column(ForeignKey("items.id"), nullable=False)
    quantity_on_hand = Column(Integer, default=0, nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    item = relationship("Item", back_populates="inventory_item")


def _jst_now() -> datetime:
    """在庫取引の発生時刻は JST で統一する（履歴の時系列整合のため）。"""
    return datetime.now(ZoneInfo("Asia/Tokyo"))


class InventoryTransaction(Base):
    __tablename__ = "inventory_transactions"

    id = Column(Integer, primary_key=True, index=True)
    item_id = Column(ForeignKey("items.id"), nullable=False)
    tx_type = Column(sqlalchemyEnum(TransactionType), nullable=False)
    delta = Column(Integer, nullable=False)
    reason = Column(String(256), nullable=True)
    note = Column(Text, nullable=True)
    occurred_at = Column(DateTime, default=_jst_now, nullable=False)
    created_by = Column(String(128), nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    item = relationship("Item", back_populates="inventory_transactions")


class PurchaseOrderStatus(str, Enum):
    DRAFT = "DRAFT"
    CONFIRMED = "CONFIRMED"
    SENT = "SENT"
    WAITING = "WAITING"
    RECEIVED = "RECEIVED"
    CANCELLED = "CANCELLED"


class PurchaseOrder(Base):
    __tablename__ = "purchase_orders"

    id = Column(Integer, primary_key=True, index=True)
    supplier_id = Column(ForeignKey("suppliers.id"), nullable=False)
    department = Column(String(128), nullable=True)
    ordered_by_user = Column(String(128), nullable=True)
    status = Column(String(32), nullable=False, default=PurchaseOrderStatus.DRAFT.value)
    issued_date = Column(Date, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    supplier = relationship("Supplier", back_populates="purchase_orders")
    lines = relationship("PurchaseOrderLine", back_populates="order", cascade="all, delete-orphan")
    document = relationship("PurchaseOrderDocument", back_populates="order", uselist=False, cascade="all, delete-orphan")
    email_logs = relationship("EmailSendLog", back_populates="order", cascade="all, delete-orphan", order_by="EmailSendLog.sent_at.desc()")


class PurchaseOrderLine(Base):
    __tablename__ = "purchase_order_lines"

    id = Column(Integer, primary_key=True, index=True)
    purchase_order_id = Column(ForeignKey("purchase_orders.id"), nullable=False)
    item_id = Column(ForeignKey("items.id"), nullable=True)
    item_name_free = Column(String(256), nullable=True)
    maker = Column(String(256), nullable=True)
    quantity = Column(Integer, nullable=False)
    received_quantity = Column(Integer, nullable=False, default=0)
    vendor_reply_due_date = Column(Date, nullable=True)
    usage_destination = Column(String(256), nullable=True)  # 使用先（管理外依頼取り込み時など）
    note = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    order = relationship("PurchaseOrder", back_populates="lines")
    item = relationship("Item", back_populates="purchase_order_lines")


class PurchaseOrderDocument(Base):
    __tablename__ = "purchase_order_documents"

    id = Column(Integer, primary_key=True, index=True)
    purchase_order_id = Column(ForeignKey("purchase_orders.id"), nullable=False, unique=True)
    pdf_path = Column(String(1024), nullable=False)
    generated_at = Column(DateTime, server_default=func.now(), nullable=False)
    generated_by = Column(String(128), nullable=True)

    order = relationship("PurchaseOrder", back_populates="document")


class EmailSendLog(Base):
    __tablename__ = "email_send_logs"

    id = Column(Integer, primary_key=True, index=True)
    purchase_order_id = Column(ForeignKey("purchase_orders.id"), nullable=False)
    sent_by = Column(String(128), nullable=True)
    sent_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    to_recipients = Column("to", Text, nullable=True)
    cc_recipients = Column("cc", Text, nullable=True)
    subject = Column(String(256), nullable=False)
    body = Column(Text, nullable=False)
    attachment_path = Column(String(1024), nullable=True)
    success = Column(Boolean, nullable=False, default=False)
    error_message = Column(Text, nullable=True)

    order = relationship("PurchaseOrder", back_populates="email_logs")


class UnmanagedOrderRequestStatus(str, Enum):
    """管理外発注依頼のステータス"""
    PENDING = "PENDING"      # 未処理
    CONVERTED = "CONVERTED"  # 発注に取り込み済み
    REJECTED = "REJECTED"    # 却下


class UnmanagedOrderRequest(Base):
    """管理外品目の発注依頼。一般ユーザー・管理者が登録し、管理者が発注に取り込む。"""
    __tablename__ = "unmanaged_order_requests"

    id = Column(Integer, primary_key=True, index=True)
    requested_at = Column(Date, nullable=False)  # 依頼日
    requested_department = Column(String(128), nullable=True)  # 依頼部署
    requested_by = Column(String(128), nullable=True)  # 依頼者
    item_id = Column(ForeignKey("items.id"), nullable=True)  # マスタにある場合
    item_code_free = Column(String(256), nullable=True)  # マスタにない場合の品番・品名
    manufacturer = Column(String(256), nullable=True)  # メーカー名
    quantity = Column(Integer, nullable=False)
    usage_destination = Column(String(256), nullable=True)  # 使用先
    note = Column(Text, nullable=True)  # 備考
    vendor_reply_due_date = Column(Date, nullable=True)  # 希望納期
    status = Column(String(32), nullable=False, default=UnmanagedOrderRequestStatus.PENDING.value)
    purchase_order_id = Column(ForeignKey("purchase_orders.id"), nullable=True)  # 取り込み先発注
    purchase_order_line_id = Column(ForeignKey("purchase_order_lines.id"), nullable=True)  # 取り込み先明細
    staged_supplier_id = Column(ForeignKey("suppliers.id"), nullable=True)  # 発注候補に追加時に選択した仕入先
    staged_at = Column(DateTime, nullable=True)  # 発注候補に追加した日時
    acknowledged_at = Column(DateTime, nullable=True)  # 依頼者が入庫済を確認してリストから削除した日時
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    item = relationship("Item", backref="unmanaged_order_requests")
    order = relationship("PurchaseOrder", foreign_keys=[purchase_order_id])
    line = relationship("PurchaseOrderLine", foreign_keys=[purchase_order_line_id])
    staged_supplier = relationship("Supplier", foreign_keys=[staged_supplier_id])


class PurchaseResult(Base):
    """購入実績（入庫計上結果を総務・資産計上用に保存）。明細単位で1行。管理外は item_id null で item_name_free に品名。"""
    __tablename__ = "purchase_results"

    id = Column(Integer, primary_key=True, index=True)
    delivery_date = Column(Date, nullable=True)
    supplier_id = Column(ForeignKey("suppliers.id"), nullable=False)
    delivery_note_number = Column(String(64), nullable=True)
    item_id = Column(ForeignKey("items.id"), nullable=True)  # 管理外の場合は null
    item_name_free = Column(String(512), nullable=True)  # 管理外の品名（item_id が null のとき）
    quantity = Column(Integer, nullable=False)
    unit_price = Column(Integer, nullable=True)
    amount = Column(Integer, nullable=True)
    purchase_month = Column(String(4), nullable=True)
    account_name = Column(String(128), nullable=True)
    expense_item_name = Column(String(128), nullable=True)
    purchaser_name = Column(String(128), nullable=True)
    note = Column(Text, nullable=True)
    source_order_id = Column(Integer, nullable=True)
    source_line_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    supplier = relationship("Supplier", back_populates="purchase_results")
    item = relationship("Item", back_populates="purchase_results")


class UnitPriceHistory(Base):
    """単価変更履歴（入庫時に単価を変更した場合など）。"""
    __tablename__ = "unit_price_history"

    id = Column(Integer, primary_key=True, index=True)
    item_id = Column(ForeignKey("items.id"), nullable=False)
    supplier_id = Column(ForeignKey("suppliers.id"), nullable=False)
    old_unit_price = Column(Integer, nullable=True)
    new_unit_price = Column(Integer, nullable=False)
    changed_at = Column(DateTime, server_default=func.now(), nullable=False)
    changed_by = Column(String(128), nullable=True)
    source = Column(String(64), nullable=True)
    reference_id = Column(Integer, nullable=True)

    item = relationship("Item", back_populates="unit_price_history")
    supplier = relationship("Supplier", back_populates="unit_price_history")


class AppUser(Base):
    __tablename__ = "app_users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(64), unique=True, nullable=False, index=True)
    display_name = Column(String(128), nullable=False)
    password_hash = Column(String(512), nullable=False)
    role = Column(sqlalchemyEnum(UserRole), nullable=False, default=UserRole.VIEWER)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
