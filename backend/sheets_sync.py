"""
sheets_sync.py — Google Sheets ↔ SQLite bidirectional sync

Strategy
  • On startup   : pull_bg() fires a background thread → Sheets → SQLite
  • After writes  : push_async() fires a background thread → SQLite → Sheets
  • Manual (HTTP) : /api/sync/pull  and  /api/sync/push

Config (environment variables / defaults in init_from_env)
  GOOGLE_SHEETS_CREDS   path to service-account JSON key file
  GOOGLE_SHEET_ID       spreadsheet ID from the URL
"""

import logging
import os
import time
import threading
from datetime import datetime

import gspread

import database as db

logger = logging.getLogger(__name__)

LOANS_COLS = [
    "loan_id", "user_name", "loan_amount", "interest_rate",
    "tenure_months", "start_date", "emi_amount", "status",
    "created_at", "updated_at",
]
SCHEDULE_COLS = [
    "id", "loan_id", "emi_number", "due_date", "emi_amount",
    "interest_component", "principal_component", "outstanding_balance",
    "status", "paid_date", "is_pre_emi",
]
PAYMENTS_COLS = [
    "id", "loan_id", "payment_id", "payment_date", "amount_paid",
    "payment_type", "emi_number", "remaining_balance_after_payment",
    "notes", "created_at",
]

_gc:        gspread.Client | None = None
_sheet_id:  str                   = ""
_enabled:   bool                  = False
_busy:      bool                  = False
_pulling:   bool                  = False
_last_pull: datetime | None       = None
_last_push: datetime | None       = None
_pull_err:  str | None            = None
_push_err:  str | None            = None


# ── Init ──────────────────────────────────────────────────────────────

def init(creds_path: str, sheet_id: str) -> bool:
    global _gc, _sheet_id, _enabled
    try:
        _gc       = gspread.service_account(filename=creds_path)
        # Give EVERY Sheets API call a connect+read deadline. gspread 6.x
        # defaults to no timeout (waits forever), so a slow/stalled call —
        # especially on the synchronous /api/sync/pull and /api/sync/push
        # endpoints — would hang the request and leave the app's Pull/Push
        # button spinning indefinitely. (10s connect, 30s read.)
        try:
            _gc.set_timeout((10, 30))
        except Exception:
            try:
                _gc.http_client.timeout = (10, 30)
            except Exception:
                logger.warning("Could not set gspread request timeout")
        # set_timeout above bounds only the data request — NOT the OAuth token
        # refresh google-auth performs inside the same AuthorizedSession, which
        # defaults to no timeout (refresh_timeout=None). A stalled token
        # endpoint would therefore still hang a pull/push until the 45s watchdog
        # trips. Bound the refresh too so the call fails fast with a clean error
        # instead of silently wedging the thread. (best-effort, private attr)
        try:
            _gc.http_client.session._refresh_timeout = 30
        except Exception:
            pass
        _sheet_id = sheet_id
        # Quick connectivity check
        ss = _gc.open_by_key(sheet_id)
        _enabled = True
        logger.info("Google Sheets connected: %s", ss.title)
        return True
    except Exception as e:
        logger.warning("Google Sheets init failed: %s", e)
        _enabled = False
        return False


def init_from_env() -> bool:
    """
    Reads config from environment variables — no defaults, no secrets in code.

      GOOGLE_SHEET_ID         spreadsheet ID (required)
      GOOGLE_SHEETS_CREDS_JSON  full service-account JSON as a single env var
                                (preferred for cloud hosts like Render, where
                                 uploading a file isn't convenient)
      GOOGLE_SHEETS_CREDS       path to a service-account JSON file on disk
                                (used if GOOGLE_SHEETS_CREDS_JSON is not set —
                                 convenient for local/dev use)
    """
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
            logger.warning("Could not write creds from GOOGLE_SHEETS_CREDS_JSON: %s", e)
            return False
        return init(creds_path, sheet_id)

    creds_path = os.environ.get("GOOGLE_SHEETS_CREDS", "cred.json")
    return init(creds_path, sheet_id)


# ── Worksheet helpers ─────────────────────────────────────────────────

def _open_ss() -> gspread.Spreadsheet:
    """Open the spreadsheet fresh (avoids stale Spreadsheet objects)."""
    return _gc.open_by_key(_sheet_id)


def _get_or_create_ws(ss: gspread.Spreadsheet,
                      name: str,
                      headers: list[str]) -> gspread.Worksheet:
    """
    Return the named worksheet.  If it does not exist, create it and
    write the header row.  Handles Google Sheets API eventual-consistency
    by retrying the lookup after a short sleep when 'already exists' is
    returned by add_worksheet.
    """
    # Fetch a fresh server-side list every time (no local cache)
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
            # API propagation lag — sheet was created just now, wait and retry
            time.sleep(1.5)
            return ss.worksheet(name)
        raise RuntimeError(f"Could not get/create worksheet '{name}': {e}") from e


def _read_ws(ws: gspread.Worksheet) -> list[dict]:
    rows = ws.get_all_values()
    if len(rows) < 2:
        return []
    headers = rows[0]
    return [
        dict(zip(headers, row))
        for row in rows[1:]
        if any(v.strip() for v in row)
    ]


def _write_ws(ws: gspread.Worksheet, headers: list[str], data: list[dict]):
    ws.clear()
    ws.append_row(headers, value_input_option="RAW")
    if data:
        matrix = [[("" if row.get(c) is None else str(row.get(c))) for c in headers] for row in data]
        ws.append_rows(matrix, value_input_option="RAW")


# ── Pull: Sheets → SQLite ─────────────────────────────────────────────

