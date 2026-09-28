from flask import Flask, render_template, request, jsonify, send_file, abort
import json
import os
import uuid
from datetime import datetime
from reportlab.lib.pagesizes import mm
from reportlab.pdfgen import canvas
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm as mm_unit
import hashlib

app = Flask(__name__)

# ── Config ──────────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
RECEIPTS_DIR = os.path.join(BASE_DIR, "receipts")
STOCK_FILE = os.path.join(DATA_DIR, "stock.json")
TRANS_FILE = os.path.join(DATA_DIR, "transactions.json")
CASHIERS_FILE = os.path.join(DATA_DIR, "cashiers.json")

# Admin password (SHA-256 hashed). Default: "admin123"
ADMIN_PASS_HASH = hashlib.sha256("admin123".encode()).hexdigest()

SHOP_NAME = "DAR-E-ARQAM SCHOOl"
SHOP_ADDRESS = "583 Q MT"
SHOP_PHONE = "+92 323 444 7292"
CURRENCY = "PKR"

RECEIPT_WIDTH = 80
RECEIPT_HEIGHT = 150

os.makedirs(RECEIPTS_DIR, exist_ok=True)


# ── Helpers ──────────────────────────────────────────────────────────────────
def load_stock():
    with open(STOCK_FILE, "r") as f:
        return json.load(f)


def save_stock(data):
    with open(STOCK_FILE, "w") as f:
        json.dump(data, f, indent=2)


def load_transactions():
    with open(TRANS_FILE, "r") as f:
        return json.load(f)


def save_transactions(data):
    with open(TRANS_FILE, "w") as f:
        json.dump(data, f, indent=2)


def load_cashiers():
    if not os.path.exists(CASHIERS_FILE):
        default = {"cashiers": []}
        save_cashiers(default)
        return default
    with open(CASHIERS_FILE, "r") as f:
        return json.load(f)


def save_cashiers(data):
    with open(CASHIERS_FILE, "w") as f:
        json.dump(data, f, indent=2)


def check_admin(password):
    return hashlib.sha256(password.encode()).hexdigest() == ADMIN_PASS_HASH


def check_cashier(username, password):
    """Return cashier dict if valid, else None."""
    cashiers = load_cashiers()
    ph = hashlib.sha256(password.encode()).hexdigest()
    for c in cashiers["cashiers"]:
        if c["username"].lower() == username.lower() and c["password_hash"] == ph:
            return c
    return None


def merge_cart_items(items):
    merged = {}
    for item in items:
        key = item["name"].strip().lower()
        if key in merged:
            merged[key]["qty"] += item["qty"]
            merged[key]["subtotal"] += item["subtotal"]
        else:
            merged[key] = {**item}
    return list(merged.values())


