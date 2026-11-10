"""
sheets_sync.py — Google Sheets ↔ SQLite sync
  • startup      : pull_bg()    Sheets → SQLite
  • after writes : push_async() SQLite → Sheets (coalesced; never dropped)
  • manual       : /api/sync/pull, /api/sync/push
Env: GOOGLE_SHEET_ID, GOOGLE_SHEETS_CREDS_JSON or GOOGLE_SHEETS_CREDS
"""
import logging
import os
import time
import threading
from datetime import datetime

import gspread
from gspread.utils import rowcol_to_a1

import database as db
import ledger

logger = logging.getLogger(__name__)

LOANS_COLS = [
    "loan_id", "user_name", "loan_amount", "interest_rate", "tenure_months",
    "start_date", "emi_amount", "status", "created_at", "updated_at",
    "sanction_date", "pre_emi_interest", "pre_emi_days", "orig_rate",
]
SCHEDULE_COLS = [
    "id", "loan_id", "emi_number", "due_date", "emi_amount",
    "interest_component", "principal_component", "outstanding_balance",
    "status", "paid_date", "is_pre_emi",
]
PAYMENTS_COLS = [
    "id", "loan_id", "payment_id", "payment_date", "amount_paid",
    "payment_type", "emi_number", "remaining_balance_after_payment",
    "notes", "created_at", "meta",
]

_gc = None
_sheet_id = ""
_enabled = False
_pulling = False
_pulled_ok = False
_last_pull = None
_last_push = None
_pull_err = None
_push_err = None

_op_lock = threading.Lock()      # only one Sheets read/write at a time
_state_lock = threading.Lock()   # guards the dirty flag
_dirty = False
_push_running = False
LOCK_WAIT = 60
PULL_TIMEOUT_SECONDS = 45


# ── Init ──────────────────────────────────────────────────────────────

def init(creds_path: str, sheet_id: str) -> bool:
    global _gc, _sheet_id, _enabled
    try:
        _gc = gspread.service_account(filename=creds_path)
        try:
            _gc.set_timeout((10, 30))
        except Exception:
            try:
                _gc.http_client.timeout = (10, 30)
            except Exception:
                logger.warning("Could not set gspread request timeout")
        try:
            _gc.http_client.session._refresh_timeout = 30
        except Exception:
            pass
        _sheet_id = sheet_id
        ss = _gc.open_by_key(sheet_id)
        _enabled = True
        logger.info("Google Sheets connected: %s", ss.title)
        return True
    except Exception as e:
        logger.warning("Google Sheets init failed: %s", e)
        _enabled = False
        return False


def init_from_env() -> bool:
    sheet_id = os.environ.get("GOOGLE_SHEET_ID")
    if not sheet_id:
        logger.info("Google Sheets not configured (GOOGLE_SHEET_ID missing)")
        return False
    creds_json = os.environ.get("GOOGLE_SHEETS_CREDS_JSON")
    if creds_json:
        creds_path = "/tmp/sheets_creds.json"
        try:
            with open(creds_path, "w") as f:
                f.write(creds_json)
        except Exception as e:
            logger.warning("Could not write creds: %s", e)
            return False
        return init(creds_path, sheet_id)
    return init(os.environ.get("GOOGLE_SHEETS_CREDS", "cred.json"), sheet_id)


# ── Worksheet helpers ─────────────────────────────────────────────────

def _open_ss():
    return _gc.open_by_key(_sheet_id)


def _get_or_create_ws(ss, name, headers):
    existing = {ws.title: ws for ws in ss.worksheets()}
    if name in existing:
        return existing[name]
    try:
        ws = ss.add_worksheet(title=name, rows=5000, cols=len(headers))
        ws.append_row(headers, value_input_option="RAW")
        logger.info("Created worksheet: %s", name)
        return ws
    except Exception as e:
        if "already exists" in str(e).lower():
            time.sleep(1.5)
            return ss.worksheet(name)
        raise RuntimeError(f"Could not get/create worksheet '{name}': {e}") from e


def _read_ws(ws):
    rows = ws.get_all_values()
    if len(rows) < 2:
        return []
    headers = rows[0]
    return [dict(zip(headers, row)) for row in rows[1:] if any(v.strip() for v in row)]


def _write_ws(ws, headers, data):
    """Write header + rows in ONE update call (no clear-then-append window where the tab is empty)."""
    matrix = [headers] + [
        [("" if row.get(c) is None else str(row.get(c))) for c in headers] for row in data
    ]
    needed = len(matrix)
    if ws.col_count < len(headers):
        ws.add_cols(len(headers) - ws.col_count)
    if ws.row_count < needed:
        ws.add_rows(needed - ws.row_count + 50)
    ws.update(range_name="A1", values=matrix, value_input_option="RAW")
    # blank out any leftover rows from a previously longer table (e.g. after a delete)
    if ws.row_count > needed:
        last_col = rowcol_to_a1(1, len(headers)).rstrip("0123456789")
        ws.batch_clear([f"A{needed + 1}:{last_col}{ws.row_count}"])


# ── Pull: Sheets → SQLite ─────────────────────────────────────────────

