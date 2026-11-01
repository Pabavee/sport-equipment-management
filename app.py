"""
================================================================================
 SPORT EQUIPMENT MANAGEMENT SYSTEM — Streamlit Front-End (PostgreSQL Backend)
================================================================================
 A single-file UI exposing EVERY PostgreSQL backend feature live during a
  project viva (7 pages):

   1. Dashboard & Live Inventory ...... live KPI cards (Total / Available / On Loan /
                                        Under Repair) + quick actions, an operating
                                        guide, color-coded inventory grid, and
                                        per-condition fleet chart
   2. Member Checkout ................. proc_member_rental_checkout + Before/After
                                       trigger state switcher (Available -> Rented)
   3. Return & Late Fine .............. fn_calculate_late_fine + proc_item_return
                                       + trg_auto_calculate_fine NOTICE
   4. Restock & Asset Acquisition ..... proc_supplier_restock, proc_acquire_new_asset
                                       + overloaded proc_register_supplier_restock (×2)
   5. Data Schema Inspector ........... inspect all 14 tables: contents, schema, rows
   6. Functions, Triggers & Integrity . all 5 functions, all 6 triggers (live tests),
                                       3 custom indexes, exception-testing suite
   7. Data Management (CRUD Console) .. metadata-driven Create / Read / Update / Delete,
                                       table-scoped to the signed-in role

 The whole app sits behind a staff login gate with role-based session management:
 credentials are checked against the `staff` table (PBKDF2-hashed passwords in the
 `staff.password_hash` column, seeded automatically on first run), session context
 (user_id / user_name / user_role) drives the checkout and return pages, and a
 "Viva Quick Login" panel lets the examiner switch roles in one click.

 Every role owns a distinct duty set (see ROLE_PERMISSIONS in section 5c). The
 navigation and the Dashboard quick actions are filtered from that map, and each
 page also calls require_page() so hidden pages cannot be reached directly:
   * Front Desk Officer ... Dashboard, Member Checkout, Return & Late Fine
   * Inventory Clerk ..... Dashboard, Restock & Assets, Schema Inspector,
                           CRUD on the 8 stock/inventory tables
   * System Administrator  Dashboard, Schema Inspector, Functions/Triggers,
                           CRUD on all 14 tables (incl. staff & password reset)

 The `equipment_catalog.total_owned / total_available` balance columns are no longer
 touched by hand-written SQL inside the procedures: three AFTER triggers on
 `inventory_items` (INSERT / condition change / DELETE) are the single source of
 truth, so the CRUD console, the checkout/return pages and the restock procedure
 all stay in sync automatically.

 Backend object inventory
--------------------------------------------------------------------------------
   * Stored Procedures (6) ... proc_member_rental_checkout, proc_item_return,
                            proc_supplier_restock, proc_acquire_new_asset,
                            proc_register_supplier_restock (overloaded ×2)
   * Functions (5) .......... fn_calculate_late_fine (scalar),
                            fn_trigger_process_checkout / fn_trigger_calculate_fine /
                            fn_validate_expected_return_date / fn_trigger_sync_catalog_availability
                            (trigger functions)
   * Triggers (6) ........... trg_after_rent_item_checkout, trg_auto_calculate_fine,
                            trg_check_insert_return_date, trg_after_inventory_insert,
                            trg_after_inventory_condition_change, trg_after_inventory_delete
   * Indexes (3) ............ idx_inv_cat_condition, idx_items_return_pending,
                            idx_maint_barcode_date
   * Tables (14) ............ inspectable in the Data Schema Inspector
   * Constraints ............ PK / FK (RESTRICT / CASCADE), CHECK, UNIQUE

 Trigger / exception demos always run inside a transaction that is ROLLED BACK,
 so the viva can be repeated without corrupting data.

--------------------------------------------------------------------------------
 REQUIRED PACKAGES  (install once)
--------------------------------------------------------------------------------
   pip install streamlit psycopg2-binary pandas

 RUN THE APP
--------------------------------------------------------------------------------
   streamlit run app.py

 DATABASE CONNECTION
--------------------------------------------------------------------------------
   Host: localhost | Port: 5432 | DB: sport_equipment_mgmt | User: postgres
   The password is never hard-coded. It is resolved in this order of priority:
       1. .streamlit/secrets.toml  ->  [db] password = "..."  (local only)
       2. Environment variable     ->  PGPASSWORD or DB_PASSWORD
       3. The blank DB_PASSWORD constant below (fallback for quick local use)

   Example (PowerShell):
       $env:PGPASSWORD = "your-postgres-password"
       streamlit run app.py

   For a viva you may safely reset data with:
       pg_restore -c --no-owner --no-privileges -d sport_equipment_mgmt sport_equipment_mgmt
================================================================================
"""

import datetime
import hashlib
import hmac
import os
import re
import secrets

import pandas as pd
import psycopg2
import psycopg2.extras
import psycopg2.sql
import streamlit as st

# ----------------------------------------------------------------------------
# 1. DATABASE CONNECTION SETTINGS
# ----------------------------------------------------------------------------
DB_HOST = "localhost"
DB_PORT = 5432
DB_NAME = "sport_equipment_mgmt"
DB_USER = "postgres"
DB_PASSWORD = ""  # Intentionally blank: set PGPASSWORD (or .streamlit/secrets.toml)

# Runtime page configuration (must be the first Streamlit command).
st.set_page_config(
    page_title="Sport Equipment Management",
    page_icon=":material/sports:",
    layout="wide",
)


# ----------------------------------------------------------------------------
# 2. SMALL UTILITIES
# ----------------------------------------------------------------------------
class DBServerError(Exception):
    """Raised whenever a database operation fails; carries a human-readable
    message (including the message of any PL/pgSQL RAISE EXCEPTION) plus the
    SQLSTATE code of the original error."""

    def __init__(self, message: str, pgcode: str = ""):
        super().__init__(message)
        self.pgcode = pgcode


def sqlstate_code(exc: Exception) -> str:
    """Return the SQLSTATE code of a psycopg2 exception (e.g. P0001, 23505)."""
    return getattr(exc, "pgcode", "") or ""


def resolve_password() -> str:
    """Resolve the DB password: st.secrets -> env var -> constant placeholder."""
    try:
        return st.secrets["db"]["password"]
    except Exception:
        pass
    return os.environ.get("PGPASSWORD") or os.environ.get("DB_PASSWORD") or DB_PASSWORD


def friendly_error(exc: Exception) -> str:
    """Extract the cleanest message from a psycopg2 exception."""
    diag = getattr(exc, "diag", None)
    msg = diag.message_primary if (diag and diag.message_primary) else (exc.pgerror or str(exc))
    msg = str(msg).strip()
    for marker in ("\nCONTEXT:", "\nLINE", "\r\n"):
        idx = msg.find(marker)
        if idx != -1:
            msg = msg[:idx]
    return msg.strip()


def extract_rental_id(notices: list) -> str | None:
    """Pull the generated rental ID out of the procedure's RAISE NOTICE."""
    for notice in notices:
        match = re.search(r"Rental ID:\s*(\d+)", notice)
        if match:
            return match.group(1)
    return None


# ----------------------------------------------------------------------------
# 3. LOW-LEVEL DATABASE LAYER  (psycopg2 + try/except/finally)
# ----------------------------------------------------------------------------
def get_conn():
    """Open a NEW connection. Each operation uses its own connection so that
    commits are isolated and the demo stays robust across Streamlit reruns."""
    return psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=DB_USER,
        password=resolve_password(),
        connect_timeout=5,
    )


def query_df(sql: str, params=None) -> pd.DataFrame:
    """Execute a SELECT and return the results as a pandas DataFrame."""
    conn, cur = None, None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(sql, params or ())
        columns = [desc[0] for desc in cur.description]
        rows = cur.fetchall()
        return pd.DataFrame(rows, columns=columns)
    except psycopg2.Error as exc:
        raise DBServerError(friendly_error(exc), sqlstate_code(exc)) from exc
    finally:
        if cur is not None:
            cur.close()
        if conn is not None:
            conn.close()


def query_scalar(sql: str, params=None):
    """Execute a SELECT that returns a single scalar value (e.g. a function)."""
    conn, cur = None, None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(sql, params or ())
        return cur.fetchone()[0]
    except psycopg2.Error as exc:
        raise DBServerError(friendly_error(exc), sqlstate_code(exc)) from exc
    finally:
        if cur is not None:
            cur.close()
        if conn is not None:
            conn.close()


def execute_sequence(statements: list) -> tuple:
    """Execute an ordered list of (sql, params) statements inside ONE transaction.

    Returns (rowcounts, notices). Notices are the RAISE NOTICE / trigger
    messages captured from the backend - extremely useful during the viva to
    prove that triggers actually fired.
    """
    conn, cur = None, None
    try:
        conn = get_conn()
        cur = conn.cursor()
        rowcounts, notices = [], []
        for sql, params in statements:
            before = len(conn.notices)
            cur.execute(sql, params or ())
            rowcounts.append(cur.rowcount)
            notices.extend(conn.notices[before:])  # only NEW notices for this statement
        before = len(conn.notices)
        conn.commit()
        notices.extend(conn.notices[before:])  # any notices emitted at COMMIT
        return rowcounts, notices
    except psycopg2.Error as exc:
        if conn is not None:
            conn.rollback()
        raise DBServerError(friendly_error(exc), sqlstate_code(exc)) from exc
    finally:
        if cur is not None:
            cur.close()
        if conn is not None:
            conn.close()


def call_procedure(proc_name: str, params: tuple) -> tuple:
    """Convenience wrapper for CALL proc_name(...)."""
    placeholders = ", ".join(["%s"] * len(params))
    return execute_sequence([(f"CALL {proc_name}({placeholders})", params)])


def execute_test(statements: list) -> dict:
    """Run an ordered list of (sql, params) statements inside ONE transaction,
    capture everything, then ROLL BACK.

    Non-destructive — ideal for trigger demos and the exception-testing suite so
    the viva can be repeated without corrupting data. If the LAST statement is a
    SELECT its result set is returned as a DataFrame.

    Returns: {"ok", "results", "notices", "error", "pgcode", "rowcounts"}
    """
    conn, cur = None, None
    result = {
        "ok": False,
        "results": None,
        "notices": [],
        "error": None,
        "pgcode": None,
        "rowcounts": [],
    }
    try:
        conn = get_conn()
        cur = conn.cursor()
        last_idx = len(statements) - 1
        for idx, (sql, params) in enumerate(statements):
            before = len(conn.notices)
            cur.execute(sql, params or ())
            result["notices"].extend(conn.notices[before:])
            result["rowcounts"].append(cur.rowcount)
            if idx == last_idx and cur.description is not None:
                columns = [desc[0] for desc in cur.description]
                result["results"] = pd.DataFrame(cur.fetchall(), columns=columns)
        conn.rollback()  # the whole point: nothing persists
        result["ok"] = True
        return result
    except psycopg2.Error as exc:
        if conn is not None:
            conn.rollback()
        result["error"] = friendly_error(exc)
        result["pgcode"] = sqlstate_code(exc)
        return result
    finally:
        if cur is not None:
            cur.close()
        if conn is not None:
            conn.close()


def query_table_preview(table_name: str, limit: int = 200) -> pd.DataFrame:
    """SELECT * FROM <table> LIMIT %s with safe identifier quoting."""
    conn, cur = None, None
    try:
        conn = get_conn()
        cur = conn.cursor()
        stmt = psycopg2.sql.SQL("SELECT * FROM {} LIMIT %s").format(
            psycopg2.sql.Identifier(table_name)
        )
        cur.execute(stmt, (int(limit),))
        columns = [desc[0] for desc in cur.description]
        rows = cur.fetchall()
        return pd.DataFrame(rows, columns=columns)
    except psycopg2.Error as exc:
        raise DBServerError(friendly_error(exc), sqlstate_code(exc)) from exc
    finally:
        if cur is not None:
            cur.close()
        if conn is not None:
            conn.close()


def query_table_row_count(table_name: str) -> int:
    """SELECT COUNT(*) FROM <table> with safe identifier quoting."""
    conn, cur = None, None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            psycopg2.sql.SQL("SELECT COUNT(*) FROM {}").format(
                psycopg2.sql.Identifier(table_name)
            )
        )
        return int(cur.fetchone()[0])
    except psycopg2.Error as exc:
        raise DBServerError(friendly_error(exc), sqlstate_code(exc)) from exc
    finally:
        if cur is not None:
            cur.close()
        if conn is not None:
            conn.close()


