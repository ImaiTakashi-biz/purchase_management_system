from __future__ import annotations

import json
import os
import re
import shutil
import smtplib
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Optional

from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.tables import (
    EmailSendLog,
    InventoryItem,
    InventoryTransaction,
    Item,
    PurchaseOrder,
    PurchaseOrderDocument,
    PurchaseOrderLine,
    PurchaseOrderStatus,
    Supplier,
    TransactionType,
)

DEFAULT_NAS_ROOT = r"\\192.168.1.200\共有\dev_tools\発注管理システム\注文書"
SMTP_KEYRING_SERVICE_NAME = "purchase_order_app"
DEFAULT_DUPLICATE_WINDOW_SECONDS = 120
INVALID_WINDOWS_SEGMENT_CHARS = re.compile(r"[\\/:*?\"<>|]")
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    "COM1",
    "COM2",
    "COM3",
    "COM4",
    "COM5",
    "COM6",
    "COM7",
    "COM8",
    "COM9",
    "LPT1",
    "LPT2",
    "LPT3",
    "LPT4",
    "LPT5",
    "LPT6",
    "LPT7",
    "LPT8",
    "LPT9",
}


@dataclass
class EmailSettings:
    smtp_server: str
    smtp_port: int
    accounts: dict[str, str]
    display_names: dict[str, str]
    account_departments: dict[str, list[str]]
    department_defaults: dict[str, str]


@dataclass
class CompanyProfile:
    company_name: str
    address: str
    url: str
    default_phone: str
    department_phones: dict[str, str]


class PurchaseOrderError(Exception):
    pass


