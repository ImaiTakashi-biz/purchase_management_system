# Changelog

## [0.1.1] - 2026-02-02
### Added
- 発注管理 `/orders` に在庫一覧と統一感のあるページ構成を導入し、左ペインに部署ツリー・highlightカード・在庫不足ハイライトカードのレイアウトを追加しました。

### Changed
- 差分 +0 も不足扱いの色（赤）に統一し、一覧のカード/hover 表示を在庫一覧と同じように調整しました。

### Fixed
- 低在庫候補のリストをカード化し、hover で強調表示されない問題を修正。これにより発注一覧のマウスオーバーで背景/境界が変化するようになりました。

## [0.1.0] - 2026-01-31
### Added
- 在庫一覧で `/inventory/inline-adjust` による +/- 調整と `/api/inventory/issues`、`/recent-transactions` API を導入し、中央ビュー・履歴を同期させた運用フローを構築。
- `manage_data` ページに仕入先/仕入品の検索付き編集フォームと `POST /api/suppliers` / `PUT /api/suppliers/{id}` / `POST /api/items` / `PUT /api/items/{id}` を実装し、CSV バッチなしでデータの追加・更新が可能に。
- 発注管理ページ `/orders` と `purchase_orders` 系テーブル／APIを追加し、発注登録・受領入力・ステータス更新・履歴記録を在庫トランザクションと連動させた仕組みを実現。
### Changed
- `/inventory/inline-adjust` は `tx_type=ADJUST` 固定・`reason` 任意化を明記し、既存の出庫/調整 API（および履歴取得）はそのまま保った上で UX をスムーズに。
- 管理画面では検索入力に `datalist` を追加して入力候補を表示、一覧クリックでフォームへスクロール、仕入先の全カラムを並べて UX を改善。
### Fixed
- `ZoneInfo("Asia/Tokyo")` 周りの `tzdata` 依存を満たし、直近履歴やダイアログのタイムスタンプを安定的に JST で表示できるように修正。
### Deps
- `requirements.txt` に `tzdata` / `python-multipart` を追加して zoneinfo/tzdata と multipart フォームの実行環境を整備。
