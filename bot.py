
import os
import re
import sqlite3
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DB_FILE = os.getenv("DB_FILE", "bot.db")
DEFAULT_MIN_WITHDRAW = 100.0
WEB_APP_URL = "https://mdmahdihasanm4-blip.github.io/ad-earning-bot/"


def connect():
    con = sqlite3.connect(DB_FILE, timeout=20)
    con.execute("""CREATE TABLE IF NOT EXISTS users(
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        balance REAL NOT NULL DEFAULT 0,
        ads_watched INTEGER NOT NULL DEFAULT 0,
        referrals INTEGER NOT NULL DEFAULT 0
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS withdrawals(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        method TEXT,
        number TEXT,
        amount REAL,
        status TEXT DEFAULT 'pending',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS settings(
        key TEXT PRIMARY KEY,
        value TEXT
    )""")
    con.executemany(
        "INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)",
        [("min_withdraw", "100"), ("ad_reward", "0.50")]
    )
    con.commit()
    return con


def ensure_user(user):
    con = connect()
    con.execute(
        "INSERT OR IGNORE INTO users(user_id,username) VALUES(?,?)",
        (user.id, user.username or "")
    )
    con.execute(
        "UPDATE users SET username=? WHERE user_id=?",
        (user.username or "", user.id)
    )
    con.commit()
    con.close()


def setting(key, default):
    con = connect()
    row = con.execute(
        "SELECT value FROM settings WHERE key=?", (key,)
    ).fetchone()
    con.close()
    return float(row[0]) if row else default


def main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Open AdEarn App",
                              web_app=WebAppInfo(url=WEB_APP_URL))],
        [
            InlineKeyboardButton("📺 Watch Ads", callback_data="ads"),
            InlineKeyboardButton("💰 Balance", callback_data="balance")
        ],
        [
            InlineKeyboardButton("👥 Refer & Earn", callback_data="refer"),
            InlineKeyboardButton("📊 Statistics", callback_data="stats")
        ],
        [
            InlineKeyboardButton("💳 Withdraw", callback_data="withdraw"),
            InlineKeyboardButton("ℹ️ Help", callback_data="help")
        ]
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user)
    await update.message.reply_text(
        "🎬 AdEarn Bot\n\n"
        "নিচের বাটন দিয়ে Mini App খুলুন।\n"
        "বিজ্ঞাপনের reward এখনো চালু হয়নি।",
        reply_markup=main_keyboard()
    )


async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ensure_user(q.from_user)
    uid = q.from_user.id

    if q.data == "balance":
        con = connect()
        row = con.execute(
            "SELECT balance,ads_watched FROM users WHERE user_id=?",
            (uid,)
        ).fetchone()
        con.close()
        await q.edit_message_text(
            f"💰 Balance: {row[0]:.2f} BDT\n"
            f"📺 Ads completed: {row[1]}",
            reply_markup=main_keyboard()
        )

    elif q.data == "ads":
        await q.edit_message_text(
            "📺 বিজ্ঞাপন বিভাগ\n\n"
            "আসল বিজ্ঞাপন যাচাই ব্যবস্থা যুক্ত না হওয়া পর্যন্ত "
            "এখানে balance যোগ হবে না।",
            reply_markup=main_keyboard()
        )

    elif q.data == "refer":
        me = await context.bot.get_me()
        link = f"https://t.me/{me.username}?start=ref_{uid}"
        await q.edit_message_text(
            f"👥 Referral link:\n{link}",
            reply_markup=main_keyboard()
        )

    elif q.data == "stats":
        con = connect()
        row = con.execute(
            "SELECT ads_watched,referrals FROM users WHERE user_id=?",
            (uid,)
        ).fetchone()
        con.close()
        await q.edit_message_text(
            f"📊 Statistics\n\nAds: {row[0]}\nReferrals: {row[1]}",
            reply_markup=main_keyboard()
        )

    elif q.data == "withdraw":
        minimum = setting("min_withdraw", DEFAULT_MIN_WITHDRAW)
        con = connect()
        bal = con.execute(
            "SELECT balance FROM users WHERE user_id=?", (uid,)
        ).fetchone()[0]
        con.close()
        await q.edit_message_text(
            f"💳 Minimum withdrawal: {minimum:.0f} BDT\n"
            f"Your balance: {bal:.2f} BDT\n\n"
            "Request:\n/withdraw bkash 01XXXXXXXXX 100\n"
            "অথবা\n/withdraw nagad 01XXXXXXXXX 100",
            reply_markup=main_keyboard()
        )

    elif q.data == "help":
        await q.edit_message_text(
            "ℹ️ Help\n\n"
            "Balance — ব্যালেন্স দেখুন\n"
            "Withdraw — bKash/Nagad request\n"
            "Refer — referral link\n"
            "Admin — /admin",
            reply_markup=main_keyboard()
        )

    elif q.data == "home":
        await q.edit_message_text("🏠 Main Menu",
                                  reply_markup=main_keyboard())


