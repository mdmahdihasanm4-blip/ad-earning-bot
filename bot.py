import os
import re
import json
import hmac
import hashlib
import sqlite3
import threading
from datetime import datetime, timezone
from urllib.parse import parse_qsl

from flask import Flask, request, jsonify
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, ContextTypes

BOT_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
BOT_USERNAME = os.getenv("BOT_USERNAME", "hdjkshdvhsjbot")
WEB_URL = "https://mdmahdihasanm4-blip.github.io/ad-earning-bot/"
WEB_ORIGIN = "https://mdmahdihasanm4-blip.github.io"
PORT = int(os.getenv("PORT", "8080"))

# পুরোনো ডেটাবেস থাকলে সেটিই ব্যবহার করবে
DB_FILE = "bot.db" if os.path.exists("bot.db") else "adearn.db"

app = Flask(__name__)
db_lock = threading.RLock()


# ---------- DATABASE ----------

def db():
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def get_columns(cur, table):
    return {
        row["name"]
        for row in cur.execute(f"PRAGMA table_info({table})").fetchall()
    }


def add_missing_columns(cur, table, definitions):
    existing = get_columns(cur, table)

    for column, definition in definitions.items():
        if column not in existing:
            cur.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
            )


def init_db():
    with db_lock:
        conn = db()
        cur = conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT DEFAULT '',
                first_name TEXT DEFAULT '',
                balance REAL DEFAULT 0,
                referred_by INTEGER DEFAULT NULL,
                joined_at TEXT DEFAULT ''
            )
        """)

        add_missing_columns(cur, "users", {
            "username": "TEXT DEFAULT ''",
            "first_name": "TEXT DEFAULT ''",
            "balance": "REAL DEFAULT 0",
            "referred_by": "INTEGER DEFAULT NULL",
            # SQLite ALTER TABLE-এ CURRENT_TIMESTAMP default দেওয়া যাবে না
            "joined_at": "TEXT DEFAULT ''",
        })

        cur.execute("""
            UPDATE users
            SET joined_at = CURRENT_TIMESTAMP
            WHERE joined_at IS NULL OR joined_at = ''
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS withdrawals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                method TEXT NOT NULL,
                account TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        add_missing_columns(cur, "withdrawals", {
            "user_id": "INTEGER",
            "amount": "REAL DEFAULT 0",
            "method": "TEXT DEFAULT ''",
            "account": "TEXT DEFAULT ''",
            "status": "TEXT DEFAULT 'pending'",
            "created_at": "TEXT DEFAULT ''",
        })

        cur.execute("""
            UPDATE withdrawals
            SET created_at = CURRENT_TIMESTAMP
            WHERE created_at IS NULL OR created_at = ''
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)

        for key, value in {
            "min_withdraw": "50",
            "daily_limit": "20",
        }.items():
            cur.execute(
                "INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)",
                (key, value),
            )

        conn.commit()
        conn.close()


def setting(key, default):
    conn = db()
    row = conn.execute(
        "SELECT value FROM settings WHERE key=?", (key,)
    ).fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(key, value):
    conn = db()
    conn.execute(
        "INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
        (key, str(value)),
    )
    conn.commit()
    conn.close()


def get_user(user_id):
    conn = db()
    row = conn.execute(
        "SELECT * FROM users WHERE user_id=?", (user_id,)
    ).fetchone()
    conn.close()
    return row


def add_user(user_id, username="", first_name="", referred_by=None):
    conn = db()

    conn.execute("""
        INSERT OR IGNORE INTO users
        (user_id, username, first_name, referred_by, joined_at)
        VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
    """, (user_id, username or "", first_name or "", referred_by))

    conn.execute("""
        UPDATE users SET username=?, first_name=?
        WHERE user_id=?
    """, (username or "", first_name or "", user_id))

    conn.commit()
    conn.close()


# ---------- TELEGRAM WEB APP VERIFICATION ----------

def verify_telegram_data(init_data):
    if not BOT_TOKEN or not init_data:
        return None

    try:
        parsed = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = parsed.pop("hash", None)

        if not received_hash:
            return None

        check_string = "\n".join(
            f"{key}={value}" for key, value in sorted(parsed.items())
        )

        secret_key = hmac.new(
            b"WebAppData",
            BOT_TOKEN.encode(),
            hashlib.sha256,
        ).digest()

        calculated_hash = hmac.new(
            secret_key,
            check_string.encode(),
            hashlib.sha256,
        ).hexdigest()

        if not hmac.compare_digest(calculated_hash, received_hash):
            return None

        auth_date = int(parsed.get("auth_date", "0"))
        now = datetime.now(timezone.utc).timestamp()

        if auth_date <= 0 or now - auth_date > 86400 or auth_date - now > 60:
            return None

        user = json.loads(parsed.get("user", "{}"))
        return user if user.get("id") else None

    except Exception:
        return None


# ---------- FLASK API ----------

@app.after_request
def add_cors(response):
    origin = request.headers.get("Origin", "")

    if origin == WEB_ORIGIN:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
        response.headers["Access-Control-Allow-Headers"] = (
            "Content-Type, X-Telegram-Init-Data"
        )
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"

    return response


@app.route("/health")
def health():
    try:
        conn = db()
        conn.execute("SELECT 1").fetchone()
        conn.close()
        return jsonify({"status": "ok", "database": DB_FILE})
    except Exception:
        app.logger.exception("Database health check failed")
        return jsonify({"status": "error"}), 500


@app.route("/api/me", methods=["GET", "OPTIONS"])
def api_me():
    if request.method == "OPTIONS":
        return "", 204

    user = verify_telegram_data(
        request.headers.get("X-Telegram-Init-Data", "")
    )

    if not user:
        return jsonify({"error": "Telegram verification failed"}), 401

    add_user(user["id"], user.get("username"), user.get("first_name"))
    row = get_user(user["id"])

    return jsonify({
        "user_id": user["id"],
        "username": user.get("username", ""),
        "first_name": user.get("first_name", ""),
        "balance": float(row["balance"] or 0),
        "min_withdraw": float(setting("min_withdraw", "50")),
        "daily_limit": int(setting("daily_limit", "20")),
        "referral_link": f"https://t.me/{BOT_USERNAME}?start={user['id']}",
    })


@app.route("/api/withdraw", methods=["POST", "OPTIONS"])
def api_withdraw():
    if request.method == "OPTIONS":
        return "", 204

    user = verify_telegram_data(
        request.headers.get("X-Telegram-Init-Data", "")
    )

    if not user:
        return jsonify({"error": "Telegram verification failed"}), 401

    data = request.get_json(silent=True) or {}

    try:
        amount = round(float(data.get("amount", 0)), 2)
    except (TypeError, ValueError):
        return jsonify({"error": "সঠিক পরিমাণ লিখুন"}), 400

    method = str(data.get("method", "")).strip()
    account = str(data.get("account", "")).strip()

    if amount <= 0:
        return jsonify({"error": "সঠিক পরিমাণ লিখুন"}), 400

    if amount < float(setting("min_withdraw", "50")):
        return jsonify({"error": "ন্যূনতম উত্তোলনের পরিমাণ পূরণ হয়নি"}), 400

    if method not in ("bKash", "Nagad"):
        return jsonify({"error": "bKash অথবা Nagad নির্বাচন করুন"}), 400

    if not re.fullmatch(r"01[3-9]\d{8}", account):
        return jsonify({"error": "সঠিক মোবাইল নম্বর দিন"}), 400

    conn = db()

    try:
        conn.execute("BEGIN IMMEDIATE")

        row = conn.execute(
            "SELECT balance FROM users WHERE user_id=?",
            (user["id"],),
        ).fetchone()

        if not row or float(row["balance"] or 0) < amount:
            conn.rollback()
            return jsonify({"error": "পর্যাপ্ত ব্যালেন্স নেই"}), 400

        conn.execute(
            "UPDATE users SET balance=balance-? WHERE user_id=?",
            (amount, user["id"]),
        )

        conn.execute("""
            INSERT INTO withdrawals
            (user_id, amount, method, account, status)
            VALUES (?, ?, ?, ?, 'pending')
        """, (user["id"], amount, method, account))

        conn.commit()

    except Exception:
        conn.rollback()
        app.logger.exception("Withdrawal request failed")
        return jsonify({"error": "উত্তোলনের অনুরোধ নেওয়া যায়নি"}), 500

    finally:
        conn.close()

    return jsonify({
        "success": True,
        "message": "উত্তোলনের অনুরোধ জমা হয়েছে",
    })


# ---------- BOT COMMANDS ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user

    if not tg_user or not update.message:
        return

    referred_by = None

    if context.args:
        try:
            candidate = int(context.args[0])
            if candidate != tg_user.id:
                referred_by = candidate
        except ValueError:
            pass

    add_user(
        tg_user.id,
        tg_user.username,
        tg_user.first_name,
        referred_by,
    )

    keyboard = [[
        InlineKeyboardButton(
            "অ্যাপ খুলুন",
            web_app=WebAppInfo(url=WEB_URL),
        )
    ]]

    await update.message.reply_text(
        "স্বাগতম! বিজ্ঞাপন দেখে আয় করার অ্যাপটি খুলুন।\n\n"
        "বিজ্ঞাপনের পুরস্কার সার্ভার থেকে যাচাই না হওয়া পর্যন্ত "
        "ব্যালেন্সে যোগ হবে না।",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    row = get_user(update.effective_user.id)
    amount = float(row["balance"] or 0) if row else 0
    await update.message.reply_text(f"আপনার ব্যালেন্স: ৳{amount:.2f}")


async def withdraw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "উত্তোলনের জন্য Mini App খুলে Withdraw অপশন ব্যবহার করুন।"
    )


def is_admin(update):
    return bool(
        update.effective_user
        and ADMIN_ID
        and update.effective_user.id == ADMIN_ID
    )


async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return

    await update.message.reply_text(
        "Admin commands:\n"
        "/stats - মোট ইউজার ও উত্তোলন\n"
        "/setmin 50 - সর্বনিম্ন উত্তোলন\n"
        "/setlimit 20 - দৈনিক বিজ্ঞাপন সীমা সেটিং\n"
        "/approve ID - উত্তোলন Paid চিহ্নিত\n"
        "/reject ID - উত্তোলন বাতিল\n"
        "/credit USER_ID AMOUNT - ব্যালেন্স যোগ"
    )


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return

    conn = db()
    users = conn.execute(
        "SELECT COUNT(*) n FROM users"
    ).fetchone()["n"]

    pending = conn.execute(
        "SELECT COUNT(*) n FROM withdrawals WHERE status='pending'"
    ).fetchone()["n"]

    conn.close()

    await update.message.reply_text(
        f"মোট ইউজার: {users}\nপেন্ডিং উত্তোলন: {pending}"
    )


async def setmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return

    try:
        value = float(context.args[0])
        if value <= 0:
            raise ValueError

        set_setting("min_withdraw", value)
        await update.message.reply_text(
            f"সর্বনিম্ন উত্তোলন ৳{value} করা হয়েছে"
        )

    except (IndexError, ValueError):
        await update.message.reply_text("ব্যবহার: /setmin 50")


async def setlimit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return

    try:
        value = int(context.args[0])
        if value <= 0:
            raise ValueError

        set_setting("daily_limit", value)
        await update.message.reply_text(
            f"দৈনিক সীমা {value} করা হয়েছে"
        )

    except (IndexError, ValueError):
        await update.message.reply_text("ব্যবহার: /setlimit 20")


async def credit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return

    try:
        user_id = int(context.args[0])
        amount = round(float(context.args[1]), 2)

        if amount <= 0:
            raise ValueError

    except (IndexError, ValueError):
        await update.message.reply_text(
            "ব্যবহার: /credit USER_ID AMOUNT"
        )
        return

    conn = db()
    cur = conn.execute(
        "UPDATE users SET balance=balance+? WHERE user_id=?",
        (amount, user_id),
    )
    changed = cur.rowcount
    conn.commit()
    conn.close()

    await update.message.reply_text(
        "ব্যালেন্স আপডেট করা হয়েছে।"
        if changed else "ইউজার পাওয়া যায়নি।"
    )


async def approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return

    try:
        withdrawal_id = int(context.args[0])
    except (IndexError, ValueError):
        await update.message.reply_text("ব্যবহার: /approve ID")
        return

    conn = db()
    cur = conn.execute("""
        UPDATE withdrawals
        SET status='paid'
        WHERE id=? AND status='pending'
    """, (withdrawal_id,))

    changed = cur.rowcount
    conn.commit()
    conn.close()

    if changed:
        await update.message.reply_text(
            "Paid হিসেবে চিহ্নিত হয়েছে। টাকা bKash/Nagad-এ "
            "ম্যানুয়ালি পাঠাতে হবে।"
        )
    else:
        await update.message.reply_text("পেন্ডিং অনুরোধ পাওয়া যায়নি।")


async def reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return

    try:
        withdrawal_id = int(context.args[0])
    except (IndexError, ValueError):
        await update.message.reply_text("ব্যবহার: /reject ID")
        return

    conn = db()

    try:
        conn.execute("BEGIN IMMEDIATE")

        row = conn.execute("""
            SELECT user_id, amount
            FROM withdrawals
            WHERE id=? AND status='pending'
        """, (withdrawal_id,)).fetchone()

        if not row:
            conn.rollback()
            await update.message.reply_text("পেন্ডিং অনুরোধ পাওয়া যায়নি।")
            return

        conn.execute(
            "UPDATE withdrawals SET status='rejected' WHERE id=?",
            (withdrawal_id,),
        )

        conn.execute(
            "UPDATE users SET balance=balance+? WHERE user_id=?",
            (row["amount"], row["user_id"]),
        )

        conn.commit()

    except Exception:
        conn.rollback()
        app.logger.exception("Withdrawal rejection failed")
        await update.message.reply_text("অনুরোধ বাতিল করা যায়নি।")
        return

    finally:
        conn.close()

    await update.message.reply_text(
        "উত্তোলন বাতিল হয়েছে এবং ব্যালেন্স ফেরত দেওয়া হয়েছে।"
    )


# ---------- SERVER START ----------

def run_web():
    app.run(
        host="0.0.0.0",
        port=PORT,
        use_reloader=False,
    )


async def post_init(application):
    # পুরোনো webhook সরাবে, কিন্তু pending update মুছবে না
    await application.bot.delete_webhook(drop_pending_updates=False)


def main():
    init_db()

    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN / TELEGRAM_BOT_TOKEN environment variable is missing"
        )

    threading.Thread(
        target=run_web,
        daemon=True,
    ).start()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("balance", balance))
    application.add_handler(CommandHandler("withdraw", withdraw))
    application.add_handler(CommandHandler("admin", admin))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(CommandHandler("setmin", setmin))
    application.add_handler(CommandHandler("setlimit", setlimit))
    application.add_handler(CommandHandler("credit", credit))
    application.add_handler(CommandHandler("approve", approve))
    application.add_handler(CommandHandler("reject", reject))

    application.run_polling()


if __name__ == "__main__":
    main()ট