def query_table_schema(table_name: str) -> dict:
    """Columns + constraints + row count for a table (introspection)."""
    columns_df = query_df(
        """
        SELECT column_name AS "Column",
               CASE WHEN character_maximum_length IS NOT NULL
                    THEN data_type || '(' || character_maximum_length || ')'
                    ELSE data_type END AS "Data Type",
               is_nullable AS "Nullable",
               COALESCE(column_default, '') AS "Default"
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s
        ORDER BY ordinal_position
        """,
        (table_name,),
    )
    constraints_df = query_df(
        """
        SELECT con.conname AS "Constraint",
               CASE con.contype
                    WHEN 'p' THEN 'PRIMARY KEY'
                    WHEN 'f' THEN 'FOREIGN KEY'
                    WHEN 'u' THEN 'UNIQUE'
                    WHEN 'c' THEN 'CHECK'
                    WHEN 'x' THEN 'EXCLUSION'
                    ELSE con.contype::text END AS "Type",
               pg_get_constraintdef(con.oid) AS "Definition"
        FROM pg_constraint con
        JOIN pg_class rel   ON rel.oid = con.conrelid
        JOIN pg_namespace n ON n.oid = rel.relnamespace
        WHERE n.nspname = 'public' AND rel.relname = %s
        ORDER BY con.conname
        """,
        (table_name,),
    )
    return {
        "columns": columns_df,
        "constraints": constraints_df,
        "row_count": query_table_row_count(table_name),
    }


# ----------------------------------------------------------------------------
# 4. CACHED DATA LOADERS  (read-only queries; cleared manually for live refresh)
# ----------------------------------------------------------------------------
@st.cache_data(ttl=15, show_spinner=False)
def load_metrics() -> tuple:
    """Total / available / rented / under-repair equipment counts for the KPI cards."""
    df = query_df(
        """
        SELECT
            COUNT(*)                                   AS total_equipment,
            COUNT(*) FILTER (WHERE condition_id = 1)   AS available_items,
            COUNT(*) FILTER (WHERE condition_id = 2)   AS rented_items,
            COUNT(*) FILTER (WHERE condition_id = 3)   AS under_repair_items
        FROM inventory_items
        """
    )
    row = df.iloc[0]
    return (
        int(row["total_equipment"]),
        int(row["available_items"]),
        int(row["rented_items"]),
        int(row["under_repair_items"]),
    )


@st.cache_data(ttl=15, show_spinner=False)
def load_condition_breakdown() -> pd.DataFrame:
    """Per-condition inventory counts for the Dashboard condition chart."""
    df = query_df(
        """
        SELECT cond.condition_label, COUNT(i.barcode) AS items
        FROM item_conditions cond
        LEFT JOIN inventory_items i ON i.condition_id = cond.condition_id
        GROUP BY cond.condition_id, cond.condition_label
        ORDER BY cond.condition_id
        """
    )
    return df.rename(columns={"condition_label": "Condition", "items": "Items"})


@st.cache_data(ttl=15, show_spinner=False)
def load_inventory_grid() -> pd.DataFrame:
    """Live inventory data grid joined with catalog + condition lookups."""
    return query_df(
        """
        SELECT
            i.barcode,
            c.item_name,
            i.serial_number,
            cond.condition_label
        FROM inventory_items i
        JOIN equipment_catalog c ON c.catalog_id = i.catalog_id
        JOIN item_conditions cond ON cond.condition_id = i.condition_id
        ORDER BY cond.condition_id, c.item_name
        """
    )


@st.cache_data(ttl=15, show_spinner=False)
def load_catalog_balances() -> pd.DataFrame:
    """Per-catalog owned vs available totals (kept in sync by the triggers)."""
    df = query_df(
        """
        SELECT
            item_name,
            total_owned     AS owned,
            total_available AS available
        FROM equipment_catalog
        ORDER BY item_name
        """
    )
    return df.rename(columns={"item_name": "Item"})


@st.cache_data(ttl=15, show_spinner=False)
def load_members() -> pd.DataFrame:
    """Members joined with their membership tier so checkout can show tier benefits."""
    return query_df(
        """
        SELECT m.member_id,
               CONCAT(m.first_name, ' ', m.last_name) AS full_name,
               m.email,
               m.phone,
               t.tier_name,
               t.discount_percent,
               t.max_rental_days
        FROM members m
        JOIN membership_types t ON t.tier_id = m.tier_id
        ORDER BY m.member_id
        """
    )


@st.cache_data(ttl=15, show_spinner=False)
def load_staff() -> pd.DataFrame:
    return query_df(
        """
        SELECT staff_id, CONCAT(first_name, ' ', last_name) AS full_name
        FROM staff
        ORDER BY staff_id
        """
    )


@st.cache_data(ttl=15, show_spinner=False)
def load_suppliers() -> pd.DataFrame:
    return query_df(
        "SELECT supplier_id, supplier_name, contact_phone FROM suppliers ORDER BY supplier_id"
    )


@st.cache_data(ttl=15, show_spinner=False)
def load_catalog_items() -> pd.DataFrame:
    return query_df(
        """
        SELECT catalog_id, item_name, total_owned, total_available
        FROM equipment_catalog
        ORDER BY item_name
        """
    )


@st.cache_data(ttl=15, show_spinner=False)
def load_conditions() -> pd.DataFrame:
    return query_df("SELECT condition_id, condition_label FROM item_conditions ORDER BY condition_id")


@st.cache_data(ttl=15, show_spinner=False)
def load_available_items() -> pd.DataFrame:
    """Barcodes that are currently Available (condition_id = 1) for checkout."""
    return query_df(
        """
        SELECT i.barcode, c.item_name
        FROM inventory_items i
        JOIN equipment_catalog c ON c.catalog_id = i.catalog_id
        WHERE i.condition_id = 1
        ORDER BY c.item_name, i.barcode
        """
    )


@st.cache_data(ttl=15, show_spinner=False)
def load_active_line_items() -> pd.DataFrame:
    """Rental line items that have not yet been returned."""
    return query_df(
        """
        SELECT
            rli.line_id,
            rli.rental_id,
            rli.barcode,
            c.item_name,
            m.first_name || ' ' || m.last_name AS member_name,
            rli.expected_return_date
        FROM rental_line_items rli
        JOIN rentals r            ON r.rental_id = rli.rental_id
        JOIN members m            ON m.member_id = r.member_id
        JOIN inventory_items i    ON i.barcode = rli.barcode
        JOIN equipment_catalog c  ON c.catalog_id = i.catalog_id
        WHERE rli.actual_return_timestamp IS NULL
        ORDER BY rli.line_id
        """
    )


@st.cache_data(ttl=15, show_spinner=False)
def load_purchase_orders() -> pd.DataFrame:
    return query_df(
        """
        SELECT po.order_id, s.supplier_name, po.order_date,
               po.total_cost, po.order_status
        FROM purchase_orders po
        JOIN suppliers s ON s.supplier_id = po.supplier_id
        ORDER BY po.order_id DESC
        LIMIT 50
        """
    )


@st.cache_data(ttl=30, show_spinner=False)
def load_tables() -> pd.DataFrame:
    """The 14 user tables in the public schema (drives the Schema Inspector)."""
    return query_df(
        """
        SELECT tablename AS "Table"
        FROM pg_tables
        WHERE schemaname = 'public'
        ORDER BY tablename
        """
    )


@st.cache_data(ttl=30, show_spinner=False)
def load_functions() -> pd.DataFrame:
    """All stored procedures (prokind='p') and functions (prokind='f')."""
    return query_df(
        """
        SELECT p.proname AS "Function",
               pg_get_function_arguments(p.oid) AS "Arguments",
               pg_get_function_result(p.oid) AS "Returns",
               CASE p.prokind WHEN 'f' THEN 'FUNCTION' WHEN 'p' THEN 'PROCEDURE' END AS "Kind"
        FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public' AND p.prokind IN ('f', 'p')
        ORDER BY p.proname, pg_get_function_arguments(p.oid)
        """
    )


@st.cache_data(ttl=30, show_spinner=False)
def load_function_definition(name: str, args: str) -> str:
    """Full CREATE OR REPLACE ... source of a specific function/procedure."""
    return query_scalar(
        """
        SELECT pg_get_functiondef(p.oid)
        FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public' AND p.proname = %s
          AND pg_get_function_arguments(p.oid) = %s
        """,
        (name, args),
    )


@st.cache_data(ttl=30, show_spinner=False)
def load_triggers() -> pd.DataFrame:
    return query_df(
        """
        SELECT tg.tgname AS "Trigger",
               pg_get_triggerdef(tg.oid) AS "Definition"
        FROM pg_trigger tg
        WHERE NOT tg.tgisinternal
        ORDER BY tg.tgname
        """
    )


@st.cache_data(ttl=30, show_spinner=False)
def load_custom_indexes() -> pd.DataFrame:
    """The hand-written performance indexes (excludes PK/UNIQUE auto-indexes)."""
    return query_df(
        """
        SELECT indexname AS "Index", indexdef AS "Definition"
        FROM pg_indexes
        WHERE schemaname = 'public'
          AND indexname NOT LIKE '%%_pkey'
          AND indexname NOT LIKE '%%_key'
        ORDER BY indexname
        """
    )


@st.cache_data(ttl=30, show_spinner=False)
def load_all_indexes() -> pd.DataFrame:
    return query_df(
        """
        SELECT indexname AS "Index", tablename AS "Table", indexdef AS "Definition"
        FROM pg_indexes
        WHERE schemaname = 'public'
        ORDER BY indexname
        """
    )


@st.cache_data(ttl=30, show_spinner=False)
def load_reference_ids() -> dict:
    """Live sample IDs used by the trigger demos and the exception-testing suite.

    Read-only lookups; each key is None when the relevant table is empty.
    """
    refs = {
        "member_id": None, "staff_id": None, "barcode": None, "any_barcode": None,
        "serial": None, "rental_id": None, "catalog_id": None,
    }
    try:
        member = query_df("SELECT member_id FROM members ORDER BY member_id LIMIT 1")
        if not member.empty:
            refs["member_id"] = int(member.iloc[0]["member_id"])
        staff = query_df("SELECT staff_id FROM staff ORDER BY staff_id LIMIT 1")
        if not staff.empty:
            refs["staff_id"] = int(staff.iloc[0]["staff_id"])
        avail = query_df(
            "SELECT barcode FROM inventory_items WHERE condition_id = 1 LIMIT 1"
        )
        if not avail.empty:
            refs["barcode"] = str(avail.iloc[0]["barcode"])
        any_item = query_df(
            "SELECT barcode FROM inventory_items ORDER BY barcode LIMIT 1"
        )
        if not any_item.empty:
            refs["any_barcode"] = str(any_item.iloc[0]["barcode"])
        serial = query_df(
            "SELECT serial_number FROM inventory_items ORDER BY serial_number LIMIT 1"
        )
        if not serial.empty:
            refs["serial"] = str(serial.iloc[0]["serial_number"])
        rental = query_df(
            "SELECT rental_id FROM rentals ORDER BY rental_id DESC LIMIT 1"
        )
        if not rental.empty:
            refs["rental_id"] = int(rental.iloc[0]["rental_id"])
        cat = query_df(
            "SELECT catalog_id FROM equipment_catalog ORDER BY catalog_id LIMIT 1"
        )
        if not cat.empty:
            refs["catalog_id"] = int(cat.iloc[0]["catalog_id"])
    except DBServerError:
        pass  # caller displays a graceful message
    return refs


def refresh_all() -> None:
    """Invalidate every cached query so the next rerun shows live DB state."""
    for loader in (
        load_metrics,
        load_condition_breakdown,
        load_inventory_grid,
        load_catalog_balances,
        load_members,
        load_staff,
        load_suppliers,
        load_catalog_items,
        load_conditions,
        load_available_items,
        load_active_line_items,
        load_purchase_orders,
        load_tables,
        load_functions,
        load_function_definition,
        load_triggers,
        load_custom_indexes,
        load_all_indexes,
        load_reference_ids,
    ):
        loader.clear()


# ----------------------------------------------------------------------------
# 5. SHARED WIDGET HELPERS
# ----------------------------------------------------------------------------
def id_options(df: pd.DataFrame, label_col: str, id_col: str) -> dict:
    """Turn a lookup DataFrame into {display_label: id} for st.selectbox."""
    return {f"#{row[id_col]} — {row[label_col]}": row[id_col] for _, row in df.iterrows()}


