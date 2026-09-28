from flask import Flask, render_template, request, jsonify, send_file, abort
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
from dotenv import load_dotenv
from pymongo import MongoClient, DESCENDING
from pymongo.errors import DuplicateKeyError

load_dotenv()

app = Flask(__name__)

# ── MongoDB Config ────────────────────────────────────────────────────────────
MONGODB_URI = os.environ["MONGODB_URI"]

_client = MongoClient(MONGODB_URI)
_db = _client["pos_db"]

col_stock = _db["stock"]
col_transactions = _db["transactions"]
col_cashiers = _db["cashiers"]

# Unique index on cashier usernames (case-insensitive handled in app logic)
col_cashiers.create_index("username", unique=True)
# Unique index on stock item names (case-insensitive key stored separately)
col_stock.create_index("name_lower", unique=True)

# ── App Config ────────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RECEIPTS_DIR = os.path.join(BASE_DIR, "receipts")

# Admin password (SHA-256 hashed). Default: "admin123"
ADMIN_PASS_HASH = hashlib.sha256("admin123".encode()).hexdigest()

SHOP_NAME = "DAR-E-ARQAM SCHOOl"
SHOP_ADDRESS = "583 Q MT"
SHOP_PHONE = "+92 323 444 7292"
CURRENCY = "PKR"

RECEIPT_WIDTH = 80  # mm
RECEIPT_HEIGHT = 100  # mm — fixed page height; content expands below if needed

os.makedirs(RECEIPTS_DIR, exist_ok=True)


# ── Helpers ───────────────────────────────────────────────────────────────────
def _clean(doc):
    """Remove MongoDB _id before returning to client."""
    if doc is None:
        return None
    doc.pop("_id", None)
    doc.pop("name_lower", None)
    return doc


def check_admin(password: str) -> bool:
    return hashlib.sha256(password.encode()).hexdigest() == ADMIN_PASS_HASH


def check_cashier(username: str, password: str):
    """Return cashier doc if valid credentials, else None."""
    ph = hashlib.sha256(password.encode()).hexdigest()
    doc = col_cashiers.find_one({"username": username.lower(), "password_hash": ph})
    return _clean(doc) if doc else None


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