class PurchaseOrderService:
    def __init__(self, db: Session, templates: Jinja2Templates, project_root: Path) -> None:
        self.db = db
        self.templates = templates
        self.project_root = project_root
        self.config_dir = project_root / "config"
        self.nas_root = Path(os.getenv("PURCHASE_ORDER_NAS_ROOT", DEFAULT_NAS_ROOT))

    def build_low_stock_candidates(self, department: str = "") -> list[dict[str, Any]]:
        selected_department = (department or "").strip()
        stmt = (
            select(Item)
            .options(selectinload(Item.inventory_item), selectinload(Item.supplier))
            .order_by(Item.item_code.asc())
        )
        items = self.db.scalars(stmt).all()

        results: list[dict[str, Any]] = []
        for item in items:
            inv = item.inventory_item
            on_hand = inv.quantity_on_hand if inv else 0
            reorder = item.reorder_point or 0
            if reorder <= 0:
                continue
            if on_hand > reorder:
                continue
            item_dept = (item.department or "").strip()
            if selected_department and item_dept != selected_department:
                continue

            supplier = item.supplier
            results.append(
                {
                    "item_id": item.id,
                    "item_code": item.item_code,
                    "name": item.name,
                    "department": item.department or "未設定",
                    "maker": item.manufacturer or "",
                    "supplier_id": supplier.id if supplier else None,
                    "supplier_name": supplier.name if supplier else "未設定",
                    "on_hand": on_hand,
                    "reorder_point": reorder,
                    "gap": on_hand - reorder,
                    "gap_label": f"{on_hand - reorder:+d}",
                    "order_quantity": max(1, reorder - on_hand),
                    "note": "",
                }
            )
        return results

    def list_orders(self, department: str = "") -> list[dict[str, Any]]:
        stmt = (
            select(PurchaseOrder)
            .options(
                selectinload(PurchaseOrder.supplier),
                selectinload(PurchaseOrder.document),
                selectinload(PurchaseOrder.lines).selectinload(PurchaseOrderLine.item),
            )
            .order_by(PurchaseOrder.created_at.desc())
        )
        orders = self.db.scalars(stmt).all()
        selected_department = (department or "").strip()

        payload: list[dict[str, Any]] = []
        for order in orders:
            if order.status == PurchaseOrderStatus.CANCELLED.value:
                # Cancelled orders are treated as archived/void.
                # Keep them in DB to preserve PO numbering, but hide from active list.
                continue
            if selected_department and (order.department or "") != selected_department:
                continue
            payload.append(
                {
                    "id": order.id,
                    "supplier_id": order.supplier_id,
                    "supplier_name": order.supplier.name if order.supplier else "",
                    "department": order.department or "",
                    "ordered_by_user": order.ordered_by_user or "",
                    "status": order.status,
                    "issued_date": order.issued_date.isoformat() if order.issued_date else "",
                    "pdf_path": order.document.pdf_path if order.document else "",
                    "lines": [
                        {
                            "id": line.id,
                            "item_id": line.item_id,
                            "item_code": line.item.item_code if line.item else "",
                            "item_name": line.item.name if line.item else (line.item_name_free or ""),
                            "maker": line.maker or "",
                            "quantity": line.quantity,
                            "received_quantity": max(0, int(line.received_quantity or 0)),
                            "remaining_quantity": max(0, int(line.quantity or 0) - int(line.received_quantity or 0)),
                            "vendor_reply_due_date": line.vendor_reply_due_date.isoformat() if line.vendor_reply_due_date else "",
                            "note": line.note or "",
                        }
                        for line in order.lines
                    ],
                }
            )
        return payload

    def create_order(
        self,
        lines: list[dict[str, Any]],
        ordered_by_user: str,
        department: str = "",
    ) -> dict[str, Any]:
        if not lines:
            raise PurchaseOrderError("発注明細が空です。")

        supplier_ids: set[int] = set()
        normalized_lines: list[dict[str, Any]] = []
        resolved_department = (department or "").strip()

        for raw in lines:
            item_id = raw.get("item_id")
            quantity = int(raw.get("quantity") or 0)
            note = (raw.get("note") or "").strip()
            item_name_free = (raw.get("item_name_free") or "").strip()
            maker = (raw.get("maker") or "").strip()

            if quantity <= 0:
                raise PurchaseOrderError("発注数は1以上で指定してください。")

            item: Optional[Item] = None
            if item_id is not None:
                item = self.db.scalar(select(Item).filter(Item.id == int(item_id)).options(selectinload(Item.supplier)))
                if not item:
                    raise PurchaseOrderError(f"品目ID {item_id} が存在しません。")
                if not item.supplier_id:
                    raise PurchaseOrderError(f"品目 {item.item_code} は仕入先未設定のため発注できません。")
                supplier_ids.add(item.supplier_id)
                if not resolved_department and item.department:
                    resolved_department = item.department
                normalized_lines.append(
                    {
                        "item_id": item.id,
                        "item_name_free": "",
                        "maker": maker or (item.manufacturer or ""),
                        "quantity": quantity,
                        "note": note,
                    }
                )
            else:
                if not item_name_free:
                    raise PurchaseOrderError("自由入力明細は品名が必要です。")
                normalized_lines.append(
                    {
                        "item_id": None,
                        "item_name_free": item_name_free,
                        "maker": maker,
                        "quantity": quantity,
                        "note": note,
                    }
                )

        if not supplier_ids:
            raise PurchaseOrderError("仕入先が特定できないため発注を作成できません。")
        if len(supplier_ids) != 1:
            raise PurchaseOrderError("異なる仕入先の品目は同一注文書に混在できません。")

        supplier_id = next(iter(supplier_ids))
        normalized_user = (ordered_by_user or "").strip()
        supplier = self.db.scalar(select(Supplier).filter(Supplier.id == supplier_id))
        if not supplier:
            raise PurchaseOrderError("仕入先が見つかりません。")

        duplicate = self._find_recent_duplicate_order(
            supplier_id=supplier_id,
            department=resolved_department or "",
            ordered_by_user=normalized_user,
            lines=normalized_lines,
        )
        if duplicate:
            return {
                "purchase_order_id": duplicate.id,
                "status": duplicate.status,
                "supplier_name": supplier.name,
                "department": duplicate.department or "",
                "reused": True,
            }

        order = PurchaseOrder(
            id=self._allocate_reusable_order_id(),
            supplier_id=supplier_id,
            department=resolved_department or "",
            ordered_by_user=normalized_user,
            status=PurchaseOrderStatus.DRAFT.value,
            issued_date=None,
        )
        self.db.add(order)
        self.db.flush()

        for line_data in normalized_lines:
            self.db.add(
                PurchaseOrderLine(
                    purchase_order_id=order.id,
                    item_id=line_data["item_id"],
                    item_name_free=line_data["item_name_free"],
                    maker=line_data["maker"],
                    quantity=line_data["quantity"],
                    received_quantity=0,
                    note=line_data["note"],
                )
            )

        self.db.commit()
        return {
            "purchase_order_id": order.id,
            "status": order.status,
            "supplier_name": supplier.name,
            "department": order.department,
            "reused": False,
        }

    def create_bulk_orders_from_low_stock(
        self,
        ordered_by_user: str,
        department: str = "",
    ) -> dict[str, Any]:
        candidates = self.build_low_stock_candidates(department)
        grouped: dict[int, list[dict[str, Any]]] = {}
        for row in candidates:
            supplier_id = row.get("supplier_id")
            if not supplier_id:
                continue
            grouped.setdefault(int(supplier_id), []).append(row)

        created_orders: list[int] = []
        created_count = 0
        reused_count = 0
        for _, rows in grouped.items():
            if not rows:
                continue
            lines = [
                {
                    "item_id": row["item_id"],
                    "quantity": row["order_quantity"],
                    "note": row.get("note") or "",
                }
                for row in rows
            ]
            result = self.create_order(
                lines=lines,
                ordered_by_user=ordered_by_user,
                department=rows[0].get("department") or "",
            )
            created_orders.append(int(result["purchase_order_id"]))
            if bool(result.get("reused")):
                reused_count += 1
            else:
                created_count += 1

        return {
            "created_count": created_count,
            "reused_count": reused_count,
            "purchase_order_ids": created_orders,
        }

    def generate_document(
        self,
        order_id: int,
        generated_by: str,
        regenerate: bool = False,
    ) -> dict[str, Any]:
        order = self._load_order_with_relations(order_id)
        if not order:
            raise PurchaseOrderError("発注が見つかりません。")
        if order.status == PurchaseOrderStatus.CANCELLED.value:
            raise PurchaseOrderError("取消済みの発注では注文書を作成できません。")

        if order.document and not regenerate and Path(order.document.pdf_path).exists():
            if order.status == PurchaseOrderStatus.DRAFT.value:
                order.status = PurchaseOrderStatus.CONFIRMED.value
            if not order.issued_date:
                order.issued_date = date.today()
            self.db.commit()
            return {
                "purchase_order_id": order.id,
                "status": order.status,
                "pdf_path": order.document.pdf_path,
                "reused": True,
            }

        issued = order.issued_date or date.today()
        html = self._render_document_html(order, issued)
        destination_path = self._build_document_destination(order, issued, regenerate)

        temp_pdf_path: Optional[Path] = None
        pdf_generated = False
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp_file:
                temp_pdf_path = Path(temp_file.name)
            self._render_html_to_pdf(html, temp_pdf_path)
            pdf_generated = True
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(temp_pdf_path, destination_path)
        except Exception as exc:
            if pdf_generated:
                self._save_failed_log(
                    order=order,
                    sent_by=generated_by,
                    subject="注文書送付の件",
                    body="",
                    attachment_path=str(destination_path),
                    error_message=f"PDF生成は成功しましたがNAS保存に失敗しました: {exc}",
                )
                raise PurchaseOrderError(f"PDF生成は成功しましたがNAS保存に失敗しました: {exc}") from exc
            raise PurchaseOrderError(f"PDF生成に失敗しました: {exc}") from exc
        finally:
            if temp_pdf_path and temp_pdf_path.exists():
                try:
                    temp_pdf_path.unlink()
                except OSError:
                    pass

        order = self._load_order_with_relations(order_id)
        if not order:
            raise PurchaseOrderError("発注が見つかりません。")

        order.issued_date = issued
        if order.status == PurchaseOrderStatus.DRAFT.value:
            order.status = PurchaseOrderStatus.CONFIRMED.value

        if order.document:
            order.document.pdf_path = str(destination_path)
            order.document.generated_at = datetime.utcnow()
            order.document.generated_by = (generated_by or "").strip()
        else:
            self.db.add(
                PurchaseOrderDocument(
                    purchase_order_id=order.id,
                    pdf_path=str(destination_path),
                    generated_at=datetime.utcnow(),
                    generated_by=(generated_by or "").strip(),
                )
            )
        self.db.commit()

        return {
            "purchase_order_id": order.id,
            "status": order.status,
            "pdf_path": str(destination_path),
            "reused": False,
        }

    def get_document_preview_html(self, order_id: int) -> str:
        order = self._load_order_with_relations(order_id)
        if not order:
            raise PurchaseOrderError("発注が見つかりません。")
        issued = order.issued_date or date.today()
        return self._render_document_html(order, issued)

    def get_document_preview_pdf(self, order_id: int) -> bytes:
        order = self._load_order_with_relations(order_id)
        if not order:
            raise PurchaseOrderError("発注が見つかりません。")
        issued = order.issued_date or date.today()
        html = self._render_document_html(order, issued)

        temp_pdf_path: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp_file:
                temp_pdf_path = Path(temp_file.name)
            self._render_html_to_pdf(html, temp_pdf_path)
            return temp_pdf_path.read_bytes()
        except Exception as exc:
            raise PurchaseOrderError(f"注文書プレビューPDFの生成に失敗しました: {exc}") from exc
        finally:
            if temp_pdf_path and temp_pdf_path.exists():
                try:
                    temp_pdf_path.unlink()
                except OSError:
                    pass

    def get_email_preview(self, order_id: int) -> dict[str, Any]:
        order = self._load_order_with_relations(order_id)
        if not order:
            raise PurchaseOrderError("発注が見つかりません。")

        if not order.document:
            raise PurchaseOrderError("注文書PDFが未作成です。先に注文書を作成してください。")

        supplier = order.supplier
        if not supplier:
            raise PurchaseOrderError("仕入先情報が見つかりません。")

        email_settings = self._load_email_settings()
        sender_email = self._resolve_sender_email(email_settings, order)
        subject = "注文書送付の件"
        to_address = (supplier.email or "").strip()
        cc_address = (supplier.assistant_email or "").strip()

        body = self._build_email_body(order, supplier, sender_email)
        return {
            "purchase_order_id": order.id,
            "status": order.status,
            "to": to_address,
            "cc": cc_address,
            "subject": subject,
            "body": body,
            "attachment_path": order.document.pdf_path,
            "sender_email": sender_email,
        }

    def send_email(
        self,
        order_id: int,
        sent_by: str,
        regenerate: bool = False,
    ) -> dict[str, Any]:
        order = self._load_order_with_relations(order_id)
        if not order:
            raise PurchaseOrderError("発注が見つかりません。")

        if order.status == PurchaseOrderStatus.CANCELLED.value:
            raise PurchaseOrderError("取消済みの発注は送信できません。")
        if order.status == PurchaseOrderStatus.RECEIVED.value:
            raise PurchaseOrderError("納品計上済みの発注は送信できません。")

        if regenerate or not order.document or not Path(order.document.pdf_path).exists():
            self.generate_document(order_id=order_id, generated_by=sent_by, regenerate=regenerate)

        preview = self.get_email_preview(order_id)
        to_address = (preview["to"] or "").strip()
        if not to_address:
            message = "仕入先メールアドレス未登録のため送信できません。"
            self._save_failed_log(
                order=order,
                sent_by=sent_by,
                subject=preview["subject"],
                body=preview["body"],
                attachment_path=preview["attachment_path"],
                error_message=message,
            )
            raise PurchaseOrderError(message)

        email_settings = self._load_email_settings()
        sender_email = self._resolve_sender_email(email_settings, order)
        try:
            import keyring
        except ModuleNotFoundError as exc:
            raise PurchaseOrderError("keyring が未インストールです。`pip install keyring` を実行してください。") from exc

        password = keyring.get_password(SMTP_KEYRING_SERVICE_NAME, sender_email)
        if not password:
            raise PurchaseOrderError(
                "keyringにSMTPパスワードが見つかりません。"
                f"`python -m keyring set purchase_order_app {sender_email}` で登録してください。"
            )

        message = MIMEMultipart()
        message["From"] = sender_email
        message["To"] = to_address
        cc_address = (preview["cc"] or "").strip()
        if cc_address:
            message["Cc"] = cc_address
        message["Subject"] = preview["subject"]
        message.attach(MIMEText(preview["body"], "plain", "utf-8"))

        attachment_path = Path(preview["attachment_path"])
        if not attachment_path.exists():
            raise PurchaseOrderError("添付PDFが見つかりません。注文書を再生成してください。")

        with attachment_path.open("rb") as attachment_file:
            part = MIMEApplication(attachment_file.read(), Name=attachment_path.name)
        part["Content-Disposition"] = f'attachment; filename="{attachment_path.name}"'
        message.attach(part)

        recipients = [to_address] + self._split_addresses(cc_address)
        try:
            with smtplib.SMTP(email_settings.smtp_server, email_settings.smtp_port, timeout=30) as smtp:
                smtp.ehlo()
                smtp.starttls()
                smtp.ehlo()
                smtp.login(sender_email, password)
                smtp.sendmail(sender_email, recipients, message.as_string())
        except Exception as exc:
            self._save_failed_log(
                order=order,
                sent_by=sent_by,
                subject=preview["subject"],
                body=preview["body"],
                attachment_path=str(attachment_path),
                error_message=f"SMTP送信失敗: {exc}",
            )
            raise PurchaseOrderError(f"SMTP送信に失敗しました: {exc}") from exc

        order = self._load_order_with_relations(order_id)
        if not order:
            raise PurchaseOrderError("発注が見つかりません。")

        # メール送信後は入庫待ちとして扱う。
        order.status = PurchaseOrderStatus.WAITING.value
        self.db.add(
            EmailSendLog(
                purchase_order_id=order.id,
                sent_by=(sent_by or "").strip(),
                sent_at=datetime.utcnow(),
                to_recipients=to_address,
                cc_recipients=cc_address,
                subject=preview["subject"],
                body=preview["body"],
                attachment_path=str(attachment_path),
                success=True,
                error_message=None,
            )
        )
        self.db.commit()

        return {
            "purchase_order_id": order.id,
            "status": order.status,
            "sent_to": to_address,
            "sent_cc": cc_address,
        }

    def update_reply_due_date(self, line_id: int, due_date: date) -> dict[str, Any]:
        line = self.db.scalar(
            select(PurchaseOrderLine)
            .filter(PurchaseOrderLine.id == line_id)
            .options(selectinload(PurchaseOrderLine.order))
        )
        if not line:
            raise PurchaseOrderError("発注明細が見つかりません。")

        line.vendor_reply_due_date = due_date
        order = line.order
        if order and order.status not in {PurchaseOrderStatus.SENT.value, PurchaseOrderStatus.WAITING.value}:
            raise PurchaseOrderError("回答納期は送信済み/入庫待ちの発注のみ更新できます。")
        if order and order.status == PurchaseOrderStatus.SENT.value:
            order.status = PurchaseOrderStatus.WAITING.value
        self.db.commit()

        return {
            "line_id": line.id,
            "purchase_order_id": line.purchase_order_id,
            "vendor_reply_due_date": line.vendor_reply_due_date.isoformat() if line.vendor_reply_due_date else "",
            "order_status": order.status if order else "",
        }

    def update_order_status(self, order_id: int, target_status: str, updated_by: str) -> dict[str, Any]:
        order = self._load_order_with_relations(order_id)
        if not order:
            raise PurchaseOrderError("発注が見つかりません。")

        normalized = (target_status or "").strip().upper()
        if normalized not in {status.value for status in PurchaseOrderStatus}:
            raise PurchaseOrderError("不正なステータスです。")
        if normalized == order.status:
            return {"purchase_order_id": order.id, "status": order.status}

        allowed: dict[str, set[str]] = {
            PurchaseOrderStatus.DRAFT.value: {PurchaseOrderStatus.CONFIRMED.value, PurchaseOrderStatus.CANCELLED.value},
            PurchaseOrderStatus.CONFIRMED.value: {PurchaseOrderStatus.SENT.value, PurchaseOrderStatus.CANCELLED.value},
            PurchaseOrderStatus.SENT.value: {PurchaseOrderStatus.WAITING.value, PurchaseOrderStatus.CANCELLED.value},
            PurchaseOrderStatus.WAITING.value: {PurchaseOrderStatus.RECEIVED.value, PurchaseOrderStatus.CANCELLED.value},
            PurchaseOrderStatus.RECEIVED.value: set(),
            PurchaseOrderStatus.CANCELLED.value: set(),
        }

        if normalized not in allowed.get(order.status, set()):
            raise PurchaseOrderError(f"{order.status} から {normalized} へは遷移できません。")

        if normalized == PurchaseOrderStatus.CANCELLED.value:
            cancelled_id = order.id
            self.db.delete(order)
            self.db.commit()
            return {"purchase_order_id": cancelled_id, "status": PurchaseOrderStatus.CANCELLED.value, "deleted": True}

        if normalized == PurchaseOrderStatus.RECEIVED.value:
            self._apply_receipt_inventory(order, updated_by)

        order.status = normalized
        self.db.commit()
        return {"purchase_order_id": order.id, "status": order.status}

    def receive_order_partial(
        self,
        order_id: int,
        updated_by: str,
        line_receipts: Optional[dict[int, int]] = None,
    ) -> dict[str, Any]:
        order = self._load_order_with_relations(order_id)
        if not order:
            raise PurchaseOrderError("発注が見つかりません。")
        if order.status == PurchaseOrderStatus.CANCELLED.value:
            raise PurchaseOrderError("取消済み発注は入庫計上できません。")
        if order.status == PurchaseOrderStatus.RECEIVED.value:
            raise PurchaseOrderError("この発注は既に入庫完了です。")
        if order.status not in {PurchaseOrderStatus.SENT.value, PurchaseOrderStatus.WAITING.value}:
            raise PurchaseOrderError("入庫計上は送信済み/入庫待ちの発注のみ実行できます。")

        normalized_map: dict[int, int] = {}
        if line_receipts:
            for raw_line_id, raw_qty in line_receipts.items():
                line_id = int(raw_line_id)
                qty = int(raw_qty)
                if qty < 0:
                    raise PurchaseOrderError("入荷数量にマイナスは指定できません。")
                normalized_map[line_id] = qty
        if normalized_map:
            order_line_ids = {int(line.id) for line in order.lines}
            unknown_line_ids = sorted(set(normalized_map.keys()) - order_line_ids)
            if unknown_line_ids:
                raise PurchaseOrderError(f"この発注に存在しない明細IDです: {', '.join(str(v) for v in unknown_line_ids)}")

        processed_count = 0
        for line in order.lines:
            ordered = int(line.quantity or 0)
            received = max(0, int(line.received_quantity or 0))
            remaining = max(0, ordered - received)
            if remaining <= 0:
                continue

            if normalized_map:
                if line.id not in normalized_map:
                    continue
                incoming = normalized_map[line.id]
            else:
                incoming = remaining

            if incoming <= 0:
                continue
            if incoming > remaining:
                raise PurchaseOrderError(
                    f"明細ID {line.id} の入荷数量が残数を超えています。残数: {remaining}"
                )

            line.received_quantity = received + incoming
            processed_count += 1

            if line.item_id:
                inventory = self.db.scalar(select(InventoryItem).filter(InventoryItem.item_id == line.item_id))
                if not inventory:
                    inventory = InventoryItem(item_id=line.item_id, quantity_on_hand=0)
                    self.db.add(inventory)
                    self.db.flush()
                inventory.quantity_on_hand = (inventory.quantity_on_hand or 0) + incoming
                self.db.add(
                    InventoryTransaction(
                        item_id=line.item_id,
                        tx_type=TransactionType.RECEIPT,
                        delta=incoming,
                        reason=f"発注#{order.id} 分納入庫",
                        note=f"発注管理 明細#{line.id}",
                        occurred_at=datetime.utcnow(),
                        created_by=(updated_by or "").strip() or "system",
                    )
                )

        if processed_count == 0:
            raise PurchaseOrderError("入庫対象がありません。入荷数量を確認してください。")

        all_received = all(
            max(0, int(line.received_quantity or 0)) >= max(0, int(line.quantity or 0))
            for line in order.lines
        )
        order.status = PurchaseOrderStatus.RECEIVED.value if all_received else PurchaseOrderStatus.WAITING.value
        self.db.commit()

        return {
            "purchase_order_id": order.id,
            "status": order.status,
            "fully_received": all_received,
        }

    def _allocate_reusable_order_id(self) -> int:
        ids = self.db.scalars(select(PurchaseOrder.id).order_by(PurchaseOrder.id.asc())).all()
        expected = 1
        for existing_id in ids:
            if existing_id == expected:
                expected += 1
                continue
            if existing_id > expected:
                return expected
        return expected

    def _apply_receipt_inventory(self, order: PurchaseOrder, updated_by: str) -> None:
        for line in order.lines:
            ordered = int(line.quantity or 0)
            received = max(0, int(line.received_quantity or 0))
            remaining = max(0, ordered - received)
            if remaining <= 0:
                continue

            line.received_quantity = received + remaining
            if not line.item_id:
                continue

            inventory = self.db.scalar(select(InventoryItem).filter(InventoryItem.item_id == line.item_id))
            if not inventory:
                inventory = InventoryItem(item_id=line.item_id, quantity_on_hand=0)
                self.db.add(inventory)
                self.db.flush()
            inventory.quantity_on_hand = (inventory.quantity_on_hand or 0) + remaining
            self.db.add(
                InventoryTransaction(
                    item_id=line.item_id,
                    tx_type=TransactionType.RECEIPT,
                    delta=remaining,
                    reason=f"発注#{order.id} 納品計上",
                    note="発注管理",
                    occurred_at=datetime.utcnow(),
                    created_by=(updated_by or "").strip() or "system",
                )
            )

    def _find_recent_duplicate_order(
        self,
        supplier_id: int,
        department: str,
        ordered_by_user: str,
        lines: list[dict[str, Any]],
    ) -> Optional[PurchaseOrder]:
        # Protect against retry / double-click by deduplicating same payload in a short window.
        window_seconds_raw = os.getenv("PURCHASE_ORDER_DUPLICATE_WINDOW_SECONDS", str(DEFAULT_DUPLICATE_WINDOW_SECONDS))
        try:
            window_seconds = max(1, int(window_seconds_raw))
        except ValueError:
            window_seconds = DEFAULT_DUPLICATE_WINDOW_SECONDS
        cutoff = datetime.utcnow() - timedelta(seconds=window_seconds)

        candidate_stmt = (
            select(PurchaseOrder)
            .filter(
                PurchaseOrder.supplier_id == supplier_id,
                PurchaseOrder.department == department,
                PurchaseOrder.ordered_by_user == ordered_by_user,
                PurchaseOrder.created_at >= cutoff,
                PurchaseOrder.status != PurchaseOrderStatus.CANCELLED.value,
            )
            .options(selectinload(PurchaseOrder.lines))
            .order_by(PurchaseOrder.created_at.desc())
        )
        target_signature = self._line_signature_from_payload(lines)
        candidates = self.db.scalars(candidate_stmt).all()
        for candidate in candidates:
            if self._line_signature_from_order(candidate) == target_signature:
                return candidate
        return None

    @staticmethod
    def _line_signature_from_payload(lines: list[dict[str, Any]]) -> list[tuple[Optional[int], str, str, int, str]]:
        signature = [
            (
                int(line["item_id"]) if line.get("item_id") is not None else None,
                str(line.get("item_name_free") or "").strip(),
                str(line.get("maker") or "").strip(),
                int(line.get("quantity") or 0),
                str(line.get("note") or "").strip(),
            )
            for line in lines
        ]
        return sorted(signature, key=lambda x: (x[0] is None, x[0] or 0, x[1], x[2], x[3], x[4]))

    @staticmethod
    def _line_signature_from_order(order: PurchaseOrder) -> list[tuple[Optional[int], str, str, int, str]]:
        signature = [
            (
                line.item_id,
                (line.item_name_free or "").strip(),
                (line.maker or "").strip(),
                int(line.quantity or 0),
                (line.note or "").strip(),
            )
            for line in order.lines
        ]
        return sorted(signature, key=lambda x: (x[0] is None, x[0] or 0, x[1], x[2], x[3], x[4]))

    def _render_document_html(self, order: PurchaseOrder, issued: date) -> str:
        supplier = order.supplier
        company = self._load_company_profile()
        phone = self._resolve_company_phone(company, order.department or "")

        rows: list[dict[str, Any]] = []
        for idx, line in enumerate(order.lines, start=1):
            item_code = line.item.item_code if line.item else ""
            item_name = line.item_name_free or ""
            maker = line.maker or (line.item.manufacturer if line.item else "") or ""
            rows.append(
                {
                    "index": idx,
                    "item_code": item_code,
                    "item_name": item_name,
                    "maker": maker,
                    "quantity": line.quantity,
                    "reply_due_date": line.vendor_reply_due_date.isoformat() if line.vendor_reply_due_date else "",
                    "note": line.note or "",
                }
            )

        template = self.templates.env.get_template("purchase_order_document.html")
        return template.render(
            order=order,
            issued_date=issued.strftime("%Y/%m/%d"),
            supplier_name=supplier.name if supplier else "",
            supplier_contact=(supplier.contact_person if supplier else "") or "ご担当者",
            company_name=company.company_name,
            company_address=company.address,
            company_phone=phone,
            company_url=company.url,
            order_user=order.ordered_by_user or "",
            sender_email=self._resolve_sender_email(self._load_email_settings(), order),
            body_message="以下の通りご注文申し上げます。",
            body_sub_message="2日以内に納期回答をご記入の上、ご返信頂けますよう宜しくお願い致します。",
            rows=rows,
        )

    def _render_html_to_pdf(self, html: str, output_path: Path) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ModuleNotFoundError as exc:
            raise PurchaseOrderError(
                "playwright が未インストールです。`pip install playwright` と "
                "`python -m playwright install chromium` を実行してください。"
            ) from exc

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content(html, wait_until="networkidle")
            page.pdf(
                path=str(output_path),
                format="A4",
                print_background=True,
                margin={"top": "12mm", "right": "12mm", "bottom": "12mm", "left": "12mm"},
            )
            browser.close()

    def _build_document_destination(self, order: PurchaseOrder, issued: date, regenerate: bool) -> Path:
        department = sanitize_windows_segment(order.department or "未設定部署")
        supplier_name = sanitize_windows_segment(order.supplier.name if order.supplier else "未設定仕入先")
        directory = self.nas_root / department / supplier_name

        base_name = f"PO_{order.id}_{issued.strftime('%Y%m%d')}"
        if not regenerate:
            return directory / f"{base_name}.pdf"

        version = 2
        while True:
            candidate = directory / f"{base_name}_v{version}.pdf"
            if not candidate.exists():
                return candidate
            version += 1

    def _build_email_body(self, order: PurchaseOrder, supplier: Supplier, sender_email: str) -> str:
        company = self._load_company_profile()
        phone = self._resolve_company_phone(company, order.department or "")
        supplier_contact = (supplier.contact_person or "ご担当者").strip()

        return (
            f"{supplier.name}\n"
            f"{supplier_contact} 様\n\n"
            "いつもお世話になっております。\n"
            "注文書を送付いたします。ご確認のうえご対応をお願いいたします。\n"
            "回答納期欄にご記入いただき、ご返信ください。\n\n"
            f"{company.company_name}\n"
            f"発注担当: {order.ordered_by_user or '未設定'}\n"
            f"住所: {company.address}\n"
            f"Email: {sender_email}\n"
            f"TEL: {phone}\n"
            f"URL: {company.url}\n"
        )

    def _save_failed_log(
        self,
        order: PurchaseOrder,
        sent_by: str,
        subject: str,
        body: str,
        attachment_path: str,
        error_message: str,
    ) -> None:
        self.db.add(
            EmailSendLog(
                purchase_order_id=order.id,
                sent_by=(sent_by or "").strip(),
                sent_at=datetime.utcnow(),
                to_recipients=(order.supplier.email if order.supplier and order.supplier.email else ""),
                cc_recipients=(
                    order.supplier.assistant_email
                    if order.supplier and order.supplier.assistant_email
                    else ""
                ),
                subject=subject,
                body=body,
                attachment_path=attachment_path,
                success=False,
                error_message=error_message,
            )
        )
        self.db.commit()

    def _load_order_with_relations(self, order_id: int) -> Optional[PurchaseOrder]:
        return self.db.scalar(
            select(PurchaseOrder)
            .filter(PurchaseOrder.id == order_id)
            .options(
                selectinload(PurchaseOrder.supplier),
                selectinload(PurchaseOrder.document),
                selectinload(PurchaseOrder.lines).selectinload(PurchaseOrderLine.item),
            )
        )

    def _load_email_settings(self) -> EmailSettings:
        settings_path = self.config_dir / "email_settings.json"
        if not settings_path.exists():
            raise PurchaseOrderError(f"SMTP設定ファイルが見つかりません: {settings_path}")
        with settings_path.open("r", encoding="utf-8-sig") as fp:
            raw = json.load(fp)

        smtp_server = str(raw.get("smtp_server") or "").strip()
        smtp_port = int(raw.get("smtp_port") or 0)
        accounts_raw = raw.get("accounts") or {}
        department_defaults_raw = raw.get("department_defaults") or {}

        accounts: dict[str, str] = {}
        display_names: dict[str, str] = {}
        account_departments: dict[str, list[str]] = {}
        if isinstance(accounts_raw, dict):
            for key, value in accounts_raw.items():
                if not isinstance(value, dict):
                    continue
                account_key = str(key).strip()
                sender = str(value.get("sender") or "").strip()
                display_name = str(value.get("display_name") or "").strip()
                departments = self._normalize_departments(value.get("department"))
                if not departments:
                    departments = self._normalize_departments(value.get("departments"))
                if not account_key or not sender:
                    continue
                accounts[account_key] = sender
                display_names[account_key] = display_name or account_key
                account_departments[account_key] = departments

        department_defaults: dict[str, str] = {}
        if isinstance(department_defaults_raw, dict):
            for department, account_key in department_defaults_raw.items():
                department_name = str(department).strip()
                account = str(account_key).strip()
                if not department_name or not account:
                    continue
                if account in accounts:
                    department_defaults[department_name] = account
                    if department_name not in account_departments.setdefault(account, []):
                        account_departments[account].append(department_name)

        if not smtp_server or smtp_port <= 0 or not accounts:
            raise PurchaseOrderError(
                "email_settings.json に smtp_server / smtp_port / accounts(sender) を設定してください。"
            )

        return EmailSettings(
            smtp_server=smtp_server,
            smtp_port=smtp_port,
            accounts=accounts,
            display_names=display_names,
            account_departments=account_departments,
            department_defaults=department_defaults,
        )

    @staticmethod
    def _compact_name(value: str) -> str:
        return re.sub(r"\s+", "", (value or "").replace("\u3000", ""))

    @staticmethod
    def _resolve_company_phone(company: CompanyProfile, department: str) -> str:
        default_phone = (company.default_phone or "").strip()
        dept_phone = (company.department_phones.get(department, "") or "").strip()
        if not dept_phone:
            return default_phone or "未設定"
        if not default_phone:
            return dept_phone

        # department_phones can hold either full phone text or guidance-only text.
        normalized_digits = re.sub(r"\D", "", dept_phone)
        looks_like_phone = bool(
            re.search(r"\d{2,4}\s*[-−ー]\s*\d{2,4}\s*[-−ー]\s*\d{3,4}", dept_phone)
            or len(normalized_digits) >= 10
        )
        if looks_like_phone:
            return dept_phone

        if dept_phone.startswith("（") and dept_phone.endswith("）"):
            return f"{default_phone}{dept_phone}"
        return f"{default_phone}（{dept_phone}）"

    @staticmethod
    def _normalize_departments(value: object) -> list[str]:
        departments: list[str] = []
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

    def _resolve_sender_email(self, settings: EmailSettings, order: PurchaseOrder) -> str:
        ordered_by_user = (order.ordered_by_user or "").strip()
        department = (order.department or "").strip()

        # 1) "部署 表示名" 形式の表示名一致
        ordered_department = department
        display_part = ordered_by_user
        normalized = re.sub(r"\s+", " ", ordered_by_user.replace("\u3000", " ")).strip()
        if " " in normalized:
            split_department, split_display = normalized.split(" ", 1)
            ordered_department = split_department.strip() or ordered_department
            display_part = split_display.strip()
        compact_display = self._compact_name(display_part)
        for account_key, display_name in settings.display_names.items():
            if not compact_display or self._compact_name(display_name) != compact_display:
                continue
            account_departments = settings.account_departments.get(account_key, [])
            if ordered_department and account_departments and ordered_department not in account_departments:
                continue
            if department and account_departments and department not in account_departments:
                continue
            return settings.accounts[account_key]

        # 2) ordered_by_user がアカウントキーの場合
        if ordered_by_user in settings.accounts:
            return settings.accounts[ordered_by_user]

        # 3) 部署デフォルト
        account_key = settings.department_defaults.get(department)
        if account_key and account_key in settings.accounts:
            return settings.accounts[account_key]

        # 4) 最初のアカウント
        for sender in settings.accounts.values():
            return sender

        raise PurchaseOrderError("送信元メールアカウントが設定されていません。")

    def _load_company_profile(self) -> CompanyProfile:
        profile_path = self.config_dir / "company_profile.json"
        if not profile_path.exists():
            return CompanyProfile(
                company_name="会社名未設定",
                address="住所未設定",
                url="https://example.invalid",
                default_phone="未設定",
                department_phones={},
            )

        with profile_path.open("r", encoding="utf-8-sig") as fp:
            raw = json.load(fp)

        department_phones = raw.get("department_phones") or {}
        if not isinstance(department_phones, dict):
            department_phones = {}

        return CompanyProfile(
            company_name=str(raw.get("company_name") or "会社名未設定"),
            address=str(raw.get("address") or "住所未設定"),
            url=str(raw.get("url") or "https://example.invalid"),
            default_phone=str(raw.get("default_phone") or "未設定"),
            department_phones={str(k): str(v) for k, v in department_phones.items()},
        )

    @staticmethod
    def _split_addresses(raw: str) -> list[str]:
        if not raw:
            return []
        return [addr.strip() for addr in re.split(r"[;,]", raw) if addr.strip()]


def sanitize_windows_segment(value: str) -> str:
    cleaned = INVALID_WINDOWS_SEGMENT_CHARS.sub("", (value or "")).strip().rstrip(" .")
    if not cleaned:
        return "UNKNOWN"
    if cleaned.upper() in WINDOWS_RESERVED_NAMES:
        cleaned = f"{cleaned}_"
    return cleaned
