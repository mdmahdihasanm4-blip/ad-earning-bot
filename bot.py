import os
import re
import json
import hmac
import secrets
import hashlib
import sqlite3
import threading

from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import parse_qsl

from flask import Flask, request, jsonify
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, ContextTypes


# ==================================================
# CONFIGURATION
# ==================================================

BOT_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
BOT_USERNAME = os.getenv("BOT_USERNAME", "")

WEB_URL = "https://mdmahdihasanm4-blip.github.io/ad-earning-bot/"
WEB_ORIGIN = "https://mdmahdihasanm4-blip.github.io"

PORT = int(os.getenv("PORT", "8080"))

POSTBACK_SECRET = os.getenv("POSTBACK_SECRET", "")
MONETAG_ZONE_ID = "12185380"

TIMEZONE = ZoneInfo("Asia/Dhaka")

DB_FILE = "bot.db" if os.path.exists("bot.db") else "adearn.db"

app = Flask(__name__)
db_lock = threading.RLock()


# ==================================================
# DATABASE
# ==================================================

def db():
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def get_columns(cur, table):
    return {
        row["name"]
        for row in cur.execute(f"PRAGMA table_info({table})")
    }


def add_columns(cur, table, definitions):
    existing = get_columns(cur, table)

    for name, definition in definitions.items():
        if name not in existing:
            cur.execute(
                f"ALTER TABLE {table} ADD COLUMN {name} {definition}"
            )


