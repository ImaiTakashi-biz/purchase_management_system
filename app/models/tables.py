from datetime import datetime
from enum import Enum

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum as sqlalchemyEnum,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from app.db.base import Base


class TransactionType(str, Enum):
    RECEIPT = "receipt"
    ISSUE = "issue"
    ADJUST = "adjust"


class Supplier(Base):
    __tablename__ = "suppliers"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(256), unique=True, nullable=False, index=True)
    contact_person = Column(String(128), nullable=True)
    mobile_number = Column(String(64), nullable=True)
    phone_number = Column(String(64), nullable=True)
    email = Column(String(256), nullable=True)
    assistant_name = Column(String(128), nullable=True)
    assistant_email = Column(String(256), nullable=True)
    fax_number = Column(String(64), nullable=True)
    notes = Column(Text, nullable=True)

    items = relationship("Item", back_populates="supplier")


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
    supplier_id = Column(ForeignKey("suppliers.id"), nullable=True)
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


class InventoryItem(Base):
    __tablename__ = "inventory_items"
    __table_args__ = (UniqueConstraint("item_id", name="uq_inventory_items_item_id"),)

    id = Column(Integer, primary_key=True, index=True)
    item_id = Column(ForeignKey("items.id"), nullable=False)
    quantity_on_hand = Column(Integer, default=0, nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    item = relationship("Item", back_populates="inventory_item")


class InventoryTransaction(Base):
    __tablename__ = "inventory_transactions"

    id = Column(Integer, primary_key=True, index=True)
    item_id = Column(ForeignKey("items.id"), nullable=False)
    tx_type = Column(sqlalchemyEnum(TransactionType), nullable=False)
    delta = Column(Integer, nullable=False)
    reason = Column(String(256), nullable=True)
    note = Column(Text, nullable=True)
    occurred_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    created_by = Column(String(128), nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    item = relationship("Item", back_populates="inventory_transactions")
