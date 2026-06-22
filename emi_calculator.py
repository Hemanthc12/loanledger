"""
app.py — LoanLedger Flask backend
SQLite is the local working DB; Google Sheets is the cloud store.
On startup: pull Sheets → SQLite (background).
After writes: push SQLite → Sheets (background, non-blocking).
Run: python app.py
"""

import io
import logging
import os
import uuid
from datetime import datetime

from flask import Flask, jsonify, request, send_file
from flask_cors import CORS

import database as db
import emi_calculator as calc
import sheets_sync
from pdf_report import generate_pdf

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ── Simple shared-passphrase auth ───────────────────────────────────────
# This app has no per-user accounts — it's meant for a small family group
# sharing one passphrase. Set APP_PASSPHRASE in the environment to enable.
# If APP_PASSPHRASE is unset, auth is disabled (useful for local dev only —
# never deploy publicly without setting this).
APP_PASSPHRASE = os.environ.get("APP_PASSPHRASE", "")

app = Flask(__name__, static_folder="../frontend", static_url_path="")

# CORS: restrict to known origins in production via ALLOWED_ORIGINS env var
# (comma-separated). Defaults to "*" for easy local development.
_allowed_origins = os.environ.get("ALLOWED_ORIGINS", "*")
if _allowed_origins == "*":
    CORS(app)
else:
    CORS(app, origins=[o.strip() for o in _allowed_origins.split(",") if o.strip()])

db.init_db()

# Connect to Google Sheets and kick off background pull
sheets_sync.init_from_env()
sheets_sync.pull_bg()


# ── Auth gate ─────────────────────────────────────────────────────────
# Every /api/* request (except /api/health and /api/auth/check) must send
# header  X-App-Passphrase: <the family passphrase>
# The frontend asks for this once and stores it in localStorage.

_EXEMPT_PATHS = {"/api/health", "/api/auth/check"}


@app.before_request
def _check_passphrase():
    if not APP_PASSPHRASE:
        return  # auth disabled (local dev)
    if not request.path.startswith("/api/"):
        return  # static frontend files are not sensitive
    if request.path in _EXEMPT_PATHS:
        return
    supplied = request.headers.get("X-App-Passphrase", "")
    if supplied != APP_PASSPHRASE:
        return err("Unauthorized — incorrect or missing passphrase", 401)


@app.route("/api/auth/check", methods=["POST"])
def auth_check():
    if not APP_PASSPHRASE:
        return ok({"auth_enabled": False})
    body = request.get_json(force=True) or {}
    if body.get("passphrase") == APP_PASSPHRASE:
        return ok({"auth_enabled": True, "valid": True})
    return err("Incorrect passphrase", 401)


# ── Helpers ───────────────────────────────────────────────────────────

def ok(data, code=200):  return jsonify({"success": True,  "data": data}), code
def err(msg, code=400):  return jsonify({"success": False, "error": msg}), code
def now_str():           return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def require(body, *fields):
    missing = [f for f in fields if not body.get(f) and body.get(f) != 0]
    if missing:
        raise ValueError(f"Missing: {', '.join(missing)}")


# ── Frontend ──────────────────────────────────────────────────────────

@app.route("/")
def index():
    resp = app.make_response(app.send_static_file("index.html"))
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


# ── Health ────────────────────────────────────────────────────────────

@app.route("/api/health")
def health():
    return ok({"status": "ok", "db": db.DB_PATH})


# ── Sync endpoints ────────────────────────────────────────────────────

@app.route("/api/sync/status")
def sync_status():
    return ok(sheets_sync.get_status())


@app.route("/api/sync/pull", methods=["POST"])
def sync_pull():
    try:
        msg = sheets_sync.pull_all()
        return ok({"message": msg})
    except Exception as e:
        return err(str(e))


@app.route("/api/sync/push", methods=["POST"])
def sync_push():
    try:
        msg = sheets_sync.push_all()
        return ok({"message": msg})
    except Exception as e:
        return err(str(e))


# ── EMI Calculator (stateless) ────────────────────────────────────────

