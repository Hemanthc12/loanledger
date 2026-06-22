"""
pdf_report.py — Loan statement PDF using ReportLab
"""

import io
from datetime import datetime
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, HRFlowable
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER


def generate_pdf(loan: dict, schedule: list[dict], payments: list[dict], summary: dict) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            rightMargin=15*mm, leftMargin=15*mm,
                            topMargin=15*mm, bottomMargin=15*mm)
    styles = getSampleStyleSheet()
    title_s = ParagraphStyle("T", parent=styles["Heading1"], fontSize=18,
                              textColor=colors.HexColor("#1a1a2e"), spaceAfter=4, alignment=TA_CENTER)
    sub_s = ParagraphStyle("S", parent=styles["Normal"], fontSize=9,
                            textColor=colors.grey, alignment=TA_CENTER)
    h2_s = ParagraphStyle("H2", parent=styles["Heading2"], fontSize=12,
                           textColor=colors.HexColor("#16213e"), spaceBefore=10, spaceAfter=6)
    story = []

    story.append(Paragraph("Home Loan Statement", title_s))
    story.append(Paragraph(f"Generated {datetime.now().strftime('%d %B %Y, %I:%M %p')}", sub_s))
    story.append(Spacer(1, 5*mm))
    story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor("#16213e")))
    story.append(Spacer(1, 4*mm))

    story.append(Paragraph("Loan Details", h2_s))
    t1 = Table([
        ["Loan ID", loan.get("loan_id", "")],
        ["Borrower", loan.get("user_name", "")],
        ["Loan Amount", f"₹{float(loan.get('loan_amount',0)):,.2f}"],
        ["Interest Rate", f"{loan.get('interest_rate','')}% p.a."],
        ["Tenure", f"{loan.get('tenure_months','')} months"],
        ["Start Date", loan.get("start_date", "")],
        ["Monthly EMI", f"₹{float(loan.get('emi_amount',0)):,.2f}"],
        ["Status", loan.get("status", "")],
    ], colWidths=[60*mm, 100*mm])
    t1.setStyle(TableStyle([
        ("BACKGROUND", (0,0),(0,-1), colors.HexColor("#eef2ff")),
        ("FONTNAME", (0,0),(0,-1), "Helvetica-Bold"),
        ("FONTSIZE", (0,0),(-1,-1), 9),
        ("GRID", (0,0),(-1,-1), 0.5, colors.HexColor("#cccccc")),
        ("PADDING", (0,0),(-1,-1), 5),
        ("ROWBACKGROUNDS", (1,0),(1,-1), [colors.white, colors.HexColor("#f9fafb")]),
    ]))
    story.append(t1)
    story.append(Spacer(1, 4*mm))

    story.append(Paragraph("Loan Summary", h2_s))
    t2 = Table([
        ["Total Paid", f"₹{float(summary.get('total_paid',0)):,.2f}"],
        ["Total Interest Paid", f"₹{float(summary.get('total_interest_paid',0)):,.2f}"],
        ["Outstanding Principal", f"₹{float(summary.get('outstanding_principal',0)):,.2f}"],
        ["Remaining EMIs", str(summary.get("remaining_emis",""))],
        ["Next EMI Date", str(summary.get("next_emi_date",""))],
    ], colWidths=[80*mm, 80*mm])
    t2.setStyle(TableStyle([
        ("BACKGROUND", (0,0),(0,-1), colors.HexColor("#e0f2f1")),
        ("FONTNAME", (0,0),(0,-1), "Helvetica-Bold"),
        ("FONTSIZE", (0,0),(-1,-1), 9),
        ("GRID", (0,0),(-1,-1), 0.5, colors.HexColor("#cccccc")),
        ("PADDING", (0,0),(-1,-1), 5),
    ]))
    story.append(t2)
    story.append(Spacer(1, 4*mm))

    story.append(Paragraph("EMI Schedule", h2_s))
    sched_data = [["#","Due Date","EMI","Interest","Principal","Balance","Status"]]
    for r in schedule:
        sched_data.append([
            str(r.get("emi_number","")), str(r.get("due_date","")),
            f"₹{float(r.get('emi_amount',0)):,.0f}",
            f"₹{float(r.get('interest_component',0)):,.0f}",
            f"₹{float(r.get('principal_component',0)):,.0f}",
            f"₹{float(r.get('outstanding_balance',0)):,.0f}",
            str(r.get("status","")),
        ])
    t3 = Table(sched_data, colWidths=[10*mm,28*mm,26*mm,26*mm,26*mm,26*mm,20*mm], repeatRows=1)
    t3.setStyle(TableStyle([
        ("BACKGROUND", (0,0),(-1,0), colors.HexColor("#1a1a2e")),
        ("TEXTCOLOR", (0,0),(-1,0), colors.white),
        ("FONTNAME", (0,0),(-1,0), "Helvetica-Bold"),
        ("FONTSIZE", (0,0),(-1,-1), 7.5),
        ("GRID", (0,0),(-1,-1), 0.3, colors.HexColor("#dddddd")),
        ("ROWBACKGROUNDS", (0,1),(-1,-1), [colors.white, colors.HexColor("#f5f5f5")]),
        ("PADDING", (0,0),(-1,-1), 4),
        ("ALIGN", (0,0),(-1,-1), "CENTER"),
    ]))
    story.append(t3)

    if payments:
        story.append(Spacer(1, 4*mm))
        story.append(Paragraph("Payment History", h2_s))
        pay_data = [["Payment ID","Date","Amount","Type","EMI#","Balance After"]]
        for p in payments:
            pay_data.append([
                str(p.get("payment_id",""))[:12],
                str(p.get("payment_date","")),
                f"₹{float(p.get('amount_paid',0)):,.2f}",
                str(p.get("payment_type","")),
                str(p.get("emi_number","") or "—"),
                f"₹{float(p.get('remaining_balance_after_payment',0)):,.2f}",
            ])
        t4 = Table(pay_data, colWidths=[35*mm,25*mm,28*mm,28*mm,15*mm,31*mm])
        t4.setStyle(TableStyle([
            ("BACKGROUND", (0,0),(-1,0), colors.HexColor("#0f3460")),
            ("TEXTCOLOR", (0,0),(-1,0), colors.white),
            ("FONTNAME", (0,0),(-1,0), "Helvetica-Bold"),
            ("FONTSIZE", (0,0),(-1,-1), 8),
            ("GRID", (0,0),(-1,-1), 0.3, colors.HexColor("#dddddd")),
            ("ROWBACKGROUNDS", (0,1),(-1,-1), [colors.white, colors.HexColor("#f0f4ff")]),
            ("PADDING", (0,0),(-1,-1), 4),
        ]))
        story.append(t4)

    doc.build(story)
    return buf.getvalue()
