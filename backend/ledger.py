"""
ledger.py — pure loan-state engine (no I/O).
Every event (EMI, part payment, rate change, foreclosure) is applied here, both
for live actions and when replaying history after a delete.
"""
import json
import uuid
from datetime import datetime, date, timedelta
from dateutil.relativedelta import relativedelta
import emi_calculator as calc

TYPE_PART = "Part Payment"
TYPE_RATE = "Rate Change"
TYPE_FORE = "Prepayment"
_NON_EMI = {TYPE_PART, TYPE_RATE, TYPE_FORE}
OPEN = ("Unpaid", "Partial")
LOAN_FIELDS = ("interest_rate", "emi_amount", "status", "sanction_date",
               "pre_emi_interest", "pre_emi_days", "orig_rate")


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def new_payment_id():
    return "PAY" + uuid.uuid4().hex[:8].upper()


def _is_pre(r):
    return int(float(r.get("is_pre_emi") or 0)) == 1


def _meta(ev):
    m = ev.get("meta")
    if isinstance(m, dict):
        return m
    try:
        return json.loads(m) if m else {}
    except Exception:
        return {}


def _order(p):
    return (p.get("id") is None, int(p.get("id") or 0))


def loan_fields(loan):
    d = {k: loan.get(k) for k in LOAN_FIELDS}
    d["updated_at"] = now_str()
    return d


class State:
    def __init__(self, loan, schedule, payments):
        self.loan = dict(loan)
        self.schedule = [dict(r) for r in schedule]
        self.payments = [dict(p) for p in payments]

def resplit_schedule(loan, schedule, payments):
    """
    Recompute interest for every EMI cycle using Actual/365, splitting at
    each part-payment date. Paid rows keep their status and
    amounts; later balances and unpaid EMIs flow from the new figures.
    Payments are never modified. Returns (new_rows, report).
    """
    if any(p["payment_type"] == TYPE_RATE for p in payments):
        raise ValueError("This loan has rate-change entries; recalculating it in place isn't supported yet.")

    rate = float(loan["interest_rate"])
    emi_now = float(loan["emi_amount"])
    pps = [(str(p["payment_date"]), float(p["amount_paid"]))
           for p in payments if p["payment_type"] == TYPE_PART]

    paid_on = {}
    for p in sorted(payments, key=_order):
        if p["payment_type"] not in _NON_EMI and p.get("emi_number") not in (None, ""):
            paid_on[int(p["emi_number"])] = str(p["payment_date"])

    rows = sorted((dict(x) for x in schedule), key=lambda x: int(x["emi_number"]))
    pre, reg = [], []
    for x in rows:
        if int(x["emi_number"]) == 0 or _is_pre(x):
            x["is_pre_emi"] = 1          # repairs rows saved with the flag missing
            pre.append(x)
        else:
            reg.append(x)
    if not reg:
        raise ValueError("No regular EMI rows to recalculate")

    old_int = {int(x["emi_number"]): float(x["interest_component"]) for x in reg}
    old_total = sum(old_int.values())
    old_last = str(reg[-1]["due_date"])

    bal = float(loan["loan_amount"])
    out, i = [], 0
    last_due = date.fromisoformat(old_last)
    # First regular EMI cycle starts one scheduled month before its due date.
    prev_due = date.fromisoformat(str(reg[0]["due_date"])) - relativedelta(months=1)
    next_no = int(reg[-1]["emi_number"]) + 1
    cycles = []

    while i < 1200 and bal > 0.005:
        if i < len(reg):
            row = reg[i]
            due = date.fromisoformat(str(row["due_date"]))
        else:
            last_due = last_due + relativedelta(months=1)
            due = last_due
            row = {"loan_id": loan["loan_id"], "emi_number": next_no,
                   "due_date": due.isoformat(), "status": "Unpaid",
                   "paid_date": "", "is_pre_emi": 0}
            next_no += 1
        i += 1

        # Carry forward the actual previous scheduled EMI due date. This
        # remains correct when the recalculated schedule extends past old rows.
        prev = prev_due
        cyc = [(d, a) for d, a in pps if prev <= date.fromisoformat(d) < due]
        pp_sum = sum(a for _, a in cyc)
        if cyc:
            interest = calc.calculate_split_interest(
                bal, rate, prev.isoformat(), cyc, cycle_end_date=due.isoformat()
            )
        else:
            period_days = (due - prev).days
            if period_days <= 0:
                raise ValueError("EMI due dates must be in chronological order")
            interest = calc._r2(
                bal * (rate / 100.0) * period_days / 365.0
            )
        bal_after = round(bal - pp_sum, 2)

        is_paid = row["status"] in ("Paid", "Partial")
        emi = float(row["emi_amount"]) if is_paid else emi_now
        princ = round(emi - interest, 2)
        if princ <= 0:
            raise ValueError(f"EMI ₹{emi:,.2f} doesn't cover interest ₹{interest:,.2f} in EMI #{row['emi_number']}")
        if princ >= bal_after - 0.005:
            princ = bal_after
            emi = round(interest + princ, 2)

        row.update(emi_amount=emi, interest_component=interest, principal_component=princ,
                   outstanding_balance=max(0.0, round(bal_after - princ, 2)), is_pre_emi=0)
        if row["status"] == "Paid" and int(row["emi_number"]) in paid_on:
            row["paid_date"] = paid_on[int(row["emi_number"])]
        if cyc:
            cycles.append({"emi_number": int(row["emi_number"]), "due_date": row["due_date"],
                           "old_interest": old_int.get(int(row["emi_number"])),
                           "new_interest": interest})
        out.append(row)
        bal = row["outstanding_balance"]
        prev_due = due

    new_rows = sorted(pre + out, key=lambda x: int(x["emi_number"]))
    report = {
        "part_payment_cycles": cycles,
        "rows_before": len(reg), "rows_after": len(out),
        "old_last_due": old_last, "new_last_due": out[-1]["due_date"],
        "old_total_interest": round(old_total, 2),
        "new_total_interest": round(sum(x["interest_component"] for x in out), 2),
    }
    return new_rows, report

