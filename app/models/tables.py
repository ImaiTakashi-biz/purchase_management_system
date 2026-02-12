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
    purchase_orders = relationship("PurchaseOrder", back_populates="supplier")


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
    purchase_order_lines = relationship("PurchaseOrderLine", back_populates="item")


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
