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

WEB_URL = "https://github.io"
WEB_ORIGIN = "https://github.io"

PORT = int(os.getenv("PORT", "8080"))

POSTBACK_SECRET = os.getenv("POSTBACK_SECRET", "")
MONETAG_ZONE_ID = "12185380"

TIMEZONE = ZoneInfo("Asia/Dhaka")
DB_FILE = "adearn.db"

app = Flask(__name__)
db_lock = threading.RLock()


# ==================================================
# DATABASE SETUP
# ==================================================

def db():
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

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
                joined_at TEXT DEFAULT '',
                is_blocked INTEGER DEFAULT 0
            )
        """)

        # Withdrawals Table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS withdrawals(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                method TEXT NOT NULL,
                account TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at TEXT DEFAULT '',
                fee_amount REAL DEFAULT 0,
                net_amount REAL DEFAULT 0,
                fee_percent REAL DEFAULT 0,
                refunded INTEGER DEFAULT 0
            )
        """)

        # Settings Table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS settings(
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)

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
    with db_lock:
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
    with db_lock:
        conn = db()
        try:
            conn.execute("""
                INSERT OR IGNORE INTO users(user_id,username,first_name,referred_by,joined_at,is_blocked)
                VALUES(?,?,?,?,?,0)
            """, (uid, username or "", first_name or "", referred_by, datetime.now(TIMEZONE).isoformat()))
            conn.execute("UPDATE users SET username=?, first_name=? WHERE user_id=?", (username or "", first_name or "", uid))
            conn.commit()
        finally:
            conn.close()

def is_blocked(uid):
    row = get_user(uid)
    return bool(row and row["is_blocked"])

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
# FLASK API ENDPOINTS
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
    today = today_text()
    ads_watched = conn.execute(
        "SELECT COUNT(*) as count FROM ad_sessions WHERE user_id=? AND substr(created_at,1,10)=? AND rewarded_at IS NOT NULL",
        (uid, today)
    ).fetchone()["count"]
    
    total_withdraw = conn.execute("SELECT SUM(amount) as total FROM withdrawals WHERE user_id=? AND status='paid'", (uid,)).fetchone()["total"] or 0
    referrals_count = conn.execute("SELECT COUNT(*) as count FROM users WHERE referred_by=?", (uid,)).fetchone()["count"]
    conn.close()

    return jsonify({
        "user_id": user_row["user_id"],
        "first_name": user_row["first_name"],
        "balance": user_row["balance"],
        "ads_watched_today": ads_watched,
        "total_withdraw": total_withdraw,
        "referrals_count": referrals_count,
        "referral_link": f"https://t.me{BOT_USERNAME}?start={uid}"
    })

@app.route("/api/withdraw", methods=["POST"])
def request_withdraw():
    data = request.json or {}
    init_data = data.get("initData", "")
    tg_user = verify_telegram_data(init_data)
    
    if not tg_user:
        return jsonify({"error": "Unauthorized"}), 401
        
    uid = tg_user.get("id")
    if is_blocked(uid):
        return jsonify({"error": "Banned"}), 403

    amount = float(data.get("amount", 0))
    method = data.get("method", "")
    account = data.get("account", "")
