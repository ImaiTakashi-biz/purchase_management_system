"""
履歴を全て削除するスクリプト（在庫取引＋発注）。

- 対象:
  - 在庫トランザクション（inventory_transactions）
  - メール送信ログ（email_send_logs）
  - 注文書（purchase_order_documents）
  - 発注明細（purchase_order_lines）
  - 発注（purchase_orders）
- 仕入先・仕入品・在庫数（quantity_on_hand）は残します。
- 実行時は --yes を付けるか、プロンプトで y を入力してください。
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(PROJECT_ROOT))

from sqlalchemy import delete, func, select

from app.db.session import SessionLocal, init_db
from app.models.tables import (
    EmailSendLog,
    InventoryTransaction,
    PurchaseOrder,
    PurchaseOrderDocument,
    PurchaseOrderLine,
)


def main() -> None:
    force = "--yes" in sys.argv or "-y" in sys.argv
    init_db()
    with SessionLocal() as session:
        count_logs = session.scalar(select(func.count()).select_from(EmailSendLog)) or 0
        count_docs = session.scalar(select(func.count()).select_from(PurchaseOrderDocument)) or 0
        count_lines = session.scalar(select(func.count()).select_from(PurchaseOrderLine)) or 0
        count_orders = session.scalar(select(func.count()).select_from(PurchaseOrder)) or 0
        count_txs = session.scalar(select(func.count()).select_from(InventoryTransaction)) or 0
        total = count_logs + count_docs + count_lines + count_orders + count_txs
        if total == 0:
            print("履歴は既に0件です。")
            return
        if not force:
            print(
                f"以下の履歴を全件削除します: "
                f"発注={count_orders}, 明細={count_lines}, 注文書={count_docs}, "
                f"メールログ={count_logs}, 在庫取引={count_txs}"
            )
            try:
                answer = input("よろしいですか？ [y/N]: ")
            except EOFError:
                answer = ""
            if answer.strip().lower() != "y":
                print("キャンセルしました。")
                return

        # 外部キー順に削除
        session.execute(delete(EmailSendLog))
        session.execute(delete(PurchaseOrderDocument))
        session.execute(delete(PurchaseOrderLine))
        session.execute(delete(PurchaseOrder))
        session.execute(delete(InventoryTransaction))
        session.commit()
        print(
            f"履歴を削除しました: 発注 {count_orders} 件, 明細 {count_lines} 件, "
            f"注文書 {count_docs} 件, メールログ {count_logs} 件, 在庫取引 {count_txs} 件"
        )


if __name__ == "__main__":
    main()