# ── Receipt PDF (thermal 80mm) ────────────────────────────────────────────────
def generate_receipt(transaction):
    tid = transaction["id"]
    filename = f"receipt_{tid}.pdf"
    filepath = os.path.join(RECEIPTS_DIR, filename)

    W = RECEIPT_WIDTH * mm_unit
    n_items = len(transaction["items"])
    # extra lines for partial payment info
    extra = 3 if transaction.get("amount_paid") is not None else 0
    h_pts = max(RECEIPT_HEIGHT, 80 + n_items * 9 + 55 + extra * 5) * mm_unit

    c = canvas.Canvas(filepath, pagesize=(W, h_pts))
    y = h_pts - 5 * mm_unit

    def draw_line(dashed=False):
        nonlocal y
        c.setDash(2, 2) if dashed else c.setDash()
        c.setLineWidth(0.5)
        c.line(3 * mm_unit, y, (RECEIPT_WIDTH - 3) * mm_unit, y)
        y -= 2.5 * mm_unit

    def text(txt, size=8, bold=False, center=False, x_off=0):
        nonlocal y
        c.setFont("Helvetica-Bold" if bold else "Helvetica", size)
        x = W / 2 if center else (3 * mm_unit + x_off)
        if center:
            c.drawCentredString(x, y, txt)
        else:
            c.drawString(x, y, txt)
        y -= (size + 2.5) * 0.352778 * mm_unit

    def right_pair(label, value, bold=False, size=8):
        nonlocal y
        font = "Helvetica-Bold" if bold else "Helvetica"
        c.setFont(font, size)
        c.drawString(3 * mm_unit, y, label)
        c.drawRightString((RECEIPT_WIDTH - 3) * mm_unit, y, value)
        y -= (size + 2.5) * 0.352778 * mm_unit

    # ── Header ──
    text(SHOP_NAME, size=13, bold=True, center=True)
    text(SHOP_ADDRESS, size=8, center=True)
    text(SHOP_PHONE, size=8, center=True)
    y -= 1 * mm_unit
    draw_line()

    text(f"Receipt #: {tid[:8].upper()}", size=8)
    text(f"Date: {transaction['date']}  Time: {transaction['time']}", size=8)
    if transaction.get("cashier"):
        text(f"Cashier: {transaction['cashier']}", size=8)
    if transaction.get("customer_name"):
        text(f"Customer: {transaction['customer_name']}", size=8)
    if transaction.get("customer_contact"):
        text(f"Contact:  {transaction['customer_contact']}", size=8)
    y -= 0.5 * mm_unit
    draw_line()

    # ── Column headers ──
    c.setFont("Helvetica-Bold", 7.5)
    c.drawString(3 * mm_unit, y, "Item")
    c.drawString(37 * mm_unit, y, "Qty")
    c.drawString(47 * mm_unit, y, "Price")
    c.drawRightString((RECEIPT_WIDTH - 3) * mm_unit, y, "Total")
    y -= 5 * mm_unit
    draw_line(dashed=True)

    # ── Items ──
    for item in transaction["items"]:
        name = item["name"][:20]
        qty = str(item["qty"])
        price = f"{item['price']:.0f}"
        subtotal = f"{item['subtotal']:.0f}"
        c.setFont("Helvetica", 8)
        c.drawString(3 * mm_unit, y, name)
        c.drawString(37 * mm_unit, y, qty)
        c.drawString(47 * mm_unit, y, price)
        c.drawRightString((RECEIPT_WIDTH - 3) * mm_unit, y, subtotal)
        y -= 5 * mm_unit

    draw_line(dashed=True)

    # ── Totals ──
    subtotal_val = transaction["subtotal"]
    tax_val = transaction.get("tax", 0)
    total_val = transaction["total"]

    right_pair("Subtotal:", f"{CURRENCY} {subtotal_val:.0f}")
    if tax_val:
        right_pair(
            f"Tax ({transaction.get('tax_rate', 0)}%):", f"{CURRENCY} {tax_val:.0f}"
        )
    draw_line()
    right_pair("TOTAL:", f"{CURRENCY} {total_val:.0f}", bold=True, size=9)

    y -= 1 * mm_unit
    text(f"Payment: {transaction.get('payment_method', 'Cash')}", size=8)

    # ── Partial payment block ──
    amount_paid = transaction.get("amount_paid")
    if amount_paid is not None:
        y -= 0.5 * mm_unit
        draw_line(dashed=True)
        right_pair("Amount Paid:", f"{CURRENCY} {amount_paid:.0f}", bold=True)
        balance = amount_paid - total_val
        if balance >= 0:
            right_pair("Change:", f"{CURRENCY} {balance:.0f}")
        else:
            right_pair("Balance Due:", f"{CURRENCY} {abs(balance):.0f}", bold=True)

    # ── Footer ──
    y -= 2 * mm_unit
    draw_line(dashed=True)
    text("Thank you for your purchase!", size=8, center=True, bold=True)
    text("Please come again.", size=7.5, center=True)

    c.save()
    return filename


# ── Routes ───────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html", shop_name=SHOP_NAME)


# Stock
@app.route("/api/stock", methods=["GET"])
def get_stock():
    return jsonify(load_stock())


@app.route("/api/stock/add", methods=["POST"])
def add_stock():
    data = request.json
    password = data.get("password", "")
    if not check_admin(password):
        return jsonify({"error": "Invalid admin password"}), 403

    stock = load_stock()
    name = data.get("name", "").strip()
    for item in stock["items"]:
        if item["name"].strip().lower() == name.lower():
            item["stock"] += int(data.get("stock", 0))
            item["price"] = float(data.get("price", item["price"]))
            item["category"] = data.get("category", item["category"])
            save_stock(stock)
            return jsonify(
                {
                    "success": True,
                    "message": f"Merged with existing item '{item['name']}'",
                    "item": item,
                }
            )

    new_item = {
        "id": str(uuid.uuid4())[:8],
        "name": name,
        "price": float(data.get("price", 0)),
        "stock": int(data.get("stock", 0)),
        "category": data.get("category", "General"),
    }
    stock["items"].append(new_item)
    save_stock(stock)
    return jsonify({"success": True, "message": "Item added", "item": new_item})


