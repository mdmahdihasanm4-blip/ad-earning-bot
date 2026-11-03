
import os
import re
import json
import hmac
import hashlib
import sqlite3
import threading
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from urllib.parse import parse_qsl

from flask import Flask, request, jsonify
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    WebAppInfo,
)
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# ---------------- CONFIG ----------------

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
BOT_USERNAME = os.getenv("BOT_USERNAME", "hdjkshdvhsjbot")

WEB_URL = "https://mdmahdihasanm4-blip.github.io/ad-earning-bot/"
WEB_ORIGIN = "https://mdmahdihasanm4-blip.github.io"

PORT = int(os.getenv("PORT", "8080"))
DB_FILE = "adearn.db"

TIMEZONE = ZoneInfo("Asia/Dhaka")

api = Flask(__name__)

# ---------------- DATABASE ----------------

def db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def setup_database():
    conn = db()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            name TEXT DEFAULT '',
            balance REAL DEFAULT 0,
            ads_today INTEGER DEFAULT 0,
            last_ad_date TEXT DEFAULT '',
            referred_by INTEGER DEFAULT NULL,
            joined_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS withdrawals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            amount REAL,
            method TEXT,
            phone TEXT,
            status TEXT DEFAULT 'pending',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    defaults = {
        "minimum_withdraw": "50",
        "daily_ad_limit": "20",
    }

    for key, value in defaults.items():
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            (key, value)
        )

    conn.commit()
    conn.close()


def get_setting(key, default="0"):
    conn = db()
    row = conn.execute(
        "SELECT value FROM settings WHERE key=?",
        (key,)
    ).fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(key, value):
    conn = db()
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
        (key, str(value))
    )
    conn.commit()
    conn.close()


def ensure_user(user_id, name="", referred_by=None):
    conn = db()

    existing = conn.execute(
        "SELECT user_id FROM users WHERE user_id=?",
        (user_id,)
    ).fetchone()

    if not existing:
        if referred_by == user_id:
            referred_by = None

        conn.execute("""
            INSERT INTO users (user_id, name, referred_by)
            VALUES (?, ?, ?)
        """, (user_id, name, referred_by))

    else:
        conn.execute(
            "UPDATE users SET name=? WHERE user_id=?",
            (name, user_id)
        )

    conn.commit()
    conn.close()


def dhaka_today():
    return datetime.now(TIMEZONE).date().isoformat()


# ---------------- TELEGRAM WEBAPP SECURITY ----------------

def verify_init_data(init_data):
    if not BOT_TOKEN or not init_data:
        return None

    try:
        parsed = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = parsed.pop("hash", None)

        if not received_hash:
            return None

        data_check_string = "\n".join(
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
            data_check_string.encode(),
            hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(calculated_hash, received_hash):
            return None

        auth_date = int(parsed.get("auth_date", "0"))

        now = int(datetime.now(timezone.utc).timestamp())

        if now - auth_date > 86400 or auth_date > now + 60:
            return None

        user_data = json.loads(parsed.get("user", "{}"))

        return user_data

    except Exception:
        return None


def get_web_user():
    auth = request.headers.get("Authorization", "")

    if not auth.startswith("tma "):
        return None

    return verify_init_data(auth[4:])


# ---------------- API ----------------

@api.after_request
def add_cors(response):
    origin = request.headers.get("Origin", "")

    if origin == WEB_ORIGIN:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Headers"] = (
            "Authorization, Content-Type"
        )
        response.headers["Access-Control-Allow-Methods"] = (
            "GET, POST, OPTIONS"
        )
        response.headers["Vary"] = "Origin"

    return response


@api.route("/api/<path:path>", methods=["OPTIONS"])
def api_options(path):
    return "", 204


