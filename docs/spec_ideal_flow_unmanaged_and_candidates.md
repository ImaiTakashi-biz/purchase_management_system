# 理想の流れ 仕様書：管理外依頼を発注候補に反映し同時発注

## 1. 概要

### 1.1 目的

- **現状（変更前）**: ① 管理外発注依頼で「発注に取り込む」→ 管理外のみで新規発注を1件作成。② 発注候補は在庫不足品目のみ。管理品と管理外を同じ発注にまとめられない。
- **理想**: ① で仕入先のみ選択して「確定」→ その内容が ②「発注候補から新規発注を作成」のリストに**行として追加**され、在庫不足品目と一緒に選択して**同一仕入先で1発注にまとめて発注**できるようにする。

### 1.2 基本方針

- ① の操作は「発注を作成する」のではなく「発注候補に追加する」に変更（または追加）する。
- ② のテーブルに「在庫不足候補」と「発注候補に追加された管理外」を**同一テーブル**で表示する。
- 「選択品目で発注作成」で、管理品・管理外を混在させた**1件の発注**を作成できるようにする。

---

## 2. 用語・状態

| 用語 | 説明 |
|------|------|
| 管理外依頼（未処理） | `UnmanagedOrderRequest.status = PENDING`。① のテーブルに表示される。 |
| 発注候補に追加済み（ステージング） | 未処理の依頼に「仕入先」を紐づけ、② のリストに出す状態。DB で保持する。 |
| 発注済み | `status = CONVERTED`。発注に取り込み済み。 |
| 在庫不足候補 | 既存の `build_low_stock_candidates` が返す品目（管理品のみ）。 |

---

## 3. データ設計

### 3.1 ステージングの保持方法

**方針**: サーバー側で保持し、ページ再表示・別タブでも一貫して「発注候補に追加済み」を表示する。

- **案A（推奨）**: `UnmanagedOrderRequest` に次を追加する。
  - `staged_supplier_id` … `ForeignKey("suppliers.id")`, nullable。発注候補に追加時に選択した仕入先。
  - `staged_at` … `DateTime`, nullable。発注候補に追加した日時。
- **意味**: `status = PENDING` かつ `staged_supplier_id IS NOT NULL` の依頼を「発注候補に追加済み」とみなし、② のリストに含める。発注作成後に CONVERTED にしたら `staged_supplier_id` / `staged_at` はクリア（またはそのままでも可。表示では CONVERTED を優先する）。

**案B（代替）**: ステージング用の別テーブル（例: `unmanaged_order_staging`: request_id, supplier_id, staged_at）を用意する。  
→ 正規化はしやすいが、依頼と 1:1 で紐づくだけなので、案A のカラム追加で十分とする。

### 3.2 既存との関係

- **「発注に取り込む」は廃止**する。① には「発注候補に追加」ボタンのみとし、管理外の発注は必ず ② で「選択品目で発注作成」から行う（在庫不足候補とまとめて 1 発注にできる）。

---

## 4. ユーザー操作フロー

### 4.1 ① 管理外発注依頼

1. 発注管理ページで部署を選択する（現行どおり）。
2. ① に「未処理」の管理外依頼が表示される。
3. 依頼を 1 件以上選択し、**仕入先**を選択する（発注担当者は ② で指定する）。
4. **「発注候補に追加」**をクリックする。
   - 選択した依頼に対して `staged_supplier_id` / `staged_at` を設定する（API: 後述）。
   - 発注は作成しない。① のテーブルからは「未処理」のまま（または「発注候補に追加済み」と分かる表示にしてもよい）。
5. 発注は ② で在庫不足候補と合わせて「選択品目で発注作成」から行う（「発注に取り込む」は廃止のためなし）。

### 4.2 ② 発注候補から新規発注を作成

1. ② のテーブルに次の 2 種が**同じテーブル**で並ぶ。
   - **在庫不足候補**: 既存の `low_stock_suggestions`（品番・品名・仕入先・在庫・発注点・単価・注文数量・金額・備考）。
   - **発注候補に追加済みの管理外**: 品番/品名（依頼の品番・品名）、仕入先（ステージング時の仕入先）、在庫/発注点は「—」または「管理外」、単価（あれば）、注文数量（依頼数量を初期値）、金額・備考。
2. ユーザーは**同一仕入先**の行だけを選択する（管理品と管理外の混在可）。
3. 発注担当者を選択し、**「選択品目で発注作成」**をクリックする。
4. 1 件の発注が作成され、その中に「在庫不足候補」の行と「発注候補に追加済み管理外」の行が含まれる。管理外については依頼が CONVERTED に更新され、`purchase_order_id` / `purchase_order_line_id` が設定される。ステージングはクリア（`staged_supplier_id` / `staged_at` を null にする）する。

