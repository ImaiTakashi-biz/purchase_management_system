# 品番が既存・仕入先が新規の場合の保存処理と設定

## 結論（要点）

- **品番**は仕入品マスタ（`items`）に既に存在する前提で、購入実績では **編集不可（表示のみ）** です。
- **仕入先**は「新規」を購入実績の保存時に作成する機能は **ありません**。  
  購入実績の保存では **仕入先マスタ（`suppliers`）に既に存在するIDのみ** を指定できます。
- 「品番は既にDBにあり、仕入先が新規」の場合は、**先にデータ管理で仕入先を新規登録し、その後に購入実績でその仕入先を選択して保存** する運用になります。

---

## 1. テーブル・マスタの関係

| マスタ／データ | テーブル | 説明 |
|----------------|----------|------|
| 仕入品マスタ | `items` | 品番（`item_code`）は一意。購入実績は `item_id` で紐づく。 |
| 仕入先マスタ | `suppliers` | 仕入先名（`name`）は一意。新規追加は「データ管理」の仕入先画面のみ。 |
| 品番×仕入先 | `item_suppliers` | 同じ品番を複数仕入先で扱う場合の単価。`(item_id, supplier_id)` は一意。 |
| 購入実績 | `purchase_results` | `supplier_id`・`item_id` は必須。いずれも既存のIDのみ。 |

---

## 2. 購入実績一覧の「保存」で行っていること

### API: `PATCH /api/purchase-results/{result_id}`（`app/main.py`）

- **受け付ける内容**
  - `delivery_date`, `supplier_id`, `delivery_note_number`, `quantity`, `unit_price`, `amount`, `purchase_month`, `account_name`, `expense_item_name`, `purchaser_name`, `note`
- **品番（item_id）**
  - リクエストでは **受け取っていない**。購入実績の「品番・品名」は表示専用で、**更新しない**。
- **仕入先（supplier_id）**
  - 指定された場合、**必ず「既存の仕入先」かどうかを検証**しています。

```python
# app/main.py 2343–2347 行付近
if payload.supplier_id is not None:
    sup = db.get(Supplier, payload.supplier_id)
    if not sup:
        raise HTTPException(status_code=400, detail="指定した仕入先が存在しません。")
    row.supplier_id = payload.supplier_id
```

- したがって **「仕入先が新規」のまま保存する」ことはできません**。  
  存在しない `supplier_id` を送ると 400 で「指定した仕入先が存在しません。」となります。

### 画面（`purchase_results.html`）

- **購入先**は `<select>` で、**サーバーから渡された `suppliers`（既存仕入先一覧）だけ** を表示しています。
- 新規仕入先をこの画面から追加する入力欄やボタンは **ありません**。

---

## 3. 仕入先の「新規登録」ができる場所

仕入先を新規作成できるのは **データ管理の「仕入先」画面のみ** です。

- **API**: `POST /api/suppliers`（`app/main.py` 2763 行付近）
- **処理内容**
  - 仕入先名（必須）・連絡先などを受け取り、`Supplier` を 1 件追加。
  - 同名の仕入先が既にいれば 400「同名の仕入先が既に存在します。」。
- 購入実績一覧や発注フローから、この API を呼んで「その場で新規仕入先を作る」処理は **ありません**。

---

## 4. 品番×仕入先（item_suppliers）が作られるタイミング

「品番は既にマスタにあり、その品番をある仕入先で扱う」という組み合わせは、次の 2 つのタイミングで **既存の仕入先** に対してだけ作られます。**仕入先が新規のときに自動で作る処理はありません**。

### 4.1 発注メール送信時（`purchase_order_service.py` 737–763 行付近）

- 発注に紐づく **仕入先**（`order.supplier_id`）は **既に発注時に選択された既存の仕入先**。
- メール送信完了後、その発注の各明細について  
  「**品番（item_id）× その発注の仕入先（sid）**」の組み合わせが `item_suppliers` に **無ければ** 1 件追加します。
- 単価は未入力（`unit_price=None`）で追加。既に同じ品番×仕入先の行があれば何もしません。

```python
# 当該発注の品番×仕入先が未登録なら item_suppliers に新規登録（単価は未入力）
existing = self.db.scalar(
    select(ItemSupplier).filter(
        ItemSupplier.item_id == line.item_id,
        ItemSupplier.supplier_id == sid,
    )
)
if existing:
    continue
self.db.add(ItemSupplier(item_id=line.item_id, supplier_id=sid, unit_price=None))
```

- ここで使う `sid` は発注に紐づいた **既存の仕入先ID** のみです。

### 4.2 入庫計上時（`purchase_order_service.py` 930–975 行付近）

- 入庫計上時も、発注の **仕入先**（`order.supplier_id`）は **既存**。
- 明細ごとに単価を変更（オーバーライド）した場合:
  - その品番×仕入先の `ItemSupplier` が **あれば** 単価を更新。
  - **なければ** 新規で `ItemSupplier` を 1 件追加し、その単価で `UnitPriceHistory` と `PurchaseResult` を登録します。

```python
if is_row:
    is_row.unit_price = override_price
else:
    self.db.add(
        ItemSupplier(
            item_id=line.item_id,
            supplier_id=order.supplier_id,
            unit_price=override_price,
        )
    )
```

- いずれも **仕入先は既に存在している** 前提です。

---

## 5. 「品番が既にあり、仕入先が新規」の場合の運用

現状の仕様では、次のようになります。

| 項目 | 内容 |
|------|------|
| 品番 | 仕入品マスタに既にある（購入実績では変更不可）。 |
| 仕入先 | 画面上は「既存の仕入先」のみ選択可能。新規仕入先を購入実績の保存時に作成する機能はない。 |
| 保存方法 | **先に「データ管理」→「仕入先」で新規仕入先を登録**し、その後で購入実績一覧の該当行の「購入先」でその仕入先を選んで「保存」する。 |
| item_suppliers | 購入実績の保存では **更新しない**。品番×仕入先の組み合わせは、発注メール送信時や入庫計上時のみ追加・更新される（いずれも既存仕入先のみ）。 |

---

## 6. 関連するモデル・制約（参考）

- **PurchaseResult**（`app/models/tables.py` 252 行付近）
  - `supplier_id`: `ForeignKey("suppliers.id")`, **nullable=False**
  - `item_id`: `ForeignKey("items.id")`, **nullable=False**
- **ItemSupplier**（同 112 行付近）
  - `(item_id, supplier_id)` にユニーク制約。同じ品番を同じ仕入先で複数行登録は不可。
- **Supplier**（同 35 行付近）
  - `name` はユニーク。新規作成は `POST /api/suppliers` のみ。

以上が、品番が既にデータベース（仕入品マスタ）にあり、仕入先が新規だった場合の保存処理・保存方法・設定の説明です。