@app.route("/api/calculate-emi", methods=["POST"])
def calculate_emi_api():
    body = request.get_json(force=True)
    try:
        require(body, "principal", "interest_rate", "tenure_months")
        P    = float(body["principal"])
        rate = float(body["interest_rate"])
        n    = int(body["tenure_months"])
        sd   = str(body.get("sanction_date", ""))
        ed   = str(body.get("start_date", ""))

        emi      = calc.calculate_emi(P, rate, n)
        total    = round(emi * n, 2)
        interest = round(total - P, 2)

        pre_int, days = (0.0, 0)
        if sd and ed and sd != ed:
            pre_int, days = calc.calculate_pre_emi(P, rate, sd, ed)

        return ok({
            "emi": emi, "total_payment": total, "total_interest": interest,
            "pre_emi_interest": pre_int, "pre_emi_days": days,
        })
    except (ValueError, TypeError) as e:
        return err(str(e))


# ── Loans ─────────────────────────────────────────────────────────────

@app.route("/api/loans", methods=["GET"])
def list_loans():
    return ok(db.list_loans())


@app.route("/api/loans", methods=["POST"])
def create_loan():
    body = request.get_json(force=True)
    try:
        require(body, "user_name", "loan_amount", "interest_rate",
                "tenure_months", "sanction_date", "start_date")

        P             = float(body["loan_amount"])
        rate          = float(body["interest_rate"])
        months        = int(body["tenure_months"])
        sanction_date = str(body["sanction_date"])
        start_date    = str(body["start_date"])

        if start_date < sanction_date:
            return err("First EMI date cannot be before sanction date")

        emi = calc.calculate_emi(P, rate, months)
        pre_int, pre_days = calc.calculate_pre_emi(P, rate, sanction_date, start_date)

        loan_id = "L" + uuid.uuid4().hex[:8].upper()
        now = now_str()

        loan_row = {
            "loan_id": loan_id, "user_name": str(body["user_name"]),
            "loan_amount": P, "interest_rate": rate,
            "tenure_months": months, "sanction_date": sanction_date,
            "start_date": start_date, "emi_amount": emi,
            "pre_emi_interest": pre_int, "pre_emi_days": pre_days,
            "status": "Active", "created_at": now, "updated_at": now,
        }
        db.insert_loan(loan_row)

        schedule = calc.generate_schedule(
            loan_id, P, rate, months, start_date,
            sanction_date=sanction_date,
        )
        db.insert_schedule_rows(schedule)

        regular_count = len([r for r in schedule if not r["is_pre_emi"]])
        logger.info(f"Created {loan_id} for {body['user_name']}, EMI={emi}, pre_emi={pre_int} ({pre_days} days)")
        sheets_sync.push_async()
        return ok({
            "loan_id": loan_id, "emi_amount": emi,
            "total_emis": regular_count,
            "pre_emi_interest": pre_int, "pre_emi_days": pre_days,
            "message": f"Loan created. {'Pre-EMI interest ₹'+str(pre_int)+' for '+str(pre_days)+' days added as EMI #0.' if pre_int else 'No stub period.'}"
        }, 201)
    except (ValueError, TypeError) as e:
        return err(str(e))


@app.route("/api/loans/<loan_id>", methods=["GET"])
def get_loan(loan_id):
    loan = db.get_loan(loan_id)
    if not loan: return err(f"Loan {loan_id} not found", 404)
    return ok(loan)


# ── Schedule ──────────────────────────────────────────────────────────

@app.route("/api/loans/<loan_id>/schedule", methods=["GET"])
def get_schedule(loan_id):
    if not db.get_loan(loan_id): return err(f"Loan {loan_id} not found", 404)
    return ok(db.get_schedule(loan_id))


# ── Summary ───────────────────────────────────────────────────────────

@app.route("/api/loans/<loan_id>/summary", methods=["GET"])
def get_summary(loan_id):
    loan = db.get_loan(loan_id)
    if not loan: return err(f"Loan {loan_id} not found", 404)
    return ok(calc.compute_summary(loan, db.get_schedule(loan_id), db.get_payments(loan_id)))


# ── Payments ──────────────────────────────────────────────────────────

@app.route("/api/loans/<loan_id>/payments", methods=["GET"])
def get_payments(loan_id):
    return ok(db.get_payments(loan_id))