### 4.3 制約

- ② で「選択品目で発注作成」するときは、**選択行はすべて同一仕入先**（現行どおり）。管理外行はステージング時に決めた仕入先で固定。
- 発注候補に追加済みの管理外は、**部署絞り**に合わせて表示する。`UnmanagedOrderRequest.requested_department` と発注管理ページの `selected_department` が一致する（または未選択時は全件）など、既存の依頼一覧の絞り方に合わせる。

---

## 5. API 仕様

### 5.1 発注候補に追加（新規）

- **エンドポイント**: `POST /api/unmanaged-order-requests/stage`（例）
- **権限**: 発注管理可能ユーザー（管理者など）。
- **リクエスト例**:
  ```json
  {
    "request_ids": [1, 2, 3],
    "supplier_id": 5
  }
  ```
- **処理**:
  - `request_ids` の依頼がすべて `status = PENDING` かつ存在することを確認する。
  - 各依頼に `staged_supplier_id = supplier_id`、`staged_at = now()` を設定する。
  - この API では**発注は作成しない**（ステージングのみ。発注は ② で「選択品目で発注作成」から行う）。
- **レスポンス**: 成功時は `{ "staged_count": 3 }` など。失敗時は 400 とメッセージ。

### 5.2 発注管理ページ用の「候補一覧」取得

- **現状**: 発注管理ページでは `build_low_stock_candidates(department)` の結果を `low_stock_suggestions` として返している。
- **変更**: 同じレスポンスに「発注候補に追加済みの管理外」を**別キー**で返すか、**統合した 1 リスト**で返す。

**案A（統合リスト）**  
- サービスに `build_order_candidates(department)` を新設（または既存メソッドを拡張）する。
  - 在庫不足候補: 既存の `build_low_stock_candidates` と同様の形式で、`type: "managed"`（または `item_id` が存在）で識別。
  - 発注候補に追加済み: `status = PENDING` かつ `staged_supplier_id IS NOT NULL` の依頼を取得し、表示用に次のような形に変換する。
    - `type: "unmanaged"` または `unmanaged_request_id` を付与。
    - 品番・品名: 依頼の `item_id` があれば Item から、なければ `item_code_free`。
    - 仕入先: `staged_supplier_id` から Supplier を取得して名前を付与。
    - 在庫・発注点: 管理外のため null または「—」用のフラグ。
    - 単価: 仕入先×品目で取れれば表示、なければ null。
    - 注文数量: 依頼の `quantity` を初期値。
    - 備考・使用先・希望納期: 依頼から転記。
  - 返却は `order_candidates: [ ... ]` のように 1 リストにまとめ、テンプレートでは 1 つのテーブルでループする。

**案B（別キー）**  
- `low_stock_suggestions` は従来どおり。
- `staged_unmanaged_candidates: [ ... ]` を追加で返す。フロントで 2 配列を結合して 1 テーブルで表示する。

**推奨**: バックエンドで統合して `order_candidates` 1 本で返す**案A**。テンプレートと JS の変更が少なく、同一仕入先チェックも「選択行の supplier_id が 1 種類か」で済む。

### 5.3 発注作成 API の拡張（POST /purchase-orders）

- **現状**: `lines` の要素は `item_id` または `item_name_free` などで、管理品/自由入力のみ。
- **変更**: `lines` の要素に **`unmanaged_request_id`**（任意）を追加する。
  - `unmanaged_request_id` が指定されている行は、その ID の `UnmanagedOrderRequest` を参照し、依頼の内容（品番・品名・メーカー・数量・使用先・備考・希望納期）で発注明細を組み立てる。仕入先は依頼の `staged_supplier_id` を使う。
  - 発注作成後、対象の依頼を `CONVERTED` にし、`purchase_order_id` / `purchase_order_line_id` を設定する。あわせて `staged_supplier_id` / `staged_at` を null にする。
- **制約**:
  - 1 発注に含まれる行はすべて**同一仕入先**（管理品の行は item から解決した仕入先、管理外の行は `staged_supplier_id`）。混在時は 400 で返す。
  - `unmanaged_request_id` の依頼は `status = PENDING` かつ `staged_supplier_id` が設定されていること。そうでなければ 400。

---

## 6. 画面・UI

### 6.1 ① 管理外発注依頼

