"""
app.py — LoanLedger Flask backend
SQLite is the local working DB; Google Sheets is the cloud store.
"""
import functools
import io
import logging
import os
import threading
import uuid
from datetime import datetime

from flask import Flask, jsonify, request, send_file
from flask_cors import CORS

import database as db
import emi_calculator as calc
import ledger
import sheets_sync
from pdf_report import generate_pdf

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

APP_PASSPHRASE = os.environ.get("APP_PASSPHRASE", "")
app = Flask(__name__, static_folder="../frontend", static_url_path="")

_allowed_origins = os.environ.get("ALLOWED_ORIGINS", "*")
if _allowed_origins == "*":
    CORS(app)
else:
    CORS(app, origins=[o.strip() for o in _allowed_origins.split(",") if o.strip()])

db.init_db()
sheets_sync.init_from_env()
sheets_sync.pull_bg()

_write_lock = threading.RLock()


def locked(fn):
    """Serialise read-modify-write endpoints so two requests can't interleave."""
    @functools.wraps(fn)
    def wrapper(*a, **k):
        with _write_lock:
            return fn(*a, **k)
    return wrapper


class NotFound(Exception):
    pass


# ── Helpers ───────────────────────────────────────────────────────────

def ok(data, code=200):  return jsonify({"success": True,  "data": data}), code
def err(msg, code=400):  return jsonify({"success": False, "error": msg}), code
def now_str():           return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
def today_str():         return datetime.now().strftime("%Y-%m-%d")


def require(body, *fields):
    missing = [f for f in fields if not body.get(f) and body.get(f) != 0]
    if missing:
        raise ValueError(f"Missing: {', '.join(missing)}")


def _state(loan_id):
    loan = db.get_loan(loan_id)
    if not loan:
        raise NotFound(f"Loan {loan_id} not found")
    return ledger.State(loan, db.get_schedule(loan_id), db.get_payments_by_id(loan_id))


def _persist(st):
    """Save the whole loan state in one transaction, then push to Sheets."""
    db.replace_loan_data(st.loan["loan_id"], ledger.loan_fields(st.loan), st.schedule, st.payments)
    sheets_sync.push_async()


@app.errorhandler(NotFound)
def _nf(e):            return err(str(e), 404)
@app.errorhandler(ValueError)
def _ve(e):            return err(str(e), 400)
@app.errorhandler(TypeError)
def _te(e):            return err(str(e), 400)


# ── Auth gate ─────────────────────────────────────────────────────────
_EXEMPT_PATHS = {"/api/health", "/api/auth/check"}


@app.before_request
def _check_passphrase():
    if request.method == "OPTIONS":
        return
    if not APP_PASSPHRASE:
        return
    if not request.path.startswith("/api/"):
        return
    if request.path in _EXEMPT_PATHS:
        return
    if request.headers.get("X-App-Passphrase", "") != APP_PASSPHRASE:
        return err("Unauthorized — incorrect or missing passphrase", 401)


@app.route("/api/auth/check", methods=["POST"])
def auth_check():
    if not APP_PASSPHRASE:
        return ok({"auth_enabled": False})
    body = request.get_json(force=True) or {}
    if body.get("passphrase") == APP_PASSPHRASE:
        return ok({"auth_enabled": True, "valid": True})
    return err("Incorrect passphrase", 401)


# ── Frontend / health ─────────────────────────────────────────────────

@app.route("/")
def index():
    resp = app.make_response(app.send_static_file("index.html"))
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.route("/api/health")
def health():
    return ok({"status": "ok", "db": db.DB_PATH})


# ── Sync ──────────────────────────────────────────────────────────────

@app.route("/api/sync/status")
def sync_status():
    return ok(sheets_sync.get_status())


@app.route("/api/sync/pull", methods=["POST"])
def sync_pull():
    try:
        return ok({"message": sheets_sync.pull_all()})
    except Exception as e:
        return err(str(e))


@app.route("/api/sync/push", methods=["POST"])
def sync_push():
    try:
        return ok({"message": sheets_sync.push_all()})
    except Exception as e:
        return err(str(e))


# ── EMI Calculator (stateless) ────────────────────────────────────────

