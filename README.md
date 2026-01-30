# 購入品一元管理システム

## 概要
社内で使用する購入品（刃物・検査治具・消耗品等）について、在庫管理・発注管理・納品管理を一元管理する社内向けWebアプリです。FastAPI をベースにしたサーバーサイドアプリとして構築しています。

## セットアップ手順
1. 仮想環境を作成したうえで、ルートで依存をインストールしてください。
   ```bash
   pip install -r requirements.txt
   ```
2. SQLite データベースを準備します（テーブルが未作成の場合は自動で作成されます）。
3. CSV を使って `items` / `inventory_items` を初期投入するには、以下を実行します。
   ```bash
   python scripts/import_items.py
   ```
   `data/seed/仕入品マスタ.csv` と `data/seed/仕入先マスタ.csv` を正データとして読み込み、品目/仕入先情報を `items` / `inventory_items` / `inventory_transactions` に反映します。

## ディレクトリ構成（概要）

```text
app/            : Webアプリ実装本体（FastAPI）
ui_reference/   : UI参考HTML・画像（実行しない）
data/seed/      : DB初期投入用CSV
scripts/        : CSVインポート等のバッチ処理
docs/           : 要件定義・設計資料
```

## 重要なファイル
- `requirements.txt`：依存一覧（FastAPI / SQLAlchemy / Alembic など）
- `app/main.py`：在庫一覧 UI / API のエントリポイント
- `scripts/import_items.py`：CSV から `items` / `inventory_items` / `inventory_transactions` を構築するスクリプト
- `data/seed/仕入品マスタ.csv`：CSV 正データ。これを変更したら再度 `import_items.py` を実行してください。

## API: 持出し記録

- `POST /api/inventory/issues`
  - リクエスト JSON: `{ "item_code": "...", "quantity": 2 }`
  - レスポンス JSON:
    - `item_code`, `on_hand`, `reorder_point`, `gap_label`, `status_label`, `status_badge`, `status_description`
    - `last_activity`, `last_updated`, `supplier`, `message`
  - 在庫モーダルの「確定」ボタンでは、現在の在庫数とプラス/マイナス入力で決めた値との差分を見て、数が減る場合はこの API を自動的に呼び、出庫トランザクション（Issue）を記録します。増加操作の場合は `/inventory/inline-adjust` が呼ばれます。
  - 画面は API を意識する必要がなく、「確定」で操作が完了し、内部的に `inventory_transactions` に記録が残ります（`created_by="system"`）。

## API: 直近トランザクション取得

- `GET /recent-transactions`
  - クエリパラメータ: `limit`（オプション、1〜20 件、デフォルト 4）
  - レスポンス JSON: `{"transactions": [...]}` 形式で `item`, `department`, `summary`, `shelf`, `item_code`, `manufacturer`, `delta`, `note`, `date` を返します。
  - モーダルの「確定」直後にフロントエンドからこのエンドポイントを呼び、画面の履歴一覧をリフレッシュする仕組みです。
