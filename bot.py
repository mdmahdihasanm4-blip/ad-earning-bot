import os
import sqlite3
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    WebAppInfo
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DB_FILE = os.getenv("DB_FILE", "bot.db")
DEFAULT_MIN_WITHDRAW = 100.0

WEB_APP_URL = "https://mdmahdihasanm4-blip.github.io/ad-earning-bot/"


def connect():
    con = sqlite3.connect(DB_FILE)
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
    con.execute("""INSERT OR IGNORE INTO settings(key,value) VALUES
        ('min_withdraw','100'),
        ('ad_reward','0.50')""")
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
        [InlineKeyboardButton(
            "🚀 Open AdEarn App",
            web_app=WebAppInfo(url=WEB_APP_URL)
        )],
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
        "🎬 *AdEarn Bot*\n\n"
        "🚀 নিচের বাটনে চাপ দিয়ে AdEarn Mini App খুলুন।\n\n"
        "📺 বিজ্ঞাপন/অফার সম্পন্ন করে reward জমাও।\n"
        "💳 Minimum withdrawal পরে Admin সেট করতে পারবে।\n\n"
        "⚠️ Ad network এখনো যুক্ত করা হয়নি।",
        parse_mode="Markdown",
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
        reward = setting("ad_reward", 0.5)
        await q.edit_message_text(
            "📺 *Watch Ads*\n\n"
            "এখানে পরে আসল ad-network integration বসবে।\n"
            f"বর্তমান reward setting: {reward:.2f} BDT\n\n"
            "⚠️ এখনো কোনো ad দেখলে balance যোগ হবে না।",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Main Menu", callback_data="home")]
            ])
        )

    elif q.data == "refer":
        me = await context.bot.get_me()
        link = f"https://t.me/{me.username}?start=ref_{uid}"

        await q.edit_message_text(
            f"👥 *Refer & Earn*\n\nতোমার referral link:\n`{link}`",
            parse_mode="Markdown",
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
            f"📊 *Your Statistics*\n\n"
            f"📺 Ads: {row[0]}\n"
            f"👥 Referrals: {row[1]}",
            parse_mode="Markdown",
            reply_markup=main_keyboard()
        )

    elif q.data == "withdraw":
        minimum = setting("min_withdraw", DEFAULT_MIN_WITHDRAW)
        con = connect()
        bal = con.execute(
            "SELECT balance FROM users WHERE user_id=?",
            (uid,)
        ).fetchone()[0]
        con.close()

        if bal < minimum:
            await q.edit_message_text(
                f"💳 Minimum withdrawal: {minimum:.0f} BDT\n"
                f"তোমার balance: {bal:.2f} BDT",
                reply_markup=main_keyboard()
            )
        else:
            await q.edit_message_text(
                "💳 Withdrawal request দিতে /withdraw কমান্ড ব্যবহার করো।\n\n"
                "Format:\n/withdraw bkash 01XXXXXXXXX 100",
                reply_markup=main_keyboard()
            )

    elif q.data == "help":
        await q.edit_message_text(
            "ℹ️ Help\n\n"
            "📺 Watch Ads — ad/offer section\n"
            "💰 Balance — বর্তমান balance\n"
            "💳 Withdraw — bKash/Nagad request\n"
            "👥 Refer — referral link\n\n"
            "Admin: /admin",
            reply_markup=main_keyboard()
        )

    elif q.data == "home":
        await q.edit_message_text(
            "🏠 Main Menu",
            reply_markup=main_keyboard()
        )


async def withdraw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user)

    if len(context.args) != 3:
        await update.message.reply_text(
            "ব্যবহার:\n/withdraw bkash 01XXXXXXXXX 100\n"
            "অথবা\n/withdraw nagad 01XXXXXXXXX 100"
        )
        return

    method, number, amount_s = context.args
    method = method.lower()

    if method not in ("bkash", "nagad"):
        await update.message.reply_text(
            "শুধু bkash অথবা nagad ব্যবহার করো।"
        )
        return

    try:
        amount = float(amount_s)
    except ValueError:
        await update.message.reply_text("Amount সঠিকভাবে দাও।")
        return

    minimum = setting("min_withdraw", DEFAULT_MIN_WITHDRAW)
    con = connect()

    bal = con.execute(
        "SELECT balance FROM users WHERE user_id=?",
        (update.effective_user.id,)
    ).fetchone()[0]

    if amount < minimum or amount > bal:
        con.close()
        await update.message.reply_text(
            f"Minimum {minimum:.0f} BDT এবং "
            f"তোমার balance {bal:.2f} BDT।"
        )
        return

    con.execute(
        "INSERT INTO withdrawals(user_id,method,number,amount) "
        "VALUES(?,?,?,?)",
        (update.effective_user.id, method, number, amount)
    )
    con.commit()
    con.close()

    await update.message.reply_text(
        "✅ Withdrawal request জমা হয়েছে। "
        "Admin payment করার পর status update করবে।"
    )

    if ADMIN_ID:
        await context.bot.send_message(
            ADMIN_ID,
            f"💳 New withdrawal\n"
            f"User: {update.effective_user.id}\n"
            f"Method: {method}\n"
            f"Number: {number}\n"
            f"Amount: {amount:.2f} BDT"
        )


async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    con = connect()
    users = con.execute(
        "SELECT COUNT(*) FROM users"
    ).fetchone()[0]
    pending = con.execute(
        "SELECT COUNT(*) FROM withdrawals WHERE status='pending'"
    ).fetchone()[0]
    con.close()

    await update.message.reply_text(
        f"👑 Admin Panel\n\n"
        f"👤 Users: {users}\n"
        f"💳 Pending withdrawals: {pending}\n\n"
        "Commands:\n/setreward 0.50\n/setmin 100"
    )


async def setreward(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID or len(context.args) != 1:
        return

    try:
        value = float(context.args[0])
    except ValueError:
        return

    con = connect()
    con.execute(
        "UPDATE settings SET value=? WHERE key='ad_reward'",
        (str(value),)
    )
    con.commit()
    con.close()

    await update.message.reply_text(
        f"✅ Ad reward set to {value:.2f} BDT"
    )


async def setmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID or len(context.args) != 1:
        return

    try:
        value = float(context.args[0])
    except ValueError:
        return

    con = connect()
    con.execute(
        "UPDATE settings SET value=? WHERE key='min_withdraw'",
        (str(value),)
    )
    con.commit()
    con.close()

    await update.message.reply_text(
        f"✅ Minimum withdrawal set to {value:.0f} BDT"
    )


def build_app():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is missing.")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("withdraw", withdraw))
    app.add_handler(CommandHandler("admin", admin))
    app.add_handler(CommandHandler("setreward", setreward))
    app.add_handler(CommandHandler("setmin", setmin))
    app.add_handler(CallbackQueryHandler(button))

    return app


if __name__ == "__main__":
    build_app().run_polling()
