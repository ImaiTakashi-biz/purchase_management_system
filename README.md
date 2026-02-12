# 購入品一元管理システム

FastAPI + SQLite で在庫管理と発注管理を行う社内向けアプリです。

## 主な機能
- ダッシュボード（在庫/発注/入出庫の集約表示）
- 在庫一覧（検索・絞り込み・不足判定）
- 在庫の出庫・数量調整・履歴表示
- 入出庫管理（入庫待ち発注の入庫計上、分納対応、管理者在庫調整）
- 仕入先/仕入品マスタ管理
- メール設定管理（admin権限、keyring連携）
- 発注管理
  - 低在庫候補から発注作成（選択作成 / 仕入先ごと一括作成）
  - 注文書PDFプレビュー・PDF生成（Playwright）
  - NAS保存（パスのみDB保存）
  - SMTPメール送信（プレビュー→確認→送信）
  - 送信ログ保存
  - 回答納期（明細行単位）転記

## セットアップ（Windows）
1. 依存インストール
```powershell
pip install -r requirements.txt
```

2. Playwrightブラウザ導入
```powershell
python -m playwright install chromium
```

3. DB初期化 + CSV取込
```powershell
python scripts/import_items.py
```

4. SMTP設定ファイル編集（パスワードは保存しない）
- `config/email_settings.json`
  - `smtp_server`
  - `smtp_port`
  - `accounts`（表示名 + sender + department）
  - `department_defaults`（部署ごとの既定アカウント）

5. 会社情報設定（注文書/署名用）
- `config/company_profile.json`
  - `company_name`
  - `address`
  - `url`
  - `default_phone`
  - `department_phones`

6. SMTPパスワードをkeyringへ保存
```powershell
python -m keyring set purchase_order_app <accounts.*.sender>
```

7. 起動
```powershell
uvicorn app.main:app --reload --port 8000
```

## ログイン/権限
- 本システムはログイン必須です。
- トップページ `/` は `/dashboard` へリダイレクトされます。
- 起動時、`app_users` に管理者ユーザーが存在しない場合は初期管理者を自動作成します。
  - ユーザー名: `APP_BOOTSTRAP_ADMIN_USERNAME`（既定: `admin`）
  - パスワード: `APP_BOOTSTRAP_ADMIN_PASSWORD`（既定: `admin12345`）
  - 表示名: `APP_BOOTSTRAP_ADMIN_DISPLAY_NAME`（既定: `管理者`）
- 既定値のまま運用せず、初回起動前に環境変数で変更してください。
- 役割:
  - `viewer`: 在庫/履歴の閲覧
  - `manager`: 発注処理、在庫更新
  - `admin`: データ管理、SMTP設定編集、上記すべて
- データ管理画面:
  - `manager`: `/manage/suppliers`, `/manage/items`
  - `admin`: `/manage/email-settings`（加えて manager 画面も利用可）

## NAS設定
- 既定保存先（環境変数未指定時）
  - `\\192.168.1.200\共有\dev_tools\発注管理システム\注文書`
- 保存階層
  - `注文書\部署名\仕入先名\PO_<order_id>_<yyyymmdd>.pdf`
- フォルダ名は Windows 禁則文字を除去して自動作成
- 開発機でNASを使わない場合は環境変数で上書き可能
```powershell
$env:PURCHASE_ORDER_NAS_ROOT='C:\temp\po_docs'
```

## 発注API（主要）
- `POST /purchase-orders`
- `POST /purchase-orders/bulk-from-low-stock`
- `GET /purchase-orders/{id}/document-preview`
- `POST /purchase-orders/{id}/document`
- `GET /purchase-orders/{id}/email-preview`
- `POST /purchase-orders/{id}/send-email`
- `POST /purchase-order-lines/{line_id}/reply-due-date`
- `POST /purchase-orders/{id}/status`

## 成功/失敗時の挙動
- メール未登録
  - 送信不可
  - 400エラー返却
  - `email_send_logs` に失敗理由を記録
- SMTP失敗
  - 400エラー返却
  - `email_send_logs` に失敗理由を記録
- NAS書き込み不可
  - 「PDF生成成功 / NAS保存失敗」を区別してエラー返却
  - `email_send_logs` に失敗理由を記録
- 再送時
  - 既存PDFを再利用（既定）
  - 再生成ONの場合は別名（`_v2`, `_v3`...）で保存

## 既存在庫機能への影響
- 既存の在庫一覧・出庫・調整・履歴API/画面は維持
- 納品計上は `POST /purchase-orders/{id}/status` で `RECEIVED` 遷移時のみ実行
- 在庫計上時は既存と同じ `inventory_items` / `inventory_transactions` に追記するため、在庫機能との整合性を維持
