# 発注メール送信後にデータベースに保存されない場合の原因と確認

## 保存される内容（送信成功時）

メール送信が **成功したあと**、次の処理が行われ、最後に `commit()` で確定します。

1. 発注のステータスを **WAITING（入庫待ち）** に更新
2. **email_send_logs** に送信成功ログを 1 件追加
3. 当該発注の「品番×仕入先」が **item_suppliers** に無ければ 1 件ずつ追加（単価は未入力）
4. **commit()** で上記を一括保存

いずれかで例外が出ると、その時点で処理が止まり、**commit まで届かないため何も保存されません**（セッションはクローズ時にロールバックされます）。

---

## 想定される原因

### 1. 送信前に例外が出ている（メールは送っていない／送信失敗）

「送信ボタンを押したが何も保存されていない」場合、**送信処理の前**で例外になっている可能性があります。

- **注文書 PDF 未作成・見つからない**  
  `generate_document` や `get_email_preview` 内で「添付PDFが見つかりません」などで例外
- **仕入先メールアドレス未登録**  
  `to_address` が空で「仕入先メールアドレス未登録のため送信できません。」で例外（この場合は送信失敗ログは `_save_failed_log` で commit される）
- **SMTP 送信失敗**  
  メール送信時に例外 → 送信失敗ログは commit されるが、ステータス更新・item_suppliers は行われない
- **keyring 未設定**  
  「keyringにSMTPパスワードが見つかりません」で例外

この場合、画面上またはサーバーログに上記のエラーメッセージが出ているはずです。

### 2. 送信は成功したが、その直後の DB 保存で例外

メールは送れたが、その直後の「ステータス更新・EmailSendLog・item_suppliers → commit」のどこかで例外になっている場合です。

- **EmailSendLog の NOT NULL 違反**  
  `subject` / `body` が DB 上 NOT NULL のため、ここに None が入ると commit 時にエラーになる。  
  → コード側で `subject` / `body` に空文字をフォールバックする修正を入れ済み。
- **その他の制約違反**  
  `flush()` を commit の直前に実行するようにしているため、制約違反は commit の直前で検出され、その時点の例外メッセージで原因が分かります。

サーバーログやブラウザの「メール送信が完了しました」の前後で 400 などが出ていないか確認してください。

### 3. item_suppliers だけ保存されていない（ステータス・送信ログは保存されている）

「発注ステータスは WAITING になるが、品番×仕入先が item_suppliers に増えない」場合は以下を確認してください。

- **発注に仕入先が紐づいているか**  
  `purchase_orders.supplier_id` が NULL の場合は、item_suppliers の登録処理自体をスキップします（通常は NOT NULL のためあまり起きません）。
- **明細に「品番」が紐づいているか**  
  `purchase_order_lines.item_id` が NULL の行（品番なしの自由入力行など）は、item_suppliers の対象外です。  
  **すべての明細が item_id NULL だと、1 件も item_suppliers に追加されません。**

確認例（SQLite の場合）:

```sql
-- 該当発注の明細の item_id
SELECT id, purchase_order_id, item_id, quantity FROM purchase_order_lines WHERE purchase_order_id = ?;
```

### 4. 別 DB や別プロセスを見ている

- 実行中のアプリが参照している DB ファイル（例: `data/purchase.db`）と、確認している DB が同じか
- 複数プロセスでアプリを起動していないか（別プロセスが別 DB を使っている可能性）

---

## 確認手順

1. **サーバーログの確認**  
   メール送信リクエストの前後に例外やスタックトレースが出ていないか確認する。
2. **送信成功レスポンス**  
   ブラウザで「メール送信が完了しました」と表示されているか。表示されていれば、少なくとも sendmail までは成功し、その後の DB 保存で例外が出ている可能性が高い。
3. **DB の直接確認**
   - `email_send_logs` に送信成功（`success=1`）の行が増えているか
   - `purchase_orders` の該当行の `status` が `WAITING` になっているか
   - `purchase_order_lines` の該当発注の明細に `item_id` が入っている行があるか
   - `item_suppliers` に期待する (item_id, supplier_id) の組み合わせが存在するか

---

## コード上の修正内容（参考）

- **送信成功後の保存**  
  `EmailSendLog` の `subject` / `body` に None が入らないよう、`preview.get("subject")` / `preview.get("body")` で空文字にフォールバック。
- **commit 前の flush**  
  `commit()` の直前に `flush()` を実行し、制約違反などを commit 前に検出できるようにした。

これにより、送信成功後に DB 保存で失敗する場合は、その時点の例外メッセージで原因を特定しやすくなっています。