# ── Receipt PDF (thermal 80 × 100 mm, larger text) ───────────────────────────
def generate_receipt(transaction):
    tid = transaction["id"]
    filename = f"receipt_{tid}.pdf"
    filepath = os.path.join(RECEIPTS_DIR, filename)

    # ── Font sizes (bumped up for readability on 80 mm roll) ──
    SZ_SHOP = 15  # shop name headline
    SZ_SUBHEAD = 9.5  # address / phone
    SZ_META = 9  # receipt #, date, cashier, customer
    SZ_COL_HDR = 9  # column header row
    SZ_ITEM = 9.5  # item rows
    SZ_TOTALS = 9.5  # subtotal / tax lines
    SZ_GRAND = 12  # TOTAL line
    SZ_PAYMENT = 9.5  # payment method
    SZ_FOOTER = 9  # thank-you lines

    # Row pitch: generous leading so nothing feels cramped
    def pitch(size):
        return (size + 3.5) * 0.352778 * mm_unit

    W = RECEIPT_WIDTH * mm_unit
    M = 4 * mm_unit  # left/right margin

    # ── Estimate total height so the page fits content exactly ──
    n_items = len(transaction["items"])
    has_paid = transaction.get("amount_paid") is not None
    has_tax = bool(transaction.get("tax", 0))
    has_cash = bool(transaction.get("cashier"))
    has_cname = bool(transaction.get("customer_name"))
    has_cphone = bool(transaction.get("customer_contact"))

    # Count text rows in each section
    hdr_rows = 3 + has_cash + has_cname + has_cphone  # shop + meta
    item_rows = n_items
    total_rows = 2 + has_tax + (2 if has_paid else 0)  # subtotal+total+optional
    footer_rows = 2

    # Rough height (mm): margins + section gaps + rows
    est_h = (
        5  # top margin
        + hdr_rows * (SZ_META * 0.352778 + 1.0)
        + 10  # two separator lines
        + 7  # column header + gap
        + item_rows * (SZ_ITEM * 0.352778 + 1.3)
        + 8  # separator lines around totals
        + total_rows * (SZ_TOTALS * 0.352778 + 1.2)
        + 4  # payment line
        + footer_rows * (SZ_FOOTER * 0.352778 + 1.0)
        + 6  # bottom padding
    )
    h_pts = max(RECEIPT_HEIGHT, est_h) * mm_unit

    c = canvas.Canvas(filepath, pagesize=(W, h_pts))
    y = h_pts - 5 * mm_unit

    # ── Drawing helpers ───────────────────────────────────────
    def draw_line(dashed=False):
        nonlocal y
        c.setDash(2, 2) if dashed else c.setDash()
        c.setLineWidth(0.5)
        c.line(M, y, W - M, y)
        y -= 3 * mm_unit

    def text(txt, size=9, bold=False, center=False):
        nonlocal y
        c.setFont("Helvetica-Bold" if bold else "Helvetica", size)
        if center:
            c.drawCentredString(W / 2, y, txt)
        else:
            c.drawString(M, y, txt)
        y -= pitch(size)

    def right_pair(label, value, bold=False, size=9.5):
        nonlocal y
        c.setFont("Helvetica-Bold" if bold else "Helvetica", size)
        c.drawString(M, y, label)
        c.drawRightString(W - M, y, value)
        y -= pitch(size)

    def item_row(name, qty, price, subtotal, size=9.5):
        nonlocal y
        # columns: name | qty @ 38mm | price @ 49mm | total right-aligned
        c.setFont("Helvetica", size)
        c.drawString(M, y, name)
        c.drawString(38 * mm_unit, y, qty)
        c.drawString(49 * mm_unit, y, price)
        c.drawRightString(W - M, y, subtotal)
        y -= pitch(size)

    # ── Header ────────────────────────────────────────────────
    text(SHOP_NAME, size=SZ_SHOP, bold=True, center=True)
    text(SHOP_ADDRESS, size=SZ_SUBHEAD, center=True)
    text(SHOP_PHONE, size=SZ_SUBHEAD, center=True)
    y -= 1.5 * mm_unit
    draw_line()

    text(f"Receipt #: {tid[:8].upper()}", size=SZ_META)
    text(f"Date: {transaction['date']}  Time: {transaction['time']}", size=SZ_META)
    if transaction.get("cashier"):
        text(f"Cashier:  {transaction['cashier']}", size=SZ_META)
    if transaction.get("customer_name"):
        text(f"Customer: {transaction['customer_name']}", size=SZ_META)
    if transaction.get("customer_contact"):
        text(f"Contact:  {transaction['customer_contact']}", size=SZ_META)
    y -= 1 * mm_unit
    draw_line()

    # ── Column headers ────────────────────────────────────────
    c.setFont("Helvetica-Bold", SZ_COL_HDR)
    c.drawString(M, y, "Item")
    c.drawString(38 * mm_unit, y, "Qty")
    c.drawString(49 * mm_unit, y, "Price")
    c.drawRightString(W - M, y, "Total")
    y -= 6 * mm_unit
    draw_line(dashed=True)

    # ── Item rows ─────────────────────────────────────────────
    for item in transaction["items"]:
        item_row(
            item["name"][:22],
            str(item["qty"]),
            f"{item['price']:.0f}",
            f"{item['subtotal']:.0f}",
            size=SZ_ITEM,
        )

    draw_line(dashed=True)

    # ── Totals ────────────────────────────────────────────────
    subtotal_val = transaction["subtotal"]
    tax_val = transaction.get("tax", 0)
    total_val = transaction["total"]

    right_pair("Subtotal:", f"{CURRENCY} {subtotal_val:.0f}", size=SZ_TOTALS)
    if tax_val:
        right_pair(
            f"Tax ({transaction.get('tax_rate', 0)}%):",
            f"{CURRENCY} {tax_val:.0f}",
            size=SZ_TOTALS,
        )
    draw_line()
    right_pair("TOTAL:", f"{CURRENCY} {total_val:.0f}", bold=True, size=SZ_GRAND)

    y -= 1.5 * mm_unit
    text(f"Payment: {transaction.get('payment_method', 'Cash')}", size=SZ_PAYMENT)

    # ── Partial payment block ─────────────────────────────────
    amount_paid = transaction.get("amount_paid")
    if amount_paid is not None:
        y -= 1 * mm_unit
        draw_line(dashed=True)
        right_pair(
            "Amount Paid:", f"{CURRENCY} {amount_paid:.0f}", bold=True, size=SZ_TOTALS
        )
        balance = amount_paid - total_val
        if balance >= 0:
            right_pair("Change:", f"{CURRENCY} {balance:.0f}", size=SZ_TOTALS)
        else:
            right_pair(
                "Balance Due:",
                f"{CURRENCY} {abs(balance):.0f}",
                bold=True,
                size=SZ_TOTALS,
            )

    # ── Footer ────────────────────────────────────────────────
    y -= 2.5 * mm_unit
    draw_line(dashed=True)
    text("Thank you for your purchase!", size=SZ_FOOTER, bold=True, center=True)
    text("Please come again.", size=SZ_FOOTER, center=True)

    c.save()
    return filename


# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html", shop_name=SHOP_NAME)


# ── Stock ─────────────────────────────────────────────────────────────────────
@app.route("/api/stock", methods=["GET"])
def get_stock():
    items = [_clean(doc) for doc in col_stock.find()]
    return jsonify({"items": items})


@app.route("/api/stock/add", methods=["POST"])
def add_stock():
    data = request.json
    password = data.get("password", "")
    if not check_admin(password):
        return jsonify({"error": "Invalid admin password"}), 403

    name = data.get("name", "").strip()
    name_lower = name.lower()

    existing = col_stock.find_one({"name_lower": name_lower})
    if existing:
        col_stock.update_one(
            {"name_lower": name_lower},
            {
                "$inc": {"stock": int(data.get("stock", 0))},
                "$set": {
                    "price": float(data.get("price", existing["price"])),
                    "category": data.get("category", existing["category"]),
                },
            },
        )
        updated = _clean(col_stock.find_one({"name_lower": name_lower}))
        return jsonify(
            {
                "success": True,
                "message": f"Merged with existing item '{updated['name']}'",
                "item": updated,
            }
        )

    new_item = {
        "id": str(uuid.uuid4())[:8],
        "name": name,
        "name_lower": name_lower,
        "price": float(data.get("price", 0)),
        "stock": int(data.get("stock", 0)),
        "category": data.get("category", "General"),
    }
    col_stock.insert_one(new_item)
    return jsonify({"success": True, "message": "Item added", "item": _clean(new_item)})


@app.route("/api/stock/update", methods=["POST"])
def update_stock():
    data = request.json
    password = data.get("password", "")
    if not check_admin(password):
        return jsonify({"error": "Invalid admin password"}), 403

    item_id = data.get("id")
    updates = {}
    if "name" in data:
        updates["name"] = data["name"]
        updates["name_lower"] = data["name"].lower()
    if "price" in data:
        updates["price"] = float(data["price"])
    if "stock" in data:
        updates["stock"] = int(data["stock"])
    if "category" in data:
        updates["category"] = data["category"]

    result = col_stock.find_one_and_update(
        {"id": item_id},
        {"$set": updates},
        return_document=True,
    )
    if not result:
        return jsonify({"error": "Item not found"}), 404
    return jsonify({"success": True, "item": _clean(result)})