@app.route("/api/stock/update", methods=["POST"])
def update_stock():
    data = request.json
    password = data.get("password", "")
    if not check_admin(password):
        return jsonify({"error": "Invalid admin password"}), 403
    stock = load_stock()
    item_id = data.get("id")
    for item in stock["items"]:
        if item["id"] == item_id:
            if "name" in data:
                item["name"] = data["name"]
            if "price" in data:
                item["price"] = float(data["price"])
            if "stock" in data:
                item["stock"] = int(data["stock"])
            if "category" in data:
                item["category"] = data["category"]
            save_stock(stock)
            return jsonify({"success": True, "item": item})
    return jsonify({"error": "Item not found"}), 404


@app.route("/api/stock/delete", methods=["POST"])
def delete_stock():
    data = request.json
    password = data.get("password", "")
    if not check_admin(password):
        return jsonify({"error": "Invalid admin password"}), 403
    stock = load_stock()
    item_id = data.get("id")
    before = len(stock["items"])
    stock["items"] = [i for i in stock["items"] if i["id"] != item_id]
    if len(stock["items"]) == before:
        return jsonify({"error": "Item not found"}), 404
    save_stock(stock)
    return jsonify({"success": True})


# Transactions
@app.route("/api/transaction", methods=["POST"])
def create_transaction():
    data = request.json
    stock = load_stock()
    trans = load_transactions()

    cart_items = data.get("items", [])
    if not cart_items:
        return jsonify({"error": "Cart is empty"}), 400

    cart_items = merge_cart_items(cart_items)

    for cart_item in cart_items:
        found = False
        for stock_item in stock["items"]:
            if stock_item["id"] == cart_item["id"]:
                found = True
                if stock_item["stock"] < cart_item["qty"]:
                    return jsonify(
                        {"error": f"Insufficient stock for '{stock_item['name']}'"}
                    ), 400
                break
        if not found:
            return jsonify({"error": f"Item '{cart_item['name']}' not found"}), 404

    for cart_item in cart_items:
        for stock_item in stock["items"]:
            if stock_item["id"] == cart_item["id"]:
                stock_item["stock"] -= cart_item["qty"]
                break

    now = datetime.now()
    tax_rate = float(data.get("tax_rate", 0))
    subtotal = sum(i["subtotal"] for i in cart_items)
    tax = round(subtotal * tax_rate / 100, 2)
    total = round(subtotal + tax, 2)

    # Partial payment
    amount_paid_raw = data.get("amount_paid")
    amount_paid = float(amount_paid_raw) if amount_paid_raw is not None else None

    transaction = {
        "id": str(uuid.uuid4()),
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "customer_name": data.get("customer_name", ""),
        "customer_contact": data.get("customer_contact", ""),
        "cashier": data.get("cashier", ""),
        "items": cart_items,
        "subtotal": round(subtotal, 2),
        "tax_rate": tax_rate,
        "tax": tax,
        "total": total,
        "payment_method": data.get("payment_method", "Cash"),
        "amount_paid": amount_paid,
        "receipt_file": "",
    }

    receipt_file = generate_receipt(transaction)
    transaction["receipt_file"] = receipt_file

    trans["transactions"].append(transaction)
    save_transactions(trans)
    save_stock(stock)

    return jsonify(
        {
            "success": True,
            "transaction": transaction,
            "receipt_url": f"/receipt/{receipt_file}",
        }
    )


@app.route("/api/transactions", methods=["GET"])
def get_transactions():
    trans = load_transactions()
    trans["transactions"] = sorted(
        trans["transactions"], key=lambda x: x["date"] + x["time"], reverse=True
    )
    return jsonify(trans)


@app.route("/receipt/<filename>")
def serve_receipt(filename):
    filepath = os.path.join(RECEIPTS_DIR, filename)
    if not os.path.exists(filepath):
        abort(404)
    return send_file(filepath, mimetype="application/pdf")


@app.route("/api/admin/verify", methods=["POST"])
def verify_admin():
    data = request.json
    if check_admin(data.get("password", "")):
        return jsonify({"success": True})
    return jsonify({"error": "Invalid password"}), 403


# ── Cashier management (admin only) ──────────────────────────────────────────
@app.route("/api/cashiers", methods=["GET"])
def get_cashiers():
    data = request.json or {}
    password = data.get("password", request.args.get("password", ""))
    if not check_admin(password):
        return jsonify({"error": "Admin only"}), 403
    cashiers = load_cashiers()
    # strip password hashes before returning
    safe = [
        {
            "id": c["id"],
            "username": c["username"],
            "display_name": c.get("display_name", ""),
        }
        for c in cashiers["cashiers"]
    ]
    return jsonify({"cashiers": safe})


