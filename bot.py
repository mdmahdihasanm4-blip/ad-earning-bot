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
# DATABASE SETUP & MIGRATION
# ==================================================

def db():
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def get_columns(cur, table):
    return {row["name"] for row in cur.execute(f"PRAGMA table_info({table})")}


def add_columns(cur, table, definitions):
    existing = get_columns(cur, table)
    for name, definition in definitions.items():
        if name not in existing:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def init_db():
    with db_lock:
        conn = db()
        cur = conn.cursor()

        # Users Table
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

        # Withdrawals Table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS withdrawals(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                method TEXT NOT NULL,
                account TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at TEXT DEFAULT ''
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

        # Settings Table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS settings(
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)

        # Default Settings (চাহিদা অনুযায়ী ১, ৪, ৫, ৮, ৯, ১০, ১১, ১২ নং ফিচারের কনফিগারেশন)
        default_buttons = [
            {"id": "ads", "name_bn": "▶ বিজ্ঞাপন দেখুন", "name_en": "▶ Watch Ads", "icon": "📺", "link": "", "position": "home", "order": 1, "active": 1},
            {"id": "withdraw", "name_bn": "↗ টাকা তুলুন", "name_en": "↗ Withdraw", "icon": "💸", "link": "", "position": "home", "order": 2, "active": 1},
            {"id": "refer", "name_bn": "🎁 রেফারেল লিংক", "name_en": "🎁 Referral Link", "icon": "🔗", "link": "", "position": "home", "order": 3, "active": 1},
            {"id": "history", "name_bn": "📜 উত্তোলনের ইতিহাস", "name_en": "📜 History", "icon": "📝", "link": "", "position": "home", "order": 4, "active": 1}
        ]

        default_payment_methods = [
            {"id": "bKash", "name": "bKash", "logo": "https://postimg.cc", "status": 1},
            {"id": "Nagad", "name": "Nagad", "logo": "https://postimg.cc", "status": 1}
        ]

        defaults = {
            "min_withdraw": "50",
            "daily_limit": "20",
            "reward_amount": "10",
            "fee_percent": "0",
            "payment_methods": json.dumps(default_payment_methods),
            "buttons_config": json.dumps(default_buttons),
            "app_name": "Earning Bot",
            "app_logo": "",
            "app_banner": "",
            "theme_bg_image": "",
            "theme_color_primary": "#8B5CF6",
            "theme_color_bg": "#0B061F",
            "admin_secret_token": secrets.token_hex(16)
        }

        for key, value in defaults.items():
            cur.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (key, value))

        # Ad Sessions Table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ad_sessions(
                ymid TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                rewarded_at TEXT DEFAULT NULL
            )
        """)

        cur.execute("CREATE INDEX IF NOT EXISTS idx_ad_sessions_user ON ad_sessions(user_id,created_at)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_withdrawals_user ON withdrawals(user_id,status)")

        conn.commit()
        conn.close()


def setting(key, default=""):
    conn = db()
    try:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default
    finally:
        conn.close()


def set_setting(key, value):
    conn = db()
    try:
        conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, str(value)))
        conn.commit()
    finally:
        conn.close()


def today_text():
    return datetime.now(TIMEZONE).date().isoformat()


def get_user(uid):
    conn = db()
    try:
        return conn.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
    finally:
        conn.close()


def add_user(uid, username="", first_name="", referred_by=None):
    conn = db()
    try:
        conn.execute("""
            INSERT OR IGNORE INTO users(user_id,username,first_name,referred_by,joined_at)
            VALUES(?,?,?,?,?)
        """, (uid, username or "", first_name or "", referred_by, datetime.now(TIMEZONE).isoformat()))
        
        conn.execute("UPDATE users SET username=?, first_name=? WHERE user_id=?", (username or "", first_name or "", uid))
        conn.commit()
    finally:
        conn.close()


def is_blocked(uid):
    row = get_user(uid)
    return bool(row and row["is_blocked"])


# ==================================================
# TELEGRAM AUTH VERIFICATION
# ==================================================

def verify_telegram_data(init_data):
    if not BOT_TOKEN or not init_data:
        return None
    try:
        parsed = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = parsed.pop("hash", None)
        if not received_hash:
            return None
        check_string = "\n".join(f"{key}={value}" for key, value in sorted(parsed.items()))
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
        if hmac.compare_digest(calculated_hash, received_hash):
            return json.loads(parsed.get("user", "{}"))
        return None
    except Exception:
        return None


# ==================================================
# FLASK FLUID API CHANNELS
# ==================================================

@app.route("/api/app-config", methods=["GET"])
def get_app_config():
    return jsonify({
        "app_name": setting("app_name", "Earning Bot"),
        "app_logo": setting("app_logo", ""),
        "app_banner": setting("app_banner", ""),
        "theme_bg_image": setting("theme_bg_image", ""),
        "theme_color_primary": setting("theme_color_primary", "#8B5CF6"),
        "theme_color_bg": setting("theme_color_bg", "#0B061F"),
        "min_withdraw": float(setting("min_withdraw", "50")),
        "daily_limit": int(setting("daily_limit", "20")),
        "reward_amount": float(setting("reward_amount", "10")),
        "fee_percent": float(setting("fee_percent", "0")),
        "payment_methods": json.loads(setting("payment_methods", "[]")),
        "buttons": json.loads(setting("buttons_config", "[]"))
    })


@app.route("/api/user-data", methods=["POST"])
def user_data():
    data = request.json or {}
    init_data = data.get("initData", "")
    tg_user = verify_telegram_data(init_data)
    
    if not tg_user:
        return jsonify({"error": "Unauthorized"}), 401

    uid = tg_user.get("id")
    if is_blocked(uid):
        return jsonify({"error": "Banned", "message": "You are blocked by Admin."}), 403

    # Handle Referral Signup
    user_row = get_user(uid)
    if not user_row:
        ref_id = None
        start_param = data.get("startParam", "")
        if start_param and start_param.isdigit():
            possible_ref = int(start_param)
            if possible_ref != uid and get_user(possible_ref):
                ref_id = possible_ref
        add_user(uid, tg_user.get("username", ""), tg_user.get("first_name", ""), ref_id)
        user_row = get_user(uid)

    conn = db()