def pull_all() -> str:
    global _last_pull, _pull_err, _pulling, _busy
    if not _enabled:
        return "Google Sheets not configured"
    if _busy:
        return "Sync already in progress — try again shortly"

    _busy = _pulling = True
    try:
        ss = _open_ss()

        ws_l = _get_or_create_ws(ss, "loans",        LOANS_COLS)
        ws_p = _get_or_create_ws(ss, "payments",      PAYMENTS_COLS)

        loans    = _read_ws(ws_l)
        payments = _read_ws(ws_p)

        if not loans and not payments:
            _last_pull = datetime.now()
            _pull_err  = None
            return "Google Sheets tabs created — no data yet (local data retained)"

        # Pull loans + payments from Sheets into SQLite.
        # emi_schedule is NOT pulled from Sheets — it is always generated
        # locally from the loan data and kept authoritative in SQLite.
        # We clear the sheet tab and rewrite it from local data to fix any
        # corruption (e.g. emi_number stored as empty string).
        schedule = db.all_schedule()
        db.replace_all(loans, schedule, payments)

        # Rewrite the emi_schedule sheet from the now-correct local data
        ws_s = _get_or_create_ws(ss, "emi_schedule", SCHEDULE_COLS)
        _write_ws(ws_s, SCHEDULE_COLS, db.all_schedule())

        _last_pull = datetime.now()
        _pull_err  = None
        msg = (f"Pulled {len(loans)} loans, "
               f"{len(payments)} payments — "
               f"emi_schedule rebuilt from local data")
        logger.info(msg)
        return msg

    except Exception as e:
        _pull_err = str(e)
        logger.exception("Sheets pull failed")
        raise

    finally:
        _busy = _pulling = False


def pull_bg():
    if not _enabled:
        return
    threading.Thread(target=_safe_pull, name="sheets-pull", daemon=True).start()


# Maximum time we'll wait for a pull before giving up on it and resetting
# state. gspread/google-auth don't set network timeouts by default, so a
# stalled connection to Google's API can otherwise hang this thread forever
# — leaving `_pulling` stuck True and the app showing "Loading..." endlessly.
PULL_TIMEOUT_SECONDS = 45


def _safe_pull():
    """
    Runs pull_all() in its own watchdog-supervised sub-thread. If it doesn't
    finish within PULL_TIMEOUT_SECONDS, we give up waiting and reset state
    so the app recovers — even though the underlying thread (stuck in a
    network call with no timeout) may continue running harmlessly in the
    background until/unless it eventually errors out on its own.
    """
    global _busy, _pulling, _pull_err

    result = {"done": False, "error": None}

    def _run():
        try:
            pull_all()
        except Exception as e:
            result["error"] = str(e)
        finally:
            result["done"] = True

    t = threading.Thread(target=_run, name="sheets-pull-worker", daemon=True)
    t.start()
    t.join(timeout=PULL_TIMEOUT_SECONDS)

    if not result["done"]:
        logger.warning(
            "Sheets pull exceeded %ss timeout — resetting status so the app "
            "isn't stuck. The underlying request may still complete in the "
            "background; local data is unaffected.", PULL_TIMEOUT_SECONDS
        )
        _pull_err = f"Pull timed out after {PULL_TIMEOUT_SECONDS}s — Google Sheets may be slow or unreachable. Local data was not changed."
        _busy = _pulling = False


# ── Push: SQLite → Sheets ─────────────────────────────────────────────

def push_all() -> str:
    global _last_push, _push_err, _busy
    if not _enabled:
        return "Google Sheets not configured"
    if _busy:
        return "Sync already in progress — try again shortly"

    _busy = True
    try:
        ss = _open_ss()

        ws_l = _get_or_create_ws(ss, "loans",        LOANS_COLS)
        ws_s = _get_or_create_ws(ss, "emi_schedule",  SCHEDULE_COLS)
        ws_p = _get_or_create_ws(ss, "payments",      PAYMENTS_COLS)

        loans    = db.list_loans()
        schedule = db.all_schedule()
        payments = db.all_payments()

        _write_ws(ws_l, LOANS_COLS,    loans)
        _write_ws(ws_s, SCHEDULE_COLS, schedule)
        _write_ws(ws_p, PAYMENTS_COLS, payments)

        _last_push = datetime.now()
        _push_err  = None
        msg = (f"Pushed {len(loans)} loans, "
               f"{len(schedule)} schedule rows, "
               f"{len(payments)} payments")
        logger.info(msg)
        return msg

    except Exception as e:
        _push_err = str(e)
        logger.exception("Sheets push failed")
        raise

    finally:
        _busy = False


def push_async():
    if not _enabled:
        return
    threading.Thread(target=_safe_push, name="sheets-push", daemon=True).start()


def _safe_push():
    """Same watchdog pattern as _safe_pull — see its docstring for why this
    is needed (gspread/google-auth calls have no default network timeout)."""
    global _busy, _push_err

    result = {"done": False}

    def _run():
        try:
            push_all()
        except Exception:
            pass
        finally:
            result["done"] = True

    t = threading.Thread(target=_run, name="sheets-push-worker", daemon=True)
    t.start()
    t.join(timeout=PULL_TIMEOUT_SECONDS)

    if not result["done"]:
        logger.warning("Sheets push exceeded %ss timeout — resetting status.", PULL_TIMEOUT_SECONDS)
        _push_err = f"Push timed out after {PULL_TIMEOUT_SECONDS}s — Google Sheets may be slow or unreachable."
        _busy = False


# ── Status ────────────────────────────────────────────────────────────

def get_status() -> dict:
    return {
        "enabled":    _enabled,
        "pulling":    _pulling,
        "last_pull":  _last_pull.isoformat() if _last_pull else None,
        "last_push":  _last_push.isoformat() if _last_push else None,
        "pull_error": _pull_err,
        "push_error": _push_err,
    }
