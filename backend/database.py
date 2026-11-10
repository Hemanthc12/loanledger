"""database.py — SQLite persistence layer (local cache for Google Sheets data)."""
import sqlite3
import os
import logging
from contextlib import contextmanager

logger = logging.getLogger(__name__)
DB_PATH = os.path.join(os.path.dirname(__file__), "emi_data.db")


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _add_col(conn, table, col, ddl):
    cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]
    if col not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")


def init_db():
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS loans (
                loan_id         TEXT PRIMARY KEY,
                user_name       TEXT NOT NULL,
                loan_amount     REAL NOT NULL,
                interest_rate   REAL NOT NULL,
                tenure_months   INTEGER NOT NULL,
                start_date      TEXT NOT NULL,
                emi_amount      REAL NOT NULL,
                status          TEXT NOT NULL DEFAULT 'Active',
                created_at      TEXT NOT NULL,
                updated_at      TEXT NOT NULL,
                sanction_date   TEXT DEFAULT '',
                pre_emi_interest REAL DEFAULT 0,
                pre_emi_days    INTEGER DEFAULT 0,
                orig_rate       REAL
            );
            CREATE TABLE IF NOT EXISTS emi_schedule (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                loan_id             TEXT NOT NULL,
                emi_number          INTEGER NOT NULL,
                due_date            TEXT NOT NULL,
                emi_amount          REAL NOT NULL,
                interest_component  REAL NOT NULL,
                principal_component REAL NOT NULL,
                outstanding_balance REAL NOT NULL,
                status              TEXT NOT NULL DEFAULT 'Unpaid',
                paid_date           TEXT DEFAULT '',
                is_pre_emi          INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (loan_id) REFERENCES loans(loan_id)
            );
            CREATE TABLE IF NOT EXISTS payments (
                id                              INTEGER PRIMARY KEY AUTOINCREMENT,
                loan_id                         TEXT NOT NULL,
                payment_id                      TEXT NOT NULL UNIQUE,
                payment_date                    TEXT NOT NULL,
                amount_paid                     REAL NOT NULL,
                payment_type                    TEXT NOT NULL,
                emi_number                      INTEGER,
                remaining_balance_after_payment REAL NOT NULL,
                notes                           TEXT DEFAULT '',
                created_at                      TEXT NOT NULL,
                meta                            TEXT DEFAULT '',
                FOREIGN KEY (loan_id) REFERENCES loans(loan_id)
            );
        """)
        # migrations for databases created by older versions
        _add_col(conn, "emi_schedule", "is_pre_emi", "INTEGER NOT NULL DEFAULT 0")
        _add_col(conn, "loans", "sanction_date", "TEXT DEFAULT ''")
        _add_col(conn, "loans", "pre_emi_interest", "REAL DEFAULT 0")
        _add_col(conn, "loans", "pre_emi_days", "INTEGER DEFAULT 0")
        _add_col(conn, "loans", "orig_rate", "REAL")
        _add_col(conn, "payments", "meta", "TEXT DEFAULT ''")
    logger.info(f"Database initialized at: {DB_PATH}")


# ── Loans ─────────────────────────────────────────────────────────────

def insert_loan(loan: dict):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO loans (loan_id, user_name, loan_amount, interest_rate,
                tenure_months, start_date, emi_amount, status, created_at, updated_at,
                sanction_date, pre_emi_interest, pre_emi_days, orig_rate)
            VALUES (:loan_id, :user_name, :loan_amount, :interest_rate,
                :tenure_months, :start_date, :emi_amount, :status, :created_at, :updated_at,
                :sanction_date, :pre_emi_interest, :pre_emi_days, :orig_rate)
        """, loan)


def get_loan(loan_id: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM loans WHERE loan_id = ?", (loan_id,)).fetchone()
        return dict(row) if row else None


def list_loans() -> list:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM loans ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]


def update_loan(loan_id: str, fields: dict):
    fields = dict(fields)
    sets = ", ".join(f"{k} = :{k}" for k in fields)
    fields["loan_id"] = loan_id
    with get_conn() as conn:
        conn.execute(f"UPDATE loans SET {sets} WHERE loan_id = :loan_id", fields)


def delete_loan(loan_id: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM payments WHERE loan_id = ?", (loan_id,))
        conn.execute("DELETE FROM emi_schedule WHERE loan_id = ?", (loan_id,))
        conn.execute("DELETE FROM loans WHERE loan_id = ?", (loan_id,))


# ── Schedule ──────────────────────────────────────────────────────────

_SCHED_SQL = """
    INSERT INTO emi_schedule
        (loan_id, emi_number, due_date, emi_amount, interest_component,
         principal_component, outstanding_balance, status, paid_date, is_pre_emi)
    VALUES
        (:loan_id, :emi_number, :due_date, :emi_amount, :interest_component,
         :principal_component, :outstanding_balance, :status, :paid_date, :is_pre_emi)
"""
_PAY_SQL = """
    INSERT INTO payments
        (loan_id, payment_id, payment_date, amount_paid, payment_type,
         emi_number, remaining_balance_after_payment, notes, created_at, meta)
    VALUES
        (:loan_id, :payment_id, :payment_date, :amount_paid, :payment_type,
         :emi_number, :remaining_balance_after_payment, :notes, :created_at, :meta)
"""


def insert_schedule_rows(rows: list):
    with get_conn() as conn:
        conn.executemany(_SCHED_SQL, rows)


def get_schedule(loan_id: str) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM emi_schedule WHERE loan_id = ? ORDER BY emi_number", (loan_id,)
        ).fetchall()
        return [dict(r) for r in rows]