def _open(st, regular_only=False):
    return [r for r in st.schedule
            if r["status"] in OPEN and not (regular_only and _is_pre(r))]


def _row(st, ev, amount, ptype, emi_number, remaining, meta=None, notes=None):
    row = {
        "loan_id": st.loan["loan_id"],
        "payment_id": ev.get("payment_id") or new_payment_id(),
        "payment_date": str(ev["payment_date"]),
        "amount_paid": round(float(amount), 2),
        "payment_type": ptype,
        "emi_number": emi_number,
        "remaining_balance_after_payment": round(float(remaining), 2),
        "notes": (ev.get("notes") or "") if notes is None else notes,
        "meta": json.dumps(meta) if meta else "",
        "created_at": ev.get("created_at") or now_str(),
    }
    st.payments.append(row)
    return row


# ── Original loan terms (used when replaying) ─────────────────────────

def original_terms(loan, events, stored_schedule=None):
    P = float(loan["loan_amount"])
    tenure = int(loan["tenure_months"])
    start = str(loan["start_date"])

    rate = loan.get("orig_rate")
    if rate in (None, ""):
        rc = [e for e in sorted(events, key=_order) if e.get("payment_type") == TYPE_RATE]
        old = _meta(rc[0]).get("old_rate") if rc else None
        rate = old if old is not None else loan["interest_rate"]
    rate = float(rate)

    sanction = str(loan.get("sanction_date") or "")
    if not sanction:
        # older loans: infer sanction date from the stored pre-EMI row, if any
        pre = [r for r in (stored_schedule or []) if _is_pre(r)]
        if pre and P * rate > 0:
            days = round(float(pre[0]["emi_amount"]) * 365 / (P * rate / 100))
            sanction = (date.fromisoformat(start) - timedelta(days=days)).isoformat()
        else:
            sanction = start
    return P, rate, tenure, start, sanction


def base_state(loan, events, stored_schedule=None):
    P, rate, tenure, start, sanction = original_terms(loan, events, stored_schedule)
    emi = calc.calculate_emi(P, rate, tenure)
    sched = calc.generate_schedule(loan["loan_id"], P, rate, tenure, start,
                                   sanction_date=sanction)
    pre_int, pre_days = calc.calculate_pre_emi(P, rate, sanction, start)
    l = dict(loan)
    l.update(interest_rate=rate, emi_amount=emi, status="Active", orig_rate=rate,
             sanction_date=sanction, pre_emi_interest=pre_int, pre_emi_days=pre_days)
    return State(l, sched, [])


# ── Event appliers ────────────────────────────────────────────────────

def apply_payment(st, ev):
    if st.loan["status"] == "Closed":
        raise ValueError("Loan is already closed")
    amount = float(ev["amount_paid"])
    if amount <= 0:
        raise ValueError("Amount must be greater than zero")
    unpaid = _open(st)
    if not unpaid:
        raise ValueError("No outstanding EMIs")

    num = ev.get("emi_number")
    num = unpaid[0]["emi_number"] if num in (None, "") else int(num)
    num = int(num)
    target = next((r for r in unpaid if int(r["emi_number"]) == num), None)
    if not target:
        raise ValueError(f"EMI #{num} not found or already paid")

    prior = sum(float(p["amount_paid"]) for p in st.payments
                if p.get("emi_number") not in (None, "")
                and int(p["emi_number"]) == num
                and p["payment_type"] not in _NON_EMI)
    due = float(target["emi_amount"])
    status = "Paid" if prior + amount >= due - 0.01 else "Partial"
    target["status"] = status
    target["paid_date"] = str(ev["payment_date"])

    bal = float(target["outstanding_balance"])
    if status == "Partial":
        bal += float(target["principal_component"])

    row = _row(st, ev, amount, ev.get("payment_type") or "EMI", num, bal)
    if not _open(st):
        st.loan["status"] = "Closed"
    label = "Pre-EMI interest" if _is_pre(target) else f"EMI #{num}"
    return {"payment_id": row["payment_id"], "emi_number": num, "status": status,
            "remaining_balance": row["remaining_balance_after_payment"],
            "message": f"{label} marked {status}"}


