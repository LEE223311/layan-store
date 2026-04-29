import sqlite3
import os
import glob
import io
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email.mime.text import MIMEText
from email import encoders
from datetime import datetime
from functools import wraps
from flask import Flask, render_template, request, jsonify, send_file, session, redirect, url_for
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "layan-k9x2-secret-store-2025")

_BASE = os.path.dirname(os.path.abspath(__file__))
_DATA = "/data" if os.path.isdir("/data") else _BASE
DB = os.path.join(_DATA, "store.db")
REPORTS_DIR = os.path.join(_DATA, "reports")

# ── email config ──────────────────────────────────────────────────────────────
SMTP_USER        = os.environ.get("SMTP_USER",        "storeq620@gmail.com")
SMTP_PASS        = os.environ.get("SMTP_PASS",        "ikfdczmrvsjxuoop")
REPORT_EMAIL_TO  = os.environ.get("REPORT_EMAIL_TO",  "storeq620@gmail.com")


# ── database ──────────────────────────────────────────────────────────────────

def get_db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    con = get_db()
    cur = con.cursor()
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS products (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            name     TEXT    NOT NULL,
            category TEXT    NOT NULL,
            price    REAL    NOT NULL,
            stock    INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS transactions (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id   INTEGER NOT NULL,
            product_name TEXT    NOT NULL,
            qty          INTEGER NOT NULL,
            total        REAL    NOT NULL,
            type         TEXT    NOT NULL CHECK(type IN ('sale','return')),
            returned     INTEGER NOT NULL DEFAULT 0,
            created_at   DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS users (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            email      TEXT    NOT NULL UNIQUE,
            password   TEXT    NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
    """)
    con.commit()
    con.close()


def _migrate():
    con = get_db()
    tx_cols   = [r[1] for r in con.execute("PRAGMA table_info(transactions)").fetchall()]
    prod_cols = [r[1] for r in con.execute("PRAGMA table_info(products)").fetchall()]

    if "returned" not in tx_cols:
        con.execute("ALTER TABLE transactions ADD COLUMN returned INTEGER NOT NULL DEFAULT 0")
        con.commit()
    if "cost_total" not in tx_cols:
        con.execute("ALTER TABLE transactions ADD COLUMN cost_total REAL NOT NULL DEFAULT 0")
        con.commit()
    if "cost_price" not in prod_cols:
        con.execute("ALTER TABLE products ADD COLUMN cost_price REAL NOT NULL DEFAULT 0")
        con.commit()
    if "customer_name" not in tx_cols:
        con.execute("ALTER TABLE transactions ADD COLUMN customer_name TEXT NOT NULL DEFAULT ''")
        con.commit()
    if "customer_phone" not in tx_cols:
        con.execute("ALTER TABLE transactions ADD COLUMN customer_phone TEXT NOT NULL DEFAULT ''")
        con.commit()
    con.execute("""
        CREATE TABLE IF NOT EXISTS goals (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            title      TEXT    NOT NULL,
            product_id INTEGER,
            target_qty INTEGER NOT NULL DEFAULT 1,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS personal_goals (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            text       TEXT    NOT NULL,
            achieved   INTEGER NOT NULL DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    con.commit()
    con.close()


# ── auth ──────────────────────────────────────────────────────────────────────

ADMIN_EMAIL    = os.environ.get("ADMIN_EMAIL",    "storeq620@gmail.com")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "Layan@2025")


def _ensure_admin():
    """Create default admin account if no users exist."""
    con = get_db()
    exists = con.execute("SELECT id FROM users LIMIT 1").fetchone()
    if not exists:
        con.execute(
            "INSERT INTO users (email, password) VALUES (?,?)",
            (ADMIN_EMAIL, generate_password_hash(ADMIN_PASSWORD)),
        )
        con.commit()
    con.close()


CRON_SECRET = os.environ.get("CRON_SECRET", "")


@app.before_request
def require_login():
    """Block all routes except /login and /static unless logged in."""
    open_endpoints = {"login_page", "logout", "static", "run_report_cron"}
    if request.endpoint in open_endpoints:
        return
    if not session.get("user_id"):
        if request.path.startswith("/api/"):
            return jsonify({"error": "غير مصرح", "redirect": "/login"}), 401
        return redirect(url_for("login_page"))


@app.route("/login", methods=["GET", "POST"])
def login_page():
    if session.get("user_id"):
        return redirect(url_for("index"))
    if request.method == "POST":
        data     = request.get_json(silent=True) or {}
        email    = (data.get("email") or "").strip().lower()
        password = (data.get("password") or "")
        con  = get_db()
        user = con.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        con.close()
        if user and check_password_hash(user["password"], password):
            session["user_id"] = user["id"]
            session["email"]   = user["email"]
            return jsonify({"ok": True})
        return jsonify({"error": "البريد الإلكتروني أو كلمة المرور غير صحيحة"}), 401
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login_page"))


# ── PDF report ────────────────────────────────────────────────────────────────

def _make_chart_png(labels, values, title, chart_type="bar"):
    """Generate a chart PNG as BytesIO using matplotlib. Returns None if unavailable."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        fig, ax = plt.subplots(figsize=(7.5, 3.0))
        col_main = "#0f3460"

        if chart_type == "bar":
            x = range(len(labels))
            bars = ax.bar(x, values, color=col_main, alpha=0.85, edgecolor="white", linewidth=0.5)
            ax.set_xticks(x)
            ax.set_xticklabels(labels, rotation=35, ha="right", fontsize=7)
            for bar, val in zip(bars, values):
                ax.text(bar.get_x() + bar.get_width() / 2,
                        bar.get_height() + max(values) * 0.01,
                        f"{val:.0f}", ha="center", va="bottom", fontsize=6, color="#333")
        elif chart_type == "line":
            x = range(len(labels))
            ax.plot(x, values, color=col_main, linewidth=2, marker="o", markersize=4)
            ax.fill_between(x, values, alpha=0.1, color=col_main)
            ax.set_xticks(x)
            ax.set_xticklabels(labels, rotation=35, ha="right", fontsize=7)

        ax.set_title(title, fontsize=10, fontweight="bold", pad=8, color="#1e2d40")
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        ax.yaxis.grid(True, alpha=0.2, linestyle="--")
        ax.set_axisbelow(True)
        ax.tick_params(axis="y", labelsize=7)
        fig.patch.set_facecolor("#ffffff")
        plt.tight_layout(pad=1.0)

        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=130, bbox_inches="tight", facecolor="#ffffff")
        plt.close(fig)
        buf.seek(0)
        return buf
    except Exception:
        return None

def _ensure_fonts():
    """Download DejaVu fonts to /data/fonts (persistent volume)."""
    fonts_dir = os.path.join(_DATA, "fonts")
    regular   = os.path.join(fonts_dir, "DejaVuSans.ttf")
    bold      = os.path.join(fonts_dir, "DejaVuSans-Bold.ttf")
    if os.path.exists(regular) and os.path.exists(bold):
        return
    try:
        import urllib.request, tarfile, tempfile
        os.makedirs(fonts_dir, exist_ok=True)
        url = "https://github.com/dejavu-fonts/dejavu-fonts/releases/download/version_2_37/dejavu-fonts-ttf-2.37.tar.bz2"
        with tempfile.NamedTemporaryFile(suffix=".tar.bz2", delete=False) as tmp:
            urllib.request.urlretrieve(url, tmp.name)
            tmp_path = tmp.name
        want = {"DejaVuSans.ttf", "DejaVuSans-Bold.ttf"}
        with tarfile.open(tmp_path, "r:bz2") as tf:
            for member in tf.getmembers():
                base = os.path.basename(member.name)
                if base in want:
                    fobj = tf.extractfile(member)
                    if fobj:
                        with open(os.path.join(fonts_dir, base), "wb") as out:
                            out.write(fobj.read())
        os.unlink(tmp_path)
        print("[fonts] DejaVu fonts downloaded successfully")
    except Exception as e:
        print(f"[fonts] Font download failed: {e}")


def _find_fonts():
    """Return (regular_ttf, bold_ttf). Either may be None."""
    # matplotlib ships DejaVu fonts — use them if available
    try:
        import matplotlib
        mpl_fonts = os.path.join(os.path.dirname(matplotlib.__file__), "mpl-data", "fonts", "ttf")
    except Exception:
        mpl_fonts = ""

    dirs = [
        mpl_fonts,
        os.path.join(_DATA, "fonts"),
        os.path.join(_BASE, "fonts"),
        _BASE,
        "/usr/share/fonts/truetype/dejavu",
        "/usr/share/fonts/dejavu",
        "/usr/share/fonts/truetype/freefont",
    ]
    def find(names):
        for d in dirs:
            if not d:
                continue
            for n in names:
                p = os.path.join(d, n)
                if os.path.exists(p):
                    return p
        return None
    return (
        find(["DejaVuSans.ttf", "FreeSans.ttf"]),
        find(["DejaVuSans-Bold.ttf", "FreeSansBold.ttf"]),
    )


def _reshape(text):
    """Reshape Arabic text for correct RTL rendering in PDF."""
    try:
        import arabic_reshaper
        from bidi.algorithm import get_display
        return get_display(arabic_reshaper.reshape(str(text)))
    except Exception:
        return str(text)


def generate_pdf_report():
    """Generate a branded PDF report. Returns the saved filename."""
    from fpdf import FPDF

    os.makedirs(REPORTS_DIR, exist_ok=True)

    logo_path = os.path.join(_BASE, "static", "logo.png")

    con = get_db()
    revenue     = con.execute("SELECT COALESCE(SUM(total),0)      FROM transactions WHERE type='sale'").fetchone()[0]
    refunds     = con.execute("SELECT COALESCE(SUM(total),0)      FROM transactions WHERE type='return'").fetchone()[0]
    cogs        = con.execute("SELECT COALESCE(SUM(cost_total),0) FROM transactions WHERE type='sale'").fetchone()[0]
    cogs_r      = con.execute("SELECT COALESCE(SUM(cost_total),0) FROM transactions WHERE type='return'").fetchone()[0]
    sales_count = con.execute("SELECT COUNT(*) FROM transactions WHERE type='sale'").fetchone()[0]
    ret_count   = con.execute("SELECT COUNT(*) FROM transactions WHERE type='return'").fetchone()[0]
    units_sold  = con.execute("SELECT COALESCE(SUM(qty),0) FROM transactions WHERE type='sale'").fetchone()[0]
    txs         = con.execute("SELECT * FROM transactions ORDER BY id DESC").fetchall()
    products    = con.execute("SELECT * FROM products ORDER BY name").fetchall()
    top_rows    = con.execute(
        "SELECT product_name, SUM(total) as rev FROM transactions WHERE type='sale' "
        "GROUP BY product_name ORDER BY rev DESC LIMIT 8"
    ).fetchall()
    daily_rows  = con.execute(
        "SELECT DATE(created_at) as day, SUM(total) as rev FROM transactions "
        "WHERE type='sale' AND created_at >= DATE('now','-30 days') "
        "GROUP BY day ORDER BY day"
    ).fetchall()
    top_customer = con.execute("""
        SELECT customer_name, customer_phone,
               COUNT(*)            AS purchases,
               SUM(qty)            AS total_qty,
               ROUND(SUM(total),3) AS total_spent
        FROM transactions
        WHERE type='sale' AND returned=0 AND customer_name != ''
          AND created_at >= DATE('now','-14 days')
        GROUP BY customer_name, customer_phone
        ORDER BY total_spent DESC
        LIMIT 1
    """).fetchone()
    con.close()

    net    = revenue - refunds
    profit = net - (cogs - cogs_r)
    now = datetime.now()

    reg_font, bold_font = _find_fonts()
    has_unicode = reg_font is not None

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.set_margins(15, 15, 15)
    pdf.add_page()

    if has_unicode:
        pdf.add_font("U",  fname=reg_font)
        pdf.add_font("U",  style="B", fname=bold_font or reg_font)

    def sf(size, bold=False):
        if has_unicode:
            pdf.set_font("U", style="B" if bold else "", size=size)
        else:
            pdf.set_font("Helvetica", style="B" if bold else "", size=size)

    def ar(text):
        return _reshape(text) if has_unicode else str(text)

    NAVY  = (15,  52,  96)
    GOLD  = (212, 175, 55)
    WHITE = (255, 255, 255)
    GREEN = (27,  94,  32)
    RED   = (165, 45,  45)
    LGREY = (245, 248, 255)

    # ── header banner ──
    pdf.set_fill_color(*NAVY)
    pdf.rect(0, 0, 210, 44, "F")
    pdf.set_fill_color(*GOLD)
    pdf.rect(0, 44, 210, 3, "F")

    if os.path.isfile(logo_path):
        pdf.image(logo_path, x=8, y=6, h=30)

    sf(20, bold=True)
    pdf.set_text_color(*GOLD)
    pdf.set_y(7)
    pdf.cell(0, 12, ar("Layan"), align="C", new_x="LMARGIN", new_y="NEXT")
    sf(10)
    pdf.set_text_color(200, 210, 230)
    pdf.cell(0, 7,  ar("كشف مالي نصف شهري"),                        align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 6,  ar(f"تاريخ الإنشاء: {now.strftime('%d %B %Y  |  %H:%M')}"), align="C", new_x="LMARGIN", new_y="NEXT")

    pdf.set_text_color(0, 0, 0)
    pdf.set_y(57)

    # ── summary cards ──
    ORANGE = (160, 90, 10)
    cards = [
        (ar("إجمالي الإيرادات"),       f"{revenue:.3f} ر.ع", GREEN,  (232, 245, 233)),
        (ar("الشحنات المرفوضة"),       f"{refunds:.3f} ر.ع",  RED,    (252, 235, 235)),
        (ar("الكمية المباعة بنجاح"),   str(int(units_sold)),   NAVY,   (232, 240, 254)),
        (ar("صافي الربح"),             f"{profit:.3f} ر.ع",   ORANGE, (255, 243, 220)),
    ]
    cw, ch, gap = 42, 28, 4
    cy = pdf.get_y()
    for i, (label, value, tc, fc) in enumerate(cards):
        x = 15 + i * (cw + gap)
        pdf.set_fill_color(*fc)
        pdf.set_draw_color(200, 210, 230)
        pdf.rect(x, cy, cw, ch, "FD")
        sf(8)
        pdf.set_text_color(100, 100, 100)
        pdf.set_xy(x, cy + 4);  pdf.cell(cw, 5,  label, align="C")
        sf(13, bold=True)
        pdf.set_text_color(*tc)
        pdf.set_xy(x, cy + 11); pdf.cell(cw, 10, value, align="C")

    pdf.set_y(cy + ch + 4)
    sf(9)
    pdf.set_text_color(80, 80, 80)
    pdf.cell(90, 7, ar(f"المبيعات: {sales_count}"),        align="C")
    pdf.cell(90, 7, ar(f"الشحنات المرفوضة: {ret_count}"),  align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    # ── top customer box ──
    AMBER_BG = (255, 248, 225)
    AMBER_BR = (230, 180, 60)
    tc_y = pdf.get_y()
    pdf.set_fill_color(*AMBER_BG)
    pdf.set_draw_color(*AMBER_BR)
    pdf.rect(15, tc_y, 180, 18, "FD")
    sf(9, bold=True)
    pdf.set_text_color(120, 80, 0)
    pdf.set_xy(15, tc_y + 2)
    pdf.cell(180, 6, ar("⭐  أفضل عميل خلال الفترة الأخيرة (14 يوم)"), align="C",
             new_x="LMARGIN", new_y="NEXT")
    sf(9)
    if top_customer and top_customer["customer_name"]:
        tc_name   = top_customer["customer_name"]
        tc_phone  = top_customer["customer_phone"] or "—"
        tc_spent  = top_customer["total_spent"]
        tc_orders = top_customer["purchases"]
        tc_qty    = top_customer["total_qty"]
        pdf.set_text_color(80, 50, 0)
        pdf.cell(180, 6,
                 ar(f"{tc_name}  ·  {tc_phone}  ·  {tc_orders} طلب  ·  {tc_qty} وحدة  ·  {tc_spent:.3f} ر.ع"),
                 align="C")
    else:
        pdf.set_text_color(160, 120, 60)
        pdf.cell(180, 6, ar("لا توجد بيانات عملاء خلال هذه الفترة"), align="C")
    pdf.ln(8)

    # ── charts ────────────────────────────────────────────────────────────────
    try:
        if top_rows:
            buf = _make_chart_png(
                [r["product_name"][:18] for r in top_rows],
                [r["rev"] for r in top_rows],
                ar("الإيرادات حسب المنتج (ر.ع)"),
                chart_type="bar",
            )
            if buf:
                if pdf.get_y() > 210: pdf.add_page()
                pdf.image(buf, x=15, w=180)
                pdf.ln(4)

        if daily_rows and len(daily_rows) > 1:
            buf2 = _make_chart_png(
                [r["day"][5:] for r in daily_rows],
                [r["rev"] for r in daily_rows],
                ar("الإيرادات اليومية — آخر 30 يوم (ر.ع)"),
                chart_type="line",
            )
            if buf2:
                if pdf.get_y() > 210: pdf.add_page()
                pdf.image(buf2, x=15, w=180)
                pdf.ln(4)
    except Exception:
        pass

    # ── inventory table ──
    sf(11, bold=True)
    pdf.set_text_color(*NAVY)
    pdf.cell(0, 8, ar("المخزون الحالي"), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(1)

    inv_cols = [(ar("المنتج"), 72), (ar("التصنيف"), 47), (ar("السعر ر.ع"), 33), (ar("المخزون"), 28)]
    pdf.set_fill_color(*NAVY); pdf.set_text_color(*WHITE)
    sf(8, bold=True)
    for h, w in inv_cols:
        pdf.cell(w, 8, h, border=1, fill=True, align="C")
    pdf.ln()
    sf(8)

    for i, p in enumerate(products):
        if pdf.get_y() > 265:
            pdf.add_page()
        pdf.set_fill_color(*LGREY) if i % 2 == 0 else pdf.set_fill_color(*WHITE)
        pdf.set_text_color(0, 0, 0)
        pdf.cell(inv_cols[0][1], 7, ar(p["name"]),       border=1, fill=True)
        pdf.cell(inv_cols[1][1], 7, ar(p["category"]),   border=1, fill=True)
        pdf.cell(inv_cols[2][1], 7, f"{p['price']:.3f}", border=1, fill=True, align="C")
        pdf.set_text_color(*(RED if p["stock"] < 15 else GREEN))
        pdf.cell(inv_cols[3][1], 7, str(p["stock"]),     border=1, fill=True, align="C")
        pdf.set_text_color(0, 0, 0)
        pdf.ln()

    pdf.ln(6)

    # ── transactions table ──
    if pdf.get_y() > 230:
        pdf.add_page()
    sf(11, bold=True)
    pdf.set_text_color(*NAVY)
    pdf.cell(0, 8, ar("سجل المعاملات"), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(1)

    tx_cols = [("#", 10), (ar("النوع"), 18), (ar("المنتج"), 78), (ar("الكمية"), 14), (ar("الإجمالي ر.ع"), 36), (ar("التاريخ"), 24)]

    def tx_header():
        pdf.set_fill_color(*NAVY); pdf.set_text_color(*WHITE)
        sf(7, bold=True)
        for h, w in tx_cols:
            pdf.cell(w, 7, h, border=1, fill=True, align="C")
        pdf.ln()

    tx_header(); sf(7)

    for i, tx in enumerate(txs):
        if pdf.get_y() > 268:
            pdf.add_page(); tx_header(); sf(7)
        pdf.set_fill_color(*LGREY) if i % 2 == 0 else pdf.set_fill_color(*WHITE)
        is_sale  = tx["type"] == "sale"
        date_str = str(tx["created_at"])[:10] if tx["created_at"] else "—"
        pdf.set_text_color(0, 0, 0)
        pdf.cell(tx_cols[0][1], 6, str(tx["id"]),          border=1, fill=True, align="C")
        pdf.set_text_color(*(GREEN if is_sale else RED))
        pdf.cell(tx_cols[1][1], 6, ar("بيع") if is_sale else ar("مرفوض"), border=1, fill=True, align="C")
        pdf.set_text_color(0, 0, 0)
        pdf.cell(tx_cols[2][1], 6, ar(tx["product_name"]), border=1, fill=True)
        pdf.cell(tx_cols[3][1], 6, str(tx["qty"]),         border=1, fill=True, align="C")
        pdf.cell(tx_cols[4][1], 6, f"{tx['total']:.3f}",   border=1, fill=True, align="C")
        pdf.cell(tx_cols[5][1], 6, date_str,                border=1, fill=True, align="C")
        pdf.ln()

    # ── footer ──
    pdf.set_y(-14)
    sf(7)
    pdf.set_text_color(160, 160, 160)
    pdf.cell(0, 6, ar(f"Layan  ·  {now.strftime('%Y-%m-%d %H:%M')}  ·  سري"), align="C")

    filename = f"Mustawda_Report_{now.strftime('%Y-%m-%d_%H%M')}.pdf"
    filepath = os.path.join(REPORTS_DIR, filename)
    pdf.output(filepath)
    send_email_report(filepath, now)
    return filename


def generate_invoice_pdf(tx_id):
    """Generate a professional one-page invoice PDF. Returns BytesIO or None."""
    from fpdf import FPDF

    logo_path = os.path.join(_BASE, "static", "logo.png")

    con = get_db()
    tx = con.execute("SELECT * FROM transactions WHERE id=? AND type='sale'", (tx_id,)).fetchone()
    con.close()
    if not tx:
        return None

    reg_font, bold_font = _find_fonts()
    has_unicode = reg_font is not None

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.set_margins(15, 15, 15)
    pdf.add_page()

    if has_unicode:
        pdf.add_font("U", fname=reg_font)
        pdf.add_font("U", style="B", fname=bold_font or reg_font)

    def sf(size, bold=False):
        if has_unicode:
            pdf.set_font("U", style="B" if bold else "", size=size)
        else:
            pdf.set_font("Helvetica", style="B" if bold else "", size=size)

    def ar(text):
        return _reshape(text) if has_unicode else str(text)

    NAVY  = (15,  52,  96)
    GOLD  = (212, 175, 55)
    WHITE = (255, 255, 255)
    LGREY = (245, 248, 255)
    GREEN = (27,  94,  32)

    # ── header ──
    pdf.set_fill_color(*NAVY)
    pdf.rect(0, 0, 210, 44, "F")
    pdf.set_fill_color(*GOLD)
    pdf.rect(0, 44, 210, 3, "F")

    if os.path.isfile(logo_path):
        pdf.image(logo_path, x=8, y=6, h=30)

    sf(22, bold=True)
    pdf.set_text_color(*GOLD)
    pdf.set_y(8)
    pdf.cell(0, 12, "Layan", align="C", new_x="LMARGIN", new_y="NEXT")
    sf(9)
    pdf.set_text_color(200, 210, 230)
    pdf.cell(0, 6, ar("استيراد وتصدير"), align="C", new_x="LMARGIN", new_y="NEXT")

    pdf.set_text_color(0, 0, 0)
    pdf.set_y(56)

    # ── invoice title ──
    sf(15, bold=True)
    pdf.set_text_color(*NAVY)
    pdf.cell(0, 10, ar("فاتورة بيع"), align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)

    # ── invoice meta ──
    date_str = str(tx["created_at"])[:10] if tx["created_at"] else datetime.now().strftime("%Y-%m-%d")
    sf(9)
    pdf.set_text_color(80, 80, 80)
    pdf.cell(90, 6, ar(f"رقم الفاتورة:  #{tx['id']:04d}"), align="L")
    pdf.cell(90, 6, ar(f"التاريخ:  {date_str}"), align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(5)

    # ── customer box ──
    c_name  = tx["customer_name"]  if tx["customer_name"]  else "—"
    c_phone = tx["customer_phone"] if tx["customer_phone"] else "—"
    cy = pdf.get_y()
    pdf.set_fill_color(*LGREY)
    pdf.set_draw_color(200, 210, 230)
    pdf.rect(15, cy, 180, 22, "FD")
    sf(9, bold=True)
    pdf.set_text_color(*NAVY)
    pdf.set_xy(15, cy + 3)
    pdf.cell(180, 6, ar("بيانات العميل"), align="C", new_x="LMARGIN", new_y="NEXT")
    sf(9)
    pdf.set_text_color(50, 50, 50)
    pdf.cell(90, 6, ar(f"الاسم:  {c_name}"), align="C")
    pdf.cell(90, 6, ar(f"الهاتف:  {c_phone}"), align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(7)

    # ── products table ──
    sf(9, bold=True)
    pdf.set_fill_color(*NAVY)
    pdf.set_text_color(*WHITE)
    cols = [(ar("المنتج"), 86), (ar("الكمية"), 26), (ar("سعر الوحدة ر.ع"), 40), (ar("الإجمالي ر.ع"), 38)]
    for h, w in cols:
        pdf.cell(w, 9, h, border=1, fill=True, align="C")
    pdf.ln()

    sf(9)
    pdf.set_fill_color(*LGREY)
    pdf.set_text_color(0, 0, 0)
    unit_price = round(tx["total"] / tx["qty"], 3) if tx["qty"] else tx["total"]
    pdf.cell(cols[0][1], 9, ar(tx["product_name"]), border=1, fill=True)
    pdf.cell(cols[1][1], 9, str(tx["qty"]),         border=1, fill=True, align="C")
    pdf.cell(cols[2][1], 9, f"{unit_price:.3f}",    border=1, fill=True, align="C")
    pdf.cell(cols[3][1], 9, f"{tx['total']:.3f}",   border=1, fill=True, align="C")
    pdf.ln()
    pdf.ln(3)

    # ── total ──
    sf(11, bold=True)
    pdf.set_text_color(*NAVY)
    pdf.cell(152, 10, ar("الإجمالي"), align="R")
    pdf.set_fill_color(232, 245, 233)
    pdf.set_draw_color(200, 220, 200)
    pdf.cell(38, 10, f"{tx['total']:.3f} ر.ع", border=1, fill=True, align="C",
             new_x="LMARGIN", new_y="NEXT")
    pdf.ln(10)

    # ── return policy ──
    cy2 = pdf.get_y()
    pdf.set_fill_color(252, 252, 252)
    pdf.set_draw_color(220, 220, 220)
    pdf.rect(15, cy2, 180, 20, "FD")
    sf(8, bold=True)
    pdf.set_text_color(80, 80, 80)
    pdf.set_xy(15, cy2 + 3)
    pdf.cell(180, 5, ar("شروط الاسترجاع"), align="C", new_x="LMARGIN", new_y="NEXT")
    sf(7)
    pdf.set_text_color(110, 110, 110)
    pdf.cell(180, 5,
             ar("يحق للعميل إرجاع المنتج خلال 7 أيام من تاريخ الشراء بحالته الأصلية مع الاحتفاظ بالفاتورة."),
             align="C")

    # ── footer ──
    pdf.set_y(-14)
    sf(7)
    pdf.set_text_color(160, 160, 160)
    now = datetime.now()
    pdf.cell(0, 6, ar(f"Layan  ·  استيراد وتصدير  ·  {now.strftime('%Y-%m-%d')}"), align="C")

    buf = io.BytesIO()
    pdf.output(buf)
    buf.seek(0)
    return buf


def send_email_report(pdf_path, report_dt=None):
    """Email the PDF report. Silently skips if credentials are not configured."""
    if not SMTP_USER or not SMTP_PASS:
        return

    dt_str = (report_dt or datetime.now()).strftime("%d %B %Y")
    subject = f"تقرير Layan — {dt_str}"

    msg = MIMEMultipart()
    msg["From"]    = SMTP_USER
    msg["To"]      = REPORT_EMAIL_TO
    msg["Subject"] = subject

    body = (
        f"السلام عليكم،\n\n"
        f"مرفق تقرير Layan الدوري بتاريخ {dt_str}.\n\n"
        f"Al Mustawda bi-weekly report — {dt_str}.\n"
    )
    msg.attach(MIMEText(body, "plain", "utf-8"))

    with open(pdf_path, "rb") as f:
        part = MIMEBase("application", "octet-stream")
        part.set_payload(f.read())
    encoders.encode_base64(part)
    part.add_header(
        "Content-Disposition",
        f'attachment; filename="{os.path.basename(pdf_path)}"',
    )
    msg.attach(part)

    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
        print(f"[email] Report sent to {REPORT_EMAIL_TO}")
    except Exception as e:
        print(f"[email] Failed to send report: {e}")


# ── routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/products", methods=["GET"])
def get_products():
    con = get_db()
    rows = con.execute("SELECT * FROM products ORDER BY name").fetchall()
    con.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/products", methods=["POST"])
def add_product():
    data = request.get_json()
    name     = (data.get("name") or "").strip()
    category = (data.get("category") or "").strip()
    try:
        price      = float(data["price"])
        cost_price = float(data.get("cost_price") or 0)
        stock      = int(data["stock"])
    except (KeyError, ValueError):
        return jsonify({"error": "بيانات غير صحيحة"}), 400
    if not name or price <= 0 or cost_price < 0 or stock < 0:
        return jsonify({"error": "يرجى ملء جميع الحقول بقيم صحيحة"}), 400
    con = get_db()
    cur = con.execute(
        "INSERT INTO products (name, category, price, cost_price, stock) VALUES (?,?,?,?,?)",
        (name, category, price, cost_price, stock))
    con.commit()
    product = dict(con.execute("SELECT * FROM products WHERE id=?", (cur.lastrowid,)).fetchone())
    con.close()
    return jsonify(product), 201


@app.route("/api/dashboard", methods=["GET"])
def dashboard():
    con = get_db()
    revenue       = con.execute("SELECT COALESCE(SUM(total),0)      FROM transactions WHERE type='sale'").fetchone()[0]
    refunds       = con.execute("SELECT COALESCE(SUM(total),0)      FROM transactions WHERE type='return'").fetchone()[0]
    cogs          = con.execute("SELECT COALESCE(SUM(cost_total),0) FROM transactions WHERE type='sale'").fetchone()[0]
    cogs_ret      = con.execute("SELECT COALESCE(SUM(cost_total),0) FROM transactions WHERE type='return'").fetchone()[0]
    sales_count   = con.execute("SELECT COUNT(*) FROM transactions WHERE type='sale'").fetchone()[0]
    returns_count = con.execute("SELECT COUNT(*) FROM transactions WHERE type='return'").fetchone()[0]
    recent        = con.execute("SELECT * FROM transactions ORDER BY id DESC LIMIT 5").fetchall()
    low_stock     = con.execute("SELECT * FROM products WHERE stock < 15").fetchall()
    con.close()
    net    = revenue - refunds
    profit = net - (cogs - cogs_ret)
    return jsonify({
        "revenue": revenue, "refunds": refunds, "net": net, "profit": profit,
        "sales_count": sales_count, "returns_count": returns_count,
        "recent":    [dict(r) for r in recent],
        "low_stock": [dict(p) for p in low_stock],
    })


@app.route("/api/sale", methods=["POST"])
def make_sale():
    data = request.get_json()
    try:
        product_id = int(data["product_id"])
        qty        = int(data["qty"])
    except (KeyError, ValueError):
        return jsonify({"error": "بيانات غير صحيحة"}), 400
    if qty < 1:
        return jsonify({"error": "يجب أن تكون الكمية واحدة على الأقل"}), 400
    customer_name  = (data.get("customer_name")  or "").strip()
    customer_phone = (data.get("customer_phone") or "").strip()
    if not customer_phone.isdigit() or len(customer_phone) != 8:
        return jsonify({"error": "رقم الهاتف مطلوب ويجب أن يتكون من 8 أرقام (نظام عُمان)"}), 400
    con = get_db()
    product = con.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
    if not product:
        con.close(); return jsonify({"error": "المنتج غير موجود"}), 404
    if qty > product["stock"]:
        con.close(); return jsonify({"error": f"المخزون غير كافٍ! المتاح فقط {product['stock']} وحدات"}), 400
    total      = round(product["price"]      * qty, 3)
    cost_total = round(product["cost_price"] * qty, 3)
    cur = con.execute(
        "INSERT INTO transactions (product_id, product_name, qty, total, cost_total, type, customer_name, customer_phone) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (product["id"], product["name"], qty, total, cost_total, "sale", customer_name, customer_phone))
    tx_id = cur.lastrowid
    con.execute("UPDATE products SET stock = stock - ? WHERE id=?", (qty, product_id))
    con.commit(); con.close()
    return jsonify({"message": "تمت عملية البيع!", "total": total, "tx_id": tx_id})


@app.route("/api/products/<int:product_id>", methods=["DELETE"])
def delete_product(product_id):
    con = get_db()
    if not con.execute("SELECT id FROM products WHERE id=?", (product_id,)).fetchone():
        con.close(); return jsonify({"error": "المنتج غير موجود"}), 404
    con.execute("DELETE FROM products WHERE id=?", (product_id,))
    con.commit(); con.close()
    return jsonify({"message": "تم حذف المنتج بنجاح"})


@app.route("/api/analytics")
def get_analytics():
    con = get_db()
    daily = con.execute("""
        SELECT DATE(created_at) as day,
               COALESCE(SUM(CASE WHEN type='sale' THEN total ELSE 0 END), 0) as revenue,
               COUNT(CASE WHEN type='sale' THEN 1 END) as count
        FROM transactions
        WHERE created_at >= DATE('now', '-30 days')
        GROUP BY DATE(created_at) ORDER BY day
    """).fetchall()
    top = con.execute("""
        SELECT product_name,
               SUM(total)      as revenue,
               SUM(cost_total) as cogs,
               SUM(total) - SUM(cost_total) as profit,
               SUM(qty)        as units
        FROM transactions WHERE type='sale'
        GROUP BY product_name ORDER BY revenue DESC LIMIT 8
    """).fetchall()
    cats = con.execute("""
        SELECT COALESCE(p.category, 'غير محدد') as category, SUM(t.total) as revenue
        FROM transactions t LEFT JOIN products p ON t.product_id = p.id
        WHERE t.type='sale' GROUP BY p.category ORDER BY revenue DESC
    """).fetchall()
    con.close()
    return jsonify({
        "daily":        [dict(r) for r in daily],
        "top_products": [dict(r) for r in top],
        "categories":   [dict(r) for r in cats],
    })


@app.route("/api/goals", methods=["GET"])
def get_goals():
    con = get_db()
    goals = con.execute("SELECT * FROM goals ORDER BY id DESC").fetchall()
    result = []
    for g in goals:
        if g["product_id"]:
            sold = con.execute(
                "SELECT COALESCE(SUM(qty),0) FROM transactions WHERE product_id=? AND type='sale'",
                (g["product_id"],)).fetchone()[0]
        else:
            sold = con.execute(
                "SELECT COALESCE(SUM(qty),0) FROM transactions WHERE type='sale'").fetchone()[0]
        d = dict(g)
        d["sold"]     = sold
        d["progress"] = min(100, int(sold / g["target_qty"] * 100)) if g["target_qty"] > 0 else 0
        result.append(d)
    con.close()
    return jsonify(result)


@app.route("/api/goals", methods=["POST"])
def add_goal():
    data = request.get_json()
    title = (data.get("title") or "").strip()
    if not title:
        return jsonify({"error": "يرجى إدخال عنوان الهدف"}), 400
    try:
        target_qty = int(data["target_qty"])
    except (KeyError, ValueError):
        return jsonify({"error": "يرجى إدخال كمية مستهدفة صحيحة"}), 400
    if target_qty < 1:
        return jsonify({"error": "يجب أن تكون الكمية المستهدفة أكبر من صفر"}), 400
    product_id = data.get("product_id") or None
    if product_id:
        try: product_id = int(product_id)
        except ValueError: product_id = None
    con = get_db()
    cur = con.execute(
        "INSERT INTO goals (title, product_id, target_qty) VALUES (?,?,?)",
        (title, product_id, target_qty))
    con.commit()
    goal = dict(con.execute("SELECT * FROM goals WHERE id=?", (cur.lastrowid,)).fetchone())
    goal["sold"] = 0; goal["progress"] = 0
    con.close()
    return jsonify(goal), 201


@app.route("/api/goals/<int:goal_id>", methods=["DELETE"])
def delete_goal(goal_id):
    con = get_db()
    if not con.execute("SELECT id FROM goals WHERE id=?", (goal_id,)).fetchone():
        con.close(); return jsonify({"error": "الهدف غير موجود"}), 404
    con.execute("DELETE FROM goals WHERE id=?", (goal_id,))
    con.commit(); con.close()
    return jsonify({"message": "تم حذف الهدف"})


@app.route("/api/transactions", methods=["GET"])
def get_transactions():
    tx_type = request.args.get("type")
    con = get_db()
    if tx_type:
        rows = con.execute(
            "SELECT * FROM transactions WHERE type=? AND returned=0 ORDER BY id DESC", (tx_type,)).fetchall()
    else:
        rows = con.execute("SELECT * FROM transactions ORDER BY id DESC").fetchall()
    con.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/return/<int:tx_id>", methods=["POST"])
def process_return(tx_id):
    data     = request.get_json(silent=True) or {}
    restock  = data.get("restock",  True)
    refunded = data.get("refunded", True)
    con = get_db()
    tx = con.execute("SELECT * FROM transactions WHERE id=? AND type='sale'", (tx_id,)).fetchone()
    if not tx:
        con.close(); return jsonify({"error": "عملية البيع غير موجودة"}), 404
    if tx["returned"]:
        con.close(); return jsonify({"error": "تمت معالجة هذا الاسترجاع بالفعل"}), 400
    con.execute("UPDATE transactions SET returned=1 WHERE id=?", (tx_id,))
    ret_total      = tx["total"]      if refunded else 0.0
    ret_cost_total = tx["cost_total"] if refunded else 0.0
    con.execute(
        "INSERT INTO transactions (product_id, product_name, qty, total, cost_total, type) VALUES (?,?,?,?,?,?)",
        (tx["product_id"], tx["product_name"], tx["qty"], ret_total, ret_cost_total, "return"))
    if restock:
        con.execute("UPDATE products SET stock = stock + ? WHERE id=?", (tx["qty"], tx["product_id"]))
    parts = []
    parts.append("تم إرجاع المنتج للمخزن" if restock else "تم إتلاف المنتج")
    parts.append("وإرجاع المبلغ للعميل" if refunded else "ولم يُرجع المبلغ للعميل")
    con.commit(); con.close()
    return jsonify({"message": " — ".join(parts)})


@app.route("/api/personal-goals", methods=["GET"])
def get_personal_goals():
    con = get_db()
    rows = con.execute(
        "SELECT * FROM personal_goals ORDER BY achieved ASC, id DESC").fetchall()
    con.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/personal-goals", methods=["POST"])
def add_personal_goal():
    data = request.get_json()
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "يرجى إدخال نص الهدف"}), 400
    con = get_db()
    cur = con.execute("INSERT INTO personal_goals (text) VALUES (?)", (text,))
    con.commit()
    goal = dict(con.execute(
        "SELECT * FROM personal_goals WHERE id=?", (cur.lastrowid,)).fetchone())
    con.close()
    return jsonify(goal), 201


@app.route("/api/personal-goals/<int:goal_id>/toggle", methods=["PATCH"])
def toggle_personal_goal(goal_id):
    con = get_db()
    g = con.execute("SELECT * FROM personal_goals WHERE id=?", (goal_id,)).fetchone()
    if not g:
        con.close(); return jsonify({"error": "الهدف غير موجود"}), 404
    new_val = 0 if g["achieved"] else 1
    con.execute("UPDATE personal_goals SET achieved=? WHERE id=?", (new_val, goal_id))
    con.commit(); con.close()
    return jsonify({"achieved": new_val})


@app.route("/api/personal-goals/<int:goal_id>", methods=["DELETE"])
def delete_personal_goal(goal_id):
    con = get_db()
    if not con.execute("SELECT id FROM personal_goals WHERE id=?", (goal_id,)).fetchone():
        con.close(); return jsonify({"error": "الهدف غير موجود"}), 404
    con.execute("DELETE FROM personal_goals WHERE id=?", (goal_id,))
    con.commit(); con.close()
    return jsonify({"message": "تم حذف الهدف الشخصي"})


@app.route("/api/report/generate", methods=["POST"])
def api_generate_report():
    try:
        filename = generate_pdf_report()
        return jsonify({"ok": True, "filename": filename})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/report/latest")
def api_report_latest():
    files = sorted(glob.glob(os.path.join(REPORTS_DIR, "*.pdf")), reverse=True)
    if not files:
        return jsonify({"available": False})
    name = os.path.basename(files[0])
    try:
        dt = datetime.strptime(name.replace("Mustawda_Report_", "").replace(".pdf", ""), "%Y-%m-%d_%H%M")
        formatted = dt.strftime("%d %B %Y, %H:%M")
    except Exception:
        formatted = name
    return jsonify({"available": True, "filename": name, "date": formatted})


@app.route("/api/report/download")
def api_report_download():
    files = sorted(glob.glob(os.path.join(REPORTS_DIR, "*.pdf")), reverse=True)
    if not files:
        return jsonify({"error": "لا يوجد تقرير بعد."}), 404
    return send_file(files[0], as_attachment=True, download_name=os.path.basename(files[0]))


@app.route("/api/transactions/<int:tx_id>", methods=["DELETE"])
def delete_transaction(tx_id):
    con = get_db()
    tx = con.execute("SELECT * FROM transactions WHERE id=? AND type='sale'", (tx_id,)).fetchone()
    if not tx:
        con.close(); return jsonify({"error": "العملية غير موجودة"}), 404
    if tx["returned"]:
        con.close(); return jsonify({"error": "لا يمكن حذف عملية تم استرجاعها"}), 400
    con.execute("UPDATE products SET stock = stock + ? WHERE id=?", (tx["qty"], tx["product_id"]))
    con.execute("DELETE FROM transactions WHERE id=?", (tx_id,))
    con.commit(); con.close()
    return jsonify({"message": "تم حذف العملية وإعادة المخزون"})


@app.route("/api/invoice/<int:tx_id>")
def download_invoice(tx_id):
    try:
        buf = generate_invoice_pdf(tx_id)
        if not buf:
            return jsonify({"error": "العملية غير موجودة"}), 404
        return send_file(
            buf,
            as_attachment=True,
            download_name=f"Layan_Invoice_{tx_id:04d}.pdf",
            mimetype="application/pdf",
        )
    except Exception as e:
        print(f"[invoice] Error generating invoice {tx_id}: {e}")
        return jsonify({"error": f"فشل إنشاء الفاتورة: {e}"}), 500


@app.route("/api/customers")
def get_customers():
    product   = request.args.get("product",   "")
    min_qty   = request.args.get("min_qty",   0, type=int)
    date_from = request.args.get("date_from", "")
    date_to   = request.args.get("date_to",   "")

    where  = ["type='sale'", "returned=0", "customer_name != ''"]
    params = []
    if product:
        where.append("product_name LIKE ?"); params.append(f"%{product}%")
    if min_qty:
        where.append("qty >= ?");           params.append(min_qty)
    if date_from:
        where.append("DATE(created_at) >= ?"); params.append(date_from)
    if date_to:
        where.append("DATE(created_at) <= ?"); params.append(date_to)

    sql = f"""
        SELECT customer_name, customer_phone,
               COUNT(*)                         AS purchases,
               SUM(qty)                         AS total_qty,
               ROUND(SUM(total), 3)             AS total_spent,
               MAX(DATE(created_at))            AS last_purchase,
               GROUP_CONCAT(DISTINCT product_name) AS products
        FROM transactions
        WHERE {' AND '.join(where)}
        GROUP BY customer_name, customer_phone
        ORDER BY total_spent DESC
    """
    con  = get_db()
    rows = con.execute(sql, params).fetchall()
    con.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/customers/top")
def get_top_customers():
    con  = get_db()
    rows = con.execute("""
        SELECT customer_name, customer_phone,
               COUNT(*)             AS purchases,
               SUM(qty)             AS total_qty,
               ROUND(SUM(total),3)  AS total_spent
        FROM transactions
        WHERE type='sale' AND returned=0 AND customer_name != ''
        GROUP BY customer_name, customer_phone
        ORDER BY total_spent DESC
        LIMIT 10
    """).fetchall()
    con.close()
    return jsonify([dict(r) for r in rows])


@app.route("/run-report")
def run_report_cron():
    key = request.args.get("key", "")
    if not CRON_SECRET or key != CRON_SECRET:
        return "Unauthorized", 401
    try:
        filename = generate_pdf_report()
        return f"OK: {filename}", 200
    except Exception as e:
        return f"ERROR: {e}", 500


@app.route("/api/clear-records", methods=["POST"])
def clear_records():
    data = request.get_json(silent=True) or {}
    if str(data.get("password", "")) != "12344":
        return jsonify({"error": "كلمة المرور غير صحيحة"}), 403
    con = get_db()
    con.execute("DELETE FROM transactions")
    con.commit()
    con.close()
    return jsonify({"message": "تم مسح جميع السجلات بنجاح"})


# ── startup ───────────────────────────────────────────────────────────────────

init_db()
_migrate()
_ensure_admin()
_ensure_fonts()

if __name__ == "__main__":
    app.run(debug=True)