- **廃止**: 「選択した依頼を発注に取り込む」ボタンおよび発注担当者セレクトは**削除**する（即時発注作成は行わない）。
- **採用**: 仕入先セレクトと「**発注候補に追加**」ボタンのみとする。
  - 押下時: 選択依頼＋選択仕入先で `POST /api/unmanaged-order-requests/stage` を呼ぶ。成功したら「② の発注候補に追加しました」などと表示し、必要ならページをリロードして ② に反映させる。
- 発注担当者は ② で指定するため、① には発注担当者入力は不要。

### 6.2 ② 発注候補から新規発注を作成

- テーブルを **order_candidates**（在庫不足＋発注候補に追加済み管理外）の 1 リストで描画する。
- 各行のデータ属性の例:
  - 管理品: `data-item-id`, `data-supplier-id`, `data-unit-price`, `data-suppliers`（既存と同様）。`data-unmanaged-request-id` はなし。
  - 管理外: `data-unmanaged-request-id`, `data-supplier-id`, `data-unit-price`（あれば）, `data-quantity`。`data-item-id` は空またはなし。
- 在庫・発注点: 管理外の行は「—」または「管理外」と表示する。
- 「選択品目で発注作成」の送信時:
  - 選択行から `lines` を組み立てる。`item_id` がある行は従来どおり。`unmanaged_request_id` がある行は `{ unmanaged_request_id, quantity, note }` などを送る（数量・備考は画面で編集可能にする）。
- 同一仕入先チェック: 選択行の `data-supplier-id` が 1 種類であることを確認する（現行の管理品のみのチェックを、管理外行を含むように拡張）。

### 6.3 説明文の更新

- 発注の流れの案内: 「① 管理外依頼の有無を確認し、あれば仕入先を選んで「発注候補に追加」→ ② で在庫不足品目と一緒に選択して発注を作成」のように文言を更新する。

---

## 7. バックエンド処理の追加・変更まとめ

| 対象 | 内容 |
|------|------|
| DB | `UnmanagedOrderRequest` に `staged_supplier_id`, `staged_at` を追加。マイグレーションまたは init_db で対応。 |
| サービス | `stage_unmanaged_requests(request_ids, supplier_id)` を追加。ステージングのみ行い、発注は作らない。 |
| サービス | 発注管理ページ用に「在庫不足候補＋発注候補に追加済み管理外」を返すメソッドを追加または拡張。返却形式は `order_candidates` の 1 リストを推奨。 |
| サービス | `create_order` の `lines` に `unmanaged_request_id` を許容。該当依頼を参照して明細を組み立て、発注作成後に CONVERTED に更新し、ステージングをクリア。 |
| API | `POST /api/unmanaged-order-requests/stage` を追加。 |
| API | 発注管理ページのコンテキストに `order_candidates`（または `low_stock_suggestions` + `staged_unmanaged_candidates`）を渡す。 |
| API | `POST /purchase-orders` のリクエストボディで `lines[].unmanaged_request_id` を受け付ける。 |

---

## 8. エッジケース・注意点

- **発注候補に追加したが発注しない**: ステージングは DB に残る。次回以降も ② に表示され続ける。不要なら「発注候補から外す」操作を別途用意するか、一定期間でクリアする運用を検討する。
- **同じ依頼を別仕入先で再度「発注候補に追加」**: 上書きする（`staged_supplier_id` を新しい仕入先に更新）でよい。
- **依頼が別タブで発注に取り込まれた**: 当該依頼は CONVERTED になり、ステージング一覧からは除外する（PENDING かつ staged のみ表示するため）。
- **部署絞り**: ② に表示する「発注候補に追加済み」は、発注管理ページの部署絞りと合わせる（依頼の `requested_department` が一致するものだけ表示するなど）。

---

## 9. 実装順序の提案

1. DB: `UnmanagedOrderRequest` に `staged_supplier_id`, `staged_at` を追加。
2. サービス: `stage_unmanaged_requests`、発注候補統合リスト取得、`create_order` の `unmanaged_request_id` 対応。
3. API: `POST /api/unmanaged-order-requests/stage`、発注管理ページのコンテキスト拡張、`POST /purchase-orders` の仕様拡張。
4. 画面: ①「発注候補に追加」ボタンとイベント、② テーブルを order_candidates 対応にし、選択・送信ロジックを管理外行に対応させる。
5. 文言・説明の更新と動作確認。

以上で「仕入先のみ選択して確定 → ② のリストに反映 → 管理も管理外も同時に発注」する理想の流れの仕様とする。
