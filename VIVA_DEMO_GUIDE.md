# Viva Demonstration Guide & Script

**Project:** Sport Equipment Management System — Streamlit + PostgreSQL 18.3
**App:** `app.py` (single file) | **Database:** `sport_equipment_mgmt` (14 tables, 6 procs, 5 functions, 6 triggers, 3 indexes) | **Auth:** staff login gate (PBKDF2 hashing) + role-based sessions with a distinct duty set per role (pages + CRUD tables)

> Use this guide live at the exam desk. Every input below is a **real, verified value** from the prepared database. Rental/line IDs shift as demos run — always read the current option from the dropdown rather than assuming a hard-coded ID.

---

## Section 0 — Before You Begin (5-minute setup)

1. **Start PostgreSQL** if it is not running (Windows: *Start → "Services" → PostgreSQL 18.3 → Start*).
2. **Open a terminal in the project folder** (the folder containing `app.py`).
3. **Provide the database password** — it is never stored in the repo. Set it for the session before launching:
   ```powershell
   $env:PGPASSWORD = "your-postgres-password"
   ```
   `resolve_password()` checks `.streamlit/secrets.toml` first, then the `PGPASSWORD`/`DB_PASSWORD` environment variables, then the blank `DB_PASSWORD` constant.
4. **Reset to pristine demo data** (only if you've been practicing):
   ```
   pg_restore -c --no-owner --no-privileges -U postgres -d sport_equipment_mgmt sport_equipment_mgmt
   ```
5. **Launch the app:**
   ```
   streamlit run app.py
   ```
6. **Confirm green health dot** in the sidebar: `PostgreSQL connected`. If you see `DB unreachable`, fix the password then restart.
7. **Log in.** The app opens on a login gate (all 7 pages hidden). Sign in with a demo account (table below) or use the **Viva Quick Login / Demo Mode** expander for one-click role switching. **Each role has its own unique duty set** — the nav is filtered from `ROLE_PERMISSIONS` in `app.py`:

   | Role | Pages visible | CRUD tables |
   |---|---|---|
   | Front Desk Officer | Dashboard, Member Checkout, Return & Late Fine (3) | none |
   | Inventory Clerk | Dashboard, Restock & Assets, Schema Inspector, Data Management (4) | 8 stock/inventory tables |
   | System Administrator | Dashboard, Schema Inspector, Functions/Triggers, Data Management (4) | all 14 + password reset |

### Live sample-data reference (from the prepared database)

| Thing you need | Real value to use |
|---|---|
| Member — Standard | `#1 — Naveen Perera` (max 3 days) |
| Member — Gold (15% off, max 7 days) | `#2 — Kumari De Silva` |
| Member — Elite Athlete (30% off, max 14 days) | `#3 — Rajitha Gunasekara` |
| Demo login — Front Desk | `amara.s@sports.com` / `counter123` |
| Demo login — Admin | `kasun.f@sports.com` / `admin123` |
| Demo login — Clerk | `dilini.j@sports.com` / `clerk123` |
| Supplier | `#2 — Elite Sports Wholesale` (011-266-7780) |
| Other suppliers | `#1 — Ace Sports Distributors`, `#3 — Ocean Swim & Aquatics Supply` |
| A free barcode | `BAR-100004` (`SN-A87FF679A2F3`) — or pick any "Available" one |
| A rented barcode (to return) | `BAR-100002` = **line #8 — OVERDUE** (due 2026-07-14) → shows a real fine |
| On-time barcode (to return) | `BAR-100021` = **line #19** (due 2026-08-11) → $0.00 fine |
| Catalog item (anchor) | `Aero-Blade Carbon Tennis Racket` (catalog_id 1, $15.00/day) |
| Other catalog examples | `Speedo Racing Goggles` ($4/day), `Nike Premier League Football` ($10/day), `Nike Yoga Mat Pro` ($5/day) — 21 realistic products in total |
| Conditions | 1 = Available, 2 = Rented, 3 = Under Repair, 4 = Decommissioned |
| Late-fine rate | **$2.50 / day** (hard-coded in `fn_calculate_late_fine`) |
| Expected late fine (line #8) | ~21 days late → **≈ $52.50** (number updates live with today's date) |

> **Key concept to say aloud:** `fn_calculate_late_fine` now computes a **live running fine** for an **open** (un-returned) line once its `expected_return_date` passes — overdue days × $2.50, shown on the Return page *before* the return is processed. At the moment of return the same amount is applied to `rentals.total_billed` by `proc_item_return` and echoed in the trigger's NOTICE.

### Suggested 15-minute demo order

1. Log In & role switching (Viva Quick Login) — 1 min
2. Dashboard — 1 min
3. Data Schema Inspector — 2 min
4. Functions / Triggers / Integrity (exceptions + trigger tests) — 3 min
5. Member Checkout — 2 min
6. Return & Late Fine — 2 min
7. Restock & Asset Acquisition (incl. overloads) — 2 min
8. **Data Management CRUD Console** (Admin/Clerk only) — 2 min

---

## Section 1 — Page-by-Page Clickthrough Script

### Page 0 · Log In (Login Gate & Role-Based Session)

**👉 Do**
1. The app opens on a **centered login card** — the sidebar shows only the connection status, and **all 7 pages are hidden**.
2. Either type **Email** + **Password** (e.g. `amara.s@sports.com` / `counter123`), or open **Viva Quick Login / Demo Mode** and click `Staff 1 — Amara Silva (Front Desk Officer)` for a one-click login.
3. Watch the sidebar change: a `Logged in as: **Amara Silva** (Front Desk Officer)` badge and a **Logout** button appear above the navigation. Front Desk sees **3 pages only** (Dashboard, Member Checkout, Return & Late Fine); the sidebar's *How to use this system* expander also states the role's duties.
4. Click **Logout** once — session keys are cleared and the app returns to the login gate.

**👀 See**
- Logged out: no navigation, no "Backend objects demoed" reference — only the login card.
- Logged in: welcome badge + **Logout** at the top of the sidebar, then the navigation.
- Role switching with **Viva Quick Login**: log in as `Kasun Fernando (System Administrator)` → nav is **Dashboard, Schema Inspector, Functions/Triggers, Data Management**. Log in as `Dilini Jayasinghe (Inventory Clerk)` → nav is **Dashboard, Restock & Assets, Schema Inspector, Data Management**, and the CRUD table dropdown lists only the 8 stock/inventory tables. Back to `Amara (Front Desk)` → checkout/return return, restock/CRUD disappear. That is runtime, session-driven RBAC with **defense in depth**: each page also calls `require_page()`.
- Wrong password → red `Invalid credentials` (the app never hints whether the email or password was wrong).

**🗣 Say**
> "Every page sits behind this login gate. Credentials are validated against the `staff` table using a parameterized query — no string concatenation, so SQL injection is impossible. Passwords are stored only as PBKDF2-HMAC-SHA256 hashes with a per-user salt and verified in constant time. Once authenticated, the session carries `user_id`, `user_name` and `user_role`. That role selects a duty set from `ROLE_PERMISSIONS`: the Front Desk Officer gets checkout and returns, the Inventory Clerk gets restocking plus CRUD on only the 8 stock tables, and the System Administrator gets oversight, triggers and all 14 tables including staff/password management. Navigation is filtered from that map, and every page re-checks the permission with `require_page()` — so even a direct URL cannot bypass the role, and the Clerk cannot edit staff rows to escalate their own privileges."

**Backend point to highlight:** switch roles with **Viva Quick Login** (Front Desk Officer → System Administrator → Inventory Clerk) and show the sidebar badge updating — instant proof that role-based sessions are working.

---

### Page 1 · Dashboard & Live Inventory

**👉 Do**
1. Open the page (it is the default landing page).
2. Point to the **four KPI cards** (Total / Available / On Loan / Under Repair).
3. Click the **Quick Actions** links (Check out item / Return & fine / Restock & acquire / Schema & data) — each jumps straight to that workflow.
4. Open **How this system works** — the operating guide summarises the whole club workflow.
5. In **Live Inventory Grid**, find `BAR-100001` and note its **Condition** column shows `Rented` (red highlight) while `BAR-100004` shows `Available` (green); `Under Repair` rows show amber.
6. Scroll to **Catalog Availability Balances** bar chart, then **Fleet Status by Condition**.
7. Click **:material/refresh: Refresh** once.

**👀 See**
- Metrics: `Total Equipment 10,004`, `Available 9,893`, `On Loan (Rented) 11`, `Under Repair 100` (live counts).
- **Quick Actions** row of four clickable page links.
- **Operating guide** expander — the 4-step lifecycle (check out → return & fine → restock & acquire → maintain).
- Styled grid — green rows = Available, red = Rented, amber = Under Repair, with Barcode / Item / Serial Number / Condition columns.
- Bar chart of `owned` vs `available` per catalog item (now with realistic product names).
- **Fleet Status by Condition** bar chart (Available / Rented / Under Repair / Decommissioned).

**🗣 Say**
> "The dashboard reads live from PostgreSQL on every rerun. The four KPIs come from one aggregate query over `inventory_items`, grouped by condition. The Quick Actions row links to the day-to-day pages, and the operating guide explains the workflow to a new clerk. Notice the Condition column — it is not stored by the UI; it is maintained automatically by backend triggers, so the moment a checkout row is inserted the item flips from Available to Rented."

**Backend point to highlight:** `equipment_catalog.total_owned` / `total_available` are **not** updated by app code or checkout procedures anymore — they are maintained entirely by three AFTER triggers on `inventory_items` (`trg_after_inventory_insert` / `_condition_change` / `_delete`, one shared function `fn_trigger_sync_catalog_availability`). Checkout flips an item's condition → the condition-change trigger re-balances the catalog. One source of truth, updated in SQL.

---

### Page 2 · Member Checkout (stored procedure + trigger)

**👉 Do**
1. Go to **Member Checkout**.
2. Open the **Member** dropdown — note each label shows the tier benefits, e.g. `#1 — Naveen Perera · Standard Student (max 3 days)`. Pick him. Then **Item barcode (Available only)** → `BAR-100004` (note: this dropdown only lists condition_id = 1 items). There is **no staff dropdown** — the form shows `Handling staff: <logged-in user> (role) — staff_id N`, pulled from the login session.
3. Click **Execute Checkout**.
4. Read the success toast, then the **Member tier** card and the **Trigger state switcher** card.

**👀 See**
- Success: `Checkout successful! Rental created for BAR-100004. Generated Rental ID: <next ID>`.
- The `Handling staff: ... staff_id N` caption — the staff_id passed to the procedure is the **logged-in user's**, not a manual pick.
- A code block with the trigger NOTICE from the procedure.
- **Member tier card** — `Member tier — Standard Student` with Discount 0%, Max rental days 3, email and phone (proves `membership_types` drives the UI).
- **Trigger state switcher — `trg_after_rent_item_checkout`**:
  - `Condition BEFORE checkout` → `1 — Available`
  - `Condition AFTER checkout` → `2 — Rented`
  - Caption showing `total_available` decremented.
- Dashboard now shows `BAR-100004` in red (Rented).

**🗣 Say**
> "This is the stored procedure `proc_member_rental_checkout(member, staff, barcode)`. The staff argument isn't typed here — the logged-in user's `staff_id` from the session is passed automatically. The member dropdown is enriched with the tier from `membership_types` — discount percent and max rental days — so the clerk sees the member's entitlements at a glance. One `CALL` performs three atomic statements: insert the rental header, insert the line item, and raise the rental-ID notice. On that insert, two triggers fire — `trg_check_insert_return_date` validates the due date, and `trg_after_rent_item_checkout` flips the item's condition to Rented. That UPDATE on `inventory_items` then fires a second trigger chain: `trg_after_inventory_condition_change` re-balances `equipment_catalog.total_available` for that catalog. Because it all runs inside one transaction, either everything commits or nothing does."

**Backend point to highlight:** the barcode dropdown is filtered to `condition_id = 1`, so the "Available-only" guarantee is enforced in SQL before the procedure even runs. (Optionally repeat the checkout with `#3 — Rajitha Gunasekara` to show an **Elite Athlete Staff** tier card with 30% discount.)

---

### Page 3 · Equipment Return & Late Fine Engine

**👉 Do**
1. Go to **Return & Late Fine**.
2. In **Active rental line item**, select an **overdue** line, e.g. `#59 — BAR-100084 (Aero-Blade Carbon Tennis Racket)` (due 2026-07-30).
3. Look at the **Late fine — fn_calculate_late_fine()** metric: it shows a **non-zero live fine** (e.g. `$15.00`) with delta `LATE` because `expected_return_date` has passed while `actual_return_timestamp` is still NULL — the scalar function computes days-late × $2.50 against `CURRENT_DATE`.
4. Click **Process Return**.
5. Read the success message, the info box, and the NOTICE.

**👀 See**
- Detail card: `Rental ID`, `Barcode / Item`, `Member`, `Expected return date`.
- A `Processed by: <logged-in user> (role)` info badge above the button — session-driven.
- After processing: success `Return processed for line item 8. A late fine of $52.50 was added to the rental's total_billed.`
- Info box: condition reset back to `1 (Available)`.
- NOTICE from `trg_auto_calculate_fine` quoting the fine.
- The item reappears in the dashboard as Available.

**🗣 Say**
> "Returning an item is a two-part operation: the procedure `proc_item_return(line_id, fine)` stamps `actual_return_timestamp` and adds the fine to the rental's `total_billed`. The trigger `trg_auto_calculate_fine` fires on that UPDATE and raises a NOTICE computed by the scalar function `fn_calculate_late_fine` — 21 days late × $2.50 = $52.50. Finally the app resets the item's condition back to 1 so it can be rented again — that UPDATE fires `trg_after_inventory_condition_change`, which puts `total_available` back up. The catalog balances need zero application code."

**Backend point to highlight:** the **on-time demo** — repeat with `#19 — BAR-100021` → `$0.00`, NOTICE "returned on time", proving the fine is truly date-driven, not hard-coded. The condition reset to 1 and the restored `total_available` are both visible on the Dashboard / Schema Inspector after the return.

---

### Page 4 · Supplier Restock & Asset Acquisition

There are **three tabs** — cover all three.

**Tab 1 — :material/package_2: Supplier Restock**

**👉 Do**
1. **Supplier** → `#2 — Elite Sports Wholesale (011-266-7780)` (each label now carries the supplier's contact number); **Total cost (USD)** → `25.00`; **New barcode** → `SKU-VIVA-1001`; **Catalog item** → `Aero-Blade Carbon Tennis Racket`; **Item condition** → `Available (1)`; **Serial number (must be unique)** → `SN-VIVA-1001`.
2. Click **Execute Restock**.

**👀 See**
- Success: `Restock recorded: SKU-VIVA-1001 added to inventory under a new purchase order.`
- A new row appears in **Recent Purchase Orders** and in the dashboard grid.
- Back in Tab 2 you could also try a **duplicate barcode** to watch it fail.

**🗣 Say**
> "`proc_supplier_restock` operates on two tables in one transaction: it inserts a row into `purchase_orders` and a row into `inventory_items`. Duplicate barcodes or serial numbers are rejected by the PRIMARY KEY and UNIQUE constraints — no application code needed, the database guarantees integrity."

**Tab 2 — :material/bolt: Acquire New Asset**

**👉 Do**
1. **New barcode** → `SKU-VIVA-2001`; **Catalog item** → `Aero-Blade Carbon Tennis Racket`; **Item condition** → `Available (1)`; **Serial number** → `SN-VIVA-2001`.
2. Click **Acquire Asset**.

**👀 See**
- Success: `Asset SKU-VIVA-2001 recorded and catalog totals incremented.`
- In **Catalog Availability Balances**, `total_owned` and `total_available` both increased by one.

**🗣 Say**
> "`proc_acquire_new_asset` is different from restock — it represents the club buying an asset outright rather than restocking from a supplier. The procedure no longer touches the catalog row itself: the INSERT into `inventory_items` fires `trg_after_inventory_insert`, which bumps `total_owned` and — because the new unit is Available — `total_available` by one. Notice in **Catalog Availability Balances** how both balances rose together."

**Tab 3 — :material/call_split: Overloaded proc_register_supplier_restock**

**👉 Do**
1. In the **Overload signature** segmented control, select **Overload A** → fill the form (`SKU-VIVA-3001` / `SN-VIVA-3001`, cost `25.00`) → **Execute Overload A** → success.
2. Select **Overload B** → **Execute Overload B** (leave defaults).

**👀 See**
- Overload A: success — `Overload A executed: purchase order + item SKU-VIVA-3001 recorded.`
- Overload B: `:material/block:` red error — `Exception captured (intentional — Overload B is the legacy broken version).`

**🗣 Say**
> "PostgreSQL supports procedure overloading — two objects share the name `proc_register_supplier_restock` but differ in their argument lists. Overload A matches the current schema and works. Overload B is a legacy definition whose body references columns that no longer exist (`equipment_id`, `quantity`), so PostgreSQL rejects it at runtime. It is included deliberately so you can watch the engine fail gracefully and transactionally."

---

### Page 5 · Data Schema Inspector

**👉 Do**
1. Open **Select a table** dropdown → choose `rentals`.
2. Read the **Row count — rentals** metric.
3. Scroll through **Schema Definition**, then **Integrity Constraints**, then **Live Contents**.
4. Repeat quickly for `inventory_items` and `rental_line_items` (or `maintenance_logs`).

**👀 See**
- Row count for `rentals`: 9 (live).
- Schema Definition table: column names, types, PK/FK markers.
- Integrity Constraints table: every `PRIMARY KEY`, `FOREIGN KEY`, `CHECK`, `UNIQUE` on that table — e.g. `rentals_total_billed_check`, `rentals_member_id_fkey`.
- Live Contents: real rows, read live with no caching.

**🗣 Say**
> "This page inspects any of the 14 tables in the `public` schema. Two security details worth noting: the table name comes from a whitelisted dropdown, and it is safely interpolated with `psycopg2.sql.Identifier` — so there is no SQL-injection surface. The Integrity Constraints panel proves every FK, CHECK and UNIQUE rule is enforced at the database engine level, not in application code."

**Backend point to highlight:** if asked to show all 14 tables, rattle them off: `brands`, `equipment_catalog`, `equipment_categories`, `inventory_items`, `item_conditions`, `maintenance_logs`, `members`, `membership_types`, `purchase_orders`, `rental_line_items`, `rentals`, `staff`, `staff_roles`, `suppliers`. Open `staff` → the **Schema Definition** panel shows the `password_hash` column — say aloud: "passwords are stored hashed (PBKDF2 + per-user salt), never plaintext."

---

### Page 6 · Functions, Triggers & Integrity Inspector

Four tabs. Cover **all four**.

**Tab 1 — :material/code: Functions**

**👉 Do**
1. Note the inventory dataframe: **5 rows** (`fn_calculate_late_fine` + 4 trigger functions).
2. Expand `fn_calculate_late_fine(...)` — show the `pg_get_functiondef` SQL source.
3. In **Interactive calculator — `fn_calculate_late_fine`**: enter line `8` (already returned, overdue) → **Calculate late fine**.

**👀 See**
- Metric: `Late fine for line #8` → `$52.50` with note `21 day(s) late × $2.50/day.`
- (Try a returned on-time line such as `15` → `$0.00`, info "No fine".)
- The fifth row is `fn_trigger_sync_catalog_availability` — the single shared body behind the three inventory triggers.

**🗣 Say**
> "The scalar function `fn_calculate_late_fine` computes days-late × $2.50. The other four functions are trigger functions — `fn_trigger_process_checkout`, `fn_trigger_calculate_fine`, `fn_validate_expected_return_date`, `fn_trigger_sync_catalog_availability` — they cannot be CALLed directly; they only run when their triggers fire. You can inspect the full body of any object with the expander."

**Tab 2 — :material/rocket_launch: Triggers**

**👉 Do** — run all four live tests (each is **rolled back**, so it is 100% safe to click repeatedly):
1. **`trg_check_insert_return_date`** → click **Attempt invalid past-date insert**.
2. **`trg_auto_calculate_fine`** → click **Fire fine trigger on latest line item**.
3. **`trg_after_rent_item_checkout`** → click **Simulate checkout (rolled back)**.
4. **`trg_after_inventory_*`** → click **Prove availability sync (rolled back)**.

**👀 See**
- Test 1: red error — `SQLSTATE P0001` — the past-date insert was aborted.
- Test 2: green — `Executed successfully — transaction rolled back safely.` + the fine NOTICE text.
- Test 3: green — the in-transaction `SELECT` shows `condition_id = 2`, then rollback restored it to 1.
- Test 4: green — the card prints `total_available` for one catalog **before** checkout, then a **decremented** value after the line-item insert, then the restored value after the simulated return — all inside one rolled-back transaction.

**🗣 Say**
> "There are **six triggers** in total. Three live on `rental_line_items`: `trg_check_insert_return_date` runs BEFORE INSERT and raises an exception for an impossible due date; `trg_auto_calculate_fine` runs AFTER UPDATE on return and emits the fine NOTICE; `trg_after_rent_item_checkout` runs AFTER INSERT and flips the item's condition. Three more live on `inventory_items` — `trg_after_inventory_insert`, `trg_after_inventory_condition_change`, `trg_after_inventory_delete` — and they all share one body, `fn_trigger_sync_catalog_availability`, which keeps `equipment_catalog.total_owned`/`total_available` perfectly in sync no matter how inventory changes (even direct CRUD edits, as you'll see on the Data Management page). Firing order on INSERT is alphabetical: `trg_after_rent_item_checkout` first, then `trg_check_insert_return_date`. And crucially — every one of these test buttons runs inside a single transaction that is always rolled back, so the database is never changed."

**Tab 3 — :material/hub: Indexes**

**👉 Do**
1. Open the **Custom Index Inspection Card**.
2. Show each of the 3 indexes and its `CREATE INDEX` definition. Expand **All indexes** to contrast with the PK/UNIQUE-backed ones.

**🗣 Say**
> "Three indexes were created explicitly. `idx_inv_cat_condition` is a composite B-tree on (catalog_id, condition_id) for the dashboard's category × availability groupings. `idx_items_return_pending` is a **partial index** — it only covers rows where `actual_return_timestamp IS NULL`, so 'what's still on loan?' scans touch just the open rows. `idx_maint_barcode_date` accelerates per-asset maintenance history, newest first."

**Tab 4 — :material/error: Exception Suite**

**👉 Do** — click all six preset buttons in order. They are all rolled back.

1. **Non-existent barcode (PL/pgSQL RAISE EXCEPTION)** → `P0001`
2. **Duplicate barcode (PRIMARY KEY violation)** → `23505`
3. **Duplicate serial number (UNIQUE violation)** → `23505`
4. **Past-date insert (trigger constraint violation)** → `P0001`
5. **Invalid member (FOREIGN KEY violation, wrapped by the procedure)** → `P0001`
6. **Negative total (CHECK violation)** → `23514`

**👀 See**
- Each card shows a red `Exception captured — SQLSTATE <code>` with the exact engine message, OR a green "rolled back safely" when the test is a success-path simulation.

**🗣 Say**
> "These six buttons intentionally raise real PostgreSQL exceptions — `RAISE EXCEPTION` from PL/pgSQL, plus `23505` unique, `23514` check, and `23503` foreign-key violations. The app catches each one through psycopg2, extracts the SQLSTATE and message, and shows it in `st.error()` — the UI never crashes. The interesting case is #5: the INSERT actually fails with `23503`, but the procedure's `EXCEPTION WHEN OTHERS` handler re-raises it as `P0001`, which is a textbook example of PL/pgSQL error propagation. And every test runs in a rolled-back transaction, so this whole page is non-destructive."

---

### Page 7 · Data Management (CRUD Console) — Admin / Clerk only

This page is **table-scoped by role**. Log in as **Kasun Fernando (System Administrator)** → all **14 tables** (including `staff`, password reset). Log in as **Dilini Jayasinghe (Inventory Clerk)** → only the **8 stock/inventory tables** (`equipment_catalog`, `inventory_items`, `maintenance_logs`, `suppliers`, `purchase_orders`, `brands`, `equipment_categories`, `item_conditions`); no `staff` table and no password reset. Front Desk users never see the page at all. Everything commits here (it is real data management), so prefer throwaway rows.

**Tab 1 — :material/add_circle: Create**

**👉 Do**
1. **Select a table** → `brands`.
2. Click **Add new row** → fill `brand_name` → **Insert row**.
3. Repeat once on `suppliers` (natural PK `supplier_name`, also a dropdown-free text field).

**👀 See**
- Success: `Inserted 1 row(s) into brands.` + the new row appears in **Tab 2 Browse**.
- Sequence-PK tables (e.g. `brands` → `brand_id`) get **no PK field** — the value comes from the `brands_brand_id_seq` sequence. Natural-PK tables (e.g. `suppliers`) **require** you to type the key.
- Foreign-key columns render as **dropdowns** pre-loaded from the referenced table (e.g. `inventory_items.condition_id` shows "Available (1) / Rented (2) / …").
- `staff.password_hash` is never shown — the form is a password field that hashes on insert.

**🗣 Say**
> "The Create tab inspects the table live — `information_schema.columns` plus `pg_attrdef`/`pg_sequence` — and builds the form from the real schema: it knows which columns are sequence-backed PKs (omitted, the sequence supplies them), which are natural PKs (required text), and which are foreign keys (rendered as dropdowns fed from the referenced table). Values go in via parameterized `%s` placeholders. Inserting a brand is a single `INSERT` — and thanks to the availability triggers you'll see next, the same machinery keeps catalog balances honest even when it's applied to `inventory_items`."

**Tab 2 — :material/table: Browse**

**👉 Do**
1. **Select a table** → `rentals` → note the **row-count metric** (9 rows).
2. Type `Naveen` in **Search** → the grid filters; clear it.
3. Click **Next page** until the count/paging resets — pagination is SQL-side (`OFFSET`/`LIMIT`).
4. Switch **Rows per page** → 5.

**👀 See**
- `Showing rows X–Y of N` pager + Prev/Next buttons.
- Search is per-table and case-insensitive (`ILIKE` on text columns, `%term%`).
- Row count always live from `COUNT(*)`.

**🗣 Say**
> "Browse is a metadata-driven grid — it builds a `SELECT` from the column list it loaded for the chosen table, filters with `WHERE col ILIKE '%term%'` across text columns, and paginates with `LIMIT/OFFSET`. No hard-coded SQL per table: one code path serves all 14."

**Tab 3 — :material/edit: Update / Delete**

**👉 Do**
1. **Select a table** → `brands`; pick the brand you created (e.g. `UI-VIVA-BRAND`).
2. In **Edit row**, change `brand_name` → **Save changes**.
3. Now the delete demo (the powerful one): **Select a table** → `rentals`; pick the rental whose line items you want to see cascade (e.g. the highest ID). Check the **Delete will also cascade to: rental_line_items** warning. Tick the **confirmation checkbox** → **Delete row**.
4. (Optional integrity demo) Try deleting a **member that has rentals** — the delete is **rejected** with `23503` because no FK on that path cascades.
5. (Optional availability demo) Delete a row from `inventory_items` → then open Schema Inspector → `equipment_catalog` → the owning catalog's `total_owned` dropped by one (proving `trg_after_inventory_delete`).

**👀 See**
- `staff.password_hash` is masked in the edit form; a dedicated **Reset password** form re-hashes with a fresh salt.
- **Cascade warning**: only `rentals → rental_line_items` and `inventory_items → maintenance_logs` cascade (verified from the live FK rules); every other FK is `RESTRICT`.
- Deleting a parent with restricted children → red `Exception captured — SQLSTATE 23503 (foreign key violation)`.

**🗣 Say**
> "Update and Delete are generated from the table's primary key. Deletes are the interesting case: the app queries `pg_constraint` to learn each FK's on-delete rule, so it can warn 'this delete cascades to rental_line_items' before you commit. Where FKs are RESTRICT — for example deleting a member who has rentals — the database itself refuses with `23503`; the app shows the SQLSTATE and rolls back. And because the CRUD console writes real data, watch it respect the triggers: delete an inventory row and the catalog balance falls without any app code touching it. Finally, the demo users: `ensure_auth_schema()` re-seeds them after any restore, and the password reset here re-runs the same PBKDF2 hasher."

---

## Section 2 — Database Feature Verification Matrix

Use this to answer "where do I show X?" instantly.

| # | Database object | Type | Where to demo in the app |
|---|---|---|---|
| 1 | `proc_member_rental_checkout` | Stored procedure | **Member Checkout** → Execute Checkout |
| 2 | `proc_item_return` | Stored procedure | **Return & Late Fine** → Process Return |
| 3 | `proc_supplier_restock` | Stored procedure | **Restock** → Tab *Supplier Restock* |
| 4 | `proc_acquire_new_asset` | Stored procedure | **Restock** → Tab *Acquire New Asset* |
| 5 | `proc_register_supplier_restock` (Overload A) | Stored procedure | **Restock** → Tab *Overloaded…* → Overload A |
| 6 | `proc_register_supplier_restock` (Overload B, broken) | Stored procedure | **Restock** → Tab *Overloaded…* → Overload B |
| 7 | `fn_calculate_late_fine` | Scalar function | Return page metric **and** Functions tab calculator |
| 8 | `fn_trigger_process_checkout` | Trigger function | Triggers tab — *Simulate checkout* |
| 9 | `fn_trigger_calculate_fine` | Trigger function | Triggers tab — *Fire fine trigger* |
| 10 | `fn_validate_expected_return_date` | Trigger function | Triggers tab — *past-date insert* / Exception #4 |
| 11 | `fn_trigger_sync_catalog_availability` | Trigger function | Triggers tab — *Prove availability sync*; shared body of the 3 inventory triggers |
| 12 | `trg_check_insert_return_date` | Trigger (BEFORE INSERT) | Triggers tab test 1; Exception #4 |
| 13 | `trg_auto_calculate_fine` | Trigger (AFTER UPDATE) | Triggers tab test 2; Return page NOTICE |
| 14 | `trg_after_rent_item_checkout` | Trigger (AFTER INSERT) | Checkout Before/After switcher; Triggers test 3 |
| 15 | `trg_after_inventory_insert` | Trigger (AFTER INSERT) | Triggers tab test 4; Restock/Acquire balances |
| 16 | `trg_after_inventory_condition_change` | Trigger (AFTER UPDATE OF condition_id) | Checkout/Return balance changes; Triggers test 4 |
| 17 | `trg_after_inventory_delete` | Trigger (AFTER DELETE) | CRUD → delete an `inventory_items` row → catalog balance drops |
| 18 | `idx_inv_cat_condition` | Custom index | Indexes tab card |
| 19 | `idx_items_return_pending` | Custom partial index | Indexes tab card |
| 20 | `idx_maint_barcode_date` | Custom index | Indexes tab card |
| 21 | 14 tables | Tables | **Data Schema Inspector** dropdown |
| 22 | PK / FK / CHECK / UNIQUE | Constraints | **Data Schema Inspector** → Integrity Constraints |
| 23 | Exception handling + ROLLBACK | Error model | **Exception Suite** tab (6 presets) |
| 24 | Login gate + role-based session | Auth | Login screen → **Viva Quick Login**; sidebar badge + **Logout** |
| 25 | RBAC CRUD gating (Admin/Clerk only) | Auth/RBAC | Log in as Front Desk vs Admin → nav goes 6 → 7 pages |
| 26 | `staff.password_hash` (PBKDF2 + salt) | Security | Login page; visible in **Schema Inspector** → `staff`; reset via **CRUD** |
| 27 | Parameterized auth query (anti SQL-injection) | Security | Login page (`WHERE email = %s`) |
| 28 | Metadata-driven CRUD console | CRUD | **Data Management** page — all 14 tables, 3 tabs, FK dropdowns, search + pagination |
| 29 | FK CASCADE rules | Constraints | **CRUD Delete** — warns only `rentals→rental_line_items`, `inventory_items→maintenance_logs` cascade; others `RESTRICT` (`23503`) |
| 30 | Sequence sync (auto-increment sanity) | Schema | CRUD create on a sequence-PK table uses `setval`-fixed sequences (e.g. `staff` → 3) |

---

## Section 3 — Edge-Case & Exception Interception Demo Guide

### The two-layer error architecture (say this clearly)

1. **Application layer (Python):** every DB call goes through `execute_test()` / `call_procedure()` / `query_df()`. When psycopg2 raises, the app extracts `.pgcode` (the SQLSTATE) and the message, and renders them with `st.error()`. **The app never crashes — it always shows a controlled red card.**
2. **Database layer (PL/pgSQL):** procedures deliberately `RAISE EXCEPTION 'message'` for business rules (unknown barcode, invalid input), and `EXCEPTION WHEN OTHERS` can re-wrap engine errors. Constraint violations (`23505`, `23514`, `23503`) are raised by the engine itself.

### Transactional isolation / ROLLBACK — the 30-second explanation

Every live test on the **Triggers** and **Exception Suite** tabs is wrapped like this:

```text
BEGIN;
    <your test statement(s)>
    -- capture notices / errors mid-transaction
ROLLBACK;   -- always reached → nothing persists
```

- The UI prints `transaction rolled back safely` on success, or the captured `SQLSTATE` on failure.
- This is why you can **click the same test button 100 times** and the database stays byte-for-byte identical.
- Contrast it with the real pages (Checkout / Return / Restock / Acquire), which **COMMIT** — those are the persistent demos.

### The one-liner that proves non-destructiveness

> "Every button on this page opens a transaction, runs the statement, captures the result, and rolls back. If I click 'duplicate barcode' a hundred times, the row count of `inventory_items` never changes — check the Schema Inspector metric before and after."

### Five edge-case questions examiners love — and the answer they expect

| Question | Where to point / answer |
|---|---|
| What happens if someone scans an unknown barcode? | `proc_member_rental_checkout` pre-check raises P0001 → Exception #1 / just try it on Checkout. |
| Can two items share a serial number? | No — UNIQUE on `inventory_items.serial_number` → `23505` (Exception #3). |
| What stops a checkout of an already-rented item? | The barcode dropdown only lists `condition_id = 1`; the procedure re-checks inside the transaction. |
| Can a return date be in the past? | No — `trg_check_insert_return_date` → P0001 (Trigger test 1 / Exception #4). |
| Does a failed operation leave partial data? | No — single transaction; failure aborts the whole statement. Overload B and the exceptions demonstrate this. |
| How are passwords stored — and is login safe from SQL injection? | `staff.password_hash` stores PBKDF2-HMAC-SHA256 + per-user salt (never plaintext); auth uses a parameterized `WHERE email = %s` query, verified with constant-time comparison. |
| Can a user see pages before logging in? | No — `main()` gates everything on `st.session_state['logged_in']`; logged out, only the login card renders (no navigation). |
| What keeps catalog totals accurate when items change outside checkout/return? | Three AFTER triggers on `inventory_items` (`trg_after_inventory_insert` / `_condition_change` / `_delete`) sharing one function — prove it with **Triggers tab test 4** or by CRUD-editing `inventory_items` and watching `equipment_catalog`. |
| Can a front-desk officer open the CRUD page? | No — `CRUD_ROLES` gates both the nav entry (`build_nav_pages`) and the page itself (`st.warning` + `st.stop`); it is Admin/Clerk only. |
| What happens when the app's new `staff` row collides with the seeded users? | Can't — sequences were `setval`-ed to `MAX(id)` (e.g. `staff` → 3), so CRUD inserts take the next free id. |

---

## Section 4 — Emergency / Recovery Cheatsheet

| Situation | What to do |
|---|---|
| App frozen / needs restart | In the terminal: `Ctrl+C`, then `streamlit run app.py` again. |
| Port 8501 already in use | `streamlit run app.py --server.port 8502` |
| Run inside the project venv | `.venv\Scripts\python -m streamlit run app.py` |
| Sidebar shows `DB unreachable` | 1) Is PostgreSQL started? `pg_isready -h localhost -p 5432` 2) Is `PGPASSWORD` set correctly in this terminal? 3) Restart the app. |
| Quick DB connectivity check | `psql -U postgres -d sport_equipment_mgmt -c "SELECT 1;"` |
| A live demo polluted the data | Best: undo in-app — return any test rental via the **Return** page. Fastest reset: `pg_restore -c --no-owner --no-privileges -U postgres -d sport_equipment_mgmt sport_equipment_mgmt` (run from the project folder; `-c` drops objects first, DB must already exist). |
| Forgot login credentials mid-demo | Use **Viva Quick Login / Demo Mode** (one click) or the demo passwords `counter123` / `admin123` / `clerk123`. |
| After a DB reset the demo users "disappear" | They don't — the app self-heals on every launch (`ensure_auth_schema()` re-adds `staff.password_hash` and the 3 demo users), even after `pg_restore -c`. |
| Forgot a rental ID mid-demo | Open **Data Schema Inspector** → `rentals` → **Live Contents** top rows. |
| "No members/staff/suppliers" error | Database was reset mid-session or connected to wrong DB — restore with the `pg_restore` command above. |
| Deleted something on the CRUD page that I need back | In-app undo isn't possible for deletes — restore the whole DB: `pg_restore -c --no-owner --no-privileges -U postgres -d sport_equipment_mgmt sport_equipment_mgmt` (run from the project folder). The demo users re-seed automatically on next launch. |
| Fine looks wrong on screen | The fine is computed at return time from `NOW()`. Confirm the expected return date in the line-item card; rate is $2.50/day. |