def apply_part_payment(st, ev):
    if st.loan["status"] == "Closed":
        raise ValueError("Loan is already closed")
    amount = float(ev["amount_paid"])
    reg = _open(st, True)
    if not reg:
        raise ValueError("No outstanding regular EMIs")
    nxt = reg[0]
    outstanding = float(nxt["outstanding_balance"]) + float(nxt["principal_component"])
    if amount <= 0:
        raise ValueError("Amount must be greater than zero")
    if amount >= outstanding - 0.005:
        raise ValueError(f"Part payment ₹{amount:,.0f} ≥ outstanding ₹{outstanding:,.0f}. Use foreclosure.")

    reduce_emi = bool(_meta(ev).get("reduce_emi", False))
    new_balance = round(outstanding - amount, 2)
    cut = int(nxt["emi_number"])
    rate = float(st.loan["interest_rate"])
    pay_date = str(ev["payment_date"])

    
    # ── Split-period interest for this EMI cycle ──

    next_due = str(nxt["due_date"])
    next_date = date.fromisoformat(next_due)

    # Find the actual previous EMI date from the schedule.
    previous_rows = [
        r for r in st.schedule
        if int(r["emi_number"]) < cut
    ]

    if previous_rows:
        previous_row = max(
            previous_rows,
            key=lambda r: str(r["due_date"])
        )
        prev_due = str(previous_row["due_date"])
    else:
        prev_due = (
            next_date - relativedelta(months=1)
        ).isoformat()

    prev_date = date.fromisoformat(prev_due)
    payment_date = date.fromisoformat(pay_date)

    # Ensure the new part payment belongs to this EMI cycle.
    if not prev_date <= payment_date < next_date:
        raise ValueError(
            f"Part payment date {pay_date} is outside the "
            f"current EMI cycle ({prev_due} to {next_due})."
        )

    # Include every earlier part payment in this cycle.
    same_cycle = [
        (str(p["payment_date"]), float(p["amount_paid"]))
        for p in st.payments
        if p["payment_type"] == TYPE_PART
        and prev_due <= str(p["payment_date"]) < next_due
    ]

    # Reconstruct original opening balance before all
    # part payments in this cycle.
    opening = outstanding + sum(
        amount for _, amount in same_cycle
    )

    # Calculate interest across all payment segments.
    cycle_interest = calc.calculate_split_interest(
        opening,
        rate,
        prev_due,
        same_cycle + [(pay_date, amount)],
        cycle_end_date=next_due,
    )

    new_emi, new_sched = calc.recalculate_after_part_payment(
        st.loan["loan_id"], new_balance, rate,
        len(reg), nxt["due_date"], reduce_emi, float(st.loan["emi_amount"]),
        start_emi_number=cut, first_interest=cycle_interest,
    )
    st.schedule = [r for r in st.schedule
                   if not (r["status"] in OPEN and int(r["emi_number"]) >= cut)] + new_sched
    if reduce_emi:
        st.loan["emi_amount"] = new_emi
    row = _row(st, ev, amount, TYPE_PART, None, new_balance,
               meta={"reduce_emi": reduce_emi}, notes=ev.get("notes") or "Part Payment")
    msg = (f"EMI reduced to ₹{new_emi:,.2f}" if reduce_emi
           else f"Tenure reduced to {len(new_sched)} months")
    return {"payment_id": row["payment_id"], "new_balance": new_balance,
            "new_emi": new_emi, "remaining_emis": len(new_sched),
            "next_emi_interest": cycle_interest,
            "message": f"Part payment ₹{amount:,.0f} applied. {msg}. "
                       f"EMI #{cut} interest (split-period): ₹{cycle_interest:,.2f}."}

