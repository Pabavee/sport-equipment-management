# Backend Architecture Analysis — Sport Equipment Management

**Stack:** Streamlit (`app.py`, single file) · PostgreSQL 18.3 (`sport_equipment_mgmt`) · psycopg2
**Scope:** Data-access/security layer, business-logic execution flows, and system introspection/error interception.

> All PL/pgSQL bodies quoted below were pulled live from `pg_proc` / `pg_trigger`; the Python excerpts are from `app.py` at the noted line numbers.

---

## 1. Data Access & Security Layer

### 1.1 Connection management (`get_conn`, app.py:160)

Every database operation opens its **own connection** and closes it in a `finally` block:

```python
def get_conn():
    return psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME,
                            user=DB_USER, password=resolve_password())
```

Design consequences (deliberate for a viva/demo app):
- **No connection pooling** → each operation is an isolated unit; a failure in one op can never poison another session's transaction.
- `try/except/finally` guarantees `cur.close()` / `conn.close()` even on exceptions → no leaked sockets or idle transactions.
- Passwords resolve at runtime: `st.secrets` → `PGPASSWORD`/`DB_PASSWORD` env → code constant (`resolve_password`, app.py:127), so no credential is hard-coded into the SQL layer.

### 1.2 Parameterized queries & injection defense

Two techniques are used, depending on whether the SQL is **static** or **dynamic**:

| Case | Mechanism | Example |
|---|---|---|
| Static SQL | `cur.execute(sql, params or ())` with `%s` placeholders | `SELECT ... FROM staff s ... WHERE s.email = %s` |
| Dynamic identifiers (table names) | `psycopg2.sql.SQL().format(Identifier(...))` | `SELECT * FROM {} LIMIT %s` (Schema Inspector) |
| Dynamic DML (CRUD console) | `psycopg2.sql.SQL().format(SQL(...), Identifier(col), Literal(...))` | `INSERT INTO {} ({}) VALUES ({})` — table and column names are `Identifier`-quoted, every value is a `%s` placeholder (`build_insert` / `build_update` / `build_delete`) |

The login path is fully parameterized — no string concatenation is ever near user input:

```sql
SELECT s.staff_id, CONCAT(s.first_name,' ',s.last_name) AS full_name,
       r.role_title, s.password_hash
FROM staff s JOIN staff_roles r ON r.role_id = s.role_id
WHERE s.email = %s
```

For the Schema Inspector, the user-chosen table name is **whitelisted by a dropdown** and additionally wrapped with `psycopg2.sql.Identifier()`, so an attacker-supplied table name is quoted as an identifier literal, never executed as SQL.

### 1.3 PBKDF2-HMAC-SHA256 password hashing

No third-party crypto library — stdlib `hashlib` / `hmac` / `secrets`:

```text
hash_password(pw)  →  "pbkdf2_sha256$200000$<16-byte-random-salt-hex>$<256-bit-digest-hex>"

verify_password(pw, stored):
    split on "$", re-derive digest with stored salt + 200_000 iterations,
    compare with hmac.compare_digest()      ← constant-time, timing-attack safe
```

Security properties worth stating in a viva:
- **Per-user random salt** (16 bytes) → identical passwords produce different hashes (defeats rainbow tables).
- **200,000 iterations** of SHA-256 → CPU-hard; slows brute-force.
- **`hmac.compare_digest`** compares both digests in constant time.
- **No plaintext is ever stored**; `staff.password_hash` is the only credential store.
- **Self-healing schema** (`ensure_auth_schema`, app.py:878): on every launch it checks `information_schema` for the column, runs `ALTER TABLE staff ADD COLUMN IF NOT EXISTS password_hash TEXT` if missing, then upserts 3 demo users with `ON CONFLICT (staff_id) DO UPDATE`. Auth survives `pg_restore -c` resets.

### 1.4 Persistent vs. non-destructive transactions

Both helpers execute an ordered list of `(sql, params)` in **one transaction**; the difference is the final action.

**`execute_sequence(...)` → COMMIT** (app.py:209) — *persistent business ops* (checkout, return, restock, acquire):

```text
BEGIN ──► stmt1 ──► stmt2 ──► ... ──► COMMIT
                                  (on psycopg2.Error → ROLLBACK, raise DBServerError)
```