@app.route("/api/loans/<loan_id>/payments", methods=["POST"])
def add_payment(loan_id):
    body = request.get_json(force=True)
    try:
        require(body, "amount_paid", "payment_date")
        loan = db.get_loan(loan_id)
        if not loan:         return err(f"Loan {loan_id} not found", 404)
        if loan["status"] == "Closed": return err("Loan is already closed")

        schedule = db.get_schedule(loan_id)
        # Include pre-EMI row in unpaid candidates
        unpaid = [r for r in schedule if r["status"] in ("Unpaid", "Partial")]
        if not unpaid: return err("No outstanding EMIs")

        emi_number = int(body["emi_number"]) if body.get("emi_number") is not None else unpaid[0]["emi_number"]
        target = next((r for r in unpaid if r["emi_number"] == emi_number), None)
        if not target: return err(f"EMI #{emi_number} not found or already paid")

        amount      = float(body["amount_paid"])
        pay_date    = str(body["payment_date"])
        new_status  = "Paid" if amount >= float(target["emi_amount"]) else "Partial"

        db.update_emi_status(loan_id, emi_number, new_status, pay_date)

        remaining = float(target["outstanding_balance"])

        payment_id = "PAY" + uuid.uuid4().hex[:8].upper()
        db.insert_payment({
            "loan_id": loan_id, "payment_id": payment_id,
            "payment_date": pay_date, "amount_paid": amount,
            "payment_type": str(body.get("payment_type", "EMI")),
            "emi_number": emi_number,
            "remaining_balance_after_payment": round(remaining, 2),
            "notes": str(body.get("notes", "")),
            "created_at": now_str(),
        })

        # Close loan if fully paid
        updated = db.get_schedule(loan_id)
        if not any(r["status"] in ("Unpaid","Partial") for r in updated):
            db.update_loan(loan_id, {"status": "Closed", "updated_at": now_str()})

        label = "Pre-EMI interest" if target.get("is_pre_emi") else f"EMI #{emi_number}"
        sheets_sync.push_async()
        return ok({"payment_id": payment_id, "emi_number": emi_number,
                   "status": new_status, "remaining_balance": remaining,
                   "message": f"{label} marked {new_status}"}, 201)
    except (ValueError, TypeError) as e:
        return err(str(e))


# ── Part Payment Savings ────────────────────────────────────────────────

@app.route("/api/loans/<loan_id>/part-payment-savings", methods=["GET"])
def part_payment_savings(loan_id):
    """
    For every recorded Part Payment on this loan, compute the interest
    saved per month going forward (what-if comparison: schedule without
    that part payment vs. the schedule that actually resulted), plus a
    combined month-by-month total across all part payments.

    Reads only from already-stored data (payments + emi_schedule history);
    does not modify anything.
    """
    loan = db.get_loan(loan_id)
    if not loan:
        return err(f"Loan {loan_id} not found", 404)

    all_payments = db.get_payments(loan_id)
    part_payments = [p for p in all_payments if p["payment_type"] == "Part Payment"]
    if not part_payments:
        return ok({"loan_id": loan_id, "part_payments": [], "combined_monthly_savings": [],
                   "combined_total_saved": 0.0,
                   "message": "No part payments recorded for this loan."})

    schedule = db.get_schedule(loan_id)
    regular = sorted(
        [r for r in schedule if not r.get("is_pre_emi")],
        key=lambda r: r["emi_number"]
    )
    annual_rate = float(loan["interest_rate"])

    results = []
    combined = {}  # month -> accumulated saved
    combined_total = 0.0

    for pp in part_payments:
        pay_date = pp["payment_date"]
        amount_paid = float(pp["amount_paid"])
        balance_after = float(pp["remaining_balance_after_payment"])
        balance_before = round(balance_after + amount_paid, 2)

        # Schedule rows strictly after this payment reflect the post-payment
        # trajectory (this is all we can reliably read back, since the old
        # pre-payment rows for this window were overwritten at apply-time).
        after_rows = [r for r in regular if r["due_date"] > pay_date]
        if not after_rows:
            continue
        first_after = after_rows[0]
        emi_after = float(first_after["emi_amount"])

        # "Before" EMI amount: the EMI that was active immediately prior to
        # this part payment. The closest reliable source is the EMI amount
        # on loan-paid rows just before the payment date, falling back to
        # the loan's current emi_amount if nothing else is available.
        prior_paid = [r for r in regular if r["status"] == "Paid" and r["due_date"] < pay_date]
        emi_before = float(prior_paid[-1]["emi_amount"]) if prior_paid else emi_after

        remaining_months_before = len(
            [r for r in regular if r["due_date"] >= pay_date]
        ) + 1  # +1 to include the EMI that would have covered this payment's period

        result = calc.calculate_part_payment_savings(
            loan_id=loan_id,
            annual_rate=annual_rate,
            balance_before=balance_before,
            balance_after=balance_after,
            emi_before=emi_before,
            emi_after=emi_after,
            remaining_months_before=remaining_months_before,
            payment_date=pay_date,
        )
        result["payment_id"] = pp["payment_id"]
        result["payment_date"] = pay_date
        result["amount_paid"] = float(pp["amount_paid"])
        results.append(result)

        for m in result["monthly_savings"]:
            combined[m["month"]] = combined.get(m["month"], 0.0) + m["interest_saved"]
        combined_total += result["total_interest_saved"]

    combined_monthly = [
        {"month": m, "interest_saved": round(combined[m], 2)}
        for m in sorted(combined.keys())
    ]

    return ok({
        "loan_id": loan_id,
        "part_payments": results,
        "combined_monthly_savings": combined_monthly,
        "combined_total_saved": round(combined_total, 2),
    })