def show_notices(notices: list) -> None:
    """Render backend RAISE NOTICE / trigger messages as code blocks."""
    notices = [n.strip() for n in notices if n.strip()]
    if notices:
        st.caption("Backend notices captured (proves the trigger/procedure fired):")
        for notice in notices:
            st.code(notice, language="text")


def db_error_box(exc) -> None:
    """Render a database error (incl. PL/pgSQL RAISE EXCEPTION) as st.error."""
    code = getattr(exc, "pgcode", "") or ""
    suffix = f" — SQLSTATE {code}" if code else ""
    st.error(f"Database operation failed{suffix}: {exc}")


# SQLSTATE codes the exception suite intentionally triggers (viva reference).
SQLSTATE_MEANING = {
    "P0001": "raise_exception — a PL/pgSQL RAISE EXCEPTION was raised",
    "23505": "unique_violation — duplicate key value",
    "23503": "foreign_key_violation — referenced row does not exist",
    "23514": "check_violation — a CHECK constraint was violated",
}


def render_test_result(res: dict, expectation: str, success_if_ok: bool = False) -> None:
    """Render the outcome of execute_test() (trigger tests / exception suite)."""
    if res["ok"]:
        if success_if_ok:
            st.success(":material/check_circle: Executed successfully — transaction rolled back safely.")
        else:
            st.error(":material/warning: No exception raised — unexpected (the constraint/trigger may be disabled).")
    else:
        st.error(f":material/block: Exception captured — SQLSTATE **{res['pgcode']}**")
        st.code(res["error"], language="text")
    if res["notices"]:
        show_notices(res["notices"])
    if res["results"] is not None:
        st.dataframe(res["results"], hide_index=True)
    st.caption(f"Expectation: {expectation}")


def exception_preset(key, title, description, statements, expected, enabled=True) -> None:
    """A self-contained 'press to break it' card for the exception-testing suite."""
    with st.container(border=True):
        st.markdown(f"**{title}**")
        st.caption(description)
        if not enabled:
            st.warning("Required reference data is missing — seed the database first.")
        else:
            if st.button(":material/experiment: Run test", key=f"exc_{key}", type="secondary", width="stretch"):
                render_test_result(
                    execute_test(statements),
                    f"Expecting SQLSTATE **{expected}** ({SQLSTATE_MEANING.get(expected, '')})",
                )
        st.caption(f"Expected SQLSTATE: `{expected}` — {SQLSTATE_MEANING.get(expected, '')}")


# ----------------------------------------------------------------------------
# 5b. AUTHENTICATION & ROLE-BASED SESSION MANAGEMENT
# ----------------------------------------------------------------------------
# Demo staff seeded on first run (self-healing after `pg_restore -c` resets).
# The plaintext passwords below are ONLY used to generate PBKDF2 hashes; the
# database stores the hashes, never the passwords.
DEMO_USERS = [
    {"staff_id": 1, "first_name": "Amara", "last_name": "Silva",
     "email": "amara.s@sports.com", "role_id": 1, "password": "counter123"},
    {"staff_id": 2, "first_name": "Kasun", "last_name": "Fernando",
     "email": "kasun.f@sports.com", "role_id": 3, "password": "admin123"},
    {"staff_id": 3, "first_name": "Dilini", "last_name": "Jayasinghe",
     "email": "dilini.j@sports.com", "role_id": 2, "password": "clerk123"},
]

_PBKDF2_ITERATIONS = 200_000

AUTH_SESSION_KEYS = ("logged_in", "user_id", "user_name", "user_role", "user_email")

# ----------------------------------------------------------------------------
# 5c. ROLE -> DUTY PERMISSION MAP
# ----------------------------------------------------------------------------
# Each role gets a unique set of pages and, for the CRUD console, a unique
# subset of tables. Role titles come from the `staff_roles` table; the rules
# themselves live here so they survive a `pg_restore -c` reset.
PAGE_LABELS = {
    "dashboard": "Dashboard & Live Inventory",
    "checkout": "Member Checkout",
    "return": "Return & Late Fine",
    "restock": "Restock & Asset Acquisition",
    "schema": "Data Schema Inspector",
    "functions": "Functions, Triggers & Integrity",
    "crud": "Data Management (CRUD)",
}

PAGE_DUTIES = {
    "dashboard": "fleet overview (totals, on-loan, under repair)",
    "checkout": "lend an item to a member",
    "return": "receive items back; late fees apply",
    "restock": "add stock/suppliers and acquire assets",
    "schema": "browse all 14 tables live (read-only)",
    "functions": "safe, rolled-back trigger & integrity demos",
    "crud": "create/update/delete rows in your assigned tables",
}

# Stock / asset domain owned by the Inventory Clerk.
STOCK_TABLES = frozenset({
    "equipment_catalog", "inventory_items", "maintenance_logs",
    "suppliers", "purchase_orders", "brands",
    "equipment_categories", "item_conditions",
})

ROLE_PERMISSIONS = {
    "Front Desk Officer": {
        "pages": frozenset({"dashboard", "checkout", "return"}),
        "crud_tables": frozenset(),
        "duties": "Member checkout, item returns and late-fine collection.",
    },
    "Inventory Clerk": {
        "pages": frozenset({"dashboard", "restock", "schema", "crud"}),
        "crud_tables": STOCK_TABLES,
        "duties": "Stock intake, supplier restock, asset acquisition, repair tracking.",
    },
    "System Administrator": {
        "pages": frozenset({"dashboard", "schema", "functions", "crud"}),
        "crud_tables": None,  # None = every table in CRUD_TABLES
        "duties": "Data governance, staff & password management, trigger/integrity audits.",
    },
}


def current_role() -> str:
    return str(st.session_state.get("user_role", ""))


def is_admin() -> bool:
    return current_role() == "System Administrator"


def current_perms() -> dict:
    return ROLE_PERMISSIONS.get(
        current_role(), {"pages": frozenset(), "crud_tables": frozenset(), "duties": ""}
    )


def can(page: str) -> bool:
    """True when the signed-in role may open `page`."""
    return page in current_perms()["pages"]


def require_page(page: str) -> None:
    """Defense-in-depth guard: hides what navigation already filters out."""
    if can(page):
        return
    perms = current_perms()
    st.error(
        f":material/lock: **Access denied.** The *{PAGE_LABELS.get(page, page)}* page "
        f"is not part of the **{current_role() or 'unknown'}** duties."
    )
    if perms["duties"]:
        st.caption(f"Your role is responsible for: {perms['duties']}")
    st.stop()


def crud_tables_for_role() -> list:
    """Tables the current role may manage in the CRUD console."""
    allowed = current_perms()["crud_tables"]
    if allowed is None:
        return list(CRUD_TABLES)
    return [t for t in CRUD_TABLES if t in allowed]


def hash_password(password: str) -> str:
    """Hash a password with PBKDF2-HMAC-SHA256 + a fresh random salt."""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS
    )
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time comparison of a plaintext password against a stored hash."""
    try:
        scheme, iterations, salt_hex, digest_hex = stored.split("$")
        if scheme != "pbkdf2_sha256":
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, int(iterations)
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def authenticate(email: str, password: str) -> dict | None:
    """Validate credentials against the `staff` table (parameterized query).

    Returns a dict of session fields on success, or None. A single failure path
    is used so the response never reveals whether the email or password was
    wrong.
    """
    row = query_df(
        """
        SELECT s.staff_id,
               CONCAT(s.first_name, ' ', s.last_name) AS full_name,
               s.email,
               r.role_title,
               s.password_hash
        FROM staff s
        JOIN staff_roles r ON r.role_id = s.role_id
        WHERE s.email = %s
        """,
        (email,),
    )
    if row.empty:
        return None
    rec = row.iloc[0]
    if not rec["password_hash"] or not verify_password(password, rec["password_hash"]):
        return None
    return {
        "user_id": int(rec["staff_id"]),
        "user_name": str(rec["full_name"]),
        "user_role": str(rec["role_title"]),
        "user_email": str(rec["email"]),
    }


def do_login(user: dict) -> None:
    """Persist the authenticated user in st.session_state."""
    for key, value in user.items():
        st.session_state[key] = value
    st.session_state["logged_in"] = True


def do_logout() -> None:
    """Clear every authentication-related session key."""
    for key in AUTH_SESSION_KEYS:
        st.session_state.pop(key, None)


def is_logged_in() -> bool:
    return bool(st.session_state.get("logged_in"))


def ensure_auth_schema() -> None:
    """Idempotent self-heal: add `staff.password_hash` and seed demo users.

    Safe to call on every launch; repairs the schema even after a
    `pg_restore -c` reset restores the original dump.
    """
    conn, cur = None, None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'staff'
              AND column_name = 'password_hash'
            """
        )
        if cur.fetchone() is None:
            cur.execute("ALTER TABLE staff ADD COLUMN password_hash TEXT")
        for u in DEMO_USERS:
            cur.execute(
                """
                INSERT INTO staff (staff_id, first_name, last_name, email, role_id, hire_date, password_hash)
                VALUES (%s, %s, %s, %s, %s, CURRENT_DATE, %s)
                ON CONFLICT (staff_id) DO UPDATE
                SET first_name = EXCLUDED.first_name,
                    last_name = EXCLUDED.last_name,
                    email = EXCLUDED.email,
                    password_hash = EXCLUDED.password_hash
                """,
                (u["staff_id"], u["first_name"], u["last_name"], u["email"],
                 u["role_id"], hash_password(u["password"])),
            )
        conn.commit()
    except psycopg2.Error as exc:
        raise DBServerError(friendly_error(exc), sqlstate_code(exc)) from exc
    finally:
        if cur is not None:
            cur.close()
        if conn is not None:
            conn.close()


