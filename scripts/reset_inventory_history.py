"""
在庫トランザクション履歴を全件削除するスクリプト。

- 対象: inventory_transactions テーブル（入庫・出庫・調整の履歴）
- 在庫数（inventory_items.quantity_on_hand）は変更しません。表示上の「直近トランザクション」が空になります。
- 実行時は --yes を付けるか、プロンプトで y を入力してください。
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(PROJECT_ROOT))

from sqlalchemy import delete, func, select

from app.db.session import SessionLocal, init_db
from app.models.tables import InventoryTransaction


def main() -> None:
    force = "--yes" in sys.argv or "-y" in sys.argv
    init_db()
    with SessionLocal() as session:
        count = session.scalar(select(func.count()).select_from(InventoryTransaction)) or 0
        if count == 0:
            print("履歴は既に0件です。")
            return
        if not force:
            try:
                answer = input(f"在庫トランザクション履歴を全件（{count}件）削除します。よろしいですか？ [y/N]: ")
            except EOFError:
                answer = ""
            if answer.strip().lower() != "y":
                print("キャンセルしました。")
                return
        session.execute(delete(InventoryTransaction))
        session.commit()
        print(f"履歴を {count} 件削除しました。")


if __name__ == "__main__":
    main()