- Returns `(rowcounts, notices)`. Notices are sliced per statement via `conn.notices[before:]`, plus any raised at COMMIT — this is what makes trigger `RAISE NOTICE` messages visible in the UI.
- Used by `call_procedure()` which builds `CALL proc_name(%s, ...)`.

**`execute_test(...)` → ROLLBACK** (app.py:247) — *non-destructive demos* (Triggers tab, Exception Suite):

```text
BEGIN ──► stmt1 ──► ... ──► [optional trailing SELECT captured] ──► ROLLBACK   (always)
                                      │ on error → capture SQLSTATE + message, still ROLLBACK
```

- Returns a dict: `{"ok", "results", "notices", "error", "pgcode", "rowcounts"}`.
- If the last statement returns rows, they're materialized into a DataFrame **before** the rollback — so the UI can prove an in-transaction state change (e.g. `condition_id` flipped to 2) that never persists.

| | `execute_sequence` | `execute_test` |
|---|---|---|
| End | `COMMIT` | `ROLLBACK` |
| Used for | Checkout / Return / Restock / Acquire | Trigger tests, exception presets |
| On error | ROLLBACK + `raise DBServerError` | Capture into dict, return (no raise) |
| Re-runnable | Changes data | Yes, infinitely — DB unchanged |

### 1.5 Metadata-driven CRUD console & RBAC gating

A single, schema-driven engine serves all 14 tables from **Page 7 — Data Management**. No per-table SQL exists.

```text
load_crud_metadata(table)            -- information_schema.columns + pg_constraint + pg_attrdef/pg_sequence
    →  column list, types, PKs, FKs, sequence-backed PKs
load_fk_options(table, column)       -- referenced table + key columns → "label (id)" options for the selectbox
build_insert / build_update / build_delete
    →  psycopg2.sql.SQL().format(SQL("..."), Identifier(table), Identifier(col)) + %s value placeholders
```

Key properties (all verified by tests):
- **Security:** table/column identifiers are quoted via `psycopg2.sql.Identifier`; every value goes through `%s` placeholders — same injection posture as the auth path.
- **Schema awareness:** sequence-backed PKs (e.g. `brands.brand_id`) are omitted on insert so the sequence supplies them; natural PKs (e.g. `suppliers.supplier_name`) are required; FK columns render as dropdowns of the referenced table.
- **`staff.password_hash` hardening:** the raw column is never displayed or written back as text. Insert/update render a password field that runs the same `hash_password()` PBKDF2 pipeline; a dedicated **Reset password** form re-hashes with a fresh salt.
- **CASCADE safety:** `pg_constraint` is queried for each FK's `confdeltype` to precompute `CASCADE_CHILDREN`. Only `rentals → rental_line_items` and `inventory_items → maintenance_logs` cascade (verified); all other FKs are `RESTRICT`, so a violating delete is rejected by the engine as `23503`.
- **RBAC:** `CRUD_ROLES = ("System Administrator", "Inventory Clerk")`. `build_nav_pages()` appends the page only for those roles (Front Desk → 6 pages, Admin/Clerk → 7), and `page_crud_console()` repeats the guard with `st.warning(...) + st.stop()` — defense in depth at the navigation and page level.

**Sequence-fix finding (why CRUD inserts are collision-free):** the dump's `staff` sequence had never been advanced past the seeded demo users, so the first CRUD-created staff row would collide with `staff_id = 2`. All 12 SERIAL sequences are now `setval`-ed to `MAX(id)` (`staff → 3`, `maintenance_logs → 10000`, …), keeping auto-generated keys ahead of seeded data.

---

## 2. Business Logic Execution Flow

### 2.1 Checkout flow — `proc_member_rental_checkout`

**Trigger wiring (verified from `pg_get_triggerdef`):** both triggers on `rental_line_items` are **BEFORE INSERT** (despite the name `trg_after_rent_item_checkout`):