@app.route("/api/cashiers/add", methods=["POST"])
def add_cashier():
    data = request.json
    password = data.get("password", "")
    if not check_admin(password):
        return jsonify({"error": "Admin only"}), 403
    username = data.get("username", "").strip()
    new_password = data.get("new_password", "").strip()
    display_name = data.get("display_name", username).strip()
    if not username or not new_password:
        return jsonify({"error": "Username and password required"}), 400
    cashiers = load_cashiers()
    if any(c["username"].lower() == username.lower() for c in cashiers["cashiers"]):
        return jsonify({"error": "Username already exists"}), 409
    cashiers["cashiers"].append(
        {
            "id": str(uuid.uuid4())[:8],
            "username": username,
            "display_name": display_name,
            "password_hash": hashlib.sha256(new_password.encode()).hexdigest(),
        }
    )
    save_cashiers(cashiers)
    return jsonify({"success": True, "message": f"Cashier '{username}' added"})


@app.route("/api/cashiers/delete", methods=["POST"])
def delete_cashier():
    data = request.json
    password = data.get("password", "")
    if not check_admin(password):
        return jsonify({"error": "Admin only"}), 403
    cid = data.get("id")
    cashiers = load_cashiers()
    before = len(cashiers["cashiers"])
    cashiers["cashiers"] = [c for c in cashiers["cashiers"] if c["id"] != cid]
    if len(cashiers["cashiers"]) == before:
        return jsonify({"error": "Cashier not found"}), 404
    save_cashiers(cashiers)
    return jsonify({"success": True})


@app.route("/api/cashier/login", methods=["POST"])
def cashier_login():
    data = request.json
    username = data.get("username", "")
    password = data.get("password", "")
    # Admin can also log in as "cashier"
    if check_admin(password) and username.lower() == "admin":
        return jsonify(
            {
                "success": True,
                "cashier": {
                    "username": "admin",
                    "display_name": "Admin",
                    "role": "admin",
                },
            }
        )
    cashier = check_cashier(username, password)
    if cashier:
        return jsonify(
            {
                "success": True,
                "cashier": {
                    "username": cashier["username"],
                    "display_name": cashier.get("display_name", cashier["username"]),
                    "role": "cashier",
                },
            }
        )
    return jsonify({"error": "Invalid username or password"}), 403


