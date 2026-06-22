"""
database.py — SQLite persistence layer (local cache for Google Sheets data)
DB file: emi_data.db — populated on startup from Google Sheets, written back
after every mutating operation via sheets_sync.push_async().
"""

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


def init_db():
    """Create all tables if they don't exist."""
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
                updated_at      TEXT NOT NULL
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
                FOREIGN KEY (loan_id) REFERENCES loans(loan_id)
            );
        """)
        # Migration: add is_pre_emi to existing databases that pre-date this column
        try:
            conn.execute(
                "ALTER TABLE emi_schedule ADD COLUMN is_pre_emi INTEGER NOT NULL DEFAULT 0"
            )
        except Exception:
            pass  # column already exists
    logger.info(f"Database initialized at: {DB_PATH}")


# ── Loans ────────────────────────────────────────────────────────────

def insert_loan(loan: dict):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO loans (loan_id, user_name, loan_amount, interest_rate,
                tenure_months, start_date, emi_amount, status, created_at, updated_at)
            VALUES (:loan_id, :user_name, :loan_amount, :interest_rate,
                :tenure_months, :start_date, :emi_amount, :status, :created_at, :updated_at)
        """, loan)


def get_loan(loan_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM loans WHERE loan_id = ?", (loan_id,)).fetchone()
        return dict(row) if row else None


def list_loans() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM loans ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]


def update_loan(loan_id: str, fields: dict):
    sets = ", ".join(f"{k} = :{k}" for k in fields)
    fields["loan_id"] = loan_id
    with get_conn() as conn:
        conn.execute(f"UPDATE loans SET {sets} WHERE loan_id = :loan_id", fields)


# ── EMI Schedule ─────────────────────────────────────────────────────

def insert_schedule_rows(rows: list[dict]):
    with get_conn() as conn:
        conn.executemany("""
            INSERT INTO emi_schedule
                (loan_id, emi_number, due_date, emi_amount, interest_component,
                 principal_component, outstanding_balance, status, paid_date, is_pre_emi)
            VALUES
                (:loan_id, :emi_number, :due_date, :emi_amount, :interest_component,
                 :principal_component, :outstanding_balance, :status, :paid_date, :is_pre_emi)
        """, rows)


def get_schedule(loan_id: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM emi_schedule WHERE loan_id = ? ORDER BY emi_number",
            (loan_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def update_emi_status(loan_id: str, emi_number: int, status: str, paid_date: str = ""):
    with get_conn() as conn:
        conn.execute(
            "UPDATE emi_schedule SET status = ?, paid_date = ? WHERE loan_id = ? AND emi_number = ?",
            (status, paid_date, loan_id, emi_number)
        )


def delete_unpaid_from(loan_id: str, from_emi_number: int):
    """Delete unpaid EMI rows from a given EMI number onward."""
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM emi_schedule WHERE loan_id = ? AND emi_number >= ? AND status IN ('Unpaid','Partial')",
            (loan_id, from_emi_number)
        )


# ── Payments ─────────────────────────────────────────────────────────

def insert_payment(payment: dict):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO payments
                (loan_id, payment_id, payment_date, amount_paid, payment_type,
                 emi_number, remaining_balance_after_payment, notes, created_at)
            VALUES
                (:loan_id, :payment_id, :payment_date, :amount_paid, :payment_type,
                 :emi_number, :remaining_balance_after_payment, :notes, :created_at)
        """, payment)


def get_payments(loan_id: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM payments WHERE loan_id = ? ORDER BY payment_date, id",
            (loan_id,)
        ).fetchall()
        return [dict(r) for r in rows]


# ── Bulk read (used by sheets_sync push) ─────────────────────────────

def all_schedule() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM emi_schedule ORDER BY loan_id, emi_number"
        ).fetchall()
        return [dict(r) for r in rows]


def all_payments() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM payments ORDER BY loan_id, id"
        ).fetchall()
        return [dict(r) for r in rows]


# ── Bulk replace (used by sheets_sync pull) ───────────────────────────

def replace_all(loans: list[dict], schedule: list[dict], payments: list[dict]):
    """
    Atomically replace all table data with rows pulled from Google Sheets.
    All values arrive as strings — we cast to the correct Python types here.
    """

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
                     start_date, emi_amount, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                r["loan_id"], r["user_name"],
                _f(r["loan_amount"]), _f(r["interest_rate"]),
                _i(r["tenure_months"]), r["start_date"],
                _f(r["emi_amount"]), r["status"],
                r["created_at"], r["updated_at"],
            ))

        for r in schedule:
            conn.execute("""
                INSERT INTO emi_schedule
                    (id, loan_id, emi_number, due_date, emi_amount,
                     interest_component, principal_component, outstanding_balance,
                     status, paid_date, is_pre_emi)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                _i(r["id"]), r["loan_id"], int(float(r.get("emi_number") or 0)),
                r["due_date"], _f(r["emi_amount"]),
                _f(r["interest_component"]), _f(r["principal_component"]),
                _f(r["outstanding_balance"]), r["status"],
                r.get("paid_date") or "", _i(r.get("is_pre_emi") or 0) or 0,
            ))

        for r in payments:
            conn.execute("""
                INSERT INTO payments
                    (id, loan_id, payment_id, payment_date, amount_paid,
                     payment_type, emi_number, remaining_balance_after_payment,
                     notes, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                _i(r["id"]), r["loan_id"], r["payment_id"],
                r["payment_date"], _f(r["amount_paid"]),
                r["payment_type"], _i(r.get("emi_number")),
                _f(r["remaining_balance_after_payment"]),
                r.get("notes") or "", r["created_at"],
            ))