```text
CALL proc_member_rental_checkout(:member_id, :staff_id, :barcode)          [UI: logged-in staff_id]
│
├─ 1. PRE-CHECK  IF NOT EXISTS barcode → RAISE EXCEPTION 'Checkout Denied ...' (P0001)
├─ 2. INSERT rentals (member_id, staff_id, NOW(), 0.00) RETURNING rental_id → v_rental_id
├─ 3. INSERT rental_line_items (v_rental_id, barcode, CURRENT_DATE + INTERVAL '7 days')
│        │
│        ├─► TRIGGER trg_after_rent_item_checkout  (BEFORE INSERT, fires 1st: "a" < "c")
│        │        fn_trigger_process_checkout:
│        │          • SELECT catalog_id, condition_id FROM inventory_items WHERE barcode=NEW.barcode
│        │          • IF condition_id != 1 → RAISE EXCEPTION 'Item ... not available'  (2nd line of defense)
│        │          • UPDATE inventory_items SET condition_id = 2            (Available → Rented)
│        │                └─► fires TRIGGER trg_after_inventory_condition_change
│        │                        fn_trigger_sync_catalog_availability:
│        │                          • recomputes total_owned = COUNT(inventory_items)
│        │                            and total_available = COUNT(condition_id = 1) for that catalog
│        │                          (the AFTER triggers on inventory_items are the single owner
│        │                           of the catalog balances — no hand-written UPDATE exists anymore)
│        │
│        └──► TRIGGER trg_check_insert_return_date  (BEFORE INSERT, fires 2nd)
│                 fn_validate_expected_return_date:
│                   IF NEW.expected_return_date < CURRENT_DATE → RAISE EXCEPTION (P0001)
│
└─ RAISE NOTICE 'Generated Rental ID: <id>'
```

Post-commit, the app:
1. Parses the ID from the NOTICE via `extract_rental_id` (regex `Rental ID:\s*(\d+)`).
2. Re-queries the DB and renders the **Before/After switcher** (`1 — Available` → `2 — Rented`) as *live proof* the trigger fired and persisted.
3. The dashboard grid shows the barcode flipped to Rented on refresh.

