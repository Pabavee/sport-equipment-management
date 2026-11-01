# Sport Equipment Management System

A database-centric equipment rental management system for a sports club. A
Streamlit front end sits on top of a PostgreSQL database, and the business
rules are implemented in SQL and PL/pgSQL (procedures, functions, triggers,
indexes) rather than in the Python layer.

Built as an Advanced Database Systems (ADBS) project.

---

## Features

Seven pages, each behind a staff login gate:

1. **Dashboard & Live Inventory** — KPI cards, quick actions, color-coded inventory grid, fleet chart.
2. **Member Checkout** — calls `proc_member_rental_checkout`, with a before/after view of the trigger that flips an item from *Available* to *Rented*.
3. **Return & Late Fine** — calls `fn_calculate_late_fine` and `proc_item_return`, showing the fine NOTICE from the trigger.
4. **Restock & Asset Acquisition** — `proc_supplier_restock`, `proc_acquire_new_asset`, and a deliberately overloaded procedure (`proc_register_supplier_restock`, two signatures).
5. **Data Schema Inspector** — inspect all 14 tables: contents, columns, constraints.
6. **Functions, Triggers & Integrity** — view and test 5 functions, 6 triggers, 3 custom indexes, plus an exception-testing suite.
7. **Data Management (CRUD Console)** — metadata-driven Create / Read / Update / Delete, table-scoped by role.

Role-based access control:

| Role | Pages | CRUD scope |
|---|---|---|
| Front Desk Officer | Dashboard, Member Checkout, Return & Late Fine | none |
| Inventory Clerk | Dashboard, Restock & Assets, Schema Inspector, Data Management | 8 stock/inventory tables |
| System Administrator | Dashboard, Schema Inspector, Functions/Triggers, Data Management | all 14 tables + password reset |

---

## Tech stack

- **Python 3.14** (tested; 3.10+ should work)
- **Streamlit 1.60**
- **PostgreSQL 18.3** (tested; 14+ should work)
- **psycopg2-binary 2.9.12**
- **pandas 3.0.5**
- SQL / PL/pgSQL for schema and backend logic
- TOML for the Streamlit theme

---

## Repository layout

```
.
├── app.py                      # Single-file Streamlit application
├── .streamlit/
│   └── config.toml             # "Fresh Court" light theme
├── sport_equipment_mgmt        # PostgreSQL custom-format dump
├── seed_late_fine_demo.sql     # Optional overdue-rental demo data
├── ARCHITECTURE_ANALYSIS.md    # Backend/data-access deep dive
├── VIVA_DEMO_GUIDE.md          # Step-by-step demonstration script
├── requirements.txt
└── README.md
```

---

## Setup

### 1. Clone the repository

```powershell
git clone https://github.com/<your-username>/<repo-name>.git
Set-Location <repo-name>
```

### 2. Create a virtual environment and install dependencies

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

If PowerShell blocks activation, run this once in the session:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
```

### 3. Create and restore the database

Make sure PostgreSQL is running. The dump uses the custom format, so restore it
with `pg_restore` (not `psql`):

```powershell
createdb -U postgres sport_equipment_mgmt
pg_restore -U postgres --no-owner --no-privileges -d sport_equipment_mgmt .\sport_equipment_mgmt
```

`--no-owner --no-privileges` lets any local superuser restore the dump without
pre-creating the original roles.

If `createdb`/`pg_restore` are not on your PATH, use the full path, for example:

```powershell
& "C:\Program Files\PostgreSQL\18\bin\pg_restore.exe" -U postgres --no-owner --no-privileges -d sport_equipment_mgmt .\sport_equipment_mgmt
```

Optional: load extra overdue rentals for the late-fine demo:

```powershell
psql -U postgres -d sport_equipment_mgmt -f .\seed_late_fine_demo.sql
```

### 4. Provide the database password

No password is stored in this repository. Set it for the terminal session:

```powershell
$env:PGPASSWORD = "your-postgres-password"
```

Alternatively, create `.streamlit/secrets.toml` locally (it is git-ignored):

```toml
[db]
password = "your-postgres-password"
```

The application resolves the password in this order: `st.secrets` →
`PGPASSWORD` / `DB_PASSWORD` environment variables → the blank fallback
constant in `app.py`.

### 5. Run the app

```powershell
streamlit run app.py
```

Open <http://localhost:8501>.

---

## Demo accounts

These are seeded automatically on first launch (PBKDF2-hashed in the database).

| Name | Email | Password | Role |
|---|---|---|---|
| Amara Silva | `amara.s@sports.com` | `counter123` | Front Desk Officer |
| Dilini Jayasinghe | `dilini.j@sports.com` | `clerk123` | Inventory Clerk |
| Kasun Fernando | `kasun.f@sports.com` | `admin123` | System Administrator |

The login page also has a **Viva Quick Login** panel for one-click role
switching. These credentials are intentionally public for demonstration; do not
reuse them for anything real.

---

## Database design (14 tables)

| Group | Tables |
|---|---|
| Catalog / reference | `brands`, `equipment_categories`, `item_conditions`, `membership_types`, `staff_roles` |
| Product catalog | `equipment_catalog` |
| Physical inventory | `inventory_items` |
| Operations | `rentals`, `rental_line_items`, `maintenance_logs`, `purchase_orders` |
| People | `members`, `staff`, `suppliers` |

Key backend objects:

- **6 procedures** — `proc_member_rental_checkout`, `proc_item_return`,
  `proc_supplier_restock`, `proc_acquire_new_asset`,
  `proc_register_supplier_restock` (two overloads)
- **5 functions** — `fn_calculate_late_fine` plus four trigger functions
- **6 triggers** — checkout validation, return-date validation, fine
  calculation, and three `inventory_items` triggers that keep
  `equipment_catalog` balances in sync
- **3 custom indexes** — `idx_inv_cat_condition`, `idx_items_return_pending`
  (partial), `idx_maint_barcode_date`

`equipment_catalog.total_owned` / `total_available` are never updated by hand;
the AFTER triggers on `inventory_items` recompute them, so the CRUD console,
checkout, return, and restock paths all stay consistent.

---

## Security and integrity

- Passwords stored as **PBKDF2-HMAC-SHA256** hashes with a per-user salt,
  verified in constant time.
- Login uses a **parameterized** query; dynamic table/column names in the CRUD
  console are quoted with `psycopg2.sql.Identifier`.
- **RBAC at two layers**: navigation is filtered from a permission map, and
  every page re-checks access with `require_page()`.
- Trigger and exception demos run inside **rolled-back transactions**, so they
  are repeatable and non-destructive.
- Foreign keys are `RESTRICT` by default; only `rental_line_items` and
  `maintenance_logs` cascade.

---

## Known limitations

- The late fine is a flat **$2.50/day**; it does not scale with the item's
  rental rate.
- Membership tier `discount_percent` and `max_rental_days` are displayed but
  not enforced: `proc_member_rental_checkout` always sets a 7-day due date and
  does not apply discounts.
- The app connects as the local `postgres` superuser. Use a least-privilege
  role in production.
- Each operation opens its own connection (no pooling) to keep demo
  transactions isolated.
- `app.py` is a single ~2,600-line file; a production version would split UI,
  data access, and authentication into modules.

---

## Documentation

- [`ARCHITECTURE_ANALYSIS.md`](ARCHITECTURE_ANALYSIS.md) — data-access layer,
  checkout/return flows, triggers, and error handling.
- [`VIVA_DEMO_GUIDE.md`](VIVA_DEMO_GUIDE.md) — live demonstration script with
  talking points and troubleshooting.