@app.route("/api/stock/delete", methods=["POST"])
def delete_stock():
    data = request.json
    password = data.get("password", "")
    if not check_admin(password):
        return jsonify({"error": "Invalid admin password"}), 403

    result = col_stock.delete_one({"id": data.get("id")})
    if result.deleted_count == 0:
        return jsonify({"error": "Item not found"}), 404
    return jsonify({"success": True})


# ── Transactions ──────────────────────────────────────────────────────────────
@app.route("/api/transaction", methods=["POST"])
def create_transaction():
    data = request.json
    cart_items = data.get("items", [])
    if not cart_items:
        return jsonify({"error": "Cart is empty"}), 400

    cart_items = merge_cart_items(cart_items)

    # Validate stock
    for cart_item in cart_items:
        stock_item = col_stock.find_one({"id": cart_item["id"]})
        if not stock_item:
            return jsonify({"error": f"Item '{cart_item['name']}' not found"}), 404
        if stock_item["stock"] < cart_item["qty"]:
            return jsonify(
                {"error": f"Insufficient stock for '{stock_item['name']}'"}
            ), 400

    # Deduct stock
    for cart_item in cart_items:
        col_stock.update_one(
            {"id": cart_item["id"]},
            {"$inc": {"stock": -cart_item["qty"]}},
        )

    now = datetime.now()
    tax_rate = float(data.get("tax_rate", 0))
    subtotal = sum(i["subtotal"] for i in cart_items)
    tax = round(subtotal * tax_rate / 100, 2)
    total = round(subtotal + tax, 2)
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

    col_transactions.insert_one(transaction)

    return jsonify(
        {
            "success": True,
            "transaction": _clean(dict(transaction)),
            "receipt_url": f"/receipt/{receipt_file}",
        }
    )


@app.route("/api/transactions", methods=["GET"])
def get_transactions():
    docs = [
        _clean(doc)
        for doc in col_transactions.find().sort(
            [("date", DESCENDING), ("time", DESCENDING)]
        )
    ]
    return jsonify({"transactions": docs})


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
    body = request.json or {}
    password = body.get("password", request.args.get("password", ""))
    if not check_admin(password):
        return jsonify({"error": "Admin only"}), 403

    safe = [
        {
            "id": c["id"],
            "username": c["username"],
            "display_name": c.get("display_name", ""),
        }
        for c in col_cashiers.find()
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

    new_cashier = {
        "id": str(uuid.uuid4())[:8],
        "username": username.lower(),
        "display_name": display_name,
        "password_hash": hashlib.sha256(new_password.encode()).hexdigest(),
    }
    try:
        col_cashiers.insert_one(new_cashier)
    except DuplicateKeyError:
        return jsonify({"error": "Username already exists"}), 409

    return jsonify({"success": True, "message": f"Cashier '{username}' added"})


@app.route("/api/cashiers/delete", methods=["POST"])
def delete_cashier():
    data = request.json
    password = data.get("password", "")
    if not check_admin(password):
        return jsonify({"error": "Admin only"}), 403

    result = col_cashiers.delete_one({"id": data.get("id")})
    if result.deleted_count == 0:
        return jsonify({"error": "Cashier not found"}), 404
    return jsonify({"success": True})


@app.route("/api/cashier/login", methods=["POST"])
def cashier_login():
    data = request.json
    username = data.get("username", "")
    password = data.get("password", "")

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

    query = {}
    if date_from and date_to:
        query["date"] = {"$gte": date_from, "$lte": date_to}
    elif date_from:
        query["date"] = {"$gte": date_from}
    elif date_to:
        query["date"] = {"$lte": date_to}

    rows = list(col_transactions.find(query).sort([("date", 1), ("time", 1)]))
    for r in rows:
        r.pop("_id", None)

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

    col_w = [
        0.45 * cm,
        1.35 * cm,
        1.05 * cm,
        1.20 * cm,
        1.55 * cm,
        2.65 * cm,
        1.25 * cm,
        1.05 * cm,
        1.25 * cm,
        1.35 * cm,
        1.35 * cm,
        1.50 * cm,
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