# ── Part Payment ──────────────────────────────────────────────────────

@app.route("/api/loans/<loan_id>/part-payment", methods=["POST"])
def part_payment(loan_id):
    body = request.get_json(force=True)
    try:
        require(body, "amount", "payment_date")
        loan = db.get_loan(loan_id)
        if not loan:         return err(f"Loan {loan_id} not found", 404)
        if loan["status"] == "Closed": return err("Loan is already closed")

        schedule = db.get_schedule(loan_id)
        regular_unpaid = [r for r in schedule if r["status"] in ("Unpaid","Partial") and not r.get("is_pre_emi")]
        if not regular_unpaid: return err("No outstanding regular EMIs")

        next_emi    = regular_unpaid[0]
        outstanding = float(next_emi["outstanding_balance"]) + float(next_emi["principal_component"])
        amount      = float(body["amount"])

        if amount >= outstanding:
            return err(f"Part payment ₹{amount:,.0f} ≥ outstanding ₹{outstanding:,.0f}. Use foreclosure.")

        new_balance     = round(outstanding - amount, 2)
        reduce_emi      = bool(body.get("reduce_emi", False))
        remaining_months= len(regular_unpaid)
        current_emi     = float(loan["emi_amount"])

        new_emi_amt, new_schedule = calc.recalculate_after_part_payment(
            loan_id, new_balance, float(loan["interest_rate"]),
            remaining_months, next_emi["due_date"], reduce_emi, current_emi,
            start_emi_number=int(next_emi["emi_number"]),
        )

        db.delete_unpaid_from(loan_id, int(next_emi["emi_number"]))
        db.insert_schedule_rows(new_schedule)

        payment_id = "PAY" + uuid.uuid4().hex[:8].upper()
        db.insert_payment({
            "loan_id": loan_id, "payment_id": payment_id,
            "payment_date": str(body["payment_date"]), "amount_paid": amount,
            "payment_type": "Part Payment", "emi_number": None,
            "remaining_balance_after_payment": new_balance,
            "notes": str(body.get("notes", "Part Payment")),
            "created_at": now_str(),
        })

        update = {"updated_at": now_str()}
        if reduce_emi: update["emi_amount"] = new_emi_amt
        db.update_loan(loan_id, update)

        sheets_sync.push_async()
        return ok({
            "payment_id": payment_id, "new_balance": new_balance,
            "new_emi": new_emi_amt, "remaining_emis": len(new_schedule),
            "message": f"Part payment ₹{amount:,.0f} applied. {'EMI reduced to ₹'+f'{new_emi_amt:,.2f}' if reduce_emi else f'Tenure reduced to {len(new_schedule)} months'}."
        })
    except (ValueError, TypeError) as e:
        return err(str(e))


# ── Rate Change ───────────────────────────────────────────────────────