@app.route("/api/calculate-emi", methods=["POST"])
def calculate_emi_api():
    body = request.get_json(force=True)
    require(body, "principal", "interest_rate", "tenure_months")
    P, rate, n = float(body["principal"]), float(body["interest_rate"]), int(body["tenure_months"])
    sd, ed = str(body.get("sanction_date", "")), str(body.get("start_date", ""))
    emi = calc.calculate_emi(P, rate, n)
    total = round(emi * n, 2)
    pre_int, days = (0.0, 0)
    if sd and ed and sd != ed:
        pre_int, days = calc.calculate_pre_emi(P, rate, sd, ed)
    return ok({"emi": emi, "total_payment": total, "total_interest": round(total - P, 2),
               "pre_emi_interest": pre_int, "pre_emi_days": days})


# ── Loans ─────────────────────────────────────────────────────────────

@app.route("/api/loans", methods=["GET"])
def list_loans():
    return ok(db.list_loans())


@app.route("/api/loans", methods=["POST"])
@locked
def create_loan():
    body = request.get_json(force=True)
    require(body, "user_name", "loan_amount", "interest_rate",
            "tenure_months", "sanction_date", "start_date")
    P, rate = float(body["loan_amount"]), float(body["interest_rate"])
    months = int(body["tenure_months"])
    sanction_date, start_date = str(body["sanction_date"]), str(body["start_date"])
    if P <= 0 or months <= 0:
        raise ValueError("Loan amount and tenure must be greater than zero")
    if start_date < sanction_date:
        return err("First EMI date cannot be before sanction date")

    emi = calc.calculate_emi(P, rate, months)
    pre_int, pre_days = calc.calculate_pre_emi(P, rate, sanction_date, start_date)
    loan_id = "L" + uuid.uuid4().hex[:8].upper()
    now = now_str()
    db.insert_loan({
        "loan_id": loan_id, "user_name": str(body["user_name"]),
        "loan_amount": P, "interest_rate": rate, "tenure_months": months,
        "sanction_date": sanction_date, "start_date": start_date, "emi_amount": emi,
        "pre_emi_interest": pre_int, "pre_emi_days": pre_days, "orig_rate": rate,
        "status": "Active", "created_at": now, "updated_at": now,
    })
    schedule = calc.generate_schedule(loan_id, P, rate, months, start_date, sanction_date=sanction_date)
    db.insert_schedule_rows(schedule)
    sheets_sync.push_async()
    regular = len([r for r in schedule if not r["is_pre_emi"]])
    note = (f"Pre-EMI interest ₹{pre_int} for {pre_days} days added as EMI #0."
            if pre_int else "No stub period.")
    return ok({"loan_id": loan_id, "emi_amount": emi, "total_emis": regular,
               "pre_emi_interest": pre_int, "pre_emi_days": pre_days,
               "message": f"Loan created. {note}"}, 201)


@app.route("/api/loans/<loan_id>", methods=["GET"])
def get_loan(loan_id):
    loan = db.get_loan(loan_id)
    if not loan:
        return err(f"Loan {loan_id} not found", 404)
    return ok(loan)


@app.route("/api/loans/<loan_id>", methods=["DELETE"])
@locked
def delete_loan(loan_id):
    if not db.get_loan(loan_id):
        return err(f"Loan {loan_id} not found", 404)
    db.delete_loan(loan_id)
    sheets_sync.push_async()
    return ok({"message": f"Loan {loan_id} deleted"})


@app.route("/api/loans/<loan_id>/schedule", methods=["GET"])
def get_schedule(loan_id):
    if not db.get_loan(loan_id):
        return err(f"Loan {loan_id} not found", 404)
    return ok(db.get_schedule(loan_id))


@app.route("/api/loans/<loan_id>/summary", methods=["GET"])
def get_summary(loan_id):
    loan = db.get_loan(loan_id)
    if not loan:
        return err(f"Loan {loan_id} not found", 404)
    schedule = db.get_schedule(loan_id)
    s = calc.compute_summary(loan, schedule, db.get_payments(loan_id))
    pre_due = sum(float(r["emi_amount"]) for r in schedule
                  if int(r.get("is_pre_emi") or 0) and r["status"] in ledger.OPEN)
    s["foreclosure_amount"] = 0.0 if loan["status"] == "Closed" else round(s["outstanding_principal"] + pre_due, 2)
    return ok(s)


# ── Payments ──────────────────────────────────────────────────────────

@app.route("/api/loans/<loan_id>/payments", methods=["GET"])
def get_payments(loan_id):
    if not db.get_loan(loan_id):
        return err(f"Loan {loan_id} not found", 404)
    return ok(db.get_payments(loan_id))


