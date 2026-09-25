# POS System

A lightweight Python/Flask Point-of-Sale system with JSON storage and PDF receipt generation.

## Quick Start

```bash
pip install flask reportlab
python app.py
```

Then open **http://localhost:5000** in your browser.

## Default Admin Password
**admin123** — Change it by editing `ADMIN_PASS_HASH` in `app.py`:

```python
import hashlib
print(hashlib.sha256("your_new_password".encode()).hexdigest())
```
Paste the result into `ADMIN_PASS_HASH` in `app.py`.

## Shop Details
Edit the top of `app.py`:
```python
SHOP_NAME    = "My Shop"
SHOP_ADDRESS = "123 Main Street"
SHOP_PHONE   = "+1 234 567 890"
```

## Receipt Size
Default: 80×150mm (expands if needed). Adjust in `app.py`:
```python
RECEIPT_WIDTH  = 80   # mm
RECEIPT_HEIGHT = 150  # mm — minimum; auto-expands for many items
```

## Features
- **POS screen**: searchable/filterable product grid, cart with qty controls
- **Customer info**: name + contact saved per transaction
- **Same-name merging**: cart items and stock entries with identical names are merged
- **Auto receipt**: 80mm thermal-style PDF generated on every checkout
- **Transaction history**: full log with inline receipt viewer & download
- **Admin panel**: password-protected stock add/edit/delete
- **No database**: all data in `data/stock.json` and `data/transactions.json`

## File Structure
```
pos_system/
├── app.py                  ← Main application
├── data/
│   ├── stock.json          ← Product catalogue
│   └── transactions.json   ← Transaction log
├── receipts/               ← Generated PDF receipts
└── templates/
    └── index.html          ← Web UI
```