def pull_all() -> str:
    global _last_pull, _pull_err, _pulling, _pulled_ok
    if not _enabled:
        return "Google Sheets not configured"
    if not _op_lock.acquire(timeout=LOCK_WAIT):
        return "Sync already in progress — try again shortly"
    _pulling = True
    try:
        ss = _open_ss()
        ws_l = _get_or_create_ws(ss, "loans", LOANS_COLS)
        ws_s = _get_or_create_ws(ss, "emi_schedule", SCHEDULE_COLS)
        ws_p = _get_or_create_ws(ss, "payments", PAYMENTS_COLS)

        loans, schedule, payments = _read_ws(ws_l), _read_ws(ws_s), _read_ws(ws_p)

        if not loans and not payments:
            _last_pull, _pull_err, _pulled_ok = datetime.now(), None, True
            return "Google Sheets tabs ready — no data yet (local data retained)"

        db.replace_all(loans, schedule, payments)

        rebuilt = _rebuild_missing_schedules()
        if rebuilt:
            _write_ws(ws_s, SCHEDULE_COLS, db.all_schedule())

        _last_pull, _pull_err, _pulled_ok = datetime.now(), None, True
        msg = (f"Pulled {len(loans)} loans, {len(schedule)} schedule rows, {len(payments)} payments"
               + (f" — rebuilt {rebuilt} missing schedule(s)" if rebuilt else ""))
        logger.info(msg)
        return msg
    except Exception as e:
        _pull_err = str(e)
        logger.exception("Sheets pull failed")
        raise
    finally:
        _pulling = False
        _op_lock.release()


def _rebuild_missing_schedules() -> int:
    """For loans with no schedule rows, rebuild by replaying loan terms + payment history."""
    rebuilt = 0
    for loan in db.list_loans():
        lid = loan["loan_id"]
        if db.get_schedule(lid):
            continue
        try:
            st = ledger.replay(loan, db.get_payments_by_id(lid), [])
            db.replace_loan_data(lid, ledger.loan_fields(st.loan), st.schedule, st.payments)
            rebuilt += 1
            logger.info("Rebuilt %d schedule rows for loan %s", len(st.schedule), lid)
        except Exception as e:
            logger.warning("Could not rebuild schedule for %s: %s", lid, e)
    return rebuilt


def pull_bg():
    if _enabled:
        threading.Thread(target=_safe_pull, name="sheets-pull", daemon=True).start()


def _safe_pull():
    global _pulling, _pull_err
    result = {"done": False}

    def _run():
        try:
            pull_all()
        except Exception:
            pass
        finally:
            result["done"] = True

    t = threading.Thread(target=_run, name="sheets-pull-worker", daemon=True)
    t.start()
    t.join(timeout=PULL_TIMEOUT_SECONDS)
    if not result["done"]:
        _pull_err = f"Pull timed out after {PULL_TIMEOUT_SECONDS}s — Google Sheets may be slow or unreachable."
        _pulling = False


# ── Push: SQLite → Sheets ─────────────────────────────────────────────

def push_all() -> str:
    global _last_push, _push_err
    if not _enabled:
        return "Google Sheets not configured"
    if not _op_lock.acquire(timeout=LOCK_WAIT):
        raise RuntimeError("Another sync is still running — try again shortly")
    try:
        loans, schedule, payments = db.list_loans(), db.all_schedule(), db.all_payments()

        # Never overwrite the Sheet with an empty DB that never loaded from it.
        if not loans and not payments and not _pulled_ok:
            return "Nothing pushed: local DB is empty and Sheets hasn't been pulled yet (protecting your data)"

        ss = _open_ss()
        ws_l = _get_or_create_ws(ss, "loans", LOANS_COLS)
        ws_s = _get_or_create_ws(ss, "emi_schedule", SCHEDULE_COLS)
        ws_p = _get_or_create_ws(ss, "payments", PAYMENTS_COLS)

        _write_ws(ws_l, LOANS_COLS, loans)
        _write_ws(ws_s, SCHEDULE_COLS, schedule)
        _write_ws(ws_p, PAYMENTS_COLS, payments)

        _last_push, _push_err = datetime.now(), None
        msg = f"Pushed {len(loans)} loans, {len(schedule)} schedule rows, {len(payments)} payments"
        logger.info(msg)
        return msg
    except Exception as e:
        _push_err = str(e)
        logger.exception("Sheets push failed")
        raise
    finally:
        _op_lock.release()


def push_async():
    """Request a push. Requests made while one is running are coalesced, never lost."""
    global _dirty, _push_running
    if not _enabled:
        return
    with _state_lock:
        _dirty = True
        if _push_running:
            return
        _push_running = True
    threading.Thread(target=_push_worker, name="sheets-push", daemon=True).start()


def _push_worker():
    global _dirty, _push_running
    fails = 0
    while True:
        with _state_lock:
            if not _dirty or fails >= 3:
                _push_running = False
                return
            _dirty = False
        try:
            push_all()
            fails = 0
        except Exception:
            fails += 1
            time.sleep(5 * fails)
            with _state_lock:
                _dirty = True


# ── Status ────────────────────────────────────────────────────────────

def get_status() -> dict:
    return {
        "enabled": _enabled,
        "pulling": _pulling,
        "last_pull": _last_pull.isoformat() if _last_pull else None,
        "last_push": _last_push.isoformat() if _last_push else None,
        "pull_error": _pull_err,
        "push_error": _push_err,
    }