async def withdraw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user)

    if len(context.args) != 3:
        await update.message.reply_text(
            "ব্যবহার:\n/withdraw bkash 01XXXXXXXXX 100\n"
            "অথবা\n/withdraw nagad 01XXXXXXXXX 100"
        )
        return

    method, number, amount_s = context.args
    method = method.lower()

    if method not in ("bkash", "nagad"):
        await update.message.reply_text("শুধু bkash অথবা nagad দাও।")
        return

    if not re.fullmatch(r"01[3-9]\d{8}", number):
        await update.message.reply_text("সঠিক বাংলাদেশি মোবাইল নম্বর দাও।")
        return

    try:
        amount = float(amount_s)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("সঠিক amount দাও।")
        return

    minimum = setting("min_withdraw", DEFAULT_MIN_WITHDRAW)
    con = connect()

    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT balance FROM users WHERE user_id=?", (user.id,)
        ).fetchone()

        if not row or amount < minimum or amount > row[0]:
            con.rollback()
            bal = row[0] if row else 0
            await update.message.reply_text(
                f"Minimum: {minimum:.0f} BDT\n"
                f"তোমার balance: {bal:.2f} BDT"
            )
            return

        cur = con.execute(
            "INSERT INTO withdrawals(user_id,method,number,amount) "
            "VALUES(?,?,?,?)",
            (user.id, method, number, amount)
        )
        request_id = cur.lastrowid

        con.execute(
            "UPDATE users SET balance=balance-? WHERE user_id=?",
            (amount, user.id)
        )
        con.commit()

    except Exception:
        con.rollback()
        raise
    finally:
        con.close()

    await update.message.reply_text(
        f"✅ Withdrawal request জমা হয়েছে।\n"
        f"Request ID: {request_id}\n"
        f"Amount: {amount:.2f} BDT"
    )

    if ADMIN_ID:
        await context.bot.send_message(
            ADMIN_ID,
            f"💳 New withdrawal #{request_id}\n"
            f"User: {user.id}\nMethod: {method}\n"
            f"Number: {number}\nAmount: {amount:.2f} BDT\n\n"
            f"Approve: /approve {request_id}\n"
            f"Reject: /reject {request_id}"
        )


async def approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.message.reply_text("ব্যবহার: /approve REQUEST_ID")
        return

    rid = int(context.args[0])
    con = connect()
    con.execute("BEGIN IMMEDIATE")
    row = con.execute(
        "SELECT user_id,amount FROM withdrawals "
        "WHERE id=? AND status='pending'", (rid,)
    ).fetchone()

    if not row:
        con.rollback()
        con.close()
        await update.message.reply_text("Pending request পাওয়া যায়নি।")
        return

    con.execute(
        "UPDATE withdrawals SET status='paid' WHERE id=?", (rid,)
    )
    con.commit()
    con.close()

    await update.message.reply_text(f"✅ Request #{rid} paid হিসেবে চিহ্নিত হয়েছে।")
    await context.bot.send_message(
        row[0], f"✅ তোমার withdrawal #{rid} paid হিসেবে চিহ্নিত হয়েছে।"
    )


async def reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.message.reply_text("ব্যবহার: /reject REQUEST_ID")
        return

    rid = int(context.args[0])
    con = connect()
    con.execute("BEGIN IMMEDIATE")
    row = con.execute(
        "SELECT user_id,amount FROM withdrawals "
        "WHERE id=? AND status='pending'", (rid,)
    ).fetchone()

    if not row:
        con.rollback()
        con.close()
        await update.message.reply_text("Pending request পাওয়া যায়নি।")
        return

    con.execute(
        "UPDATE users SET balance=balance+? WHERE user_id=?",
        (row[1], row[0])
    )
    con.execute(
        "UPDATE withdrawals SET status='rejected' WHERE id=?", (rid,)
    )
    con.commit()
    con.close()

    await update.message.reply_text(
        f"↩️ Request #{rid} rejected এবং balance ফেরত দেওয়া হয়েছে।"
    )
    await context.bot.send_message(
        row[0], f"↩️ তোমার withdrawal #{rid} বাতিল হয়েছে। টাকা balance-এ ফেরত দেওয়া হয়েছে।"
    )


async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    con = connect()
    users = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    pending = con.execute(
        "SELECT COUNT(*) FROM withdrawals WHERE status='pending'"
    ).fetchone()[0]
    con.close()

    await update.message.reply_text(
        f"👑 Admin Panel\nUsers: {users}\n"
        f"Pending withdrawals: {pending}\n\n"
        "/setreward 0.50\n/setmin 100\n"
        "/approve ID\n/reject ID"
    )


async def set_value(update, context, key, label):
    if update.effective_user.id != ADMIN_ID or len(context.args) != 1:
        return
    try:
        value = float(context.args[0])
        if value < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("সঠিক সংখ্যা দাও।")
        return

    con = connect()
    con.execute(
        "INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
        (key, str(value))
    )
    con.commit()
    con.close()
    await update.message.reply_text(f"✅ {label}: {value:.2f} BDT")


async def setreward(update, context):
    await set_value(update, context, "ad_reward", "Ad reward")


async def setmin(update, context):
    await set_value(update, context, "min_withdraw", "Minimum withdrawal")


def build_app():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is missing.")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("withdraw", withdraw))
    app.add_handler(CommandHandler("admin", admin))
    app.add_handler(CommandHandler("approve", approve))
    app.add_handler(CommandHandler("reject", reject))
    app.add_handler(CommandHandler("setreward", setreward))
    app.add_handler(CommandHandler("setmin", setmin))
    app.add_handler(CallbackQueryHandler(button))
    return app


if __name__ == "__main__":
    build_app().run_polling()