**Design note:** the *availability* guarantee is enforced twice — once in the UI (barcode dropdown filters `condition_id = 1`) and again in the trigger (`fn_trigger_process_checkout` aborts if the item isn't available). Defense in depth.

### 2.2 Return & late-fine flow

Two components cooperate: a **scalar function** (compute) and a **BEFORE UPDATE trigger** (report), driven by `proc_item_return`.

```text
UI:  fine = SELECT fn_calculate_late_fine(:line_id)
│         └─ live running fine for OPEN lines (days since due × $2.50);  ← key semantics
│            final fine only for RETURNED lines (return date > due date)
│
execute_sequence([
   CALL proc_item_return(:line_id, :fine),
   UPDATE inventory_items SET condition_id = 1 WHERE barcode = (the line's barcode)
])
│
└─ CALL proc_item_return:
     ├─ UPDATE rental_line_items SET actual_return_timestamp = NOW()
     │       │
     │       └─► TRIGGER trg_auto_calculate_fine (BEFORE UPDATE)
     │               fn_trigger_calculate_fine:
     │                 • (only when a return timestamp is newly set)
     │                 • v_calculated_fine := fn_calculate_late_fine(NEW.line_id)   ← NOW non-zero
     │                 • RAISE NOTICE 'Item returned LATE ... $X'  OR  'returned on time'
     │
     ├─ IF p_late_fine > 0 → UPDATE rentals SET total_billed += p_late_fine
     ├─ RAISE NOTICE 'Item Return Processed Successfully for Line ID ...'
     └─ EXCEPTION WHEN OTHERS → RAISE EXCEPTION 'Return process failed: %', SQLERRM
```

**`fn_calculate_late_fine` semantics** (the subtlety):

```sql
IF v_return_date IS NULL THEN
    -- item still out: live running fine against today
    IF CURRENT_DATE > v_expected_date THEN
        v_days_late := CURRENT_DATE - v_expected_date;
        v_fine := v_days_late * 2.50;    -- rate hard-coded as constant
    END IF;
ELSIF v_return_date::DATE > v_expected_date THEN
    v_days_late := v_return_date::DATE - v_expected_date;
    v_fine := v_days_late * 2.50;
END IF;
RETURN v_fine;
```

For an **open** (unreturned) line the function returns a **live running fine** (overdue days × $2.50 against `CURRENT_DATE`) — that's the non-zero amount the Return page metric shows before the return is processed. When the return is processed, `proc_item_return` adds that amount to `rentals.total_billed` and the trigger's NOTICE echoes the same `v_calculated_fine` (`fn_trigger_calculate_fine` → `trg_auto_calculate_fine`). The app then resets the item to `Available (1)` so it can be rented again; that UPDATE fires `trg_after_inventory_condition_change`, which restores the catalog's `total_available`. Separation of concerns: the **trigger owns item state**, the **procedure owns billing**, and the **availability triggers own the catalog balances**.

### 2.3 Restock & procedure overloading

**`proc_supplier_restock`** — multi-table, single transaction:

```sql
INSERT INTO purchase_orders (supplier_id, total_cost, order_status)
VALUES (p_supplier_id, p_cost, 'Completed') RETURNING order_id INTO v_order_id;

INSERT INTO inventory_items (barcode, catalog_id, condition_id, serial_number)
VALUES (p_barcode, p_catalog_id, p_condition_id, p_serial_number);
-- EXCEPTION WHEN OTHERS → RAISE EXCEPTION 'Restock process failed: %'
```

**`proc_acquire_new_asset`** differs semantically: it represents outright purchase, so it adds a unit to inventory *and* the catalog counts both move. The procedure body no longer touches the catalog row directly — the INSERT fires the trigger:

```sql
INSERT INTO inventory_items (barcode, catalog_id, condition_id, serial_number)
VALUES (p_barcode, p_catalog_id, p_condition_id, p_serial_number);
-- fires TRIGGER trg_after_inventory_insert
--   → fn_trigger_sync_catalog_availability recomputes
--     total_owned = COUNT(*), total_available = COUNT(condition_id = 1)
```

So `total_available` correctly bumps by one only when the new unit is `condition_id = 1` — the trigger derives it from the data rather than the caller promising it. This de-duplication removed the hand-written `UPDATE equipment_catalog` that previously lived in both this procedure and `fn_trigger_process_checkout`.

**Overload resolution (`proc_register_supplier_restock`, two signatures):**

PostgreSQL resolves a `CALL` by **argument list** (name + arity + types). The UI exposes a `segmented_control` built from `pg_get_function_arguments`, then routes the form by inspecting the signature string (`"p_catalog_id" in sig`):

| Overload | Signature | Behavior |
|---|---|---|
| A (working) | `(supplier_id, catalog_id, condition_id, barcode, serial_number, unit_cost)` | Inserts `purchase_orders` + `inventory_items` correctly. |
| B (legacy, broken) | `(supplier_id, equipment_id, quantity_received, unit_cost)` | Body references **non-existent columns** `purchase_orders.equipment_id/quantity/unit_cost` and `inventory_items.quantity/equipment_id` → runtime error `SQLSTATE 42703 (undefined_column)`; wrapped by `EXCEPTION WHEN OTHERS` into a clean message. |

Overload B is kept deliberately so the viva can show PostgreSQL rejecting a **schema-mismatched** definition at runtime, transactionally. It also contains a genuine anti-pattern worth flagging: **explicit `COMMIT`/`ROLLBACK` inside a procedure body** — transaction control belongs at the caller layer, not inside the routine (a nested `COMMIT` aborts any outer transaction and is non-portable). The working overload is the modern pattern; B is the legacy one that violates it.

### 2.4 Availability-sync trigger architecture (single owner of catalog balances)

**The defect this fixes:** `equipment_catalog.total_owned` / `total_available` were previously maintained by hand-written `UPDATE` statements inside `fn_trigger_process_checkout` and `proc_acquire_new_asset`. That meant any inventory change made through *other* paths — direct CRUD inserts/updates/deletes (exactly what the Data-Management console does) — left the balances stale.

**The design:** one trigger function, three AFTER triggers on `inventory_items`:

```text
fn_trigger_sync_catalog_availability(catalog_id, condition_id)
   total_owned     := COUNT(*)            FROM inventory_items WHERE catalog_id = X
   total_available := COUNT(*)            FROM inventory_items WHERE catalog_id = X
                              AND condition_id = 1
   → UPDATE equipment_catalog SET total_owned, total_available WHERE catalog_id = X

trg_after_inventory_insert           AFTER INSERT   ON inventory_items
trg_after_inventory_condition_change AFTER UPDATE OF condition_id ON inventory_items
trg_after_inventory_delete           AFTER DELETE   ON inventory_items
```

Properties worth stating in a viva:
- **One source of truth.** The rule lives in a single `pg_proc` body shared by three `pg_trigger` definitions; a balance can only change by going through this function. The old manual `total_available = total_available - 1` lines were deleted from both the checkout trigger and the acquire procedure (verified by `pg_get_functiondef`).
- **Recompute, not increment.** For a single-row event an aggregate `COUNT` gives the same answer as an increment, but it is *self-healing*: even if a balance were ever wrong, the next event on that catalog repairs it. A one-time reconciliation pass was run so current balances already equal reality (0 drift across all 21 catalogs).
- **Correct semantics on every path.** Insert adds +1 owned (+1 available iff Available); a condition flip (Available ↔ Rented) moves the item in/out of `total_available`; delete subtracts. Because the aggregate recomputes from the row's own `catalog_id`, the three triggers are correct for any condition value — including repairs/decommissions.
- **Nested firing is safe.** When the checkout trigger `UPDATE`s `condition_id`, `trg_after_inventory_condition_change` re-enters for that statement; when restock/acquire `INSERT` an item, `trg_after_inventory_insert` fires. Each only touches its own catalog row, so there is no double-counting.

---

## 3. System Integrity & Introspection

### 3.1 Catalog-view introspection

The inspector pages read straight from PostgreSQL's system catalogs (never guessed):

| UI feature | Catalog source | Key columns / functions |
|---|---|---|
| Table list (Schema Inspector) | `pg_tables` | `schemaname='public'` |
| Column metadata | `information_schema.columns` | `data_type`, `character_maximum_length`, `is_nullable` |
| Constraints | `pg_constraint` + `pg_class`/`pg_namespace` | `contype` (p/f/u/c/x), `pg_get_constraintdef(oid)` |
| Procedures/Functions inventory | `pg_proc` | `prokind` ('p'/'f'), `pg_get_function_arguments()`, `pg_get_function_result()` |
| Full SQL source | `pg_proc` | `pg_get_functiondef(oid)` |
| Triggers | `pg_trigger` | `NOT tgisinternal`, `pg_get_triggerdef(oid)` |
| FK on-delete rules (CRUD console) | `pg_constraint` | `confdeltype` (`'c'` = CASCADE, `'r'` = RESTRICT) → feeds `CASCADE_CHILDREN` |
| Sequence sanity (CRUD console) | `pg_sequence` + `pg_attrdef` | detect sequence-backed PKs; `setval`-ed to `MAX(id)` |
| Indexes (custom vs all) | `pg_indexes` | `indexdef`; custom = exclude `%_pkey`/`%_key` |

The custom-index filter (`LIKE '%%_pkey'`, escaped for psycopg2) separates **hand-written indexes** (`idx_inv_cat_condition`, `idx_items_return_pending` partial, `idx_maint_barcode_date`) from constraint-backed auto-indexes.

The current inventory is **5 functions / 6 triggers** (`fn_calculate_late_fine` + 4 trigger functions; 3 triggers on `rental_line_items`, 3 on `inventory_items`). The Functions tab shows `fn_trigger_sync_catalog_availability` exactly once in `pg_proc` even though three `pg_trigger` definitions reference it — a useful live proof of the one-body/many-triggers pattern.

**Realism pass (seed data & UI surfacing):** the seeded catalog was renamed from generic `"Nike Field Sports Pro Asset Line #N"` rows to 21 realistic products (e.g. `Aero-Blade Carbon Tennis Racket`, `Speedo Racing Goggles`, `Nike Premier League Football`), with `brand_id` re-mapped so products match their brands and `rental_rate`/`replacement_value` set per product type. The database now seeds **7 members across all 3 tiers** and **3 suppliers** (each with contact details) plus 3 historical purchase orders. These are pure data changes — the object inventory (14 tables / 6 procs / 5 functions / 6 triggers / 3 indexes) is untouched and catalog drift remains 0.

The app surfaces the new richness without extra pages: the Dashboard gained an **Under Repair** KPI, a **Fleet Status by Condition** chart, a **Quick Actions** row of `st.page_link`s to the operational pages, and an **operating guide** expander; the Checkout member dropdown now joins `membership_types` so each option reads `#1 — Naveen Perera · Standard Student (max 3 days)` and a post-checkout tier card shows discount/max-days/contact from the tier row. The Restock supplier dropdown appends `contact_phone`. All of it is read-only query enrichment (`load_members`/`load_suppliers` joins) — the transactions still run entirely through the stored procedures.

### 3.2 Error interception — how SQLSTATE reaches the UI

```text
PostgreSQL raises (psycopg2.Error with .pgcode / .diag)
        │
        ├─ sqlstate_code(exc) ──► extracts SQLSTATE (e.g. P0001, 23505)
        │
        ├─ friendly_error(exc) ──► strips diag/CONTEXT noise → clean message
        │
        └─ wrapped in DBServerError(message, pgcode)   (app.py:112)
                │
                ├─ Live pages (checkout/return/restock) → db_error_box() → st.error("... SQLSTATE <code>")
                └─ execute_test() path → dict {"ok":False,"pgcode","error"} → render_test_result()
```

SQLSTATEs the Exception Suite deliberately triggers (each rolled back):

| Class | SQLSTATE | Meaning | Triggered by |
|---|---|---|---|
| PL/pgSQL | `P0001` | `raise_exception` | unknown barcode, past date, wrapped FK |
| Unique | `23505` | duplicate barcode / serial | `proc_acquire_new_asset` on PK/UNIQUE |
| Foreign key | `23503` | referenced row missing | `CALL proc_member_rental_checkout` with bad member |
| Check | `23514` | `total_billed = -5` | `rentals_total_billed_check` |

**The notable subtlety:** the UI expects **`P0001`** for the "invalid member" preset, not `23503`. Reason — `proc_member_rental_checkout` ends with:

```sql
EXCEPTION WHEN OTHERS THEN
    RAISE EXCEPTION 'Transaction rolled back. Engine details: %', SQLERRM;
```

The inner FK violation (`23503`) is caught by the procedure's handler and **re-raised as `P0001`**. This is textbook PL/pgSQL error propagation: the handler preserves the original message (`SQLERRM`) while normalizing the SQLSTATE.

**Robustness guarantee:** because every demo goes through `execute_test`, the UI *never crashes* — an exception is captured, displayed with its SQLSTATE, and the transaction is rolled back, so the exception suite is safe to click repeatedly.

---

## 4. Senior-Engineer Observations (audit findings)

1. **Least privilege:** the app connects as superuser `postgres`. Production would use a dedicated role with only `SELECT/INSERT/UPDATE` on business tables.
2. **Naming inconsistency:** `trg_after_rent_item_checkout` is actually a **BEFORE INSERT** trigger — misleading name, harmless but confusing.
3. **Overload B anti-pattern:** transaction control (`COMMIT`/`ROLLBACK`) inside a procedure body + reliance on non-existent columns — correctly rejected by the engine.
4. **Per-operation connections:** no pooling means higher connection churn; acceptable because the app is single-user/demo and it gives clean transaction isolation.
5. **Fine policy in the function body** (`$2.50` constant) rather than a config table — fine for coursework; a `policy`/`config` table would be the normalized design.
6. **Auth hardening is production-grade:** parameterized auth query, PBKDF2 + per-user salt, constant-time compare, uniform "Invalid credentials", and no plaintext storage.
7. **Availability-balance ownership was fragmented and is now single-owner:** the old catalog totals were hand-updated in two places (`fn_trigger_process_checkout`, `proc_acquire_new_asset`), so non-checkout inventory changes drifted them. The fix centralizes them in `fn_trigger_sync_catalog_availability` behind three AFTER triggers on `inventory_items` — verified with a recomputed-vs-stored reconciliation (0 drift) and round-trip tests proving rollback leaks nothing.
8. **Sequence drift was a real latent bug:** `staff`'s sequence had never advanced past the seeded demo rows, so the first CRUD insert would have collided with `staff_id = 2`. All 12 SERIAL sequences are `setval`-ed to `MAX(id)` and the CRUD datalayer test asserts insert-without-collision.
9. **CRUD console keeps the injection bar high:** dynamic table/column identifiers are quoted with `psycopg2.sql.Identifier` while all values stay `%s` parameters — identical discipline to the login path, but on a code path that touches *every* table.
10. **RBAC is applied at two layers:** `build_nav_pages()` hides the CRUD page from non-admin roles and `page_crud_console()` independently re-checks with `st.warning + st.stop` — navigation and page both enforce the policy.
11. **Deletes are FK-aware:** the console introspects `confdeltype` and warns when a delete cascades (only `rentals→rental_line_items` and `inventory_items→maintenance_logs`); every other FK is `RESTRICT` and the engine rejects violations with `23503`.
