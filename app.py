"""
Gilded — Barber Loyalty System
A lightweight Flask + SQLite app for tracking customer visits and loyalty rewards.

Run:
    pip install -r requirements.txt
    python app.py
Then open http://localhost:5000
"""

import os
import io
import re
import sqlite3
import secrets
from datetime import datetime
from functools import wraps

from flask import (
    Flask, request, jsonify, session,
    render_template, send_file, redirect, url_for, g
)

# --------------------------------------------------------------------------- #
#  CONFIG  —  edit these values to rebrand the shop. Nothing else needs to     #
#  change. Keep ADMIN_PASSWORD secret and change it before going live.         #
# --------------------------------------------------------------------------- #
CONFIG = {
    "shop_name":       os.environ.get("SHOP_NAME", "MALEK NASRI"),
    "shop_tagline":    os.environ.get("SHOP_TAGLINE", "Barber Co."),
    "shop_city":       os.environ.get("SHOP_CITY", "Amman, Jordan"),
    "shop_tagline": os.environ.get("SHOP_TAGLINE", "Powered by RM Studio"),
    "shop_phone":      os.environ.get("SHOP_PHONE", "0799269883"),
    "member_prefix":   os.environ.get("MEMBER_PREFIX", "GLD"),
    "visits_per_reward": int(os.environ.get("VISITS_PER_REWARD", "3")),  # 3 cuts = 1 free
    "admin_password":  os.environ.get("ADMIN_PASSWORD", "malek004"),    # CHANGE THIS
    # Public base URL the QR code points to (set on your VPS, e.g. https://shop.example.com)
    "public_url":      os.environ.get("PUBLIC_URL", ""),
}

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "barbershop.db"))

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

VISITS_PER_REWARD = CONFIG["visits_per_reward"]