def apply_rate_change(st, ev):
    if st.loan["status"] == "Closed":
        raise ValueError("Loan is already closed")
    meta = _meta(ev)
    if meta.get("new_rate") is None:
        raise ValueError("Rate change entry has no new rate recorded")
    new_rate = float(meta["new_rate"])
    reg = _open(st, True)
    if not reg:
        raise ValueError("No outstanding EMIs to apply rate change")

    from_emi = meta.get("from_emi")
    apply_from = reg[0]
    if from_emi not in (None, ""):
        apply_from = next((r for r in reg if int(r["emi_number"]) == int(from_emi)), reg[0])
    cut = int(apply_from["emi_number"])
    outstanding = float(apply_from["outstanding_balance"]) + float(apply_from["principal_component"])
    rem = len([r for r in reg if int(r["emi_number"]) >= cut])

    keep_emi = bool(meta.get("keep_emi", False))
    old = float(st.loan["interest_rate"])
    old_emi = float(st.loan["emi_amount"])

    new_emi, new_sched = calc.recalculate_after_rate_change(
        st.loan["loan_id"], outstanding, new_rate, rem, apply_from["due_date"],
        start_emi_number=cut, keep_emi=keep_emi, current_emi=old_emi,
    )
    st.schedule = [r for r in st.schedule
                   if not (r["status"] in OPEN and int(r["emi_number"]) >= cut)] + new_sched
    st.loan["interest_rate"] = new_rate
    st.loan["emi_amount"] = new_emi

    row = _row(st, ev, 0.0, TYPE_RATE, cut, outstanding,
               meta={"old_rate": old, "new_rate": new_rate, "from_emi": cut,
                     "keep_emi": keep_emi},
               notes=ev.get("notes") or f"Rate {old}% → {new_rate}%")
    how = (f"EMI stays ₹{new_emi:,.2f}, tenure now {len(new_sched)} months"
           if keep_emi else f"New EMI: ₹{new_emi:,.2f}")
    return {"payment_id": row["payment_id"], "new_rate": new_rate, "new_emi": new_emi,
            "applied_from_emi": cut, "remaining_emis": len(new_sched),
            "message": f"Rate changed to {new_rate}% from EMI #{cut}. {how}"}

def apply_foreclose(st, ev):
    if st.loan["status"] == "Closed":
        raise ValueError("Loan already closed")
    unpaid = _open(st)
    if not unpaid:
        raise ValueError("No outstanding EMIs")
    reg = [r for r in unpaid if not _is_pre(r)]
    pre = [r for r in unpaid if _is_pre(r)]
    pay_date = str(ev["payment_date"])

    principal = 0.0
    if reg:
        principal = float(reg[0]["outstanding_balance"]) + float(reg[0]["principal_component"])
    pre_amt = sum(float(r["emi_amount"]) for r in pre)
    amount = round(principal + pre_amt, 2)

    for r in pre:
        r["status"], r["paid_date"] = "Paid", pay_date
    if reg:
        drop = {id(r) for r in reg}
        st.schedule = [r for r in st.schedule if id(r) not in drop]
        st.schedule.append({
            "loan_id": st.loan["loan_id"], "emi_number": int(reg[0]["emi_number"]),
            "due_date": pay_date, "emi_amount": round(principal, 2),
            "interest_component": 0.0, "principal_component": round(principal, 2),
            "outstanding_balance": 0.0, "status": "Paid", "paid_date": pay_date,
            "is_pre_emi": 0,
        })
    st.loan["status"] = "Closed"
    row = _row(st, ev, amount, TYPE_FORE, None, 0.0, notes=ev.get("notes") or "Loan Foreclosure")
    return {"payment_id": row["payment_id"], "foreclosure_amount": amount,
            "message": f"Loan closed. Paid ₹{amount:,.2f}"}


def apply_event(st, ev):
    t = ev.get("payment_type")
    if t == TYPE_PART:
        return apply_part_payment(st, ev)
    if t == TYPE_RATE:
        return apply_rate_change(st, ev)
    if t == TYPE_FORE:
        return apply_foreclose(st, ev)
    return apply_payment(st, ev)


# ── Replay (used for delete + savings what-if) ────────────────────────

def replay(loan, events, stored_schedule=None):
    """Rebuild the loan from original terms by re-applying events in the order they were entered."""
    st = base_state(loan, events, stored_schedule)
    for ev in sorted(events, key=_order):
        apply_event(st, ev)
    st.schedule.sort(key=lambda r: int(r["emi_number"]))
    return st


def signature(loan, schedule):
    reg = [r for r in schedule if r["status"] in OPEN and not _is_pre(r)]
    out = (float(reg[0]["outstanding_balance"]) + float(reg[0]["principal_component"])) if reg else 0.0
    return {"status": loan["status"], "rate": round(float(loan["interest_rate"]), 4),
            "emi": float(loan["emi_amount"]), "out": out, "n": len(reg)}


def matches(a, b):
    return (a["status"] == b["status"] and abs(a["rate"] - b["rate"]) < 1e-6
            and abs(a["emi"] - b["emi"]) <= 1 and abs(a["out"] - b["out"]) <= 1
            and a["n"] == b["n"])