@api.route("/api/me", methods=["GET"])
def api_me():
    user = get_web_user()

    if not user:
        return jsonify({"error": "Unauthorized"}), 401

    user_id = int(user["id"])
    name = user.get("first_name", "")

    ensure_user(user_id, name)

    conn = db()

    row = conn.execute(
        "SELECT * FROM users WHERE user_id=?",
        (user_id,)
    ).fetchone()

    referrals = conn.execute(
        "SELECT COUNT(*) AS total FROM users WHERE referred_by=?",
        (user_id,)
    ).fetchone()["total"]

    conn.close()

    today = dhaka_today()
    ads_today = row["ads_today"]

    if row["last_ad_date"] != today:
        ads_today = 0

    referral_link = f"https://t.me/{BOT_USERNAME}?start=ref_{user_id}"

    return jsonify({
        "user_id": user_id,
        "name": row["name"],
        "balance": row["balance"],
        "ads_today": ads_today,
        "daily_limit": int(get_setting("daily_ad_limit", "20")),
        "minimum_withdraw": float(
            get_setting("minimum_withdraw", "50")
        ),
        "referrals": referrals,
        "referral_link": referral_link
    })


@api.route("/api/withdraw", methods=["POST"])
def api_withdraw():
    user = get_web_user()

    if not user:
        return jsonify({"error": "Unauthorized"}), 401

    try:
        data = request.get_json(force=True)

        amount = float(data.get("amount", 0))
        method = str(data.get("method", "")).lower().strip()
        phone = str(data.get("phone", "")).strip()

        if method not in ("bkash", "nagad"):
            return jsonify({"error": "Invalid payment method"}), 400

        if not re.fullmatch(r"01[3-9]\d{8}", phone):
            return jsonify({"error": "Invalid phone number"}), 400

        minimum = float(get_setting("minimum_withdraw", "50"))

        if amount < minimum:
            return jsonify({
                "error": f"Minimum withdrawal is {minimum} taka"
            }), 400

        if amount <= 0:
            return jsonify({"error": "Invalid amount"}), 400

        user_id = int(user["id"])

        conn = db()
        conn.execute("BEGIN IMMEDIATE")

        row = conn.execute(
            "SELECT balance FROM users WHERE user_id=?",
            (user_id,)
        ).fetchone()

        if not row or row["balance"] < amount:
            conn.rollback()
            conn.close()
            return jsonify({"error": "Insufficient balance"}), 400

        conn.execute(
            "UPDATE users SET balance=balance-? WHERE user_id=?",
            (amount, user_id)
        )

        conn.execute("""
            INSERT INTO withdrawals
            (user_id, amount, method, phone)
            VALUES (?, ?, ?, ?)
        """, (user_id, amount, method, phone))

        conn.commit()
        conn.close()

        return jsonify({
            "success": True,
            "message": "Withdrawal request submitted"
        })

    except Exception:
        return jsonify({"error": "Something went wrong"}), 500


# ---------------- TELEGRAM BOT ----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_user = update.effective_user

    if not telegram_user:
        return

    user_id = telegram_user.id
    name = telegram_user.first_name or ""

    referred_by = None

    if context.args:
        argument = context.args[0]

        if argument.startswith("ref_"):
            try:
                referred_by = int(argument.replace("ref_", ""))
            except ValueError:
                referred_by = None

    ensure_user(user_id, name, referred_by)

    keyboard = [
        [
            InlineKeyboardButton(
                "🚀 Open AdEarn",
                web_app=WebAppInfo(url=WEB_URL)
            )
        ],
        [
            InlineKeyboardButton(
                "💰 My Balance",
                callback_data="balance"
            ),
            InlineKeyboardButton(
                "📊 Statistics",
                callback_data="stats"
            )
        ],
        [
            InlineKeyboardButton(
                "💳 Withdraw",
                callback_data="withdraw"
            )
        ]
    ]

    await update.message.reply_text(
        f"স্বাগতম {name}!\n\nAdEarn-এ আপনাকে স্বাগতম।",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    conn = db()
    row = conn.execute(
        "SELECT balance FROM users WHERE user_id=?",
        (user_id,)
    ).fetchone()
    conn.close()

    amount = row["balance"] if row else 0

    await update.message.reply_text(
        f"আপনার ব্যালেন্স: {amount:.2f} টাকা"
    )


async def withdraw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "উইথড্র করতে Mini App খুলুন এবং Withdraw অপশন ব্যবহার করুন।"
    )


async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    conn = db()

    users = conn.execute(
        "SELECT COUNT(*) AS total FROM users"
    ).fetchone()["total"]

    pending = conn.execute(
        "SELECT COUNT(*) AS total FROM withdrawals WHERE status='pending'"
    ).fetchone()["total"]

    conn.close()

    await update.message.reply_text(
        f"Admin Panel\n\n"
        f"Total users: {users}\n"
        f"Pending withdrawals: {pending}\n\n"
        f"/setmin 50\n"
        f"/setlimit 20\n"
        f"/approve ID\n"
        f"/reject ID"
    )