# --------------------------------------------------------------------------- #
#  Database helpers                                                            #
# --------------------------------------------------------------------------- #
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        phone       TEXT UNIQUE NOT NULL,
        name        TEXT,
        created_at  TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS visits (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER NOT NULL,
        date_time   TEXT NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS rewards (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id             INTEGER NOT NULL UNIQUE,
        free_haircuts_used  INTEGER NOT NULL DEFAULT 0,
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
    );

    CREATE INDEX IF NOT EXISTS idx_visits_user ON visits(user_id);
    """)
    db.commit()
    db.close()


# --------------------------------------------------------------------------- #
#  Domain logic                                                               #
# --------------------------------------------------------------------------- #
PHONE_RE = re.compile(r"[^\d+]")


def normalize_phone(raw):
    """Strip spaces/dashes, keep digits and a leading +. Returns None if invalid."""
    if not raw:
        return None
    cleaned = PHONE_RE.sub("", raw.strip())
    digits = re.sub(r"\D", "", cleaned)
    if len(digits) < 6:          # too short to be a real number
        return None
    return cleaned


def member_code(user_id):
    return f"{CONFIG['member_prefix']}-{user_id:04d}"


def loyalty_status(db, user_id):
    """Compute everything the loyalty system needs for one user."""
    total = db.execute(
        "SELECT COUNT(*) AS c FROM visits WHERE user_id = ?", (user_id,)
    ).fetchone()["c"]

    row = db.execute(
        "SELECT free_haircuts_used FROM rewards WHERE user_id = ?", (user_id,)
    ).fetchone()
    used = row["free_haircuts_used"] if row else 0

    earned = total // VISITS_PER_REWARD
    available = max(0, earned - used)
    progress = total % VISITS_PER_REWARD          # cuts toward the next reward
    remaining = VISITS_PER_REWARD - progress       # cuts still needed

    return {
        "total_haircuts":        total,
        "free_earned":           earned,
        "free_used":             used,
        "free_available":        available,
        "progress":              progress,
        "goal":                  VISITS_PER_REWARD,
        "remaining_to_reward":   remaining if available == 0 else 0,
        "reward_ready":          available > 0,
    }


def user_payload(db, user):
    """Full customer profile dict for the API."""
    visits = db.execute(
        "SELECT date_time FROM visits WHERE user_id = ? ORDER BY date_time DESC",
        (user["id"],),
    ).fetchall()
    data = {
        "id":          user["id"],
        "member_code": member_code(user["id"]),
        "name":        user["name"] or "",
        "phone":       user["phone"],
        "created_at":  user["created_at"],
        "visits":      [v["date_time"] for v in visits],
    }
    data.update(loyalty_status(db, user["id"]))
    return data


def ensure_reward_row(db, user_id):
    db.execute(
        "INSERT OR IGNORE INTO rewards (user_id, free_haircuts_used) VALUES (?, 0)",
        (user_id,),
    )


# --------------------------------------------------------------------------- #
#  Admin auth (single shared password — intentionally simple)                 #
# --------------------------------------------------------------------------- #
def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("is_admin"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "unauthorized"}), 401
            return redirect(url_for("admin_login"))
        return fn(*args, **kwargs)
    return wrapper


# --------------------------------------------------------------------------- #
#  Page routes                                                                #
# --------------------------------------------------------------------------- #
@app.route("/")
def landing():
    return render_template("landing.html", config=CONFIG)


@app.route("/dashboard")
def dashboard():
    return render_template("dashboard.html", config=CONFIG)


@app.route("/admin")
def admin_home():
    if not session.get("is_admin"):
        return redirect(url_for("admin_login"))
    return render_template("admin.html", config=CONFIG)


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    error = None
    if request.method == "POST":
        if request.form.get("password") == CONFIG["admin_password"]:
            session["is_admin"] = True
            return redirect(url_for("admin_home"))
        error = "Wrong password. Try again."
    return render_template("admin_login.html", config=CONFIG, error=error)


@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    return redirect(url_for("landing"))


@app.route("/qr")
@admin_required
def qr_page():
    return render_template("qr.html", config=CONFIG)


@app.route("/qr.png")
def qr_image():
    """Generate the QR code that points to the public landing page."""
    import qrcode
    base = CONFIG["public_url"] or request.url_root.rstrip("/")
    img = qrcode.make(base)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png")


# --------------------------------------------------------------------------- #
#  Customer API                                                               #
# --------------------------------------------------------------------------- #
@app.route("/api/register", methods=["POST"])
def api_register():
    """Register or log in by phone number. Creates the user on first contact."""
    body = request.get_json(silent=True) or {}
    phone = normalize_phone(body.get("phone"))
    name = (body.get("name") or "").strip()

    if not phone:
        return jsonify({"error": "Please enter a valid phone number."}), 400

    db = get_db()
    user = db.execute("SELECT * FROM users WHERE phone = ?", (phone,)).fetchone()

    if user is None:
        now = datetime.now().isoformat(timespec="seconds")
        cur = db.execute(
            "INSERT INTO users (phone, name, created_at) VALUES (?, ?, ?)",
            (phone, name or None, now),
        )
        user_id = cur.lastrowid
        ensure_reward_row(db, user_id)
        db.commit()
        user = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        created = True
    else:
        # update name if newly provided
        if name and not user["name"]:
            db.execute("UPDATE users SET name = ? WHERE id = ?", (name, user["id"]))
            db.commit()
            user = db.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
        created = False

    payload = user_payload(db, user)
    payload["new_member"] = created
    return jsonify(payload)


@app.route("/api/customer/<int:user_id>")
def api_customer(user_id):
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if user is None:
        return jsonify({"error": "Member not found."}), 404
    return jsonify(user_payload(db, user))


# --------------------------------------------------------------------------- #
#  Admin API                                                                  #
# --------------------------------------------------------------------------- #
@app.route("/api/admin/customers")
@admin_required
def api_admin_customers():
    db = get_db()
    q = (request.args.get("q") or "").strip()
    if q:
        like = f"%{q}%"
        rows = db.execute(
            "SELECT * FROM users WHERE phone LIKE ? OR name LIKE ? ORDER BY id DESC",
            (like, like),
        ).fetchall()
    else:
        rows = db.execute("SELECT * FROM users ORDER BY id DESC").fetchall()

    customers = [user_payload(db, r) for r in rows]

    totals = {
        "members":       len(db.execute("SELECT id FROM users").fetchall()),
        "visits":        db.execute("SELECT COUNT(*) AS c FROM visits").fetchone()["c"],
        "rewards_ready": sum(1 for c in customers if c["reward_ready"]),
    }
    # totals.members should reflect ALL members, recompute if searching
    totals["members"] = db.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
    return jsonify({"customers": customers, "totals": totals})


@app.route("/api/admin/visit", methods=["POST"])
@admin_required
def api_admin_add_visit():
    """Log a haircut for a member (walk-in or in-shop scan)."""
    body = request.get_json(silent=True) or {}
    user_id = body.get("user_id")
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if user is None:
        return jsonify({"error": "Member not found."}), 404

    now = datetime.now().isoformat(timespec="seconds")
    db.execute(
        "INSERT INTO visits (user_id, date_time) VALUES (?, ?)", (user["id"], now)
    )
    ensure_reward_row(db, user["id"])
    db.commit()
    return jsonify(user_payload(db, user))


@app.route("/api/admin/redeem", methods=["POST"])
@admin_required
def api_admin_redeem():
    """Mark one earned free haircut as used."""
    body = request.get_json(silent=True) or {}
    user_id = body.get("user_id")
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if user is None:
        return jsonify({"error": "Member not found."}), 404

    status = loyalty_status(db, user["id"])
    if status["free_available"] <= 0:
        return jsonify({"error": "No free haircut available to redeem."}), 400

    ensure_reward_row(db, user["id"])
    db.execute(
        "UPDATE rewards SET free_haircuts_used = free_haircuts_used + 1 WHERE user_id = ?",
        (user["id"],),
    )
    db.commit()
    return jsonify(user_payload(db, user))


@app.route("/api/admin/customer/<int:user_id>", methods=["DELETE"])
@admin_required
def api_admin_delete(user_id):
    db = get_db()
    db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    db.commit()
    return jsonify({"ok": True})


# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=bool(os.environ.get("DEBUG")))