@app.route("/api/loans/<loan_id>/payments", methods=["POST"])
@locked
def add_payment(loan_id):
    body = request.get_json(force=True)
    require(body, "amount_paid", "payment_date")
    st = _state(loan_id)
    info = ledger.apply_payment(st, {
        "amount_paid": float(body["amount_paid"]),
        "payment_date": str(body["payment_date"]),
        "payment_type": str(body.get("payment_type", "EMI")),
        "emi_number": body.get("emi_number"),
        "notes": str(body.get("notes", "")),
    })
    _persist(st)
    return ok(info, 201)


@app.route("/api/loans/<loan_id>/payments/<payment_id>", methods=["DELETE"])
@locked
def delete_payment(loan_id, payment_id):
    """
    Delete a mistaken entry (EMI / part payment / rate change / foreclosure).
    The loan is rebuilt from its ORIGINAL terms by replaying every remaining
    entry, so schedule, balance, EMI and status are all recalculated, then the
    result is saved and pushed to Google Sheets.
    """
    loan = db.get_loan(loan_id)
    if not loan:
        return err(f"Loan {loan_id} not found", 404)
    events = db.get_payments_by_id(loan_id)
    target = next((p for p in events if p["payment_id"] == payment_id), None)
    if not target:
        return err(f"Payment {payment_id} not found", 404)

    stored = db.get_schedule(loan_id)

    # Safety check: can history be replayed exactly as it stands today?
    if not request.args.get("force"):
        try:
            full = ledger.replay(loan, events, stored)
            same = ledger.matches(ledger.signature(full.loan, full.schedule),
                                  ledger.signature(loan, stored))
        except ValueError:
            same = False
        if not same:
            return err("LEGACY: This loan has older history (e.g. a rate change or a "
                       "reduce-EMI choice) that wasn't recorded in enough detail to replay "
                       "exactly. Deleting will recalculate from the original loan terms plus "
                       "the remaining entries, which may differ from what you see now.", 409)

    remaining = [p for p in events if p["payment_id"] != payment_id]
    try:
        st = ledger.replay(loan, remaining, stored)
    except ValueError as e:
        return err(f"Can't delete this entry because a later entry depends on it: {e}. "
                   f"Delete the later entries first.", 409)

    _persist(st)
    return ok({"deleted": payment_id, "loan_status": st.loan["status"],
               "emi_amount": st.loan["emi_amount"], "interest_rate": st.loan["interest_rate"],
               "message": f"Deleted {target['payment_type']} of ₹{float(target['amount_paid']):,.0f}. "
                          f"Schedule recalculated from {len(remaining)} remaining entr"
                          f"{'y' if len(remaining) == 1 else 'ies'}."})


# ── Part payment ──────────────────────────────────────────────────────

@app.route("/api/loans/<loan_id>/part-payment", methods=["POST"])
@locked
def part_payment(loan_id):
    body = request.get_json(force=True)
    require(body, "amount", "payment_date")
    st = _state(loan_id)
    info = ledger.apply_part_payment(st, {
        "amount_paid": float(body["amount"]),
        "payment_date": str(body["payment_date"]),
        "payment_type": ledger.TYPE_PART,
        "notes": str(body.get("notes") or "Part Payment"),
        "meta": {"reduce_emi": bool(body.get("reduce_emi", False))},
    })
    _persist(st)
    return ok(info)

@app.route("/api/loans/<loan_id>/recalculate", methods=["POST"])
@locked
def recalculate(loan_id):
    body = request.get_json(silent=True) or {}
    st = _state(loan_id)
    new_rows, rep = ledger.resplit_schedule(st.loan, st.schedule, st.payments)
    if body.get("preview"):
        return ok({**rep, "saved": False})
    db.replace_schedule(loan_id, new_rows)
    sheets_sync.push_async()
    return ok({**rep, "saved": True,
               "message": f"Schedule recalculated. {len(rep['part_payment_cycles'])} part-payment cycle(s) re-split; "
                          f"payoff now {rep['new_last_due']} (was {rep['old_last_due']})."})

def _interest_by_month(schedule):
    out = {}
    for r in schedule:
        if int(r.get("is_pre_emi") or 0):
            continue
        k = str(r["due_date"])[:7]
        out[k] = out.get(k, 0.0) + float(r["interest_component"])
    return out