@app.route("/api/loans/<loan_id>/update-rate", methods=["POST"])
def update_rate(loan_id):
    body = request.get_json(force=True)
    try:
        require(body, "new_rate")
        loan = db.get_loan(loan_id)
        if not loan:         return err(f"Loan {loan_id} not found", 404)
        if loan["status"] == "Closed": return err("Loan is already closed")

        schedule = db.get_schedule(loan_id)
        unpaid   = [r for r in schedule if r["status"] in ("Unpaid","Partial") and not r.get("is_pre_emi")]
        if not unpaid: return err("No outstanding EMIs to apply rate change")

        from_emi   = int(body["from_emi"]) if body.get("from_emi") else unpaid[0]["emi_number"]
        apply_from = next((r for r in unpaid if r["emi_number"] == from_emi), unpaid[0])
        outstanding= float(apply_from["outstanding_balance"]) + float(apply_from["principal_component"])
        rem_months = len([r for r in unpaid if r["emi_number"] >= apply_from["emi_number"]])
        new_rate   = float(body["new_rate"])

        new_emi_amt, new_schedule = calc.recalculate_after_rate_change(
            loan_id, outstanding, new_rate, rem_months, apply_from["due_date"],
            start_emi_number=int(apply_from["emi_number"]),
        )

        db.delete_unpaid_from(loan_id, int(apply_from["emi_number"]))
        db.insert_schedule_rows(new_schedule)
        db.update_loan(loan_id, {"interest_rate": new_rate, "emi_amount": new_emi_amt, "updated_at": now_str()})

        sheets_sync.push_async()
        return ok({
            "new_rate": new_rate, "new_emi": new_emi_amt,
            "applied_from_emi": apply_from["emi_number"],
            "remaining_emis": len(new_schedule),
            "message": f"Rate changed to {new_rate}% from EMI #{apply_from['emi_number']}. New EMI: ₹{new_emi_amt:,.2f}"
        })
    except (ValueError, TypeError) as e:
        return err(str(e))


# ── Foreclose ─────────────────────────────────────────────────────────

@app.route("/api/loans/<loan_id>/foreclose", methods=["POST"])
def foreclose(loan_id):
    body = request.get_json(force=True)
    try:
        require(body, "payment_date")
        loan = db.get_loan(loan_id)
        if not loan:         return err(f"Loan {loan_id} not found", 404)
        if loan["status"] == "Closed": return err("Loan already closed")

        schedule = db.get_schedule(loan_id)
        unpaid   = [r for r in schedule if r["status"] in ("Unpaid","Partial")]
        if not unpaid: return err("No outstanding EMIs")

        regular_unpaid = [r for r in unpaid if not r.get("is_pre_emi")]
        next_emi    = regular_unpaid[0] if regular_unpaid else unpaid[0]
        outstanding = float(next_emi["outstanding_balance"]) + float(next_emi["principal_component"])
        pay_date    = str(body["payment_date"])

        payment_id = "PAY" + uuid.uuid4().hex[:8].upper()
        db.insert_payment({
            "loan_id": loan_id, "payment_id": payment_id,
            "payment_date": pay_date, "amount_paid": outstanding,
            "payment_type": "Prepayment", "emi_number": None,
            "remaining_balance_after_payment": 0.0,
            "notes": "Loan Foreclosure", "created_at": now_str(),
        })

        for emi in unpaid:
            db.update_emi_status(loan_id, emi["emi_number"], "Paid", pay_date)

        db.update_loan(loan_id, {"status": "Closed", "updated_at": now_str()})
        sheets_sync.push_async()
        return ok({"payment_id": payment_id, "foreclosure_amount": round(outstanding, 2),
                   "message": f"Loan closed. Paid ₹{outstanding:,.2f}"})
    except (ValueError, TypeError) as e:
        return err(str(e))


# ── PDF Report ────────────────────────────────────────────────────────

@app.route("/api/loans/<loan_id>/report")
def download_report(loan_id):
    loan = db.get_loan(loan_id)
    if not loan: return err(f"Loan {loan_id} not found", 404)
    schedule = db.get_schedule(loan_id)
    payments = db.get_payments(loan_id)
    summary  = calc.compute_summary(loan, schedule, payments)
    pdf_bytes= generate_pdf(loan, schedule, payments, summary)
    return send_file(io.BytesIO(pdf_bytes), mimetype="application/pdf",
                     as_attachment=True, download_name=f"loan_{loan_id}.pdf")


# ── Error handlers ────────────────────────────────────────────────────

@app.errorhandler(404)
def not_found(e):   return err("Not found", 404)

@app.errorhandler(500)
def server_error(e):
    logger.exception("Unhandled error")
    return err("Internal server error", 500)


# ── Run ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"""
╔══════════════════════════════════════════╗
║       LoanLedger — EMI Calculator        ║
╠══════════════════════════════════════════╣
║  Open:     http://localhost:{port}         ║
║  Database: {db.DB_PATH[-40:]:<40}║
╚══════════════════════════════════════════╝
""")
    app.run(host="0.0.0.0", port=port, debug=False)