async def setmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    if not context.args:
        await update.message.reply_text("ব্যবহার: /setmin 50")
        return

    try:
        value = float(context.args[0])

        if value <= 0:
            raise ValueError

        set_setting("minimum_withdraw", value)

        await update.message.reply_text(
            f"Minimum withdrawal set to {value} taka"
        )

    except ValueError:
        await update.message.reply_text("সঠিক পরিমাণ লিখুন।")


async def setlimit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    if not context.args:
        await update.message.reply_text("ব্যবহার: /setlimit 20")
        return

    try:
        value = int(context.args[0])

        if value < 1:
            raise ValueError

        set_setting("daily_ad_limit", value)

        await update.message.reply_text(
            f"Daily limit set to {value}"
        )

    except ValueError:
        await update.message.reply_text("সঠিক সংখ্যা লিখুন।")


async def approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    if not context.args:
        await update.message.reply_text("ব্যবহার: /approve REQUEST_ID")
        return

    try:
        request_id = int(context.args[0])

        conn = db()

        row = conn.execute(
            "SELECT * FROM withdrawals WHERE id=? AND status='pending'",
            (request_id,)
        ).fetchone()

        if not row:
            conn.close()
            await update.message.reply_text("Request পাওয়া যায়নি।")
            return

        conn.execute(
            "UPDATE withdrawals SET status='paid' WHERE id=?",
            (request_id,)
        )

        conn.commit()
        conn.close()

        await update.message.reply_text(
            f"Request #{request_id} paid হিসেবে চিহ্নিত হয়েছে।"
        )

    except ValueError:
        await update.message.reply_text("সঠিক ID দিন।")


async def reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    if not context.args:
        await update.message.reply_text("ব্যবহার: /reject REQUEST_ID")
        return

    try:
        request_id = int(context.args[0])

        conn = db()

        row = conn.execute(
            "SELECT * FROM withdrawals WHERE id=? AND status='pending'",
            (request_id,)
        ).fetchone()

        if not row:
            conn.close()
            await update.message.reply_text("Request পাওয়া যায়নি।")
            return

        conn.execute(
            "UPDATE users SET balance=balance+? WHERE user_id=?",
            (row["amount"], row["user_id"])
        )

        conn.execute(
            "UPDATE withdrawals SET status='rejected' WHERE id=?",
            (request_id,)
        )

        conn.commit()
        conn.close()

        await update.message.reply_text(
            f"Request #{request_id} rejected এবং টাকা ফেরত দেওয়া হয়েছে।"
        )

    except ValueError:
        await update.message.reply_text("সঠিক ID দিন।")


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    conn = db()

    referrals = conn.execute(
        "SELECT COUNT(*) AS total FROM users WHERE referred_by=?",
        (user_id,)
    ).fetchone()["total"]

    row = conn.execute(
        "SELECT balance FROM users WHERE user_id=?",
        (user_id,)
    ).fetchone()

    conn.close()

    amount = row["balance"] if row else 0

    await update.message.reply_text(
        f"আপনার Statistics\n\n"
        f"Referrals: {referrals}\n"
        f"Balance: {amount:.2f} টাকা"
    )


# ---------------- START SERVER ----------------

def run_api():
    api.run(
        host="0.0.0.0",
        port=PORT,
        debug=False,
        use_reloader=False
    )


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable missing")

    setup_database()

    threading.Thread(
        target=run_api,
        daemon=True
    ).start()

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("balance", balance))
    application.add_handler(CommandHandler("withdraw", withdraw))
    application.add_handler(CommandHandler("admin", admin))
    application.add_handler(CommandHandler("setmin", setmin))
    application.add_handler(CommandHandler("setlimit", setlimit))
    application.add_handler(CommandHandler("approve", approve))
    application.add_handler(CommandHandler("reject", reject))
    application.add_handler(CommandHandler("stats", stats))

    print("AdEarn bot is starting...")

    application.run_polling()


if __name__ == "__main__":
    main()