def render_login() -> None:
    """Centered login gate shown when no user is authenticated."""
    st.markdown(
        """
        <style>
        div[data-testid="stMainBlockContainer"] {
            max-width: 560px;
            margin: 0 auto;
            padding-top: 2.5rem;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    with st.container(border=True):
        st.markdown("### :material/sports_and_outdoors: Sport Equipment Management")
        st.caption("Sign in with your staff account to manage the inventory.")

        with st.form("login_form", border=False):
            email = st.text_input("Email", placeholder="you@club.com")
            password = st.text_input("Password", type="password", placeholder="••••••••")
            submitted = st.form_submit_button(
                "Log In", type="primary", icon=":material/login:", width="stretch"
            )

        if submitted:
            if not email or not password:
                st.warning("Please enter both email and password.")
            else:
                try:
                    user = authenticate(email, password)
                except DBServerError as exc:
                    db_error_box(exc)
                else:
                    if user is None:
                        st.error(":material/block: Invalid credentials")
                    else:
                        do_login(user)
                        st.rerun()

        st.divider()
        with st.expander("Viva Quick Login / Demo Mode"):
            st.caption("One-click role switching for the examiner (no typing required).")
            try:
                staff_df = query_df(
                    """
                    SELECT s.staff_id,
                           CONCAT(s.first_name, ' ', s.last_name) AS full_name,
                           r.role_title, s.email
                    FROM staff s
                    JOIN staff_roles r ON r.role_id = s.role_id
                    ORDER BY s.staff_id
                    """
                )
            except DBServerError as exc:
                db_error_box(exc)
                staff_df = None
            if staff_df is not None:
                for _, row in staff_df.iterrows():
                    if st.button(
                        f"Staff {row['staff_id']} — {row['full_name']} ({row['role_title']})",
                        key=f"quick_login_{int(row['staff_id'])}",
                        type="secondary",
                        width="stretch",
                    ):
                        do_login(
                            {
                                "user_id": int(row["staff_id"]),
                                "user_name": str(row["full_name"]),
                                "user_role": str(row["role_title"]),
                                "user_email": str(row["email"]),
                            }
                        )
                        st.rerun()
                st.caption("Demo passwords: `counter123` · `admin123` · `clerk123`")


# ----------------------------------------------------------------------------
# 6. PAGE 1 — DASHBOARD & LIVE INVENTORY TRACKER
# ----------------------------------------------------------------------------
def page_dashboard() -> None:
    require_page("dashboard")
    st.title(":material/inventory_2: Dashboard & Live Inventory Tracker")
    st.caption(
        "At-a-glance club operations: how much equipment exists, what is on loan, "
        "and what is out of service. All numbers are read live from PostgreSQL."
    )

    try:
        total, available, rented, under_repair = load_metrics()
        condition_df = load_condition_breakdown()
        inventory_df = load_inventory_grid()
        catalog_df = load_catalog_balances()
    except DBServerError as exc:
        db_error_box(exc)
        st.info("Check that PostgreSQL is running and that `PGPASSWORD` is set in the terminal.")
        return

    # KPI metric cards
    with st.container(horizontal=True):
        st.metric("Total Equipment", f"{total:,}", border=True)
        st.metric("Available Items", f"{available:,}", border=True)
        st.metric("On Loan (Rented)", f"{rented:,}", border=True)
        st.metric("Under Repair", f"{under_repair:,}", border=True)

    # Quick actions — jump straight to the workflows this role is allowed to use
    quick_actions = [
        ("checkout", "Check out item", ":material/add_shopping_cart:"),
        ("return", "Return & fine", ":material/assignment_return:"),
        ("restock", "Restock / acquire", ":material/warehouse:"),
        ("schema", "Schema & data", ":material/table_view:"),
        ("crud", "Manage data", ":material/edit_note:"),
    ]
    available_actions = [a for a in quick_actions if a[0] in _NAV_PAGES]
    if available_actions:
        with st.container(border=True):
            st.markdown("**Quick Actions**")
            qc = st.columns(len(available_actions))
            for col, (key, label, icon) in zip(qc, available_actions):
                col.page_link(_NAV_PAGES[key], label=label, icon=icon)

    with st.expander(":material/school: How this system works (operating guide)", expanded=False):
        st.markdown(
            """
            A typical day at the club follows **four steps**, each backed by a page:

            1. **Check out equipment** → *Member Checkout* page. Pick the member and an
               *Available* item; the stored procedure `proc_member_rental_checkout`
               creates the rental and a trigger flips the item to **Rented**.
            2. **Return & late fine** → *Return & Late Fine* page. Returning an item
               stamps the return time and the backend computes any late fine
               (US$2.50/day) automatically.
            3. **Restock & acquire** → *Restock & Asset Acquisition* page. Buy stock from
               a supplier or acquire a brand-new asset; the catalog counts update
               automatically via the inventory triggers.
            4. **Maintain** → items in poor condition are tracked as **Under Repair**.
               Use the **CRUD console** (Admin/Clerk only) to inspect repair history and
               update item conditions back to Available.

            **Why the numbers always match:** `equipment_catalog.total_owned` and
            `total_available` are maintained by three AFTER triggers on `inventory_items`
            (`trg_after_inventory_insert` / `_condition_change` / `_delete`), not by app
            code — so no matter how an item changes (checkout, return, restock, repair),
            the dashboard balances stay correct.
            """
        )

    st.divider()

    # Live inventory grid (condition highlighting)
    header, refresh_col = st.columns([4, 1], vertical_alignment="bottom")
    header.subheader("Live Inventory Grid")
    if refresh_col.button(":material/refresh: Refresh", key="dash_refresh", type="tertiary"):
        refresh_all()
        st.rerun()

    st.caption(
        "**Condition** is maintained automatically by backend triggers: checkout flips an "
        "item to Rented (`trg_after_rent_item_checkout`), and the catalog balances are "
        "re-balanced by the inventory triggers (`trg_after_inventory_*`). Run a checkout "
        "and hit Refresh to see the state flip live."
    )

    def _style_condition(value: str) -> str:
        if value == "Available":
            return "color: #0B6B4F; background-color: #E6F4EF; font-weight: 600;"
        if value == "Rented":
            return "color: #9F1239; background-color: #FCE8EE; font-weight: 600;"
        if value == "Under Repair":
            return "color: #92400E; background-color: #FDF1DC; font-weight: 600;"
        if value == "Decommissioned":
            return "color: #4B5563; background-color: #EEF1F2; font-weight: 600;"
        return ""

    styled = inventory_df.style.map(_style_condition, subset=["condition_label"])
    st.dataframe(
        styled,
        column_config={
            "barcode": st.column_config.TextColumn("Barcode"),
            "item_name": st.column_config.TextColumn("Item"),
            "serial_number": st.column_config.TextColumn("Serial Number"),
            "condition_label": st.column_config.TextColumn("Condition"),
        },
        hide_index=True,
        height=360,
    )

    st.divider()

    # Catalog availability balances maintained by the triggers
    with st.container(border=True):
        st.subheader("Catalog Availability Balances")
        st.caption(
            "`equipment_catalog.total_owned` / `total_available` are maintained by the "
            "AFTER triggers on `inventory_items` (`fn_trigger_sync_catalog_availability`), "
            "so checkout, return, restock and repairs all keep these totals accurate."
        )
        st.bar_chart(catalog_df, x="Item", y=["owned", "available"])

    # Per-condition fleet status
    with st.container(border=True):
        st.subheader("Fleet Status by Condition")
        st.caption(
            "How the whole inventory is split across the four item conditions — Available, "
            "Rented, Under Repair and Decommissioned."
        )
        st.bar_chart(condition_df, x="Condition", y="Items")


# ----------------------------------------------------------------------------
# 7. PAGE 2 — MEMBER CHECKOUT (STORED PROCEDURE + TRIGGER DEMO)
# ----------------------------------------------------------------------------
def page_checkout() -> None:
    require_page("checkout")
    st.title(":material/add_shopping_cart: Member Checkout")
    st.caption(
        "Calls **`proc_member_rental_checkout(member, staff, barcode)`**. Inside the "
        "backend, triggers **`trg_check_insert_return_date`** (validates the due date) "
        "and **`trg_after_rent_item_checkout`** (flips the item to Rented) fire "
        "automatically — the condition UPDATE then re-balances the catalog via "
        "**`trg_after_inventory_condition_change`**."
    )

    try:
        members = load_members()
        available = load_available_items()
        conditions = load_conditions()
    except DBServerError as exc:
        db_error_box(exc)
        return

    if members.empty:
        st.error("No members found in the database. Please seed the `members` table first.")
        st.stop()

    # Member labels carry the tier benefits so the clerk can see them at a glance.
    member_opts = {
        f"#{row['member_id']} — {row['full_name']}  ·  {row['tier_name']} (max {int(row['max_rental_days'])} days)": row["member_id"]
        for _, row in members.iterrows()
    }
    member_by_id = {int(row["member_id"]): row for _, row in members.iterrows()}

    with st.form("checkout_form", border=True):
        col_m, col_b = st.columns(2)

        member_label = col_m.selectbox(
            "Member", list(member_opts), placeholder="Select a member..."
        )

        if not available.empty:
            barcode_opts = {
                f"{row['barcode']}  —  {row['item_name']}": row["barcode"]
                for _, row in available.iterrows()
            }
            barcode_label = col_b.selectbox(
                "Item barcode (Available only)", list(barcode_opts),
                placeholder="Select an available item...",
            )
            barcode = barcode_opts[barcode_label]
        else:
            barcode = col_b.text_input(
                "Item barcode (no available items — type manually)",
                placeholder="e.g. SKU-0001",
            )

        st.caption(
            f"Handling staff: **{st.session_state['user_name']}** "
            f"({st.session_state['user_role']}) — `staff_id {st.session_state['user_id']}`"
        )

        submitted = st.form_submit_button(
            "Execute Checkout", type="primary", icon=":material/done_all:", width="stretch"
        )

    if submitted:
        if not member_label or not barcode:
            st.warning("Please fill in all fields before executing the checkout.")
            st.stop()

        try:
            rowcounts, notices = call_procedure(
                "proc_member_rental_checkout",
                (member_opts[member_label], st.session_state["user_id"], barcode),
            )
            rental_id = extract_rental_id(notices)
            refresh_all()

            st.success(
                f"Checkout successful! Rental created for **{barcode}**. "
                f"Generated **Rental ID: {rental_id}**." if rental_id
                else f"Checkout successful for **{barcode}**."
            )
            show_notices(notices)

            # Member tier benefits card — surfaces membership_types in the live flow.
            member_row = member_by_id.get(member_opts[member_label])
            if member_row is not None:
                with st.container(border=True):
                    st.markdown(f"**Member tier — {member_row['tier_name']}**")
                    tc = st.columns(4)
                    tc[0].metric("Discount", f"{float(member_row['discount_percent']):.0f}%", border=True)
                    tc[1].metric("Max rental days", str(member_row["max_rental_days"]), border=True)
                    tc[2].metric("Member email", member_row["email"], border=True)
                    tc[3].metric("Member phone", member_row["phone"] or "—", border=True)
                    st.caption(
                        "The rental terms shown here come from `membership_types` — the backend "
                        "enforces the club's fixed due date of checkout + 7 days inside the "
                        "stored procedure."
                    )

            # Live "Before / After" switcher proving trg_after_rent_item_checkout fired.
            try:
                after_df = query_df(
                    "SELECT i.condition_id, c.total_available "
                    "FROM inventory_items i "
                    "JOIN equipment_catalog c ON c.catalog_id = i.catalog_id "
                    "WHERE i.barcode = %s",
                    (barcode,),
                )
                after_condition = int(after_df.iloc[0]["condition_id"])
                after_available = int(after_df.iloc[0]["total_available"])
            except DBServerError:
                after_condition, after_available = 2, "?"

            with st.container(border=True):
                st.markdown("**Trigger state switcher — `trg_after_rent_item_checkout`**")
                ba = st.columns(2)
                ba[0].metric("Condition BEFORE checkout", "1 — Available", border=True)
                ba[1].metric(
                    "Condition AFTER checkout",
                    f"{after_condition} — Rented" if after_condition == 2 else f"{after_condition}",
                    border=True,
                )
                st.caption(
                    "BEFORE: the barcode was selected from the **Available-only** dropdown, so its "
                    "`condition_id` was 1. AFTER: the trigger fired on INSERT and flipped it to 2, "
                    f"also decrementing the catalog's `total_available` to **{after_available}**. "
                    "Refresh the Dashboard grid to confirm."
                )
        except DBServerError as exc:
            db_error_box(exc)
            st.caption(
                "Expected failure modes (PL/pgSQL `RAISE EXCEPTION` handled gracefully): "
                "unknown barcode, item already rented, invalid member/staff FK, or a "
                "duplicate serial number."
            )

    st.divider()
    st.caption(
        f"Currently available items: **{len(available):,}** | Conditions: "
        + ", ".join(f"{r['condition_label']} ({r['condition_id']})" for _, r in conditions.iterrows())
    )


# ----------------------------------------------------------------------------
# 8. PAGE 3 — EQUIPMENT RETURN & LATE FINE ENGINE
# ----------------------------------------------------------------------------
def page_return() -> None:
    require_page("return")
    st.title(":material/assignment_return: Equipment Return & Late Fine Engine")
    st.caption(
        "Calculates the late fine with scalar function **`fn_calculate_late_fine`** "
        "($2.50/day), then processes the return via **`proc_item_return(line_id, fine)`**. "
        "The backend trigger **`trg_auto_calculate_fine`** fires on the update and raises a "
        "NOTICE with the fine. The app then resets the item to **Available (1)** so it can "
        "be rented again."
    )

    try:
        active = load_active_line_items()
    except DBServerError as exc:
        db_error_box(exc)
        return

    if active.empty:
        st.info(
            ":material/info: No items are currently out on rental. "
            "Check out an item on the **Member Checkout** page first."
        )
        return

    line_opts = {
        f"#{row['line_id']}  —  {row['barcode']}  ({row['item_name']})": row["line_id"]
        for _, row in active.iterrows()
    }

    line_label = st.selectbox("Active rental line item", list(line_opts), placeholder="Select a line item...")
    if not line_label:
        st.stop()

    line_id = line_opts[line_label]
    selected = active.loc[active["line_id"] == line_id].iloc[0]

    # Detail card for the selected line item
    with st.container(border=True):
        det = st.columns(4)
        det[0].metric("Rental ID", int(selected["rental_id"]))
        det[1].metric("Barcode / Item", f"{selected['barcode']} · {selected['item_name']}")
        det[2].metric("Member", selected["member_name"])
        det[3].metric("Expected return date", str(selected["expected_return_date"]))

    # Fine calculation (scalar function demo)
    try:
        fine = float(query_scalar("SELECT fn_calculate_late_fine(%s)", (line_id,)))
    except DBServerError as exc:
        db_error_box(exc)
        return

    fine_col, proc_col = st.columns(2)
    fine_col.metric(
        "Late fine — fn_calculate_late_fine()",
        f"${fine:,.2f}",
        border=True,
        delta="on time" if fine <= 0 else "LATE",
        delta_color="off" if fine <= 0 else "inverse",
    )
    proc_col.metric("Fine rate", "$2.50 / day late", border=True)

    st.caption(
        "Calculate is a live `SELECT fn_calculate_late_fine(line_id)` — rerun it any time "
        "by reselecting the line item. When the return is processed, `fn_trigger_calculate_fine` "
        "(wired to `trg_auto_calculate_fine`) fires on the UPDATE and raises the fine NOTICE below."
    )

    st.info(
        f":material/person: **Processed by:** {st.session_state['user_name']} "
        f"({st.session_state['user_role']})"
    )

    if st.button("Process Return", type="primary", icon=":material/check_circle:", width="stretch"):
        try:
            # 1) Stamp the return timestamp + add any late fine (backend procedure
            #    + trg_auto_calculate_fine trigger fire here).
            # 2) Reset the physical item back to Available so it can be rented again.
            rowcounts, notices = execute_sequence(
                [
                    ("CALL proc_item_return(%s, %s)", (line_id, fine)),
                    (
                        "UPDATE inventory_items SET condition_id = 1 "
                        "WHERE barcode = (SELECT barcode FROM rental_line_items WHERE line_id = %s)",
                        (line_id,),
                    ),
                ]
            )
            refresh_all()

            fine_note = f" A late fine of **${fine:,.2f}** was added to the rental's `total_billed`." if fine > 0 else " The item was returned on time — no fine applied."
            st.success(f"Return processed for line item **{int(line_id)}**.{fine_note}")
            st.info(
                ":material/visibility: The item's `condition_id` was reset back to "
                "**1 (Available)**. Refresh the Dashboard grid to confirm."
            )
            show_notices(notices)
        except DBServerError as exc:
            db_error_box(exc)


# ----------------------------------------------------------------------------
# 9. PAGE 4 — SUPPLIER RESTOCK & ASSET ACQUISITION
# ----------------------------------------------------------------------------
def _condition_selector(container, key_suffix: str) -> int:
    """Shared condition selectbox (1 = Available, 2 = Rented)."""
    try:
        conditions = load_conditions()
    except DBServerError as exc:
        db_error_box(exc)
        return 1
    opts = {
        f"{row['condition_label']} ({row['condition_id']})": row["condition_id"]
        for _, row in conditions.iterrows()
    }
    label = container.selectbox(
        "Item condition", list(opts), index=0, key=f"cond_{key_suffix}"
    )
    return opts[label]


def page_supplier() -> None:
    require_page("restock")
    st.title(":material/warehouse: Supplier Restock & Asset Acquisition")
    st.caption(
        "Two stored procedures operating on multiple tables inside one transaction: "
        "**`proc_supplier_restock`** (creates a `purchase_orders` row + inserts an "
        "`inventory_items` row) and **`proc_acquire_new_asset`** (inserts an item). "
        "Catalog balances update automatically through **`trg_after_inventory_insert`**."
    )

    try:
        suppliers = load_suppliers()
        catalog = load_catalog_items()
    except DBServerError as exc:
        db_error_box(exc)
        return

    if suppliers.empty or catalog.empty:
        st.error("Seed `suppliers` and `equipment_catalog` before using this page.")
        st.stop()

    supplier_opts = {
        f"#{row['supplier_id']} — {row['supplier_name']}  ({row['contact_phone'] or '—'})": row["supplier_id"]
        for _, row in suppliers.iterrows()
    }
    catalog_opts = {
        f"{row['item_name']}  (owned: {row['total_owned']} / available: {row['total_available']})": row["catalog_id"]
        for _, row in catalog.iterrows()
    }

    tab_restock, tab_acquire, tab_overload = st.tabs(
        [
            ":material/package_2: Supplier Restock",
            ":material/bolt: Acquire New Asset",
            ":material/call_split: Overloaded proc_register_supplier_restock",
        ]
    )

    # ---- Tab 1: proc_supplier_restock -------------------------------
    with tab_restock:
        with st.form("restock_form", border=True):
            st.subheader("proc_supplier_restock(supplier, cost, barcode, catalog, condition, serial)")
            c1, c2 = st.columns(2)
            supplier_label = c1.selectbox("Supplier", list(supplier_opts), placeholder="Select supplier...")
            restock_cost = c2.number_input("Total cost (USD)", min_value=0.0, value=0.0, step=5.0, format="%.2f")
            new_barcode = c1.text_input("New barcode", placeholder="e.g. SKU-1001")
            catalog_label = c2.selectbox("Catalog item", list(catalog_opts), placeholder="Select item...")
            condition_id = _condition_selector(c1, "restock")
            serial_number = c2.text_input("Serial number (must be unique)", placeholder="e.g. SN-2026-0001")

            submitted_restock = st.form_submit_button(
                "Execute Restock", type="primary", icon=":material/local_shipping:", width="stretch"
            )

        if submitted_restock:
            if not (supplier_label and new_barcode and catalog_label and serial_number):
                st.warning("Barcode, serial number, supplier and catalog item are all required.")
            else:
                try:
                    rowcounts, notices = call_procedure(
                        "proc_supplier_restock",
                        (
                            supplier_opts[supplier_label],
                            restock_cost,
                            new_barcode,
                            catalog_opts[catalog_label],
                            condition_id,
                            serial_number,
                        ),
                    )
                    refresh_all()
                    st.success(f"Restock recorded: **{new_barcode}** added to inventory under a new purchase order.")
                    show_notices(notices)
                except DBServerError as exc:
                    db_error_box(exc)
                    st.caption("Note: `serial_number` and `barcode` must both be unique — duplicates are rejected by integrity constraints.")

    # ---- Tab 2: proc_acquire_new_asset ------------------------------
    with tab_acquire:
        with st.form("acquire_form", border=True):
            st.subheader("proc_acquire_new_asset(barcode, catalog, condition, serial)")
            c1, c2 = st.columns(2)
            asset_barcode = c1.text_input("New barcode", placeholder="e.g. SKU-2001", key="asset_barcode")
            asset_catalog = c2.selectbox("Catalog item", list(catalog_opts), placeholder="Select item...", key="asset_catalog")
            asset_condition = _condition_selector(c1, "acquire")
            asset_serial = c2.text_input("Serial number (must be unique)", placeholder="e.g. SN-2026-0101", key="asset_serial")

            submitted_acquire = st.form_submit_button(
                "Acquire Asset", type="primary", icon=":material/handyman:", width="stretch"
            )

        if submitted_acquire:
            if not (asset_barcode and asset_catalog and asset_serial):
                st.warning("Barcode, catalog item and serial number are all required.")
            else:
                try:
                    rowcounts, notices = call_procedure(
                        "proc_acquire_new_asset",
                        (
                            asset_barcode,
                            catalog_opts[asset_catalog],
                            asset_condition,
                            asset_serial,
                        ),
                    )
                    refresh_all()
                    st.success(f"Asset **{asset_barcode}** recorded and catalog totals incremented.")
                    show_notices(notices)
                except DBServerError as exc:
                    db_error_box(exc)

    # ---- Tab 3: overloaded proc_register_supplier_restock ------------
    with tab_overload:
        st.caption(
            "PostgreSQL supports **procedure overloading**: multiple procedures share a name "
            "but differ in their argument lists. This database defines **two** signatures of "
            "`proc_register_supplier_restock` — pick one and execute it."
        )
        try:
            overloads = load_functions()
            reg_overloads = overloads[overloads["Function"] == "proc_register_supplier_restock"]
        except DBServerError as exc:
            db_error_box(exc)
            reg_overloads = None

        if reg_overloads is not None and not reg_overloads.empty:
            sig = st.segmented_control(
                "Overload signature",
                options=list(reg_overloads["Arguments"]),
                format_func=lambda a: f"({a})",
                key="overload_sig",
            )
            st.caption(
                "Overload A matches the live schema and works. Overload B is a legacy definition "
                "whose body references **non-existent columns** (`equipment_id`, `quantity`) — it "
                "always fails, and is included so you can watch PostgreSQL reject it gracefully."
            )

            if sig is not None:
                if "p_catalog_id" in sig:
                    # Overload A — working (supplier, catalog, condition, barcode, serial, cost)
                    with st.form("overload_a_form", border=True):
                        st.subheader("Overload A — working: (supplier_id, catalog_id, condition_id, barcode, serial_number, unit_cost)")
                        c1, c2 = st.columns(2)
                        oa_supplier = c1.selectbox("Supplier", list(supplier_opts), key="oa_supplier")
                        oa_catalog = c2.selectbox("Catalog item", list(catalog_opts), key="oa_catalog")
                        oa_condition = _condition_selector(c1, "overload_a")
                        oa_barcode = c1.text_input("Barcode", placeholder="e.g. SKU-3001", key="oa_barcode")
                        oa_serial = c2.text_input("Serial number (unique)", placeholder="e.g. SN-2026-0301", key="oa_serial")
                        oa_cost = c2.number_input("Unit cost (USD)", min_value=0.0, value=0.0, step=5.0, format="%.2f", key="oa_cost")
                        oa_submit = st.form_submit_button("Execute Overload A", type="primary", width="stretch")

                    if oa_submit:
                        if not (oa_supplier and oa_catalog and oa_barcode and oa_serial):
                            st.warning("Supplier, catalog item, barcode and serial number are all required.")
                        else:
                            try:
                                rowcounts, notices = call_procedure(
                                    "proc_register_supplier_restock",
                                    (
                                        supplier_opts[oa_supplier],
                                        catalog_opts[oa_catalog],
                                        oa_condition,
                                        oa_barcode,
                                        oa_serial,
                                        oa_cost,
                                    ),
                                )
                                refresh_all()
                                st.success(f"Overload A executed: purchase order + item **{oa_barcode}** recorded.")
                                show_notices(notices)
                            except DBServerError as exc:
                                db_error_box(exc)
                else:
                    # Overload B — legacy/broken (supplier, equipment_id, quantity, cost)
                    with st.form("overload_b_form", border=True):
                        st.subheader("Overload B — legacy (BROKEN): (supplier_id, equipment_id, quantity_received, unit_cost)")
                        c1, c2 = st.columns(2)
                        ob_supplier = c1.selectbox("Supplier", list(supplier_opts), key="ob_supplier")
                        ob_equipment = c2.number_input("Equipment ID", min_value=1, step=1, key="ob_equipment")
                        ob_qty = c1.number_input("Quantity received", min_value=1, step=1, key="ob_qty")
                        ob_cost = c2.number_input("Unit cost (USD)", min_value=0.0, value=0.0, step=5.0, format="%.2f", key="ob_cost")
                        ob_submit = st.form_submit_button("Execute Overload B", type="primary", width="stretch")

                    if ob_submit:
                        try:
                            rowcounts, notices = call_procedure(
                                "proc_register_supplier_restock",
                                (supplier_opts[ob_supplier], int(ob_equipment), int(ob_qty), ob_cost),
                            )
                            st.success("Unexpected: Overload B executed without error.")
                        except DBServerError as exc:
                            st.error(":material/block: Exception captured (intentional — Overload B is the legacy broken version).")
                            st.code(str(exc), language="text")
                            st.caption(
                                "Its body inserts into non-existent columns of `purchase_orders` / "
                                "`inventory_items`, so PostgreSQL aborts with 'column does not exist' "
                                "and the procedure's EXCEPTION handler re-raises it. This proves PL/pgSQL "
                                "error propagation is surfaced cleanly by the UI."
                            )

    st.divider()

    # Purchase order history (proves multi-row handling of the restock proc)
    try:
        orders_df = load_purchase_orders()
    except DBServerError as exc:
        db_error_box(exc)
        return
    with st.container(border=True):
        st.subheader("Recent Purchase Orders")
        st.dataframe(
            orders_df,
            column_config={
                "order_id": st.column_config.NumberColumn("Order ID", format="%d"),
                "supplier_name": st.column_config.TextColumn("Supplier"),
                "order_date": st.column_config.DateColumn("Order Date"),
                "total_cost": st.column_config.NumberColumn("Total Cost", format="$%.2f"),
                "order_status": st.column_config.TextColumn("Status"),
            },
            hide_index=True,
            height=260,
        )


# ----------------------------------------------------------------------------
# 6b. PAGE 5 — DATA SCHEMA INSPECTOR (all 14 tables)
# ----------------------------------------------------------------------------
def page_schema_inspector() -> None:
    require_page("schema")
    st.title(":material/table_view: Data Schema Inspector")
    st.caption(
        "Inspect **any of the 14 `public` tables**: live contents, schema definition and row "
        "count. Table names come from a whitelisted dropdown and are safely quoted with "
        "`psycopg2.sql.Identifier`."
    )

    try:
        tables_df = load_tables()
    except DBServerError as exc:
        db_error_box(exc)
        return

    if tables_df.empty:
        st.error("No tables found in the `public` schema.")
        st.stop()

    table_list = tables_df["Table"].tolist()
    col_t, col_r, col_n = st.columns([3, 1, 1], vertical_alignment="bottom")
    table_name = col_t.selectbox("Select a table", table_list, key="schema_table")
    rows_to_show = col_n.number_input("Rows to preview", min_value=5, max_value=1000, value=200, step=5, key="schema_rows")
    if col_r.button(":material/refresh: Reload", key="schema_reload", type="tertiary"):
        st.rerun()

    try:
        schema = query_table_schema(table_name)
        preview = query_table_preview(table_name, limit=int(rows_to_show))
    except DBServerError as exc:
        db_error_box(exc)
        return

    st.metric(f"Row count — {table_name}", f"{schema['row_count']:,}", border=True)

    with st.container(border=True):
        st.subheader("Schema Definition")
        st.dataframe(schema["columns"], hide_index=True)

    with st.container(border=True):
        st.subheader("Integrity Constraints")
        st.dataframe(schema["constraints"], hide_index=True)

    with st.container(border=True):
        st.subheader(f"Live Contents (top {int(rows_to_show)})")
        st.dataframe(preview, hide_index=True)
        st.caption("Data is read live from PostgreSQL — there is no caching on this preview.")


# ----------------------------------------------------------------------------
# 6c. PAGE 6 — FUNCTIONS, TRIGGERS & INTEGRITY INSPECTOR
# ----------------------------------------------------------------------------
def page_functions_triggers() -> None:
    require_page("functions")
    st.title(":material/bolt: Functions, Triggers & Integrity Inspector")
    st.caption(
        "Live inventory of the database programmability layer: **5 functions / procedures, "
        "6 triggers, performance indexes** and a **non-destructive exception-testing suite**. "
        "Every live test below runs inside a transaction that is always **rolled back**, so "
        "repeated demos never corrupt data."
    )

    tab_funcs, tab_trigs, tab_idx, tab_exc = st.tabs(
        [
            ":material/code: Functions",
            ":material/rocket_launch: Triggers",
            ":material/hub: Indexes",
            ":material/error: Exception Suite",
        ]
    )

    # ---- TAB A: FUNCTIONS ----------------------------------------------
    with tab_funcs:
        try:
            fns = load_functions()
        except DBServerError as exc:
            db_error_box(exc)
        else:
            st.dataframe(fns, hide_index=True)
            st.caption("Expand any object to view its full `pg_get_functiondef` SQL source.")
            for _, row in fns.iterrows():
                with st.expander(f"{row['Function']}({row['Arguments']}) — {row['Kind']}"):
                    try:
                        st.code(load_function_definition(row["Function"], row["Arguments"]), language="sql")
                    except DBServerError as exc:
                        db_error_box(exc)

            st.divider()
            st.subheader("Interactive calculator — `fn_calculate_late_fine`")
            fcol, _ = st.columns([1, 3])
            fine_line = fcol.number_input("Rental line ID", min_value=1, step=1, key="fine_calc_line")
            if st.button("Calculate late fine", type="primary", icon=":material/calculate:"):
                try:
                    fine = float(query_scalar("SELECT fn_calculate_late_fine(%s)", (int(fine_line),)))
                    st.metric(f"Late fine for line #{int(fine_line)}", f"${fine:,.2f}", border=True)
                    if fine > 0:
                        st.success(f"**{fine / 2.5:.0f}** day(s) late × $2.50/day.")
                    else:
                        st.info("No fine — returned on or before the expected return date.")
                except DBServerError as exc:
                    db_error_box(exc)
            st.caption(
                "The other four functions are **trigger functions** — they cannot be called "
                "directly, they run automatically when their triggers fire (see the Triggers tab)."
            )

    # ---- TAB B: TRIGGERS ------------------------------------------------
    with tab_trigs:
        try:
            trigs = load_triggers()
            refs = load_reference_ids()
        except DBServerError as exc:
            db_error_box(exc)
        else:
            st.dataframe(trigs, hide_index=True)
            st.caption(
                "Firing order on `INSERT INTO rental_line_items` is alphabetical by trigger name: "
                "`trg_after_rent_item_checkout` runs first, then `trg_check_insert_return_date`."
            )

            st.divider()
            st.subheader("Live trigger demonstrations (all rolled back)")

            # 1) trg_check_insert_return_date
            with st.container(border=True):
                st.markdown("**`trg_check_insert_return_date`** — past-date insert blocker")
                st.caption(
                    "Attempts to insert a line item with a due date of 2001-01-01. The trigger's "
                    "BEFORE INSERT check must abort it with a RAISE EXCEPTION."
                )
                if st.button(":material/block: Attempt invalid past-date insert", key="trg_date_test"):
                    if refs["rental_id"] is None or refs["any_barcode"] is None:
                        st.warning("Need an existing rental and inventory barcode to run this test.")
                    else:
                        render_test_result(
                            execute_test([
                                (
                                    "INSERT INTO rental_line_items (rental_id, barcode, expected_return_date) "
                                    "VALUES (%s, %s, '2001-01-01')",
                                    (refs["rental_id"], refs["any_barcode"]),
                                )
                            ]),
                            "SQLSTATE **P0001** — `fn_validate_expected_return_date` raises before the row is written.",
                        )

            # 2) trg_auto_calculate_fine
            with st.container(border=True):
                st.markdown("**`trg_auto_calculate_fine`** — fine-escalation NOTICE on update")
                st.caption(
                    "Updates a line item's `actual_return_timestamp` directly (bypassing the return "
                    "procedure). The trigger calls `fn_calculate_late_fine` and raises a NOTICE — "
                    "watch it appear below, then the whole test is rolled back."
                )
                if st.button(":material/notifications: Fire fine trigger on latest line item", key="trg_fine_test"):
                    if refs["rental_id"] is None:
                        st.warning("Need at least one rental line item to run this test.")
                    else:
                        render_test_result(
                            execute_test([
                                (
                                    "UPDATE rental_line_items SET actual_return_timestamp = NOW() "
                                    "WHERE line_id = (SELECT MAX(line_id) FROM rental_line_items)",
                                    (),
                                )
                            ]),
                            "A RAISE NOTICE ('Item returned LATE …' or 'returned on time') captured from `fn_trigger_calculate_fine`.",
                            success_if_ok=True,
                        )

            # 3) trg_after_rent_item_checkout
            with st.container(border=True):
                st.markdown("**`trg_after_rent_item_checkout`** — non-destructive state simulation")
                st.caption(
                    "Simulates a checkout inside a transaction: insert a line item for an available "
                    "barcode, then SELECT its condition **before** the rollback. You will see it "
                    "flip to **2 (Rented)** inside the transaction; the rollback restores it to "
                    "**1 (Available)**. The real, persistent demo lives on the Member Checkout page."
                )
                if st.button(":material/swap_horiz: Simulate checkout (rolled back)", key="trg_checkout_sim"):
                    if refs["barcode"] is None or refs["rental_id"] is None:
                        st.warning("Need an available barcode and an existing rental to run this test.")
                    else:
                        render_test_result(
                            execute_test([
                                (
                                    "INSERT INTO rental_line_items (rental_id, barcode, expected_return_date) "
                                    "VALUES (%s, %s, CURRENT_DATE + INTERVAL '7 days')",
                                    (refs["rental_id"], refs["barcode"]),
                                ),
                                ("SELECT condition_id FROM inventory_items WHERE barcode = %s", (refs["barcode"],)),
                            ]),
                            "The SELECT above shows condition_id = 2 inside the transaction — ROLLBACK restored it to 1.",
                            success_if_ok=True,
                        )

            # 4) availability-sync triggers (catalog totals are DB-maintained)
            with st.container(border=True):
                st.markdown("**`trg_after_inventory_*`** — catalog balances stay in sync automatically")
                st.caption(
                    "Reads `total_available` for one catalog **before** a rolled-back checkout, "
                    "**after** the line-item insert (triggers flip the item to Rented and deduct "
                    "the balance), and **after** the item is returned. No hand-written SQL touches "
                    "the balance — the AFTER triggers on `inventory_items` are the single source of truth."
                )
                if st.button(":material/balance: Prove availability sync (rolled back)", key="trg_avail_sync_test"):
                    if refs["barcode"] is None or refs["rental_id"] is None:
                        st.warning("Need an available barcode and an existing rental to run this test.")
                    else:
                        render_test_result(
                            execute_test([
                                (
                                    "SELECT catalog_id, total_available FROM equipment_catalog "
                                    "WHERE catalog_id = (SELECT catalog_id FROM inventory_items WHERE barcode = %s)",
                                    (refs["barcode"],),
                                ),
                                (
                                    "INSERT INTO rental_line_items (rental_id, barcode, expected_return_date) "
                                    "VALUES (%s, %s, CURRENT_DATE + INTERVAL '7 days')",
                                    (refs["rental_id"], refs["barcode"]),
                                ),
                                (
                                    "SELECT catalog_id, total_available FROM equipment_catalog "
                                    "WHERE catalog_id = (SELECT catalog_id FROM inventory_items WHERE barcode = %s)",
                                    (refs["barcode"],),
                                ),
                                (
                                    "UPDATE rental_line_items SET actual_return_timestamp = NOW() WHERE barcode = %s",
                                    (refs["barcode"],),
                                ),
                                (
                                    "UPDATE inventory_items SET condition_id = 1 WHERE barcode = %s",
                                    (refs["barcode"],),
                                ),
                                (
                                    "SELECT catalog_id, total_available FROM equipment_catalog "
                                    "WHERE catalog_id = (SELECT catalog_id FROM inventory_items WHERE barcode = %s)",
                                    (refs["barcode"],),
                                ),
                            ]),
                            "The three SELECTs show total_available dropping by 1 on checkout and restored on return.",
                            success_if_ok=True,
                        )

    # ---- TAB C: INDEXES --------------------------------------------------
    with tab_idx:
        try:
            custom = load_custom_indexes()
            all_idx = load_all_indexes()
        except DBServerError as exc:
            db_error_box(exc)
        else:
            INDEX_EXPLANATIONS = {
                "idx_inv_cat_condition": (
                    "Composite B-tree on (catalog_id, condition_id). Speeds up the dashboard's "
                    "category × availability groupings and the checkout trigger's item lookups."
                ),
                "idx_items_return_pending": (
                    "Partial B-tree on barcode WHERE actual_return_timestamp IS NULL. Makes "
                    "'what's still out on loan?' scans touch only un-returned rows — the Return "
                    "page and any overdue reports rely on this."
                ),
                "idx_maint_barcode_date": (
                    "Composite B-tree on (barcode, service_date DESC). Fast maintenance-history "
                    "lookups per asset, newest first."
                ),
            }
            with st.container(border=True):
                st.subheader(":material/hub: Custom Index Inspection Card")
                for _, row in custom.iterrows():
                    st.markdown(f"**{row['Index']}**")
                    st.code(row["Definition"], language="sql")
                    st.caption(INDEX_EXPLANATIONS.get(row["Index"], "Defined to accelerate query performance."))
                    st.divider()
            with st.expander("All indexes — including constraint-backed (PK / UNIQUE)"):
                st.dataframe(all_idx, hide_index=True)
                st.caption(
                    "Indexes ending in `_pkey` / `_key` are auto-created by PRIMARY KEY and UNIQUE "
                    "constraints; the three above were created explicitly with CREATE INDEX."
                )

    # ---- TAB D: EXCEPTION TESTING SUITE ----------------------------------
    with tab_exc:
        st.caption(
            "Intentionally trigger PostgreSQL exceptions and watch `st.error()` capture the "
            "PL/pgSQL `RAISE EXCEPTION` / constraint messages verbatim. Every test is executed "
            "inside a transaction and rolled back — **100% safe to click repeatedly**."
        )
        try:
            refs = load_reference_ids()
        except DBServerError as exc:
            db_error_box(exc)
        else:
            exception_preset(
                "bad_barcode",
                "Non-existent barcode (PL/pgSQL RAISE EXCEPTION)",
                "Calls `proc_member_rental_checkout` with a barcode that does not exist. The "
                "procedure's PRE-CHECK raises 'Checkout Denied: The barcode [...] does not exist…'.",
                [("CALL proc_member_rental_checkout(%s, %s, %s)", (refs["member_id"], refs["staff_id"], "NON-EXISTENT-9999"))],
                "P0001",
                enabled=refs["member_id"] is not None and refs["staff_id"] is not None,
            )

            exception_preset(
                "dup_barcode",
                "Duplicate barcode (PRIMARY KEY violation)",
                "Calls `proc_acquire_new_asset` reusing an existing barcode → `23505 unique_violation`.",
                [("CALL proc_acquire_new_asset(%s, %s, %s, %s)", (refs["any_barcode"], refs["catalog_id"], 1, "SN-DEMO-999"))],
                "23505",
                enabled=refs["any_barcode"] is not None and refs["catalog_id"] is not None,
            )

            exception_preset(
                "dup_serial",
                "Duplicate serial number (UNIQUE violation)",
                "Calls `proc_acquire_new_asset` with a new barcode but a serial_number already in "
                "`inventory_items` → `23505 unique_violation`.",
                [("CALL proc_acquire_new_asset(%s, %s, %s, %s)", ("DEMO-NEW-BARCODE-123", refs["catalog_id"], 1, refs["serial"]))],
                "23505",
                enabled=refs["catalog_id"] is not None and refs["serial"] is not None,
            )

            exception_preset(
                "past_date",
                "Past-date insert (trigger constraint violation)",
                "Attempts to insert a line item with `expected_return_date = 2001-01-01`. "
                "`trg_check_insert_return_date` blocks it → `P0001`.",
                [(
                    "INSERT INTO rental_line_items (rental_id, barcode, expected_return_date) "
                    "VALUES (%s, %s, '2001-01-01')",
                    (refs["rental_id"], refs["any_barcode"]),
                )],
                "P0001",
                enabled=refs["rental_id"] is not None and refs["any_barcode"] is not None,
            )

            exception_preset(
                "bad_fk",
                "Invalid member (FOREIGN KEY violation, wrapped by the procedure)",
                "Calls `proc_member_rental_checkout` with a member_id that does not exist in "
                "`members`. The INSERT fails with `23503`, but the procedure's "
                "`EXCEPTION WHEN OTHERS` handler re-raises it as **P0001** "
                "('Transaction rolled back. Engine details: …') — a great example of "
                "PL/pgSQL error propagation.",
                [("CALL proc_member_rental_checkout(%s, %s, %s)", (999999, refs["staff_id"], refs["any_barcode"]))],
                "P0001",
                enabled=refs["staff_id"] is not None and refs["any_barcode"] is not None,
            )

            exception_preset(
                "check_violation",
                "Negative total (CHECK violation)",
                "Inserts a rental with `total_billed = -5.00` → `23514 check_violation` "
                "(constraint `rentals_total_billed_check`).",
                [("INSERT INTO rentals (member_id, staff_id, total_billed) VALUES (%s, %s, -5.00)", (refs["member_id"], refs["staff_id"]))],
                "23514",
                enabled=refs["member_id"] is not None and refs["staff_id"] is not None,
            )


# ----------------------------------------------------------------------------
# 9B. DATA-MANAGEMENT CRUD CONSOLE
# ----------------------------------------------------------------------------
CRUD_TABLES = [
    "inventory_items", "equipment_catalog", "rentals", "rental_line_items",
    "purchase_orders", "members", "staff", "maintenance_logs",
    "suppliers", "brands", "equipment_categories", "item_conditions",
    "membership_types", "staff_roles",
]

FRIENDLY = {
    "members": ("member_id", "first_name", "last_name"),
    "staff": ("staff_id", "first_name", "last_name"),
    "suppliers": ("supplier_id", "supplier_name"),
    "brands": ("brand_id", "brand_name"),
    "equipment_categories": ("category_id", "category_name"),
    "equipment_catalog": ("catalog_id", "item_name"),
    "inventory_items": ("barcode",),
    "item_conditions": ("condition_id", "condition_label"),
    "membership_types": ("tier_id", "tier_name"),
    "staff_roles": ("role_id", "role_title"),
    "rentals": ("rental_id", "checkout_timestamp"),
    "rental_line_items": ("line_id", "barcode", "expected_return_date"),
    "purchase_orders": ("order_id", "order_date"),
    "maintenance_logs": ("log_id", "barcode", "service_date"),
}

CASCADE_CHILDREN = {
    "inventory_items": ["maintenance_logs"],
    "rentals": ["rental_line_items"],
}


def _ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _human(name: str) -> str:
    return name.replace("_", " ").title()


def _fmt(value) -> str:
    return "" if value is None else str(value)


def _row_label(row: dict, table: str) -> str:
    cols = FRIENDLY.get(table) or (next(iter(row)),)
    parts = [str(row[c]) for c in cols if c in row and row[c] is not None]
    return " | ".join(parts)


def load_crud_metadata(table_name: str) -> dict:
    """Columns (+ PK / sequence / FK info) for one table, read live from the catalog."""
    cols = query_df(
        """
        SELECT column_name AS col, data_type, character_maximum_length AS max_len,
               is_nullable = 'NO' AS not_null,
               COALESCE(column_default, '') AS default_expr
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s
        ORDER BY ordinal_position
        """,
        (table_name,),
    ).to_dict("records")
    pk = query_df(
        """
        SELECT a.attname AS col
        FROM pg_index i
        JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
        WHERE i.indrelid = %s::regclass AND i.indisprimary
        ORDER BY array_position(i.indkey, a.attnum)
        """,
        (table_name,),
    )
    fks_df = query_df(
        """
        SELECT a.attname AS col,
               c.confrelid::regclass::text AS ref_table,
               (SELECT x.attname FROM pg_attribute x
                 WHERE x.attrelid = c.confrelid AND x.attnum = c.confkey[1]) AS ref_col
        FROM pg_constraint c
        JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1]
        WHERE c.contype = 'f' AND c.conrelid = %s::regclass
        """,
        (table_name,),
    )
    fks = {r["col"]: {"ref_table": r["ref_table"], "ref_col": r["ref_col"]} for _, r in fks_df.iterrows()}
    return {"table": table_name, "columns": cols, "pk_cols": list(pk["col"]), "fks": fks}


def load_fk_options(ref_table: str, ref_col: str) -> dict:
    """{display label: FK value} for a referenced table (sorted by its first display column)."""
    display = FRIENDLY.get(ref_table, (ref_col,))
    qcols = list(dict.fromkeys([ref_col] + list(display)))
    select = ", ".join(_ident(c) for c in qcols)
    df = query_df(f"SELECT {select} FROM {_ident(ref_table)} ORDER BY {_ident(qcols[0])}")
    options = {}
    for _, r in df.iterrows():
        label = " | ".join(str(r[c]) for c in qcols if r[c] is not None)
        options[label] = r[ref_col]
    return options


def _crud_search_clause(meta: dict, search: str):
    where_sql, params = "", []
    if search:
        text_cols = [
            c["col"] for c in meta["columns"]
            if "char" in c["data_type"] or c["data_type"] in ("text", "date")
        ]
        if text_cols:
            conds = " OR ".join(f"{_ident(c)}::text ILIKE %s" for c in text_cols)
            where_sql = f" WHERE {conds}"
            params = [f"%{search}%"] * len(text_cols)
    return where_sql, params


def load_crud_rows(table_name: str, search: str, limit: int, offset: int):
    meta = load_crud_metadata(table_name)
    cols = [c["col"] for c in meta["columns"]]
    select = ", ".join(_ident(c) for c in cols)
    where_sql, params = _crud_search_clause(meta, search)
    order = meta["pk_cols"][0] if meta["pk_cols"] else cols[0]
    sql = (f"SELECT {select} FROM {_ident(table_name)} {where_sql} "
           f"ORDER BY {_ident(order)} LIMIT %s OFFSET %s")
    return meta, query_df(sql, params + [limit, offset])


def count_crud_rows(table_name: str, search: str) -> int:
    meta = load_crud_metadata(table_name)
    where_sql, params = _crud_search_clause(meta, search)
    sql = f"SELECT COUNT(*) FROM {_ident(table_name)} {where_sql}"
    return int(query_scalar(sql, params))


def build_insert(table_name: str, data: dict):
    if not data:
        return f"INSERT INTO {_ident(table_name)} DEFAULT VALUES", ()
    cols = list(data.keys())
    sql = (f"INSERT INTO {_ident(table_name)} ({', '.join(_ident(c) for c in cols)}) "
           f"VALUES ({', '.join(['%s'] * len(cols))})")
    return sql, tuple(data.values())


def build_update(table_name: str, data: dict, pk_values: dict):
    sets = ", ".join(f"{_ident(c)} = %s" for c in data)
    where = " AND ".join(f"{_ident(c)} = %s" for c in pk_values)
    params = list(data.values()) + list(pk_values.values())
    return f"UPDATE {_ident(table_name)} SET {sets} WHERE {where}", tuple(params)


def build_delete(table_name: str, pk_values: dict):
    where = " AND ".join(f"{_ident(c)} = %s" for c in pk_values)
    return f"DELETE FROM {_ident(table_name)} WHERE {where}", tuple(pk_values.values())


def render_crud_form(meta: dict, values: dict | None, key_prefix: str, mode: str) -> dict:
    """Render one widget per editable column and return {column: raw_value}.

    `mode == "create"` allows entering natural primary keys; `mode == "edit"`
    shows primary keys as read-only and uses them only to locate the row.
    """
    values = values or {}
    data = {}
    for col in meta["columns"]:
        cname = col["col"]
        if cname in meta["pk_cols"] and "nextval" in col["default_expr"]:
            continue  # sequence PK -> generated by the database
        is_pk = cname in meta["pk_cols"]

        if cname == "password_hash":
            pw = st.text_input(
                "New password (blank = keep current on update)" if mode == "edit"
                else "Password (hashed with PBKDF2 before storage)",
                type="password",
                key=f"{key_prefix}_pw",
            )
            if pw:
                data[cname] = hash_password(pw)
            elif mode == "create":
                data[cname] = None
            continue

        fk = meta["fks"].get(cname)
        if fk is not None:
            options = load_fk_options(fk["ref_table"], fk["ref_col"])
            labels = list(options.keys())
            default = values.get(cname)
            index = 0
            for i, lab in enumerate(labels):
                if options[lab] == default:
                    index = i
                    break
            pick = st.selectbox(
                f"{_human(cname)} — FK to `{fk['ref_table']}`",
                labels,
                index=index,
                key=f"{key_prefix}_fk_{cname}",
                disabled=is_pk and mode == "edit",
            )
            data[cname] = options[pick]
            continue

        if is_pk:
            st.text_input(f"{_human(cname)} (primary key)", value=_fmt(values.get(cname)),
                          key=f"{key_prefix}_pk_{cname}", disabled=True)
            continue

        col_type = col["data_type"]
        if col_type in ("integer", "bigint", "smallint"):
            data[cname] = st.number_input(_human(cname), min_value=None,
                                          value=int(values[cname]) if values.get(cname) is not None else 0,
                                          step=1, key=f"{key_prefix}_{cname}")
        elif col_type in ("numeric", "double precision", "real", "money"):
            data[cname] = st.number_input(_human(cname),
                                          value=float(values[cname]) if values.get(cname) is not None else 0.0,
                                          format="%.2f", key=f"{key_prefix}_{cname}")
        elif col_type == "date":
            dv = values.get(cname)
            data[cname] = st.date_input(_human(cname),
                                        value=dv if isinstance(dv, datetime.date) else datetime.date.today(),
                                        key=f"{key_prefix}_{cname}")
        elif col_type in ("timestamp without time zone", "timestamp with time zone"):
            tv = values.get(cname)
            data[cname] = st.datetime_input(_human(cname),
                                            value=tv if isinstance(tv, datetime.datetime) else datetime.datetime.now(),
                                            key=f"{key_prefix}_{cname}")
        else:
            data[cname] = st.text_input(_human(cname), value=_fmt(values.get(cname)),
                                        key=f"{key_prefix}_{cname}")
    return data


def _crud_clean(meta: dict, data: dict) -> dict:
    by_col = {c["col"]: c for c in meta["columns"]}
    clean = {}
    for cname, val in data.items():
        if val is None or val == "":
            continue
        if by_col[cname]["data_type"] in ("integer", "bigint", "smallint"):
            clean[cname] = int(val)
        else:
            clean[cname] = val
    return clean


def page_crud_console() -> None:
    require_page("crud")

    allowed_tables = crud_tables_for_role()
    if not allowed_tables:
        st.warning(":material/lock: This role has no tables assigned in the CRUD console.")
        st.stop()

    st.title(":material/edit_note: Data Management (CRUD Console)")
    st.caption(
        f"Metadata-driven **C**reate / **R**ead / **U**pdate / **D**elete across the "
        f"**{len(allowed_tables)}** table(s) assigned to the **{current_role()}** role. "
        "The `equipment_catalog.total_owned / total_available` balances are maintained by the "
        "AFTER triggers on `inventory_items`, so every insert / condition change / delete stays in sync."
    )

    if st.session_state.get("crud_table") not in allowed_tables:
        st.session_state["crud_table"] = allowed_tables[0]
    table = st.selectbox("Table", allowed_tables, key="crud_table")
    meta = load_crud_metadata(table)
    pk_cols = meta["pk_cols"]
    st.caption(
        f"**{len(meta['columns'])}** columns · PK: `{', '.join(pk_cols) or '(none)'}` · "
        f"FKs: {', '.join(meta['fks']) or '(none)'} · rows: **{count_crud_rows(table, ''):,}**"
    )

    c_search, c_per = st.columns([3, 1])
    with c_search:
        search = st.text_input("Search", placeholder=f"Filter `{table}`…", key="crud_search").strip()
    with c_per:
        per_page = int(st.number_input("Rows / page", 5, 100, 15, key="crud_per_page"))

    if "crud_page" not in st.session_state:
        st.session_state["crud_page"] = 0
    total = count_crud_rows(table, search)
    pages = max(1, -(-total // per_page))
    page = min(int(st.session_state["crud_page"]), pages - 1)
    st.session_state["crud_page"] = page
    meta, df = load_crud_rows(table, search, per_page, page * per_page)

    c1, c2, c3 = st.columns([1, 3, 1])
    with c1:
        if st.button(":material/skip_previous: Prev", key="crud_prev", disabled=page == 0):
            st.session_state["crud_page"] = page - 1
            st.rerun()
    with c2:
        st.caption(
            f"Page **{page + 1}** of **{pages}** — {total:,} row(s)",
            text_alignment="center",
        )
    with c3:
        if st.button(":material/skip_next: Next", key="crud_next", disabled=page >= pages - 1):
            st.session_state["crud_page"] = page + 1
            st.rerun()

    tab_create, tab_browse, tab_edit = st.tabs(
        [
            ":material/add_box: Create",
            ":material/table_view: Browse",
            ":material/update: Update / Delete",
        ]
    )

    with tab_create:
        st.subheader(f"New row — `{table}`")
        st.caption(
            "Sequence-backed primary keys are generated automatically. Fields left blank are omitted "
            "so the database default applies (or NULL for nullable columns)."
        )
        create_data = render_crud_form(meta, values=None, key_prefix="crud_new", mode="create")
        required = [
            c["col"] for c in meta["columns"]
            if c["not_null"] and not c["default_expr"] and c["col"] not in create_data
        ]
        if required:
            st.caption(":material/error: Required (no DB default): " + ", ".join(f"`{c}`" for c in required))
        if st.button(f"Insert into `{table}`", type="primary", key="crud_insert_btn", icon=":material/save:"):
            clean = _crud_clean(meta, create_data)
            still_missing = [c for c in required if c not in clean]
            if still_missing:
                st.warning("Please fill the required fields first: " + ", ".join(still_missing))
            else:
                try:
                    sql, params = build_insert(table, clean)
                    _, notices = execute_sequence([(sql, params)])
                    st.success(f"Inserted 1 row into `{table}`.")
                    show_notices(notices)
                except DBServerError as exc:
                    db_error_box(exc)

    with tab_browse:
        st.subheader(f"Rows in `{table}` (page {page + 1})")
        if df.empty:
            st.info("No rows match the current search.")
        else:
            st.dataframe(df, hide_index=True, width="stretch")
        if table == "staff":
            st.caption("`password_hash` is never rendered — passwords only ever exist as PBKDF2 hashes.")

    with tab_edit:
        st.subheader(f"Update or delete a row in `{table}`")
        if df.empty:
            st.info("No rows on this page to edit.")
        else:
            rows = df.to_dict("records")
            labels = [_row_label(r, table) for r in rows]
            pick = st.selectbox(
                "Row on this page", range(len(rows)),
                format_func=lambda i: labels[i], key="crud_pick",
            )
            row = rows[pick]
            pk_values = {c: row[c] for c in pk_cols}
            with st.form(f"crud_edit_{table}"):
                st.caption("Primary key columns are read-only; they identify the row being updated.")
                edit_data = render_crud_form(
                    meta, values=row, key_prefix=f"crud_edit_{pick}", mode="edit"
                )
                submitted = st.form_submit_button(f"Save changes to `{table}`", icon=":material/check:")
            if submitted:
                clean = _crud_clean(meta, edit_data)
                if not clean:
                    st.info("No changes to save.")
                else:
                    try:
                        sql, params = build_update(table, clean, pk_values)
                        _, notices = execute_sequence([(sql, params)])
                        st.success(f"Updated 1 row in `{table}`.")
                        show_notices(notices)
                    except DBServerError as exc:
                        db_error_box(exc)

            if table == "staff" and is_admin():
                st.divider()
                with st.form("crud_reset_pw", clear_on_submit=True):
                    new_pw = st.text_input(
                        "Reset password (PBKDF2-hashed before storage)", type="password",
                        key="crud_staff_pw",
                    )
                    if st.form_submit_button("Set new password", icon=":material/password:"):
                        if not new_pw:
                            st.warning("Enter a password first.")
                        else:
                            try:
                                execute_sequence([
                                    ("UPDATE staff SET password_hash = %s WHERE staff_id = %s",
                                     (hash_password(new_pw), int(row["staff_id"]))),
                                ])
                                st.success(f"Password updated for staff_id={row['staff_id']}.")
                            except DBServerError as exc:
                                db_error_box(exc)

            st.divider()
            with st.form("crud_delete"):
                st.markdown("#### :material/delete: Delete this row")
                if table in CASCADE_CHILDREN:
                    st.warning(
                        "**ON DELETE CASCADE** children: " + ", ".join(CASCADE_CHILDREN[table]) +
                        " — deleting this row also deletes matching child rows."
                    )
                confirm = st.checkbox("I understand this permanently deletes the row.", key="crud_del_confirm")
                if st.form_submit_button(f"Delete this row from `{table}`", icon=":material/delete_forever:"):
                    if not confirm:
                        st.warning("Tick the confirmation box to delete.")
                    else:
                        try:
                            sql, params = build_delete(table, pk_values)
                            _, notices = execute_sequence([(sql, params)])
                            st.success(f"Deleted 1 row from `{table}`.")
                            show_notices(notices)
                        except DBServerError as exc:
                            db_error_box(exc)


# ----------------------------------------------------------------------------
# 10. SIDEBAR & NAVIGATION
# ----------------------------------------------------------------------------
def sidebar_backend_reference() -> None:
    """Collapsible cheat-sheet of every backend object exposed by this UI."""
    with st.sidebar.expander(":material/database: Backend objects demoed", expanded=False):
        st.markdown(
            """
            **Stored Procedures (6)**
            - `proc_member_rental_checkout`
            - `proc_item_return`
            - `proc_supplier_restock`
            - `proc_acquire_new_asset`
            - `proc_register_supplier_restock` — *overloaded ×2*
              - (supplier, catalog, condition, barcode, serial, cost) ✓
              - (supplier, equipment, qty, cost) ✗ legacy

            **Functions (5)**
            - `fn_calculate_late_fine` — scalar
            - `fn_trigger_process_checkout` — trigger
            - `fn_trigger_calculate_fine` — trigger
            - `fn_validate_expected_return_date` — trigger
            - `fn_trigger_sync_catalog_availability` — trigger

            **Triggers (6)**
            - `trg_after_rent_item_checkout`
            - `trg_auto_calculate_fine`
            - `trg_check_insert_return_date`
            - `trg_after_inventory_insert`
            - `trg_after_inventory_condition_change`
            - `trg_after_inventory_delete`

            **Indexes (3 custom)**
            - `idx_inv_cat_condition`
            - `idx_items_return_pending`
            - `idx_maint_barcode_date`

            **Tables (14)** — inspect all in the Schema Inspector
            **Constraints** — PK / FK (`RESTRICT` / `CASCADE`),
            `CHECK`, `UNIQUE`
            """
        )


# Page objects are rebuilt each run; this dict lets pages link to each other
# (e.g. the Dashboard "Quick Actions" row) without duplicating definitions.
_NAV_PAGES: dict = {}


def build_nav_pages() -> list:
    """Navigation list filtered by the signed-in role's permission map.

    Every page object is created so `_NAV_PAGES` can still resolve cross-links,
    but only the role's assigned pages are registered with `st.navigation` and
    exposed through `_NAV_PAGES`.
    """
    page_defs = {
        "dashboard": st.Page(page_dashboard, title=PAGE_LABELS["dashboard"],
                             icon=":material/inventory_2:", default=True),
        "checkout": st.Page(page_checkout, title=PAGE_LABELS["checkout"],
                            icon=":material/add_shopping_cart:"),
        "return": st.Page(page_return, title=PAGE_LABELS["return"],
                          icon=":material/assignment_return:"),
        "restock": st.Page(page_supplier, title=PAGE_LABELS["restock"],
                           icon=":material/warehouse:"),
        "schema": st.Page(page_schema_inspector, title=PAGE_LABELS["schema"],
                          icon=":material/table_view:"),
        "functions": st.Page(page_functions_triggers, title=PAGE_LABELS["functions"],
                             icon=":material/bolt:"),
        "crud": st.Page(page_crud_console, title=PAGE_LABELS["crud"],
                        icon=":material/edit_note:"),
    }

    allowed_keys = [key for key in page_defs if can(key)]
    _NAV_PAGES.clear()
    _NAV_PAGES.update({key: page_defs[key] for key in allowed_keys})
    return [page_defs[key] for key in allowed_keys]


def main() -> None:
    # Sidebar is always available; the DB connection status shows regardless of
    # whether the user has authenticated.
    with st.sidebar:
        st.markdown("## :material/sports_and_outdoors: Sport Equipment Mgmt")
        try:
            _ = get_conn()
            st.success(":material/cloud_done: PostgreSQL connected")
        except Exception:
            st.error(":material/cloud_off: DB unreachable — check `DB_PASSWORD`.")

    # Self-heal the auth schema (staff.password_hash + demo users) on every launch.
    try:
        ensure_auth_schema()
    except DBServerError as exc:
        db_error_box(exc)
        return

    # LOGIN GATE: until a user is authenticated, show ONLY the login interface
    # and hide the navigation, welcome badge and backend reference.
    if not is_logged_in():
        render_login()
        return

    with st.sidebar:
        st.success(
            f"Logged in as: **{st.session_state['user_name']}** "
            f"({st.session_state['user_role']})"
        )
        if st.button(":material/logout: Logout", key="logout_btn", type="secondary", width="stretch"):
            do_logout()
            st.rerun()
        with st.sidebar.expander(":material/school: How to use this system", expanded=False):
            role = current_role()
            perms = current_perms()
            allowed_pages = [key for key in PAGE_LABELS if can(key)]
            guide_lines = [
                f"**You are a `{role}`.**",
                "",
                f"*Duties:* {perms['duties']}",
                "",
            ]
            guide_lines += [
                f"{i}. **{PAGE_LABELS[key]}** — {PAGE_DUTIES[key]}."
                for i, key in enumerate(allowed_pages, start=1)
            ]
            if can("crud"):
                managed = crud_tables_for_role()
                guide_lines += [
                    "",
                    "**Tables you may manage:** " + ", ".join(f"`{t}`" for t in managed),
                ]
            st.markdown("\n".join(guide_lines))
        sidebar_backend_reference()

    pages = st.navigation(build_nav_pages())
    pages.run()


if __name__ == "__main__":
    main()