# ── Payments ──────────────────────────────────────────────────────────

def insert_payment(payment: dict):
    p = dict(payment)
    p.setdefault("meta", "")
    with get_conn() as conn:
        conn.execute(_PAY_SQL, p)


def get_payments(loan_id: str) -> list:
    """For display: ordered by date."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM payments WHERE loan_id = ? ORDER BY payment_date, id", (loan_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_payments_by_id(loan_id: str) -> list:
    """For replay: ordered by the order they were entered."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM payments WHERE loan_id = ? ORDER BY id", (loan_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def replace_loan_data(loan_id: str, loan_fields: dict, schedule: list, payments: list):
    """Atomically replace one loan's schedule + payments and update its row."""
    fields = dict(loan_fields)
    sets = ", ".join(f"{k} = :{k}" for k in fields)
    fields["loan_id"] = loan_id
    sched = [{**r, "loan_id": loan_id} for r in schedule]
    pays = [{**{"meta": ""}, **p, "loan_id": loan_id} for p in payments]
    with get_conn() as conn:
        conn.execute("DELETE FROM payments WHERE loan_id = ?", (loan_id,))
        conn.execute("DELETE FROM emi_schedule WHERE loan_id = ?", (loan_id,))
        conn.execute(f"UPDATE loans SET {sets} WHERE loan_id = :loan_id", fields)
        conn.executemany(_SCHED_SQL, sched)
        conn.executemany(_PAY_SQL, pays)


# ── Bulk (Sheets sync) ────────────────────────────────────────────────

def all_schedule() -> list:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM emi_schedule ORDER BY loan_id, emi_number").fetchall()]


def all_payments() -> list:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM payments ORDER BY loan_id, id").fetchall()]

def replace_schedule(loan_id: str, rows: list):
    """Replace only this loan's schedule rows. Payments are not touched."""
    sched = [{**r, "loan_id": loan_id} for r in rows]
    with get_conn() as conn:
        conn.execute("DELETE FROM emi_schedule WHERE loan_id = ?", (loan_id,))
        conn.executemany(_SCHED_SQL, sched)

def replace_all(loans: list, schedule: list, payments: list):
    def _n(v):
        return None if (v is None or str(v).strip() == "") else v

    def _f(v):
        return float(v) if _n(v) is not None else None

    def _i(v):
        return int(float(v)) if _n(v) is not None else None

    with get_conn() as conn:
        conn.execute("DELETE FROM payments")
        conn.execute("DELETE FROM emi_schedule")
        conn.execute("DELETE FROM loans")

        for r in loans:
            conn.execute("""
                INSERT INTO loans
                    (loan_id, user_name, loan_amount, interest_rate, tenure_months,
                     start_date, emi_amount, status, created_at, updated_at,
                     sanction_date, pre_emi_interest, pre_emi_days, orig_rate)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                r["loan_id"], r["user_name"], _f(r["loan_amount"]), _f(r["interest_rate"]),
                _i(r["tenure_months"]), r["start_date"], _f(r["emi_amount"]), r["status"],
                r["created_at"], r["updated_at"],
                r.get("sanction_date") or "", _f(r.get("pre_emi_interest")) or 0.0,
                _i(r.get("pre_emi_days")) or 0, _f(r.get("orig_rate")),
            ))

        for r in schedule:
            conn.execute("""
                INSERT INTO emi_schedule
                    (id, loan_id, emi_number, due_date, emi_amount, interest_component,
                     principal_component, outstanding_balance, status, paid_date, is_pre_emi)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, (
                _i(r.get("id")), r["loan_id"], int(float(r.get("emi_number") or 0)),
                r["due_date"], _f(r["emi_amount"]), _f(r["interest_component"]),
                _f(r["principal_component"]), _f(r["outstanding_balance"]), r["status"],
                r.get("paid_date") or "", _i(r.get("is_pre_emi") or 0) or 0,
            ))

        for r in payments:
            conn.execute("""
                INSERT INTO payments
                    (id, loan_id, payment_id, payment_date, amount_paid, payment_type,
                     emi_number, remaining_balance_after_payment, notes, created_at, meta)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, (
                _i(r.get("id")), r["loan_id"], r["payment_id"], r["payment_date"],
                _f(r["amount_paid"]), r["payment_type"], _i(r.get("emi_number")),
                _f(r["remaining_balance_after_payment"]),
                r.get("notes") or "", r["created_at"], r.get("meta") or "",
            ))