# ── Transaction Report (A4 PDF) ───────────────────────────────────────────────
@app.route("/api/report", methods=["POST"])
def generate_report():
    data = request.json
    date_from = data.get("date_from", "")
    date_to = data.get("date_to", "")

    trans = load_transactions()
    rows = trans["transactions"]

    if date_from:
        rows = [t for t in rows if t["date"] >= date_from]
    if date_to:
        rows = [t for t in rows if t["date"] <= date_to]

    rows = sorted(rows, key=lambda x: x["date"] + x["time"])

    from io import BytesIO
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import cm

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=1.5 * cm,
        rightMargin=1.5 * cm,
        topMargin=1.5 * cm,
        bottomMargin=1.5 * cm,
    )

    styles = getSampleStyleSheet()
    title_s = ParagraphStyle(
        "title_s", parent=styles["Title"], fontSize=16, spaceAfter=4
    )
    sub_s = ParagraphStyle(
        "sub_s",
        parent=styles["Normal"],
        fontSize=9,
        textColor=colors.grey,
        spaceAfter=12,
    )
    h3_s = ParagraphStyle(
        "h3_s", parent=styles["Heading3"], fontSize=10, spaceBefore=8, spaceAfter=4
    )
    body_s = ParagraphStyle("body_s", parent=styles["Normal"], fontSize=8.5)

    story = []

    date_range_str = (
        f"{date_from}  →  {date_to}"
        if date_from and date_to
        else f"From {date_from}"
        if date_from
        else f"Up to {date_to}"
        if date_to
        else "All time"
    )

    story.append(Paragraph(f"{SHOP_NAME} — Transaction Report", title_s))
    story.append(
        Paragraph(
            f"Period: {date_range_str}   |   Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}   |   Currency: {CURRENCY}",
            sub_s,
        )
    )

    if not rows:
        story.append(
            Paragraph("No transactions found for the selected date range.", body_s)
        )
        doc.build(story)
        buf.seek(0)
        return send_file(
            buf,
            mimetype="application/pdf",
            as_attachment=True,
            download_name="report.pdf",
        )

    total_revenue = sum(t["total"] for t in rows)
    total_subtotal = sum(t["subtotal"] for t in rows)
    total_tax = sum(t.get("tax", 0) for t in rows)
    payment_counts = {}
    for t in rows:
        pm = t.get("payment_method", "Cash")
        payment_counts[pm] = payment_counts.get(pm, 0) + 1

    summary_data = [
        [
            "Transactions",
            f"Revenue ({CURRENCY})",
            f"Subtotal ({CURRENCY})",
            f"Tax ({CURRENCY})",
        ],
        [
            str(len(rows)),
            f"{total_revenue:,.0f}",
            f"{total_subtotal:,.0f}",
            f"{total_tax:,.0f}",
        ],
    ]
    summary_table = Table(
        summary_data, colWidths=[3.0 * cm, 5.0 * cm, 5.0 * cm, 5.0 * cm]
    )
    summary_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#3c3f7a")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                (
                    "ROWBACKGROUNDS",
                    (0, 1),
                    (-1, -1),
                    [colors.HexColor("#f0f0f8"), colors.white],
                ),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cccccc")),
                ("ALIGN", (1, 0), (-1, -1), "CENTER"),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    story.append(Paragraph("Summary", h3_s))
    story.append(summary_table)

    pm_data = [["Payment Method", "Count"]] + [
        [pm, str(cnt)] for pm, cnt in payment_counts.items()
    ]
    pm_table = Table(pm_data, colWidths=[6 * cm, 3 * cm])
    pm_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#555577")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 8.5),
                (
                    "ROWBACKGROUNDS",
                    (0, 1),
                    (-1, -1),
                    [colors.HexColor("#f8f8f8"), colors.white],
                ),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cccccc")),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    story.append(Spacer(1, 8))
    story.append(pm_table)

    story.append(Paragraph("Transaction Detail", h3_s))

    tx_header = [
        "#",
        "Date",
        "Time",
        "Cashier",
        "Customer",
        "Items",
        "Subtotal",
        "Tax",
        "Total",
        "Payment",
        "Paid",
        "Balance",
    ]
    tx_rows = [tx_header]
    for idx, t in enumerate(rows, 1):
        items_str = ", ".join(f"{i['name']}×{i['qty']}" for i in t["items"])
        if len(items_str) > 24:
            items_str = items_str[:21] + "…"
        ap = t.get("amount_paid")
        bal = (ap - t["total"]) if ap is not None else None
        tx_rows.append(
            [
                str(idx),
                t["date"],
                t["time"],
                t.get("cashier", "")[:10] or "—",
                (t.get("customer_name") or "Walk-in")[:12],
                items_str,
                f"{t['subtotal']:,.0f}",
                f"{t.get('tax', 0):,.0f}",
                f"{t['total']:,.0f}",
                t.get("payment_method", "Cash"),
                f"{ap:,.0f}" if ap is not None else "Full",
                f"{bal:+,.0f}" if bal is not None else "—",
            ]
        )

    # Keep the transaction table strictly inside the printable A4 width.
    # Printable width = A4 width - left/right margins = 18 cm.
    # The previous widths totaled 23.6 cm, causing horizontal overflow.
    col_w = [
        0.45 * cm,  # #
        1.35 * cm,  # Date
        1.05 * cm,  # Time
        1.20 * cm,  # Cashier
        1.55 * cm,  # Customer
        2.65 * cm,  # Items
        1.25 * cm,  # Subtotal
        1.05 * cm,  # Tax
        1.25 * cm,  # Total
        1.35 * cm,  # Payment
        1.35 * cm,  # Paid
        1.50 * cm,  # Balance
    ]
    tx_table = Table(tx_rows, colWidths=col_w, repeatRows=1)
    tx_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#3c3f7a")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 6.0),
                (
                    "ROWBACKGROUNDS",
                    (0, 1),
                    (-1, -1),
                    [colors.HexColor("#f0f0f8"), colors.white],
                ),
                ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#dddddd")),
                ("ALIGN", (6, 0), (-1, -1), "RIGHT"),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    story.append(tx_table)

    story.append(Spacer(1, 10))
    footer_data = [
        [
            "",
            "",
            "",
            "",
            "",
            "TOTALS →",
            f"{total_subtotal:,.0f}",
            f"{total_tax:,.0f}",
            f"{total_revenue:,.0f}",
            "",
            "",
            "",
        ]
    ]
    ft = Table(footer_data, colWidths=col_w)
    ft.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, -1), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 7.5),
                ("ALIGN", (5, 0), (-1, -1), "RIGHT"),
                ("TEXTCOLOR", (5, 0), (-1, -1), colors.HexColor("#3c3f7a")),
                ("LINEABOVE", (0, 0), (-1, 0), 1, colors.HexColor("#3c3f7a")),
            ]
        )
    )
    story.append(ft)

    doc.build(story)
    buf.seek(0)
    return send_file(
        buf,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"report_{date_from or 'all'}_{date_to or 'all'}.pdf",
    )


if __name__ == "__main__":
    app.run(debug=True, port=5000)
