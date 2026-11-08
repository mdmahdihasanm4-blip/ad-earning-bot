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
 for name, definition in definitions.items(:
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