@app.route("/api/loans/<loan_id>/part-payment-savings", methods=["GET"])
def part_payment_savings(loan_id):
    """Interest saved by each part payment: actual history vs. history without that payment."""
    loan = db.get_loan(loan_id)
    if not loan:
        return err(f"Loan {loan_id} not found", 404)
    events = db.get_payments_by_id(loan_id)
    pps = [p for p in events if p["payment_type"] == ledger.TYPE_PART]
    empty = {"loan_id": loan_id, "part_payments": [], "combined_monthly_savings": [],
             "combined_total_saved": 0.0}
    if not pps:
        return ok({**empty, "message": "No part payments recorded for this loan."})

    stored = db.get_schedule(loan_id)
    try:
        actual = _interest_by_month(ledger.replay(loan, events, stored).schedule)
    except ValueError as e:
        return ok({**empty, "message": f"Could not compute savings: {e}"})

    results, combined = [], {}
    for pp in pps:
        try:
            alt = ledger.replay(loan, [p for p in events if p["payment_id"] != pp["payment_id"]], stored)
        except ValueError:
            continue
        without = _interest_by_month(alt.schedule)
        first = str(pp["payment_date"])[:7]
        monthly, total = [], 0.0
        for m in sorted(m for m in set(actual) | set(without) if m >= first):
            w, a = without.get(m, 0.0), actual.get(m, 0.0)
            s = round(w - a, 2)
            if not monthly and s == 0:
                continue  # skip months before the payment had any effect
            monthly.append({"month": m, "interest_without_part_payment": round(w, 2),
                            "interest_with_part_payment": round(a, 2), "interest_saved": s})
            total += s
            combined[m] = combined.get(m, 0.0) + s
        results.append({"payment_id": pp["payment_id"], "payment_date": pp["payment_date"],
                        "amount_paid": float(pp["amount_paid"]), "monthly_savings": monthly,
                        "total_interest_saved": round(total, 2), "months_compared": len(monthly)})

    return ok({"loan_id": loan_id, "part_payments": results,
               "combined_monthly_savings": [{"month": m, "interest_saved": round(v, 2)}
                                            for m, v in sorted(combined.items())],
               "combined_total_saved": round(sum(r["total_interest_saved"] for r in results), 2)})


# ── Rate change ───────────────────────────────────────────────────────

@app.route("/api/loans/<loan_id>/update-rate", methods=["POST"])
@locked
def update_rate(loan_id):
    body = request.get_json(force=True)
    require(body, "new_rate")
    st = _state(loan_id)
    info = ledger.apply_rate_change(st, {
        "amount_paid": 0.0,
        "payment_date": str(body.get("effective_date") or today_str()),
        "payment_type": ledger.TYPE_RATE,
        "meta": {"new_rate": float(body["new_rate"]),
                 "from_emi": int(body["from_emi"]) if body.get("from_emi") else None,
                 "keep_emi": bool(body.get("keep_emi", False))},
    })
    _persist(st)
    return ok(info)


# ── Foreclose ─────────────────────────────────────────────────────────

@app.route("/api/loans/<loan_id>/foreclose", methods=["POST"])
@locked
def foreclose(loan_id):
    body = request.get_json(force=True)
    require(body, "payment_date")
    st = _state(loan_id)
    info = ledger.apply_foreclose(st, {"amount_paid": 0.0, "payment_date": str(body["payment_date"]),
                                       "payment_type": ledger.TYPE_FORE})
    _persist(st)
    return ok(info)


# ── PDF ───────────────────────────────────────────────────────────────

@app.route("/api/loans/<loan_id>/report")
def download_report(loan_id):
    loan = db.get_loan(loan_id)
    if not loan:
        return err(f"Loan {loan_id} not found", 404)
    schedule, payments = db.get_schedule(loan_id), db.get_payments(loan_id)
    summary = calc.compute_summary(loan, schedule, payments)
    return send_file(io.BytesIO(generate_pdf(loan, schedule, payments, summary)),
                     mimetype="application/pdf", as_attachment=True,
                     download_name=f"loan_{loan_id}.pdf")


@app.errorhandler(404)
def not_found(e):   return err("Not found", 404)

@app.errorhandler(500)
def server_error(e):
    logger.exception("Unhandled error")
    return err("Internal server error", 500)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"LoanLedger running on http://localhost:{port}  (DB: {db.DB_PATH})")
    app.run(host="0.0.0.0", port=port, debug=False)