def init_db():
    with db_lock:
        conn = db()
        cur = conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS users(
                user_id INTEGER PRIMARY KEY,
                username TEXT DEFAULT '',
                first_name TEXT DEFAULT '',
                balance REAL DEFAULT 0,
                referred_by INTEGER DEFAULT NULL,
                joined_at TEXT DEFAULT ''
            )
        """)

        add_columns(cur, "users", {
            "username": "TEXT DEFAULT ''",
            "first_name": "TEXT DEFAULT ''",
            "balance": "REAL DEFAULT 0",
            "referred_by": "INTEGER DEFAULT NULL",
            "joined_at": "TEXT DEFAULT ''",
            "is_blocked": "INTEGER DEFAULT 0"
        })

        cur.execute("""
            UPDATE users
            SET joined_at=CURRENT_TIMESTAMP
            WHERE joined_at IS NULL OR joined_at=''
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS withdrawals(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                method TEXT NOT NULL,
                account TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        add_columns(cur, "withdrawals", {
            "user_id": "INTEGER",
            "amount": "REAL DEFAULT 0",
            "method": "TEXT DEFAULT ''",
            "account": "TEXT DEFAULT ''",
            "status": "TEXT DEFAULT 'pending'",
            "created_at": "TEXT DEFAULT ''",
            "fee_amount": "REAL DEFAULT 0",
            "net_amount": "REAL DEFAULT 0",
            "fee_percent": "REAL DEFAULT 0",
            "refunded": "INTEGER DEFAULT 0"
        })

        cur.execute("""
            UPDATE withdrawals
            SET net_amount=amount
            WHERE net_amount IS NULL OR net_amount=0
        """)

        cur.execute("""
            UPDATE withdrawals
            SET created_at=CURRENT_TIMESTAMP
            WHERE created_at IS NULL OR created_at=''
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS settings(
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)

        defaults = {
            "min_withdraw": "50",
            "daily_limit": "20",
            "reward_amount": "10",
            "fee_percent": "0",
            "payment_methods": '["bKash","Nagad"]',
            "design_config": "{}"
        }

        for key, value in defaults.items():
            cur.execute("""
                INSERT OR IGNORE INTO settings(key,value)
                VALUES(?,?)
            """, (key, value))

        cur.execute("""
            CREATE TABLE IF NOT EXISTS ad_sessions(
                ymid TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                rewarded_at TEXT DEFAULT NULL
            )
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_ad_sessions_user
            ON ad_sessions(user_id,created_at)
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_withdrawals_user
            ON withdrawals(user_id,status)
        """)

        conn.commit()
        conn.close()


def setting(key, default):
    conn = db()

    try:
        row = conn.execute(
            "SELECT value FROM settings WHERE key=?",
            (key,)
        ).fetchone()

        return row["value"] if row else default

    finally:
        conn.close()


def set_setting(key, value):
    conn = db()

    try:
        conn.execute("""
            INSERT OR REPLACE INTO settings(key,value)
            VALUES(?,?)
        """, (key, str(value)))

        conn.commit()

    finally:
        conn.close()


def today_text():
    return datetime.now(TIMEZONE).date().isoformat()


def get_user(uid):
    conn = db()

    try:
        return conn.execute(
            "SELECT * FROM users WHERE user_id=?",
            (uid,)
        ).fetchone()

    finally:
        conn.close()


def add_user(uid, username="", first_name="", referred_by=None):
    conn = db()

    try:
        conn.execute("""
            INSERT OR IGNORE INTO users(
                user_id,username,first_name,referred_by,joined_at
            )
            VALUES(?,?,?,?,?)
        """, (
            uid,
            username or "",
            first_name or "",
            referred_by,
            datetime.now(TIMEZONE).isoformat()
        ))

        conn.execute("""
            UPDATE users
            SET username=?,first_name=?
            WHERE user_id=?
        """, (
            username or "",
            first_name or "",
            uid
        ))

        conn.commit()

    finally:
        conn.close()


def is_blocked(uid):
    row = get_user(uid)
    return bool(row and row["is_blocked"])


def get_payment_methods():
    try:
        methods = json.loads(
            setting("payment_methods", '["bKash","Nagad"]')
        )

        if not isinstance(methods, list):
            return ["bKash", "Nagad"]

        methods = [
            method for method in methods
            if method in ("bKash", "Nagad")
        ]

        return methods or ["bKash"]

    except Exception:
        return ["bKash", "Nagad"]


# ==================================================
# TELEGRAM WEB APP AUTHENTICATION
# ==================================================

def verify_telegram_data(init_data):
    if not BOT_TOKEN or not init_data:
        return None

    try:
        parsed = dict(
            parse_qsl(init_data, keep_blank_values=True)
        )

        received_hash = parsed.pop("hash", None)

        if not received_hash:
            return None

        check_string = "\n".join(
            f"{key}={value}"
            for key, value in sorted(parsed.items())
        )

        secret_key = hmac.new(
            b"WebAppData",
            BOT_TOKEN.encode(),
            hashlib.sha256
        ).digest()

        calculated_hash = hmac.new(
            secret_key,
            check_string.encode(),
            hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(
            calculated_hash,
            received_hash
        ):
            return None

        auth_date = int(parsed.get("auth_date", "0"))
        now = datetime.now(timezone.utc).timestamp()

        if auth_date <= 0:
            return None

        if now - auth_date > 86400:
            return None

        if auth_date - now > 60:
            return None

        user = json.loads(parsed.get("user", "{}"))

        if not user.get("id"):
            return None

        return user

    except Exception:
        return None


def require_user():
    user = verify_telegram_data(
        request.headers.get("X-Telegram-Init-Data", "")
    )

    if not user:
        return None, (
            jsonify(error="Telegram verification failed"),
            401
        )

    add_user(
        user["id"],
        user.get("username"),
        user.get("first_name")
    )

    if is_blocked(user["id"]):
        return None, (
            jsonify(error="আপনার অ্যাকাউন্টটি বন্ধ করা হয়েছে"),
            403
        )

    return user, None


def verified_admin():
    user = verify_telegram_data(
        request.headers.get("X-Telegram-Init-Data", "")
    )

    if not user or not ADMIN_ID:
        return None

    if int(user.get("id", 0)) != ADMIN_ID:
        return None

    return user


# ==================================================
# CORS
# ==================================================

@app.after_request
def cors(response):
    origin = request.headers.get("Origin", "")

    if origin == WEB_ORIGIN:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"

        response.headers["Access-Control-Allow-Headers"] = (
            "Content-Type, X-Telegram-Init-Data"
        )

        response.headers["Access-Control-Allow-Methods"] = (
            "GET, POST, OPTIONS"
        )

    return response


# ==================================================
# HEALTH CHECK
# ==================================================

@app.route("/health")
def health():
    try:
        conn = db()
        conn.execute("SELECT 1")
        conn.close()

        return jsonify(
            status="ok",
            database=DB_FILE
        )

    except Exception:
        app.logger.exception("Health check failed")
        return jsonify(status="error"), 500


# ==================================================
# USER API
# ==================================================

@app.route("/api/me", methods=["GET", "OPTIONS"])
def api_me():
    if request.method == "OPTIONS":
        return "", 204

    user, error = require_user()

    if error:
        return error

    row = get_user(user["id"])
    conn = db()

    try:
        ads = conn.execute("""
            SELECT COUNT(*) n
            FROM ad_sessions
            WHERE user_id=? AND rewarded_at LIKE ?
        """, (
            user["id"],
            today_text() + "%"
        )).fetchone()["n"]

        referrals = conn.execute("""
            SELECT COUNT(*) n
            FROM users
            WHERE referred_by=?
        """, (user["id"],)).fetchone()["n"]

        total_paid = conn.execute("""
            SELECT COALESCE(SUM(net_amount),0) n
            FROM withdrawals
            WHERE user_id=? AND status='paid'
        """, (user["id"],)).fetchone()["n"]

        latest = conn.execute("""
            SELECT status
            FROM withdrawals
            WHERE user_id=?
            ORDER BY id DESC
            LIMIT 1
        """, (user["id"],)).fetchone()

    finally:
        conn.close()

    return jsonify(
        user_id=user["id"],
        username=user.get("username", ""),
        first_name=user.get("first_name", ""),
        balance=float(row["balance"] or 0),
        min_withdraw=float(setting("min_withdraw", "50")),
        daily_limit=int(setting("daily_limit", "20")),
        reward_amount=float(setting("reward_amount", "10")),
        fee_percent=float(setting("fee_percent", "0")),
        payment_methods=get_payment_methods(),
        ads_today=ads,
        referrals=referrals,
        total_withdrawn=float(total_paid or 0),
        latest_withdrawal_status=(
            latest["status"] if latest else None
        ),
        referral_link=(
            f"https://t.me/{BOT_USERNAME}?start={user['id']}"
            if BOT_USERNAME else ""
        )
    )


# ==================================================
# AD SYSTEM
# ==================================================

@app.route("/api/ad/start", methods=["POST", "OPTIONS"])
def api_ad_start():
    if request.method == "OPTIONS":
        return "", 204

    user, error = require_user()

    if error:
        return error

    conn = db()

    try:
        conn.execute("BEGIN IMMEDIATE")

        limit = int(setting("daily_limit", "20"))

        done = conn.execute("""
            SELECT COUNT(*) n
            FROM ad_sessions
            WHERE user_id=? AND rewarded_at LIKE ?
        """, (
            user["id"],
            today_text() + "%"
        )).fetchone()["n"]

        if done >= limit:
            conn.rollback()
            return jsonify(error="আজকের বিজ্ঞাপনের সীমা শেষ"), 400

        active = conn.execute("""
            SELECT COUNT(*) n
            FROM ad_sessions
            WHERE user_id=?
            AND rewarded_at IS NULL
            AND created_at>=?
        """, (
            user["id"],
            (
                datetime.now(TIMEZONE) -
                timedelta(minutes=30)
            ).isoformat()
        )).fetchone()["n"]

        if active >= 3:
            conn.rollback()
            return jsonify(
                error="আগের বিজ্ঞাপন যাচাই হওয়ার জন্য অপেক্ষা করুন"
            ), 429

        token = secrets.token_hex(16)

        conn.execute("""
            INSERT INTO ad_sessions(ymid,user_id,created_at)
            VALUES(?,?,?)
        """, (
            token,
            user["id"],
            datetime.now(TIMEZONE).isoformat()
        ))

        conn.commit()

        return jsonify(ymid=token)

    except Exception:
        conn.rollback()
        app.logger.exception("Ad session creation failed")
        return jsonify(error="বিজ্ঞাপন চালু করা যায়নি"), 500

    finally:
        conn.close()


@app.route("/postback", methods=["GET"])
def monetag_postback():
    if not POSTBACK_SECRET:
        return "Server secret missing", 500

    if not hmac.compare_digest(
        POSTBACK_SECRET,
        request.args.get("secret", "")
    ):
        return "Unauthorized", 401

    if request.args.get("zone_id") != MONETAG_ZONE_ID:
        return "Ignored zone", 200

    if request.args.get("reward_event_type") != "valued":
        return "Non-valued event", 200

    if request.args.get("event_type") not in (
        "impression",
        "click"
    ):
        return "Ignored event", 200

    token = request.args.get("ymid", "").strip()

    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", token):
        return "Invalid ymid", 400

    conn = db()

    try:
        conn.execute("BEGIN IMMEDIATE")

        session = conn.execute("""
            SELECT user_id,created_at,rewarded_at
            FROM ad_sessions
            WHERE ymid=?
        """, (token,)).fetchone()

        if not session:
            conn.rollback()
            return "Unknown session", 200

        if (
            request.args.get("telegram_id")
            and request.args["telegram_id"] != str(session["user_id"])
        ):
            conn.rollback()
            return "User mismatch", 200

        if session["rewarded_at"]:
            conn.rollback()
            return "Already rewarded", 200

        blocked = conn.execute("""
            SELECT is_blocked
            FROM users
            WHERE user_id=?
        """, (session["user_id"],)).fetchone()

        if not blocked or blocked["is_blocked"]:
            conn.rollback()
            return "User unavailable", 200

        try:
            created = datetime.fromisoformat(session["created_at"])

            if created.tzinfo is None:
                created = created.replace(tzinfo=TIMEZONE)

            if (
                datetime.now(TIMEZONE) - created
                > timedelta(hours=24)
            ):
                conn.rollback()
                return "Session expired", 200

        except Exception:
            conn.rollback()
            return "Invalid session time", 200

        count = conn.execute("""
            SELECT COUNT(*) n
            FROM ad_sessions
            WHERE user_id=? AND rewarded_at LIKE ?
        """, (
            session["user_id"],
            today_text() + "%"
        )).fetchone()["n"]

        if count >= int(setting("daily_limit", "20")):
            conn.rollback()
            return "Daily limit reached", 200

        reward = float(setting("reward_amount", "10"))

        if reward <= 0:
            conn.rollback()
            return "Invalid reward setting", 500

        stamp = datetime.now(TIMEZONE).isoformat()

        updated = conn.execute("""
            UPDATE ad_sessions
            SET rewarded_at=?
            WHERE ymid=? AND rewarded_at IS NULL
        """, (stamp, token))

        if updated.rowcount != 1:
            conn.rollback()
            return "Already rewarded", 200

        conn.execute("""
            UPDATE users
            SET balance=balance+?
            WHERE user_id=?
            AND COALESCE(is_blocked,0)=0
        """, (
            reward,
            session["user_id"]
        ))

        conn.commit()
        return "OK", 200

    except Exception:
        conn.rollback()
        app.logger.exception("Monetag postback failed")
        return "Server error", 500

    finally:
        conn.close()


# ==================================================
# WITHDRAWAL SYSTEM
# ==================================================

@app.route("/api/withdraw", methods=["POST", "OPTIONS"])
def api_withdraw():
    if request.method == "OPTIONS":
        return "", 204

    user, error = require_user()

    if error:
        return error

    data = request.get_json(silent=True) or {}

    try:
        amount = round(float(data.get("amount", 0)), 2)
    except (TypeError, ValueError):
        return jsonify(error="সঠিক পরিমাণ লিখুন"), 400

    method = str(data.get("method", "")).strip()
    account = str(data.get("account", "")).strip()

    if amount <= 0:
        return jsonify(error="সঠিক পরিমাণ লিখুন"), 400

    if amount < float(setting("min_withdraw", "50")):
        return jsonify(
            error="ন্যূনতম উত্তোলনের পরিমাণ পূরণ হয়নি"
        ), 400

    if method not in get_payment_methods():
        return jsonify(
            error="এই পেমেন্ট মাধ্যমটি বর্তমানে চালু নেই"
        ), 400

    if not re.fullmatch(r"01[3-9]\d{8}", account):
        return jsonify(error="সঠিক মোবাইল নম্বর দিন"), 400

    fee_percent = float(setting("fee_percent", "0"))

    if not 0 <= fee_percent <= 50:
        fee_percent = 0

    fee_amount = round(amount * fee_percent / 100, 2)
    net_amount = round(amount - fee_amount, 2)

    conn = db()

    try:
        conn.execute("BEGIN IMMEDIATE")

        row = conn.execute("""
            SELECT balance,is_blocked
            FROM users
            WHERE user_id=?
        """, (user["id"],)).fetchone()

        if not row or row["is_blocked"]:
            conn.rollback()
            return jsonify(error="অ্যাকাউন্ট বন্ধ"), 403

        if float(row["balance"] or 0) < amount:
            conn.rollback()
            return jsonify(error="পর্যাপ্ত ব্যালেন্স নেই"), 400

        conn.execute("""
            UPDATE users
            SET balance=balance-?
            WHERE user_id=?
        """, (
            amount,
            user["id"]
        ))

        conn.execute("""
            INSERT INTO withdrawals(
                user_id,amount,method,account,status,
                fee_amount,net_amount,fee_percent,refunded
            )
            VALUES(?,?,?,?,?,?,?,?,0)
        """, (
            user["id"],
            amount,
            method,
            account,
            "pending",
            fee_amount,
            net_amount,
            fee_percent
        ))

        conn.commit()

        return jsonify(
            success=True,
            message="উত্তোলনের অনুরোধ জমা হয়েছে",
            amount=amount,
            fee_amount=fee_amount,
            net_amount=net_amount
        )

    except Exception:
        conn.rollback()
        app.logger.exception("Withdrawal request failed")
        return jsonify(error="উত্তোলনের অনুরোধ নেওয়া যায়নি"), 500

    finally:
        conn.close()


@app.route("/api/withdrawals", methods=["GET", "OPTIONS"])
def api_withdrawals():
    if request.method == "OPTIONS":
        return "", 204

    user, error = require_user()

    if error:
        return error

    try:
        limit = max(
            1,
            min(int(request.args.get("limit", 20)), 50)
        )
    except ValueError:
        return jsonify(error="ভুল limit"), 400

    conn = db()

    try:
        rows = conn.execute("""
            SELECT
                id,amount,method,account,status,created_at,
                fee_amount,net_amount,fee_percent
            FROM withdrawals
            WHERE user_id=?
            ORDER BY id DESC
            LIMIT ?
        """, (
            user["id"],
            limit
        )).fetchall()

        return jsonify(items=[dict(row) for row in rows])

    finally:
        conn.close()


# ==================================================
# DESIGN SYSTEM
# ==================================================

SECTIONS = [
    "home",
    "ads",
    "balance",
    "referral",
    "withdraw",
    "withdrawHistory",
    "profile",
    "statistics"
]


DEFAULT_DESIGN = {
    "theme": "dark",
    "app_name": "AdEarn",
    "header_text": "বিজ্ঞাপন দেখে আয় করুন",
    "footer_text": "ধন্যবাদ আমাদের সাথে থাকার জন্য",
    "logo_url": "",
    "banner_url": "",

    "colors": {
        "background": "#101827",
        "card": "#1b2638",
        "button": "#8b5cf6",
        "text": "#ffffff",
        "border": "#334155",
        "muted": "#9ca3af",
        "accent": "#8b5cf6"
    },

    "labels": {
        "home": "হোম",
        "ads": "বিজ্ঞাপন",
        "balance": "ব্যালেন্স",
        "referral": "রেফার",
        "withdraw": "উত্তোলন",
        "withdrawHistory": "উত্তোলনের ইতিহাস",
        "profile": "প্রোফাইল",
        "statistics": "পরিসংখ্যান",
        "appName": "AdEarn",
        "history": "উত্তোলনের ইতিহাস"
    },

    "order": SECTIONS[:],

    "visibility": {
        section: True for section in SECTIONS
    },

    "buttons": []
}


def get_design():
    try:
        saved = json.loads(
            setting("design_config", "{}")
        )
    except Exception:
        saved = {}

    result = json.loads(
        json.dumps(DEFAULT_DESIGN)
    )

    if not isinstance(saved, dict):
        return result

    for key, value in saved.items():

        if key in ("colors", "labels") and isinstance(value, dict):
            result[key].update(value)

        elif key == "visibility" and isinstance(value, dict):

            if "history" in value and "withdrawHistory" not in value:
                value["withdrawHistory"] = value["history"]

            result[key].update({
                k: v
                for k, v in value.items()
                if k in SECTIONS and isinstance(v, bool)
            })

        elif key == "order" and isinstance(value, list):

            mapped = [
                "withdrawHistory" if x == "history" else x
                for x in value
            ]

            result[key] = [
                x for x in mapped if x in SECTIONS
            ]

            result[key] += [
                x for x in SECTIONS
                if x not in result[key]
            ]

        elif key == "buttons" and isinstance(value, list):
            result["buttons"] = value[:30]

        elif key in (
            "theme",
            "app_name",
            "header_text",
            "footer_text",
            "logo_url",
            "banner_url"
        ):
            result[key] = value

    return result


@app.route("/api/design", methods=["GET", "OPTIONS"])
def public_design():
    if request.method == "OPTIONS":
        return "", 204

    return jsonify(
        design=get_design(),
        fee_percent=float(setting("fee_percent", "0")),
        payment_methods=get_payment_methods()
    )


@app.route(
    "/api/admin/design",
    methods=["GET", "POST", "OPTIONS"]
)
def admin_design():

    if request.method == "OPTIONS":
        return "", 204

    if not verified_admin():
        return jsonify(error="অ্যাডমিন অনুমতি নেই"), 403

    if request.method == "GET":
        return jsonify(design=get_design())

    data = request.get_json(silent=True) or {}
    current = get_design()

    theme = str(
        data.get("theme", current["theme"])
    ).lower()

    if theme not in (
        "dark", "light", "green", "blue", "red", "custom"
    ):
        return jsonify(error="ভুল থিম"), 400

    colors = dict(current["colors"])
    incoming_colors = data.get("colors", {})

    if not isinstance(incoming_colors, dict):
        return jsonify(error="রঙের সেটিংস সঠিক নয়"), 400

    for key, value in incoming_colors.items():
        if key not in colors:
            continue

        value = str(value).strip()

        if not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
            return jsonify(error="রঙের HEX কোড সঠিক নয়"), 400

        colors[key] = value

    labels = dict(current["labels"])
    incoming_labels = data.get("labels", {})

    if not isinstance(incoming_labels, dict):
        return jsonify(error="লেবেল সেটিংস সঠিক নয়"), 400

    for key, value in incoming_labels.items():
        if key == "history":
            key = "withdrawHistory"

        if key not in labels:
            continue

        value = str(value).strip()

        if not value or len(value) > 60:
            return jsonify(error="লেখার দৈর্ঘ্য সঠিক নয়"), 400

        labels[key] = value

    order = data.get("order", current["order"])

    if not isinstance(order, list):
        return jsonify(error="সেকশনের অবস্থান সঠিক নয়"), 400

    order = [
        "withdrawHistory" if x == "history" else x
        for x in order
    ]

    if len(order) != len(SECTIONS) or set(order) != set(SECTIONS):
        return jsonify(error="সেকশনের অবস্থান সঠিক নয়"), 400

    visibility = data.get(
        "visibility",
        current["visibility"]
    )

    if not isinstance(visibility, dict):
        return jsonify(error="Visibility সেটিংস সঠিক নয়"), 400

    vis = dict(current["visibility"])

    for key, value in visibility.items():
        if key == "history":
            key = "withdrawHistory"

        if key not in SECTIONS or not isinstance(value, bool):
            return jsonify(error="Visibility সেটিংস সঠিক নয়"), 400

        vis[key] = value

    buttons = data.get(
        "buttons",
        current["buttons"]
    )

    if not isinstance(buttons, list) or len(buttons) > 30:
        return jsonify(error="সর্বোচ্চ ৩০টি কাস্টম বাটন রাখা যাবে"), 400

    clean_buttons = []

    for button in buttons:

        if not isinstance(button, dict):
            return jsonify(error="কাস্টম বাটনের তথ্য সঠিক নয়"), 400

        name = str(button.get("name", "")).strip()
        icon = str(button.get("icon", "🔗")).strip()[:8]
        url = str(button.get("url", "")).strip()
        action = str(button.get("action", "link")).strip()
        visible = button.get("visible", True)

        if not name or len(name) > 40:
            return jsonify(error="বাটনের নাম সঠিক নয়"), 400

        if action not in (
            "link", "ads", "wallet", "history", "referral"
        ):
            return jsonify(error="বাটনের কাজ সঠিক নয়"), 400

        if action == "link" and not re.fullmatch(
            r"https?://\S+", url, re.I
        ):
            return jsonify(error="কাস্টম বাটনের URL সঠিক নয়"), 400

        if not isinstance(visible, bool):
            visible = True

        clean_buttons.append({
            "name": name,
            "icon": icon or "🔗",
            "url": url,
            "action": action,
            "visible": visible
        })

    logo_url = str(
        data.get("logo_url", current["logo_url"])
    ).strip()

    banner_url = str(
        data.get("banner_url", current["banner_url"])
    ).strip()

    for image_url in (logo_url, banner_url):
        if image_url and not re.fullmatch(
            r"https?://\S+", image_url, re.I
        ):
            return jsonify(error="লোগো বা ব্যানারের URL সঠিক নয়"), 400

    config = {
        "theme": theme,

        "app_name": str(
            data.get("app_name", current["app_name"])
        )[:60],

        "header_text": str(
            data.get("header_text", current["header_text"])
        )[:120],

        "footer_text": str(
            data.get("footer_text", current["footer_text"])
        )[:160],

        "logo_url": logo_url,
        "banner_url": banner_url,
        "colors": colors,
        "labels": labels,
        "order": order,
        "visibility": vis,
        "buttons": clean_buttons
    }

    set_setting(
        "design_config",
        json.dumps(config, ensure_ascii=False)
    )

    return jsonify(
        success=True,
        message="ডিজাইন সংরক্ষণ হয়েছে",
        design=config
    )


# ==================================================
# ADMIN SUMMARY
# ==================================================

@app.route(
    "/api/admin/summary",
    methods=["GET", "OPTIONS"]
)
def admin_summary():

    if request.method == "OPTIONS":
        return "", 204

    if not verified_admin():
        return jsonify(error="অ্যাডমিন অনুমতি নেই"), 403

    conn = db()

    try:
        users = conn.execute("""
            SELECT COUNT(*) n FROM users
        """).fetchone()["n"]

        total_balance = conn.execute("""
            SELECT COALESCE(SUM(balance),0) n
            FROM users
        """).fetchone()["n"]

        pending = conn.execute("""
            SELECT COUNT(*) n,COALESCE(SUM(amount),0) amount
            FROM withdrawals
            WHERE status='pending'
        """).fetchone()

        ads = conn.execute("""
            SELECT COUNT(*) n
            FROM ad_sessions
            WHERE rewarded_at LIKE ?
        """, (today_text() + "%",)).fetchone()["n"]

        return jsonify(
            total_users=users,
            total_balance=float(total_balance or 0),
            pending_count=pending["n"],
            pending_amount=float(pending["amount"] or 0),
            ads_today=ads,

            settings={
                "reward_amount": float(setting("reward_amount", "10")),
                "daily_limit": int(setting("daily_limit", "20")),
                "min_withdraw": float(setting("min_withdraw", "50")),
                "fee_percent": float(setting("fee_percent", "0")),
                "payment_methods": get_payment_methods()
            }
        )

    finally:
        conn.close()


# ==================================================
# ADMIN USERS
# ==================================================

@app.route(
    "/api/admin/users",
    methods=["GET", "OPTIONS"]
)
def admin_users():

    if request.method == "OPTIONS":
        return "", 204

    if not verified_admin():
        return jsonify(error="অ্যাডমিন অনুমতি নেই"), 403

    query = str(
        request.args.get("q", "")
    ).strip()[:100]

    try:
        limit = max(
            1,
            min(int(request.args.get("limit", 100)), 200)
        )

        offset = max(
            0,
            int(request.args.get("offset", 0))
        )

    except ValueError:
        return jsonify(error="ভুল limit বা offset"), 400

    conn = db()

    try:
        where = ""
        params = []

        if query.isdigit():
            where = "WHERE CAST(user_id AS TEXT) LIKE ?"
            params = [f"%{query}%"]

        elif query:
            where = """
                WHERE username LIKE ?
                OR first_name LIKE ?
            """

            params = [
                f"%{query}%",
                f"%{query}%"
            ]

        total = conn.execute(
            "SELECT COUNT(*) n FROM users " + where,
            params
        ).fetchone()["n"]

        rows = conn.execute("""
            SELECT
                user_id,
                COALESCE(username,'') username,
                COALESCE(first_name,'') first_name,
                COALESCE(balance,0) balance,
                COALESCE(joined_at,'') joined_at,
                COALESCE(is_blocked,0) is_blocked
            FROM users
        """ + where + """
            ORDER BY joined_at DESC
            LIMIT ? OFFSET ?
        """, params + [limit, offset]).fetchall()

        return jsonify(
            total=total,
            items=[dict(row) for row in rows]
        )

    finally:
        conn.close()


# ==================================================
# ADMIN BLOCK / UNBLOCK
# ==================================================

@app.route(
    "/api/admin/user/block",
    methods=["POST", "OPTIONS"]
)
@app.route(
    "/api/admin/users/block",
    methods=["POST", "OPTIONS"]
)
def admin_user_block():

    if request.method == "OPTIONS":
        return "", 204

    if not verified_admin():
        return jsonify(error="অ্যাডমিন অনুমতি নেই"), 403

    data = request.get_json(silent=True) or {}

    try:
        uid = int(data.get("user_id", data.get("id", 0)))
    except (TypeError, ValueError):
        return jsonify(error="সঠিক ইউজার ID দিন"), 400

    action = str(data.get("action", "")).lower()
    blocked = data.get("is_blocked")

    if blocked is None:
        if action not in ("block", "unblock"):
            return jsonify(error="অ্যাকশন সঠিক নয়"), 400

        blocked = action == "block"

    if not isinstance(blocked, bool):
        return jsonify(error="Block status সঠিক নয়"), 400

    if uid <= 0 or uid == ADMIN_ID:
        return jsonify(error="এই ইউজারকে ব্লক করা যাবে না"), 400

    conn = db()

    try:
        cur = conn.execute("""
            UPDATE users
            SET is_blocked=?
            WHERE user_id=?
        """, (
            1 if blocked else 0,
            uid
        ))

        conn.commit()

        if not cur.rowcount:
            return jsonify(error="ইউজার পাওয়া যায়নি"), 404

        return jsonify(
            success=True,
            is_blocked=blocked,
            message=(
                "ইউজার ব্লক করা হয়েছে"
                if blocked else "ইউজার আনব্লক করা হয়েছে"
            )
        )

    finally:
        conn.close()


# ==================================================
# ADMIN WITHDRAWALS
# ==================================================

@app.route(
    "/api/admin/withdrawals",
    methods=["GET", "OPTIONS"]
)
def admin_withdrawals():

    if request.method == "OPTIONS":
        return "", 204

    if not verified_admin():
        return jsonify(error="অ্যাডমিন অনুমতি নেই"), 403

    conn = db()

    try:
        rows = conn.execute("""
            SELECT
                w.id,
                w.user_id,
                w.amount,
                w.method,
                w.account,
                w.status,
                w.created_at,
                w.fee_amount,
                w.net_amount,
                w.fee_percent,
                COALESCE(u.first_name,'') first_name,
                COALESCE(u.username,'') username
            FROM withdrawals w
            LEFT JOIN users u
            ON u.user_id=w.user_id
            WHERE w.status='pending'
            ORDER BY w.id DESC
            LIMIT 100
        """).fetchall()

        return jsonify(
            items=[dict(row) for row in rows]
        )

    finally:
        conn.close()


def withdrawal_action(withdrawal_id, action):
    conn = db()

    try:
        conn.execute("BEGIN IMMEDIATE")

        row = conn.execute("""
            SELECT user_id,amount,status,refunded
            FROM withdrawals
            WHERE id=?
        """, (withdrawal_id,)).fetchone()

        if not row:
            conn.rollback()
            return False, "উত্তোলনের অনুরোধ পাওয়া যায়নি"

        if row["status"] != "pending":
            conn.rollback()
            return False, "এই অনুরোধটি আগে থেকেই প্রসেস করা হয়েছে"

        if action == "approve":

            conn.execute("""
                UPDATE withdrawals
                SET status='paid'
                WHERE id=? AND status='pending'
            """, (withdrawal_id,))

            message = "উত্তোলন Paid হিসেবে চিহ্নিত হয়েছে"

        elif action == "reject":

            if not row["refunded"]:
                conn.execute("""
                    UPDATE users
                    SET balance=balance+?
                    WHERE user_id=?
                """, (
                    row["amount"],
                    row["user_id"]
                ))

            conn.execute("""
                UPDATE withdrawals
                SET status='rejected',refunded=1
                WHERE id=? AND status='pending'
            """, (withdrawal_id,))

            message = "উত্তোলন বাতিল হয়েছে এবং টাকা ফেরত দেওয়া হয়েছে"

        else:
            conn.rollback()
            return False, "ভুল অ্যাকশন"

        conn.commit()
        return True, message

    except Exception:
        conn.rollback()
        app.logger.exception("Withdrawal action failed")
        return False, "সার্ভার সমস্যা"

    finally:
        conn.close()


@app.route(
    "/api/admin/withdrawal/action",
    methods=["POST", "OPTIONS"]
)
@app.route(
    "/api/admin/withdrawals/action",
    methods=["POST", "OPTIONS"]
)
def admin_withdrawal_action():

    if request.method == "OPTIONS":
        return "", 204

    if not verified_admin():
        return jsonify(error="অ্যাডমিন অনুমতি নেই"), 403

    data = request.get_json(silent=True) or {}

    try:
        wid = int(
            data.get("id", data.get("withdrawal_id", 0))
        )
    except (TypeError, ValueError):
        return jsonify(error="ভুল withdrawal ID"), 400

    action = str(data.get("action", "")).lower()

    if action in ("paid", "approve", "approved"):
        action = "approve"

    elif action in ("reject", "rejected"):
        action = "reject"

    success, message = withdrawal_action(wid, action)

    if not success:
        return jsonify(error=message), 400

    return jsonify(
        success=True,
        message=message
    )


# ==================================================
# ADMIN SETTINGS
# ==================================================

@app.route(
    "/api/admin/settings",
    methods=["GET", "POST", "OPTIONS"]
)
def admin_settings():

    if request.method == "OPTIONS":
        return "", 204

    if not verified_admin():
        return jsonify(error="অ্যাডমিন অনুমতি নেই"), 403

    if request.method == "GET":
        return jsonify(
            reward_amount=float(setting("reward_amount", "10")),
            daily_limit=int(setting("daily_limit", "20")),
            min_withdraw=float(setting("min_withdraw", "50")),
            fee_percent=float(setting("fee_percent", "0")),
            payment_methods=get_payment_methods()
        )

    data = request.get_json(silent=True) or {}

    try:
        reward = float(data.get(
            "reward_amount",
            setting("reward_amount", "10")
        ))

        limit = int(data.get(
            "daily_limit",
            setting("daily_limit", "20")
        ))

        minimum = float(data.get(
            "min_withdraw",
            setting("min_withdraw", "50")
        ))

        fee = float(data.get(
            "fee_percent",
            setting("fee_percent", "0")
        ))

    except (TypeError, ValueError):
        return jsonify(error="সেটিংসের মান সঠিক নয়"), 400

    if reward <= 0 or limit < 1 or minimum <= 0:
        return jsonify(error="সেটিংসের মান সঠিক নয়"), 400

    if fee < 0 or fee > 50:
        return jsonify(
            error="সার্ভিস ফি ০ থেকে ৫০ শতাংশের মধ্যে রাখুন"
        ), 400

    methods = data.get(
        "payment_methods",
        get_payment_methods()
    )

    if not isinstance(methods, list):
        return jsonify(error="পেমেন্ট মাধ্যম সঠিক নয়"), 400

    methods = list(dict.fromkeys(
        method for method in methods
        if method in ("bKash", "Nagad")
    ))

    if not methods:
        return jsonify(
            error="কমপক্ষে একটি পেমেন্ট মাধ্যম চালু রাখুন"
        ), 400

    set_setting("reward_amount", reward)
    set_setting("daily_limit", limit)
    set_setting("min_withdraw", minimum)
    set_setting("fee_percent", fee)

    set_setting(
        "payment_methods",
        json.dumps(methods)
    )

    return jsonify(
        success=True,
        message="সেটিংস সংরক্ষণ হয়েছে",
        reward_amount=reward,
        daily_limit=limit,
        min_withdraw=minimum,
        fee_percent=fee,
        payment_methods=methods
    )


# ==================================================
# TELEGRAM BOT COMMANDS
# ==================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user

    if not user:
        return

    uid = user.id
    referred_by = None

    if context.args:
        try:
            ref = int(context.args[0])

            if ref != uid:
                referred_by = ref

        except ValueError:
            pass

    add_user(
        uid,
        user.username,
        user.first_name,
        referred_by
    )

    if is_blocked(uid):
        await update.message.reply_text(
            "আপনার অ্যাকাউন্টটি সাময়িকভাবে বন্ধ করা হয়েছে।"
        )
        return

    keyboard = [[
        InlineKeyboardButton(
            "অ্যাপ খুলুন",
            web_app=WebAppInfo(url=WEB_URL)
        )
    ]]

    await update.message.reply_text(
        "স্বাগতম AdEarn-এ!\n\n"
        "বিজ্ঞাপন দেখে আয় করুন এবং আপনার ব্যালেন্স জমা করুন।\n\n"
        "অ্যাপ ব্যবহার করতে নিচের বাটনে চাপ দিন।",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def balance_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    if not user:
        return

    row = get_user(user.id)

    if not row:
        await update.message.reply_text("প্রথমে /start দিন।")
        return

    await update.message.reply_text(
        f"আপনার ব্যালেন্স: ৳{float(row['balance'] or 0):.2f}"
    )


async def admin_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    if not user or user.id != ADMIN_ID:
        await update.message.reply_text(
            "এই কমান্ড শুধু অ্যাডমিনের জন্য।"
        )
        return

    conn = db()

    try:
        users = conn.execute(
            "SELECT COUNT(*) n FROM users"
        ).fetchone()["n"]

        total_balance = conn.execute(
            "SELECT COALESCE(SUM(balance),0) n FROM users"
        ).fetchone()["n"]

        pending = conn.execute("""
            SELECT COUNT(*) n
            FROM withdrawals
            WHERE status='pending'
        """).fetchone()["n"]

    finally:
        conn.close()

    await update.message.reply_text(
        "AdEarn Admin Summary\n\n"
        f"মোট ইউজার: {users}\n"
        f"মোট ইউজার ব্যালেন্স: ৳{total_balance:.2f}\n"
        f"পেন্ডিং উত্তোলন: {pending}\n\n"
        "সেটিংস পরিবর্তন করতে Admin Panel ব্যবহার করুন।"
    )


# ==================================================
# ADMIN PANEL COMMAND
# ==================================================

async def panel_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    user = update.effective_user

    if not user or user.id != ADMIN_ID:
        await update.message.reply_text(
            "এই কমান্ড শুধু অ্যাডমিনের জন্য।"
        )
        return

    panel_url = WEB_URL.rstrip("/") + "/admin.html"

    keyboard = [[
        InlineKeyboardButton(
            "🛠 Admin Panel খুলুন",
            web_app=WebAppInfo(url=panel_url)
        )
    ]]

    await update.message.reply_text(
        "🛠 AdEarn Admin Panel\n\n"
        "নিচের বাটনে চাপ দিয়ে অ্যাডমিন প্যানেল খুলুন।",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# ==================================================
# START SERVER
# ==================================================

def run_flask():
    app.run(
        host="0.0.0.0",
        port=PORT,
        debug=False,
        use_reloader=False
    )


def main():

    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN environment variable সেট করা হয়নি"
        )

    if not ADMIN_ID:
        raise RuntimeError(
            "ADMIN_ID environment variable সেট করা হয়নি"
        )

    init_db()

    threading.Thread(
        target=run_flask,
        daemon=True
    ).start()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler("start", start)
    )

    application.add_handler(
        CommandHandler("balance", balance_command)
    )

    application.add_handler(
        CommandHandler("admin", admin_command)
    )

    # নতুন /panel কমান্ড
    application.add_handler(
        CommandHandler("panel", panel_command)
    )

    print("AdEarn Bot is starting...")
    print("Database:", DB_FILE)
    print("Flask Port:", PORT)

    application.run_polling()


if __name__ == "__main__":
    main()
