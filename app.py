import sqlite3
import os
import glob
import io
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email.mime.text import MIMEText
from email import encoders
from datetime import datetime, timedelta
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
WHATSAPP_NUMBER  = os.environ.get("WHATSAPP_NUMBER",  "")


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
    goal_cols = [r[1] for r in con.execute("PRAGMA table_info(goals)").fetchall()]
    pg_cols   = [r[1] for r in con.execute("PRAGMA table_info(personal_goals)").fetchall()]
    if "deadline" not in goal_cols:
        con.execute("ALTER TABLE goals ADD COLUMN deadline TEXT")
        con.commit()
    if "deadline" not in pg_cols:
        con.execute("ALTER TABLE personal_goals ADD COLUMN deadline TEXT")
        con.commit()
    if "is_deferred" not in tx_cols:
        con.execute("ALTER TABLE transactions ADD COLUMN is_deferred INTEGER NOT NULL DEFAULT 0")
        con.commit()
    con.execute("""
        CREATE TABLE IF NOT EXISTS password_resets (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            email      TEXT    NOT NULL,
            token      TEXT    NOT NULL UNIQUE,
            expires_at DATETIME NOT NULL,
            used       INTEGER NOT NULL DEFAULT 0
        )
    """)
    # Per-product cash withdrawals (audit trail — never deleted, keeps report math intact)
    con.execute("""
        CREATE TABLE IF NOT EXISTS withdrawals (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id   INTEGER,
            product_name TEXT    NOT NULL,
            amount       REAL    NOT NULL,
            profit       REAL    NOT NULL DEFAULT 0,
            created_at   DATETIME DEFAULT CURRENT_TIMESTAMP
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


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "GET":
        return render_template("forgot_password.html")
    data  = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    con   = get_db()
    user  = con.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if not user:
        con.close()
        return jsonify({"ok": True})
    import random
    otp     = str(random.randint(100000, 999999))
    expires = datetime.now() + timedelta(minutes=10)
    con.execute("DELETE FROM password_resets WHERE email=?", (email,))
    con.execute("INSERT INTO password_resets (email, token, expires_at) VALUES (?,?,?)",
                (email, otp, expires.strftime("%Y-%m-%d %H:%M:%S")))
    con.commit()
    con.close()
    gmail_user = os.environ.get("GMAIL_USER", "")
    gmail_pass = os.environ.get("GMAIL_PASS", "")
    try:
        import smtplib
        from email.mime.text import MIMEText
        body = f"""السلام عليكم،

رمز التحقق الخاص بك لإعادة تعيين كلمة المرور:

  {otp}

هذا الرمز صالح لمدة 10 دقائق فقط.
إذا لم تطلب إعادة تعيين كلمة المرور، تجاهل هذا البريد."""
        msg = MIMEText(body, "plain", "utf-8")
        msg["From"]    = gmail_user
        msg["To"]      = email
        msg["Subject"] = "Layan — رمز التحقق OTP"
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
            server.login(gmail_user, gmail_pass)
            server.send_message(msg)
    except Exception as e:
        print(f"[otp email] {e}")
    session["otp_email"] = email
    return jsonify({"ok": True})


@app.route("/verify-otp", methods=["GET", "POST"])
def verify_otp():
    if request.method == "GET":
        if "otp_email" not in session:
            return redirect(url_for("forgot_password"))
        return render_template("verify_otp.html")
    data  = request.get_json(silent=True) or {}
    otp   = (data.get("otp") or "").strip()
    email = session.get("otp_email", "")
    if not email:
        return jsonify({"ok": False, "error": "انتهت الجلسة، أعد المحاولة"}), 400
    con = get_db()
    row = con.execute(
        "SELECT * FROM password_resets WHERE email=? AND token=? AND used=0 AND expires_at > datetime('now')",
        (email, otp)
    ).fetchone()
    con.close()
    if not row:
        return jsonify({"ok": False, "error": "الرمز غير صحيح أو منتهي الصلاحية"}), 400
    session["otp_verified"] = True
    return jsonify({"ok": True})


@app.route("/reset-password", methods=["GET", "POST"])
def reset_password():
    if "otp_email" not in session or not session.get("otp_verified"):
        return redirect(url_for("forgot_password"))
    if request.method == "GET":
        return render_template("reset_password.html")
    data   = request.get_json(silent=True) or {}
    new_pw = data.get("new_password", "")
    if len(new_pw) < 6:
        return jsonify({"ok": False, "error": "كلمة المرور يجب أن تكون 6 أحرف على الأقل"}), 400
    email = session["otp_email"]
    con   = get_db()
    con.execute("UPDATE users SET password=? WHERE email=?",
                (generate_password_hash(new_pw), email))
    con.execute("UPDATE password_resets SET used=1 WHERE email=?", (email,))
    con.commit()
    con.close()
    session.pop("otp_email", None)
    session.pop("otp_verified", None)
    return jsonify({"ok": True})


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
    withdrawn   = con.execute("SELECT COALESCE(SUM(amount),0) FROM withdrawals").fetchone()[0]
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
    if withdrawn > 0:
        sf(9)
        pdf.set_text_color(120, 80, 0)
        pdf.cell(0, 7, ar(f"إجمالي المبالغ المسحوبة من الصندوق: {withdrawn:.3f} ر.ع"),
                 align="C", new_x="LMARGIN", new_y="NEXT")
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
    pdf.cell(180, 6, ar("أفضل عميل خلال الفترة الأخيرة (14 يوم)"), align="C",
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

    # ── top products (compact) ──
    if top_rows:
        sf(10, bold=True)
        pdf.set_text_color(*NAVY)
        pdf.cell(0, 8, ar("أفضل المنتجات"), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(1)
        tp_cols = [(ar("المنتج"), 120), (ar("الإيرادات ر.ع"), 60)]
        pdf.set_fill_color(*NAVY); pdf.set_text_color(*WHITE)
        sf(8, bold=True)
        for h, w in tp_cols:
            pdf.cell(w, 7, h, border=1, fill=True, align="C")
        pdf.ln(); sf(8)
        for i, r in enumerate(top_rows[:6]):
            pdf.set_fill_color(*LGREY) if i % 2 == 0 else pdf.set_fill_color(*WHITE)
            pdf.set_text_color(0, 0, 0)
            pdf.cell(tp_cols[0][1], 6, ar(r["product_name"]), border=1, fill=True)
            pdf.set_text_color(*GREEN)
            pdf.cell(tp_cols[1][1], 6, f"{r['rev']:.3f}", border=1, fill=True, align="C")
            pdf.ln()
        pdf.ln(4)

    # ── inventory (compact, low stock only) ──
    low_stock = [p for p in products if p["stock"] < 15]
    if low_stock:
        sf(10, bold=True)
        pdf.set_text_color(*RED)
        pdf.cell(0, 8, ar("تحذير — منتجات مخزونها منخفض"), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(1)
        inv_cols = [(ar("المنتج"), 100), (ar("المخزون"), 40), (ar("السعر ر.ع"), 40)]
        pdf.set_fill_color(*NAVY); pdf.set_text_color(*WHITE)
        sf(8, bold=True)
        for h, w in inv_cols:
            pdf.cell(w, 7, h, border=1, fill=True, align="C")
        pdf.ln(); sf(8)
        for i, p in enumerate(low_stock[:8]):
            pdf.set_fill_color((255,240,240)) if i % 2 == 0 else pdf.set_fill_color(*WHITE)
            pdf.set_text_color(0, 0, 0)
            pdf.cell(inv_cols[0][1], 6, ar(p["name"]), border=1, fill=True)
            pdf.set_text_color(*RED)
            pdf.cell(inv_cols[1][1], 6, str(p["stock"]), border=1, fill=True, align="C")
            pdf.set_text_color(0, 0, 0)
            pdf.cell(inv_cols[2][1], 6, f"{p['price']:.3f}", border=1, fill=True, align="C")
            pdf.ln()
        pdf.ln(4)

    # ── footer ──
    pdf.set_y(-14)
    sf(7)
    pdf.set_text_color(160, 160, 160)
    pdf.cell(0, 6, ar(f"Layan  ·  {now.strftime('%Y-%m-%d %H:%M')}  ·  سري"), align="C")

    filename = f"Mustawda_Report_{now.strftime('%Y-%m-%d_%H%M')}.pdf"
    filepath = os.path.join(REPORTS_DIR, filename)
    pdf.output(filepath)
    import threading
    threading.Thread(target=send_email_report, args=(filepath, now), daemon=True).start()
    return filename


def generate_invoice_pdf(tx_id, force_deferred=False):
    """Generate a professional one-page invoice PDF. Returns BytesIO or None."""
    from fpdf import FPDF

    logo_path = os.path.join(_BASE, "static", "logo.png")

    con = get_db()
    tx = con.execute("SELECT * FROM transactions WHERE id=? AND type='sale'", (tx_id,)).fetchone()
    con.close()
    if not tx:
        return None
    is_deferred = force_deferred or bool(tx["is_deferred"])

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
        pdf.image(logo_path, x=75, y=5, h=32)

    sf(9)
    pdf.set_text_color(200, 210, 230)
    pdf.set_y(39)
    wa_line = ar(f"واتساب: {WHATSAPP_NUMBER}") if WHATSAPP_NUMBER else ""
    if wa_line:
        pdf.cell(0, 5, wa_line, align="C", new_x="LMARGIN", new_y="NEXT")

    pdf.set_text_color(0, 0, 0)
    pdf.set_y(56)

    # ── invoice title ──
    sf(15, bold=True)
    pdf.set_text_color(*NAVY)
    title_text = ar("فاتورة بيع — مؤجلة الدفع") if is_deferred else ar("فاتورة بيع")
    pdf.cell(0, 10, title_text, align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    if is_deferred:
        pdf.set_fill_color(254, 226, 226)
        pdf.set_draw_color(220, 38, 38)
        stamp_y = pdf.get_y()
        pdf.rect(15, stamp_y, 180, 12, "FD")
        sf(11, bold=True)
        pdf.set_text_color(180, 0, 0)
        pdf.set_xy(15, stamp_y + 2)
        pdf.cell(180, 8, ar("مؤجلة الدفع / غير مدفوعة"), align="C", new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(0, 0, 0)
        pdf.ln(4)
    else:
        pdf.ln(1)

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

    # ── signature area ──
    pdf.ln(6)
    sig_y = pdf.get_y()
    if sig_y < 230:
        pdf.set_fill_color(252, 252, 252)
        pdf.set_draw_color(210, 210, 210)
        pdf.rect(15, sig_y, 180, 24, "FD")
        sf(8, bold=True)
        pdf.set_text_color(80, 80, 80)
        pdf.set_xy(15, sig_y + 3)
        pdf.cell(90, 5, ar("توقيع العميل"), align="C")
        pdf.cell(90, 5, ar("توقيع المسؤول"), align="C", new_x="LMARGIN", new_y="NEXT")
        pdf.set_draw_color(160, 160, 160)
        pdf.line(25, sig_y + 18, 95, sig_y + 18)
        pdf.line(115, sig_y + 18, 185, sig_y + 18)

    # ── footer ──
    pdf.set_y(-14)
    sf(7)
    pdf.set_text_color(160, 160, 160)
    now = datetime.now()
    footer = f"Layan  ·  {now.strftime('%Y-%m-%d')}"
    if WHATSAPP_NUMBER:
        footer += f"  ·  {WHATSAPP_NUMBER}"
    pdf.cell(0, 6, ar(footer), align="C")

    buf = io.BytesIO()
    pdf.output(buf)
    buf.seek(0)
    return buf


def send_email_report(pdf_path, report_dt=None):
    """Email the PDF report via Gmail SMTP."""
    gmail_user = os.environ.get("GMAIL_USER", "")
    gmail_pass = os.environ.get("GMAIL_PASS", "")
    if not gmail_user or not gmail_pass:
        print("[email] GMAIL_USER or GMAIL_PASS not set — skipping email")
        return
    try:
        import smtplib
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText
        from email.mime.base import MIMEBase
        from email import encoders
        dt_str = (report_dt or datetime.now()).strftime("%d %B %Y")
        msg = MIMEMultipart()
        msg["From"] = gmail_user
        msg["To"] = REPORT_EMAIL_TO
        msg["Subject"] = f"تقرير Layan — {dt_str}"
        msg.attach(MIMEText(f"السلام عليكم،\n\nمرفق تقرير Layan الدوري بتاريخ {dt_str}.", "plain", "utf-8"))
        with open(pdf_path, "rb") as f:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", f"attachment; filename={os.path.basename(pdf_path)}")
        msg.attach(part)
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
            server.login(gmail_user, gmail_pass)
            server.send_message(msg)
        print(f"[email] Sent to {REPORT_EMAIL_TO}")
    except Exception as e:
        print(f"[email] Failed: {e}")
    send_whatsapp_report(pdf_path, report_dt)


def send_whatsapp_report(pdf_path, report_dt=None):
    """Send PDF report via Green API WhatsApp."""
    instance_id = os.environ.get("GREENAPI_INSTANCE", "")
    api_token   = os.environ.get("GREENAPI_TOKEN", "")
    wa_number   = os.environ.get("WHATSAPP_NUMBER", "")
    if not instance_id or not api_token or not wa_number:
        print("[whatsapp] Green API credentials not set — skipping")
        return
    try:
        import urllib.request, json
        dt_str  = (report_dt or datetime.now()).strftime("%d %B %Y")
        chat_id = f"{wa_number}@c.us"
        url     = f"https://api.green-api.com/waInstance{instance_id}/sendFileByUpload/{api_token}"
        boundary = "----FormBoundary7MA4YWxkTrZu0gW"
        with open(pdf_path, "rb") as f:
            file_data = f.read()
        filename = os.path.basename(pdf_path)
        body  = f"--{boundary}\r\n"
        body += f'Content-Disposition: form-data; name="chatId"\r\n\r\n{chat_id}\r\n'
        body += f"--{boundary}\r\n"
        body += f'Content-Disposition: form-data; name="caption"\r\n\r\nتقرير Layan — {dt_str}\r\n'
        body += f"--{boundary}\r\n"
        body += f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        body += "Content-Type: application/pdf\r\n\r\n"
        body_bytes = body.encode("utf-8") + file_data + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(url, data=body_bytes, method="POST")
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        with urllib.request.urlopen(req, timeout=60) as resp:
            print(f"[whatsapp] Sent — {resp.read().decode()}")
    except Exception as e:
        print(f"[whatsapp] Failed: {e}")


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
    revenue       = con.execute("SELECT COALESCE(SUM(total),0)      FROM transactions WHERE type='sale'  AND is_deferred=0 AND returned=0").fetchone()[0]
    refunds       = con.execute("SELECT COALESCE(SUM(total),0)      FROM transactions WHERE type='return' AND returned=0").fetchone()[0]
    cogs          = con.execute("SELECT COALESCE(SUM(cost_total),0) FROM transactions WHERE type='sale'  AND is_deferred=0 AND returned=0").fetchone()[0]
    cogs_ret      = con.execute("SELECT COALESCE(SUM(cost_total),0) FROM transactions WHERE type='return' AND returned=0").fetchone()[0]
    sales_count   = con.execute("SELECT COUNT(*) FROM transactions WHERE type='sale'  AND is_deferred=0").fetchone()[0]
    returns_count = con.execute("SELECT COUNT(*) FROM transactions WHERE type='return'").fetchone()[0]
    withdrawn     = con.execute("SELECT COALESCE(SUM(amount),0) FROM withdrawals").fetchone()[0]
    recent        = con.execute("SELECT * FROM transactions ORDER BY id DESC LIMIT 5").fetchall()
    low_stock     = con.execute("SELECT * FROM products WHERE stock < 15").fetchall()
    con.close()
    net       = revenue - refunds
    profit    = net - (cogs - cogs_ret)
    liquidity = round(net - withdrawn, 3)   # cash actually in the box after withdrawals
    return jsonify({
        "revenue": revenue, "refunds": refunds, "net": net, "profit": profit,
        "withdrawn": round(withdrawn, 3), "liquidity": liquidity,
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
    manual_price   = data.get("manual_price")
    confirm_loss   = data.get("confirm_loss", False)
    is_deferred    = 1 if data.get("is_deferred") else 0
    if not customer_phone.isdigit() or len(customer_phone) != 8:
        return jsonify({"error": "رقم الهاتف مطلوب ويجب أن يتكون من 8 أرقام (نظام عُمان)"}), 400
    con = get_db()
    product = con.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
    if not product:
        con.close(); return jsonify({"error": "المنتج غير موجود"}), 404
    if qty > product["stock"]:
        con.close(); return jsonify({"error": f"المخزون غير كافٍ! المتاح فقط {product['stock']} وحدات"}), 400
    if manual_price is not None:
        try:
            unit_price = float(manual_price)
        except (ValueError, TypeError):
            con.close(); return jsonify({"error": "سعر البيع غير صحيح"}), 400
        if unit_price <= 0:
            con.close(); return jsonify({"error": "سعر البيع يجب أن يكون أكبر من صفر"}), 400
        if unit_price < product["cost_price"] and not confirm_loss:
            con.close()
            return jsonify({
                "loss_warning": True,
                "message": f"سعر البيع ({unit_price:.3f} ر.ع) أقل من سعر التكلفة ({product['cost_price']:.3f} ر.ع). هل أنت متأكد؟"
            }), 200
    else:
        unit_price = product["price"]
    total      = round(unit_price   * qty, 3)
    cost_total = round(product["cost_price"] * qty, 3)
    cur = con.execute(
        "INSERT INTO transactions (product_id, product_name, qty, total, cost_total, type, customer_name, customer_phone, is_deferred) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (product["id"], product["name"], qty, total, cost_total, "sale", customer_name, customer_phone, is_deferred))
    tx_id = cur.lastrowid
    con.execute("UPDATE products SET stock = stock - ? WHERE id=?", (qty, product_id))
    con.commit(); con.close()
    msg = "تم تسجيل الدفع المؤجل!" if is_deferred else "تمت عملية البيع!"
    return jsonify({"message": msg, "total": total, "tx_id": tx_id, "customer_phone": customer_phone, "is_deferred": is_deferred})


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
    deadline = (data.get("deadline") or "").strip() or None
    con = get_db()
    cur = con.execute(
        "INSERT INTO goals (title, product_id, target_qty, deadline) VALUES (?,?,?,?)",
        (title, product_id, target_qty, deadline))
    con.commit()
    goal = dict(con.execute("SELECT * FROM goals WHERE id=?", (cur.lastrowid,)).fetchone())
    goal["sold"] = 0; goal["progress"] = 0
    con.close()
    return jsonify(goal), 201


@app.route("/api/goals/active")
def get_active_goals():
    con = get_db()
    goals = con.execute("SELECT * FROM goals WHERE deadline IS NOT NULL ORDER BY deadline ASC LIMIT 5").fetchall()
    pg    = con.execute("SELECT * FROM personal_goals WHERE achieved=0 AND deadline IS NOT NULL ORDER BY deadline ASC LIMIT 5").fetchall()
    con.close()
    return jsonify({"goals": [dict(g) for g in goals], "personal": [dict(g) for g in pg]})


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
    deadline = (data.get("deadline") or "").strip() or None
    con = get_db()
    cur = con.execute("INSERT INTO personal_goals (text, deadline) VALUES (?,?)", (text, deadline))
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
        return send_file(buf, as_attachment=True,
                         download_name=f"Layan_Invoice_{tx_id:04d}.pdf",
                         mimetype="application/pdf")
    except Exception as e:
        return jsonify({"error": f"فشل إنشاء الفاتورة: {e}"}), 500


@app.route("/api/invoice/deferred/<int:tx_id>")
def download_deferred_invoice(tx_id):
    try:
        buf = generate_invoice_pdf(tx_id, force_deferred=True)
        if not buf:
            return jsonify({"error": "العملية غير موجودة"}), 404
        return send_file(buf, as_attachment=True,
                         download_name=f"Layan_Deferred_{tx_id:04d}.pdf",
                         mimetype="application/pdf")
    except Exception as e:
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


@app.route("/api/customers/export")
def export_customers():
    import csv, io as _io
    con  = get_db()
    rows = con.execute("""
        SELECT customer_name, customer_phone,
               COUNT(*)                      AS purchases,
               SUM(qty)                      AS total_qty,
               ROUND(SUM(total),3)           AS total_spent,
               MAX(DATE(created_at))         AS last_purchase,
               GROUP_CONCAT(DISTINCT product_name) AS products,
               SUM(CASE WHEN returned=1 THEN 1 ELSE 0 END) AS rejected_count
        FROM transactions
        WHERE type='sale' AND customer_name != ''
        GROUP BY customer_name, customer_phone
        ORDER BY total_spent DESC
    """).fetchall()
    con.close()
    out = _io.StringIO()
    w   = csv.writer(out)
    w.writerow(["الاسم","الهاتف","الطلبات","الكمية","المبلغ (ر.ع)","آخر شراء","المنتجات","شحنات مرفوضة"])
    for r in rows:
        w.writerow([r["customer_name"], r["customer_phone"], r["purchases"],
                    r["total_qty"], r["total_spent"], r["last_purchase"],
                    r["products"], r["rejected_count"]])
    out.seek(0)
    return send_file(
        io.BytesIO(out.getvalue().encode("utf-8-sig")),
        as_attachment=True, download_name="customers_layan.csv", mimetype="text/csv"
    )


@app.route("/api/deferred")
def get_deferred():
    con  = get_db()
    rows = con.execute(
        "SELECT * FROM transactions WHERE type='sale' AND is_deferred=1 AND returned=0 ORDER BY id DESC"
    ).fetchall()
    con.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/deferred/<int:tx_id>/mark-paid", methods=["POST"])
def mark_deferred_paid(tx_id):
    con = get_db()
    row = con.execute("SELECT * FROM transactions WHERE id=? AND is_deferred=1", (tx_id,)).fetchone()
    if not row:
        con.close()
        return jsonify({"error": "العملية غير موجودة"}), 404
    con.execute("UPDATE transactions SET is_deferred=0 WHERE id=?", (tx_id,))
    con.commit()
    con.close()
    return jsonify({"ok": True})


@app.route("/api/customer/lookup")
def customer_lookup():
    """Smart lookup — return the existing customer name for an 8-digit phone, if any."""
    phone = (request.args.get("phone") or "").strip()
    if not phone.isdigit() or len(phone) != 8:
        return jsonify({"found": False})
    con = get_db()
    row = con.execute(
        "SELECT customer_name FROM transactions "
        "WHERE customer_phone=? AND customer_name != '' "
        "ORDER BY id DESC LIMIT 1", (phone,)
    ).fetchone()
    con.close()
    if row and row["customer_name"]:
        return jsonify({"found": True, "customer_name": row["customer_name"]})
    return jsonify({"found": False})


@app.route("/api/product-finances")
def product_finances():
    """Independent per-product cash accounting: revenue, profit, withdrawn, and current balance.

    Aggregates by product_id from the transactions table so that products which were
    deleted from the catalog but still carry real sales history are still shown. This
    keeps the section total reconciled with the dashboard's available-liquidity figure.
    """
    con = get_db()
    prods = con.execute("SELECT id, name, category FROM products").fetchall()
    sales = con.execute(
        "SELECT product_id, MAX(product_name) AS product_name, "
        "       COALESCE(SUM(total),0) AS rev, "
        "       COALESCE(SUM(total - cost_total),0) AS profit, "
        "       COALESCE(SUM(qty),0) AS units "
        "FROM transactions "
        "WHERE type='sale' AND is_deferred=0 AND returned=0 "
        "GROUP BY product_id"
    ).fetchall()
    wds = con.execute(
        "SELECT product_id, COALESCE(SUM(amount),0) AS w_amt, COALESCE(SUM(profit),0) AS w_prof "
        "FROM withdrawals GROUP BY product_id"
    ).fetchall()
    con.close()

    wd_map = {w["product_id"]: w for w in wds}
    data   = {}   # keyed by product_id
    # seed with catalog products (so zero-sales products still appear)
    for p in prods:
        data[p["id"]] = {
            "product_id": p["id"], "product_name": p["name"], "category": p["category"] or "",
            "revenue": 0.0, "profit": 0.0, "units": 0, "exists": True,
        }
    # fold in sales — includes deleted products (product_id not in catalog)
    for s in sales:
        pid = s["product_id"]
        if pid in data:
            data[pid]["revenue"] = s["rev"]
            data[pid]["profit"]  = s["profit"]
            data[pid]["units"]   = s["units"]
        else:
            data[pid] = {
                "product_id": pid, "product_name": s["product_name"] or "—", "category": "",
                "revenue": s["rev"], "profit": s["profit"], "units": s["units"], "exists": False,
            }

    result = []
    total_balance = 0.0
    for pid, d in data.items():
        w      = wd_map.get(pid)
        w_amt  = w["w_amt"]  if w else 0.0
        w_prof = w["w_prof"] if w else 0.0
        balance        = round(d["revenue"] - w_amt,  3)
        profit_balance = round(d["profit"]  - w_prof, 3)
        total_balance += balance
        result.append({
            "product_id":     pid,
            "product_name":   d["product_name"],
            "category":       d["category"],
            "revenue":        round(d["revenue"], 3),
            "profit":         round(d["profit"], 3),
            "units":          d["units"],
            "withdrawn":      round(w_amt, 3),
            "balance":        balance,
            "profit_balance": profit_balance,
            "exists":         d["exists"],
        })
    result.sort(key=lambda r: (-r["revenue"], r["product_name"]))
    return jsonify({"products": result, "total_balance": round(total_balance, 3)})


@app.route("/api/product-finances/<int:product_id>/withdraw", methods=["POST"])
def withdraw_product(product_id):
    """Withdraw (reset to zero) the available cash counter for one product.

    Records the withdrawn amount in the audit-trail `withdrawals` table without
    touching historical transactions, so the bi-weekly PDF report stays correct.
    """
    con = get_db()
    agg = con.execute(
        "SELECT COALESCE(SUM(total),0) rev, COALESCE(SUM(total - cost_total),0) profit, "
        "       MAX(product_name) pname "
        "FROM transactions "
        "WHERE product_id=? AND type='sale' AND is_deferred=0 AND returned=0",
        (product_id,)
    ).fetchone()
    prow  = con.execute("SELECT name FROM products WHERE id=?", (product_id,)).fetchone()
    pname = (prow["name"] if prow else None) or agg["pname"]
    if not pname:
        con.close(); return jsonify({"error": "المنتج غير موجود"}), 404
    wd = con.execute(
        "SELECT COALESCE(SUM(amount),0) w_amt, COALESCE(SUM(profit),0) w_prof "
        "FROM withdrawals WHERE product_id=?", (product_id,)
    ).fetchone()
    balance        = round(agg["rev"]    - wd["w_amt"],  3)
    profit_balance = round(agg["profit"] - wd["w_prof"], 3)
    if balance <= 0:
        con.close(); return jsonify({"error": "لا يوجد رصيد متاح للسحب لهذا المنتج"}), 400
    con.execute(
        "INSERT INTO withdrawals (product_id, product_name, amount, profit) VALUES (?,?,?,?)",
        (product_id, pname, balance, profit_balance)
    )
    con.commit(); con.close()
    return jsonify({"ok": True, "withdrawn": balance, "profit_withdrawn": profit_balance})


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


@app.route("/api/change-password", methods=["POST"])
def change_password():
    if "user_id" not in session:
        return jsonify({"ok": False, "error": "غير مصرح", "redirect": "/login"}), 401
    data        = request.get_json(silent=True) or {}
    current_pw  = data.get("current_password", "")
    new_pw      = data.get("new_password", "")
    if not current_pw or not new_pw:
        return jsonify({"ok": False, "error": "جميع الحقول مطلوبة"}), 400
    if len(new_pw) < 6:
        return jsonify({"ok": False, "error": "كلمة المرور الجديدة يجب أن تكون 6 أحرف على الأقل"}), 400
    con  = get_db()
    user = con.execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()
    if not user or not check_password_hash(user["password"], current_pw):
        con.close()
        return jsonify({"ok": False, "error": "كلمة المرور الحالية غير صحيحة"}), 403
    con.execute("UPDATE users SET password=? WHERE id=?",
                (generate_password_hash(new_pw), session["user_id"]))
    con.commit()
    con.close()
    return jsonify({"ok": True})


@app.route("/api/test-email")
def test_email():
    gmail_user = os.environ.get("GMAIL_USER", "")
    gmail_pass = os.environ.get("GMAIL_PASS", "")
    if not gmail_user or not gmail_pass:
        return jsonify({"ok": False, "error": "GMAIL_USER or GMAIL_PASS not set"}), 500
    try:
        import smtplib
        from email.mime.text import MIMEText
        msg = MIMEText("هذا بريد تجريبي من تطبيق Layan.", "plain", "utf-8")
        msg["From"] = gmail_user
        msg["To"] = REPORT_EMAIL_TO
        msg["Subject"] = "Layan — اختبار البريد"
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
            server.login(gmail_user, gmail_pass)
            server.send_message(msg)
        return jsonify({"ok": True, "msg": f"Sent to {REPORT_EMAIL_TO}"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


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
