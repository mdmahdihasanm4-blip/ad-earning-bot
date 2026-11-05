import os, re, json, hmac, secrets, hashlib, sqlite3, threading
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
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
POSTBACK_SECRET = os.getenv("POSTBACK_SECRET", "")
DB_FILE = "bot.db" if os.path.exists("bot.db") else "adearn.db"
MONETAG_ZONE_ID = "12185380"
TIMEZONE = ZoneInfo("Asia/Dhaka")
app = Flask(__name__)
db_lock = threading.RLock()

def db():
    c = sqlite3.connect(DB_FILE, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=30000")
    return c

def cols(cur, table):
    return {r["name"] for r in cur.execute(f"PRAGMA table_info({table})")}

def add_cols(cur, table, definitions):
    existing = cols(cur, table)
    for name, definition in definitions.items():
        if name not in existing:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

def init_db():
    with db_lock:
        c = db()
        q = c.cursor()
        q.execute("""CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY, username TEXT DEFAULT '',
            first_name TEXT DEFAULT '', balance REAL DEFAULT 0,
            referred_by INTEGER DEFAULT NULL, joined_at TEXT DEFAULT '')""")
        add_cols(q, "users", {
            "username":"TEXT DEFAULT ''", "first_name":"TEXT DEFAULT ''",
            "balance":"REAL DEFAULT 0", "referred_by":"INTEGER DEFAULT NULL",
            "joined_at":"TEXT DEFAULT ''", "is_blocked":"INTEGER DEFAULT 0"})
        q.execute("UPDATE users SET joined_at=CURRENT_TIMESTAMP WHERE joined_at IS NULL OR joined_at=''")
        q.execute("""CREATE TABLE IF NOT EXISTS withdrawals(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            amount REAL NOT NULL, method TEXT NOT NULL, account TEXT NOT NULL,
            status TEXT DEFAULT 'pending', created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        add_cols(q, "withdrawals", {
            "user_id":"INTEGER", "amount":"REAL DEFAULT 0", "method":"TEXT DEFAULT ''",
            "account":"TEXT DEFAULT ''", "status":"TEXT DEFAULT 'pending'",
            "created_at":"TEXT DEFAULT ''"})
        q.execute("UPDATE withdrawals SET created_at=CURRENT_TIMESTAMP WHERE created_at IS NULL OR created_at=''")
        q.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        for k,v in {"min_withdraw":"50","daily_limit":"20","reward_amount":"10","design_config":"{}"}.items():
            q.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)",(k,v))
        q.execute("""CREATE TABLE IF NOT EXISTS ad_sessions(
            ymid TEXT PRIMARY KEY,user_id INTEGER NOT NULL,created_at TEXT NOT NULL,
            rewarded_at TEXT DEFAULT NULL)""")
        q.execute("CREATE INDEX IF NOT EXISTS idx_ad_sessions_user ON ad_sessions(user_id,created_at)")
        c.commit()
        c.close()

def setting(key, default):
    c=db()
    try:
        r=c.execute("SELECT value FROM settings WHERE key=?",(key,)).fetchone()
        return r["value"] if r else default
    finally: c.close()

def set_setting(key,value):
    c=db()
    try:
        c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",(key,str(value)))
        c.commit()
    finally: c.close()

def get_user(uid):
    c=db()
    try: return c.execute("SELECT * FROM users WHERE user_id=?",(uid,)).fetchone()
    finally: c.close()

def is_blocked(uid):
    row=get_user(uid)
    return bool(row and row["is_blocked"])

def add_user(uid, username="", first_name="", referred_by=None):
    c=db()
    try:
        c.execute("""INSERT OR IGNORE INTO users(user_id,username,first_name,referred_by,joined_at)
            VALUES(?,?,?,?,CURRENT_TIMESTAMP)""",(uid,username or "",first_name or "",referred_by))
        c.execute("UPDATE users SET username=?,first_name=? WHERE user_id=?",
                  (username or "",first_name or "",uid))
        c.commit()
    finally: c.close()

def today_text():
    return datetime.now(TIMEZONE).date().isoformat()

def verify_telegram_data(init_data):
    if not BOT_TOKEN or not init_data: return None
    try:
        parsed=dict(parse_qsl(init_data,keep_blank_values=True))
        received=parsed.pop("hash",None)
        if not received: return None
        check="\n".join(f"{k}={v}" for k,v in sorted(parsed.items()))
        key=hmac.new(b"WebAppData",BOT_TOKEN.encode(),hashlib.sha256).digest()
        digest=hmac.new(key,check.encode(),hashlib.sha256).hexdigest()
        if not hmac.compare_digest(digest,received): return None
        auth=int(parsed.get("auth_date","0"))
        now=datetime.now(timezone.utc).timestamp()
        if auth<=0 or now-auth>86400 or auth-now>60: return None
        u=json.loads(parsed.get("user","{}"))
        return u if u.get("id") else None
    except Exception: return None

def require_user():
    u=verify_telegram_data(request.headers.get("X-Telegram-Init-Data",""))
    if not u: return None, (jsonify({"error":"Telegram verification failed"}),401)
    add_user(u["id"],u.get("username"),u.get("first_name"))
    if is_blocked(u["id"]):
        return None, (jsonify({"error":"আপনার অ্যাকাউন্টটি সাময়িকভাবে বন্ধ করা হয়েছে"}),403)
    return u,None

@app.after_request
def cors(resp):
    origin=request.headers.get("Origin","")
    if origin==WEB_ORIGIN:
        resp.headers["Access-Control-Allow-Origin"]=origin
        resp.headers["Vary"]="Origin"
        resp.headers["Access-Control-Allow-Headers"]="Content-Type, X-Telegram-Init-Data"
        resp.headers["Access-Control-Allow-Methods"]="GET, POST, OPTIONS"
    return resp

@app.route("/health")
def health():
    try:
        c=db(); c.execute("SELECT 1"); c.close()
        return jsonify(status="ok",database=DB_FILE)
    except Exception:
        app.logger.exception("Health check failed")
        return jsonify(status="error"),500

@app.route("/api/me",methods=["GET","OPTIONS"])
def api_me():
    if request.method=="OPTIONS": return "",204
    u,e=require_user()
    if e: return e
    row=get_user(u["id"]); c=db()
    try:
        ads=c.execute("SELECT COUNT(*) n FROM ad_sessions WHERE user_id=? AND rewarded_at LIKE ?",
                      (u["id"],today_text()+"%")).fetchone()["n"]
        refs=c.execute("SELECT COUNT(*) n FROM users WHERE referred_by=?",(u["id"],)).fetchone()["n"]
        total_paid=c.execute("SELECT COALESCE(SUM(amount),0) n FROM withdrawals WHERE user_id=? AND status='paid'",
                             (u["id"],)).fetchone()["n"]
        latest=c.execute("SELECT status FROM withdrawals WHERE user_id=? ORDER BY id DESC LIMIT 1",
                         (u["id"],)).fetchone()
    finally: c.close()
    return jsonify(user_id=u["id"],username=u.get("username",""),first_name=u.get("first_name",""),
        balance=float(row["balance"] or 0),min_withdraw=float(setting("min_withdraw","50")),
        daily_limit=int(setting("daily_limit","20")),reward_amount=float(setting("reward_amount","10")),
        ads_today=ads,referrals=refs,total_withdrawn=float(total_paid or 0),
        latest_withdrawal_status=latest["status"] if latest else None,
        referral_link=f"https://t.me/{BOT_USERNAME}?start={u['id']}")

@app.route("/api/ad/start",methods=["POST","OPTIONS"])
def api_ad_start():
    if request.method=="OPTIONS": return "",204
    u,e=require_user()
    if e: return e
    c=db()
    try:
        c.execute("BEGIN IMMEDIATE")
        today=today_text(); limit=int(setting("daily_limit","20"))
        done=c.execute("SELECT COUNT(*) n FROM ad_sessions WHERE user_id=? AND rewarded_at LIKE ?",
                       (u["id"],today+"%")).fetchone()["n"]
        if done>=limit:
            c.rollback(); return jsonify(error="আজকের বিজ্ঞাপনের সীমা শেষ"),400
        active=c.execute("""SELECT COUNT(*) n FROM ad_sessions
            WHERE user_id=? AND rewarded_at IS NULL AND created_at>=?""",
            (u["id"],(datetime.now(TIMEZONE)-timedelta(minutes=30)).isoformat())).fetchone()["n"]
        if active>=3:
            c.rollback(); return jsonify(error="আগের বিজ্ঞাপন যাচাই হওয়ার জন্য অপেক্ষা করো"),429
        token=secrets.token_hex(16)
        c.execute("INSERT INTO ad_sessions(ymid,user_id,created_at) VALUES(?,?,?)",
                  (token,u["id"],datetime.now(TIMEZONE).isoformat()))
        c.commit()
        return jsonify(ymid=token)
    except Exception:
        c.rollback(); app.logger.exception("Ad session creation failed")
        return jsonify(error="বিজ্ঞাপন চালু করা যায়নি"),500
    finally: c.close()

@app.route("/postback",methods=["GET"])
def monetag_postback():
    if not POSTBACK_SECRET: return "Server secret missing",500
    if not hmac.compare_digest(POSTBACK_SECRET,request.args.get("secret","")): return "Unauthorized",401
    if request.args.get("zone_id")!=MONETAG_ZONE_ID: return "Ignored zone",200
    if request.args.get("reward_event_type")!="valued": return "Non-valued event",200
    if request.args.get("event_type") not in ("impression","click"): return "Ignored event",200
    token=request.args.get("ymid","").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}",token): return "Invalid ymid",400
    c=db()
    try:
        c.execute("BEGIN IMMEDIATE")
        s=c.execute("SELECT user_id,created_at,rewarded_at FROM ad_sessions WHERE ymid=?",(token,)).fetchone()
        if not s: c.rollback(); return "Unknown session",200
        if request.args.get("telegram_id") and request.args["telegram_id"]!=str(s["user_id"]):
            c.rollback(); return "User mismatch",200
        if s["rewarded_at"]: c.rollback(); return "Already rewarded",200
        blocked=c.execute("SELECT is_blocked FROM users WHERE user_id=?",(s["user_id"],)).fetchone()
        if not blocked or blocked["is_blocked"]: c.rollback(); return "User unavailable",200
        try:
            created=datetime.fromisoformat(s["created_at"])
            if created.tzinfo is None: created=created.replace(tzinfo=TIMEZONE)
            if datetime.now(TIMEZONE)-created>timedelta(hours=24):
                c.rollback(); return "Session expired",200
        except Exception:
            c.rollback(); return "Invalid session time",200
        count=c.execute("SELECT COUNT(*) n FROM ad_sessions WHERE user_id=? AND rewarded_at LIKE ?",
                        (s["user_id"],today_text()+"%")).fetchone()["n"]
        if count>=int(setting("daily_limit","20")): c.rollback(); return "Daily limit reached",200
        reward=float(setting("reward_amount","10"))
        if reward<=0: c.rollback(); return "Invalid reward setting",500
        stamp=datetime.now(TIMEZONE).isoformat()
        c.execute("UPDATE ad_sessions SET rewarded_at=? WHERE ymid=? AND rewarded_at IS NULL",(stamp,token))
        if c.total_changes<1: c.rollback(); return "Already rewarded",200
        c.execute("UPDATE users SET balance=balance+? WHERE user_id=? AND COALESCE(is_blocked,0)=0",
                  (reward,s["user_id"]))
        c.commit()
        return "OK",200
    except Exception:
        c.rollback(); app.logger.exception("Monetag postback failed"); return "Server error",500
    finally: c.close()

@app.route("/api/withdraw",methods=["POST","OPTIONS"])
def api_withdraw():
    if request.method=="OPTIONS": return "",204
    u,e=require_user()
    if e: return e
    data=request.get_json(silent=True) or {}
    try: amount=round(float(data.get("amount",0)),2)
    except (TypeError,ValueError): return jsonify(error="সঠিক পরিমাণ লিখুন"),400
    method=str(data.get("method","")).strip(); account=str(data.get("account","")).strip()
    if amount<=0: return jsonify(error="সঠিক পরিমাণ লিখুন"),400
    if amount<float(setting("min_withdraw","50")): return jsonify(error="ন্যূনতম উত্তোলনের পরিমাণ পূরণ হয়নি"),400
    if method not in ("bKash","Nagad"): return jsonify(error="bKash অথবা Nagad নির্বাচন করুন"),400
    if not re.fullmatch(r"01[3-9]\d{8}",account): return jsonify(error="সঠিক মোবাইল নম্বর দিন"),400
    c=db()
    try:
        c.execute("BEGIN IMMEDIATE")
        row=c.execute("SELECT balance,is_blocked FROM users WHERE user_id=?",(u["id"],)).fetchone()
        if not row or row["is_blocked"]: c.rollback(); return jsonify(error="অ্যাকাউন্ট বন্ধ"),403
        if float(row["balance"] or 0)<amount: c.rollback(); return jsonify(error="পর্যাপ্ত ব্যালেন্স নেই"),400
        c.execute("UPDATE users SET balance=balance-? WHERE user_id=?",(amount,u["id"]))
        c.execute("""INSERT INTO withdrawals(user_id,amount,method,account,status)
            VALUES(?,?,?,?,'pending')""",(u["id"],amount,method,account))
        c.commit()
        return jsonify(success=True,message="উত্তোলনের অনুরোধ জমা হয়েছে")
    except Exception:
        c.rollback(); app.logger.exception("Withdrawal request failed")
        return jsonify(error="উত্তোলনের অনুরোধ নেওয়া যায়নি"),500
    finally: c.close()

@app.route("/api/withdrawals",methods=["GET","OPTIONS"])
def api_withdrawals():
    if request.method=="OPTIONS": return "",204
    u,e=require_user()
    if e: return e
    try: limit=max(1,min(int(request.args.get("limit",20)),50))
    except ValueError: return jsonify(error="ভুল limit"),400
    c=db()
    try:
        rows=c.execute("""SELECT id,amount,method,account,status,created_at FROM withdrawals
            WHERE user_id=? ORDER BY id DESC LIMIT ?""",(u["id"],limit)).fetchall()
        return jsonify(items=[dict(x) for x in rows])
    finally: c.close()

# Design schema follows the admin panel and also accepts legacy saved settings.
SECTIONS=["home","ads","balance","referral","withdraw","withdrawHistory","profile","statistics"]
DEFAULT_DESIGN={
 "theme":"dark",
 "app_name":"AdEarn","header_text":"বিজ্ঞাপন দেখে আয় করুন","footer_text":"ধন্যবাদ আমাদের সাথে থাকার জন্য",
 "colors":{"background":"#101827","card":"#1b2638","button":"#8b5cf6","text":"#ffffff","border":"#334155","muted":"#9ca3af","accent":"#8b5cf6"},
 "labels":{"home":"হোম","ads":"বিজ্ঞাপন","balance":"ব্যালেন্স","referral":"রেফার","withdraw":"উত্তোলন",
           "withdrawHistory":"উত্তোলনের ইতিহাস","profile":"প্রোফাইল","statistics":"পরিসংখ্যান",
           "appName":"AdEarn","history":"উত্তোলনের ইতিহাস"},
 "order":SECTIONS[:],
 "visibility":{x:True for x in SECTIONS}
}

def get_design():
    try: saved=json.loads(setting("design_config","{}"))
    except Exception: saved={}
    result=json.loads(json.dumps(DEFAULT_DESIGN))
    if not isinstance(saved,dict): return result
    for k,v in saved.items():
        if k in ("colors","labels") and isinstance(v,dict): result[k].update(v)
        elif k=="visibility" and isinstance(v,dict):
            # Migrate old "history" key to current panel key.
            if "history" in v and "withdrawHistory" not in v: v["withdrawHistory"]=v["history"]
            result[k].update({a:b for a,b in v.items() if a in SECTIONS and isinstance(b,bool)})
        elif k=="order" and isinstance(v,list):
            mapped=["withdrawHistory" if x=="history" else x for x in v]
            result[k]=[x for x in mapped if x in SECTIONS]
            result[k]+= [x for x in SECTIONS if x not in result[k]]
        elif k in ("theme","app_name","header_text","footer_text"): result[k]=v
    if "history" in result["labels"] and "withdrawHistory" not in result["labels"]:
        result["labels"]["withdrawHistory"]=result["labels"]["history"]
    return result

@app.route("/api/design",methods=["GET","OPTIONS"])
def public_design():
    if request.method=="OPTIONS": return "",204
    return jsonify(design=get_design())

@app.route("/api/admin/design",methods=["GET","POST","OPTIONS"])
def admin_design():
    if request.method=="OPTIONS": return "",204
    if not verified_admin(): return jsonify(error="অ্যাডমিন অনুমতি নেই"),403
    if request.method=="GET": return jsonify(design=get_design())
    data=request.get_json(silent=True) or {}; cur=get_design()
    theme=str(data.get("theme",cur["theme"])).lower()
    if theme not in ("dark","light","green","blue","red","custom"):
        return jsonify(error="ভুল থিম"),400
    colors=dict(cur["colors"]); incoming=data.get("colors",{})
    if not isinstance(incoming,dict): return jsonify(error="রঙের সেটিংস সঠিক নয়"),400
    for k,v in incoming.items():
        if k not in colors: continue
        v=str(v).strip()
        if not re.fullmatch(r"#[0-9a-fA-F]{6}",v): return jsonify(error="রঙের HEX কোড সঠিক নয়"),400
        colors[k]=v
    labels=dict(cur["labels"]); incoming=data.get("labels",{})
    if not isinstance(incoming,dict): return jsonify(error="লেবেল সেটিংস সঠিক নয়"),400
    aliases={"history":"withdrawHistory","appName":"appName"}
    for k,v in incoming.items():
        k=aliases.get(k,k)
        if k not in labels: continue
        v=str(v).strip()
        if not v or len(v)>60: return jsonify(error="লেখা ১ থেকে ৬০ অক্ষরের মধ্যে দিন"),400
        labels[k]=v
    order=data.get("order",cur["order"])
    if not isinstance(order,list): return jsonify(error="সেকশনের অবস্থান সঠিক নয়"),400
    order=["withdrawHistory" if x=="history" else x for x in order]
    if len(order)!=len(SECTIONS) or set(order)!=set(SECTIONS):
        return jsonify(error="সেকশনের অবস্থান সঠিক নয়"),400
    visibility=data.get("visibility",cur["visibility"])
    if not isinstance(visibility,dict): return jsonify(error="Visibility সেটিংস সঠিক নয়"),400
    vis=dict(cur["visibility"])
    for k,v in visibility.items():
        k="withdrawHistory" if k=="history" else k
        if k not in SECTIONS or not isinstance(v,bool): return jsonify(error="Visibility সেটিংস সঠিক নয়"),400
        vis[k]=v
    config={"theme":theme,"app_name":str(data.get("app_name",data.get("appName",cur["app_name"])))[:60],
        "header_text":str(data.get("header_text",cur["header_text"]))[:120],
        "footer_text":str(data.get("footer_text",cur["footer_text"]))[:160],
        "colors":colors,"labels":labels,"order":order,"visibility":vis}
    set_setting("design_config",json.dumps(config,ensure_ascii=False))
    return jsonify(success=True,message="ডিজাইন সংরক্ষণ হয়েছে",design=config)

def verified_admin():
    u=verify_telegram_data(request.headers.get("X-Telegram-Init-Data",""))
    if not u or not ADMIN_ID or int(u.get("id",0))!=ADMIN_ID: return None
    return u

@app.route("/api/admin/summary",methods=["GET","OPTIONS"])
def admin_summary():
    if request.method=="OPTIONS": return "",204
    if not verified_admin(): return jsonify(error="অ্যাডমিন অনুমতি নেই"),403
    c=db()
    try:
        users=c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
        bal=c.execute("SELECT COALESCE(SUM(balance),0) n FROM users").fetchone()["n"]
        pending=c.execute("SELECT COUNT(*) n,COALESCE(SUM(amount),0) amount FROM withdrawals WHERE status='pending'").fetchone()
        ads=c.execute("SELECT COUNT(*) n FROM ad_sessions WHERE rewarded_at LIKE ?",(today_text()+"%",)).fetchone()["n"]
        return jsonify(total_users=users,total_balance=float(bal or 0),pending_count=pending["n"],
            pending_amount=float(pending["amount"] or 0),ads_today=ads,
            settings={"reward_amount":float(setting("reward_amount","10")),
                      "daily_limit":int(setting("daily_limit","20")),
                      "min_withdraw":float(setting("min_withdraw","50"))})
    finally: c.close()

@app.route("/api/admin/users",methods=["GET","OPTIONS"])
def admin_users():
    if request.method=="OPTIONS": return "",204
    if not verified_admin(): return jsonify(error="অ্যাডমিন অনুমতি নেই"),403
    q=str(request.args.get("q","")).strip()[:100]
    try: limit=max(1,min(int(request.args.get("limit",100)),200)); offset=max(0,int(request.args.get("offset",0)))
    except ValueError: return jsonify(error="ভুল limit বা offset"),400
    c=db()
    try:
        where=""; params=[]
        if q.isdigit(): where="WHERE CAST(user_id AS TEXT) LIKE ?"; params=[f"%{q}%"]
        elif q: where="WHERE username LIKE ? OR first_name LIKE ?"; params=[f"%{q}%",f"%{q}%"]
        total=c.execute("SELECT COUNT(*) n FROM users "+where,params).fetchone()["n"]
        rows=c.execute("""SELECT user_id,COALESCE(username,'') username,COALESCE(first_name,'') first_name,
            COALESCE(balance,0) balance,COALESCE(joined_at,'') joined_at,COALESCE(is_blocked,0) is_blocked
            FROM users """+where+" ORDER BY joined_at DESC LIMIT ? OFFSET ?",params+[limit,offset]).fetchall()
        return jsonify(total=total,items=[dict(x) for x in rows])
    finally: c.close()

@app.route("/api/admin/user/block",methods=["POST","OPTIONS"])
@app.route("/api/admin/users/block",methods=["POST","OPTIONS"])
def admin_user_block():
    if request.method=="OPTIONS": return "",204
    if not verified_admin(): return jsonify(error="অ্যাডমিন অনুমতি নেই"),403
    d=request.get_json(silent=True) or {}
    try: uid=int(d.get("user_id",d.get("id",0)))
    except (TypeError,ValueError): return jsonify(error="সঠিক ইউজার ID দিন"),400
    action=str(d.get("action","")).lower()
    blocked=d.get("is_blocked")
    if blocked is None:
        if action not in ("block","unblock"): return jsonify(error="অ্যাকশন সঠিক নয়"),400
        blocked=action=="block"
    if not isinstance(blocked,bool): return jsonify(error="Block status সঠিক নয়"),400
    if uid<=0 or uid==ADMIN_ID: return jsonify(error="এই ইউজারকে ব্লক করা যাবে না"),400
    c=db()
    try:
        cur=c.execute("UPDATE users SET is_blocked=? WHERE user_id=?",(1 if blocked else 0,uid))
        c.commit()
        if not cur.rowcount: return jsonify(error="ইউজার পাওয়া যায়নি"),404
        return jsonify(success=True,is_blocked=blocked,message="ইউজার ব্লক করা হয়েছে" if blocked else "ইউজার আনব্লক করা হয়েছে")
    finally: c.close()

@app.route("/api/admin/withdrawals",methods=["GET","OPTIONS"])
def admin_withdrawals():
    if request.method=="OPTIONS": return "",204
    if not verified_admin(): return jsonify(error="অ্যাডমিন অনুমতি নেই"),403
    c=db()
    try:
        rows=c.execute("""SELECT w.id,w.user_id,w.amount,w.method,w.account,w.status,w.created_at,
            COALESCE(u.first_name,'') first_name,COALESCE(u.username,'') username
            FROM withdrawals w LEFT JOIN users u ON u.user_id=w.user_id
            WHERE w.status='pending' ORDER BY w.id DESC LIMIT 100""").fetchall()
        return jsonify(items=[dict(x) for x in rows])
    finally: c.close()

def withdrawal_action(wid,action):
    c=db()
    try:
        c.execute("BEGIN IMMEDIATE")
        row=c.execute("SELECT user_id,amount FROM withdrawals WHERE id=? AND status='pending'",(wid,)).fetchone()
        if not row: c.rollback(); return False,"পেন্ডিং অনুরোধ পাওয়া যায়নি"
        if action=="approve":
            c.execute("UPDATE withdrawals SET status='paid' WHERE id=? AND status='pending'",(wid,))
            msg="Paid হিসেবে চিহ্নিত হয়েছে। টাকা bKash/Nagad-এ ম্যানুয়ালি পাঠাতে হবে।"
        else:
            c.execute("UPDATE withdrawals SET status='rejected' WHERE id=? AND status='pending'",(wid,))
            c.execute("UPDATE users SET balance=balance+? WHERE user_id=?",(row["amount"],row["user_id"]))
            msg="অনুরোধ বাতিল এবং ব্যালেন্স ফেরত দেওয়া হয়েছে।"
        c.commit(); return True,msg
    except Exception:
        c.rollback(); raise
    finally: c.close()

@app.route("/api/admin/withdrawal/action",methods=["POST","OPTIONS"])
def admin_withdrawal_action():
    if request.method=="OPTIONS": return "",204
    if not verified_admin(): return jsonify(error="অ্যাডমিন অনুমতি নেই"),403
    d=request.get_json(silent=True) or {}
    try: wid=int(d.get("id",0))
    except (TypeError,ValueError): return jsonify(error="সঠিক অনুরোধ ID দিন"),400
    action=str(d.get("action","")).lower()
    if wid<=0 or action not in ("approve","reject"): return jsonify(error="অ্যাকশন বা ID সঠিক নয়"),400
    try: ok,msg=withdrawal_action(wid,action)
    except Exception:
        app.logger.exception("Withdrawal action failed"); return jsonify(error="অ্যাকশন সম্পন্ন করা যায়নি"),500
    return (jsonify(success=True,message=msg) if ok else (jsonify(error=msg),404))

@app.route("/api/admin/settings",methods=["POST","OPTIONS"])
def admin_settings():
    if request.method=="OPTIONS": return "",204
    if not verified_admin(): return jsonify(error="অ্যাডমিন অনুমতি নেই"),403
    d=request.get_json(silent=True) or {}
    try:
        reward=round(float(d["reward_amount"]),2); limit=int(d["daily_limit"]); minimum=round(float(d["min_withdraw"]),2)
        if not (0.01<=reward<=100000 and 1<=limit<=10000 and 1<=minimum<=10000000): raise ValueError
    except (KeyError,TypeError,ValueError): return jsonify(error="সেটিংসের মান সঠিক সীমার মধ্যে দিন"),400
    c=db()
    try:
        c.execute("BEGIN IMMEDIATE")
        for k,v in (("reward_amount",reward),("daily_limit",limit),("min_withdraw",minimum)):
            c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",(k,str(v)))
        c.commit()
        return jsonify(success=True,message="সেটিংস সংরক্ষণ হয়েছে")
    except Exception:
        c.rollback(); app.logger.exception("Settings update failed")
        return jsonify(error="সেটিংস সংরক্ষণ করা যায়নি"),500
    finally: c.close()

# Telegram commands
def tg_blocked(uid): return is_blocked(uid)

async def start(update:Update,context:ContextTypes.DEFAULT_TYPE):
    t=update.effective_user
    if not t or not update.message: return
    if tg_blocked(t.id):
        await update.message.reply_text("আপনার অ্যাকাউন্টটি সাময়িকভাবে বন্ধ করা হয়েছে।")
        return
    ref=None
    if context.args:
        try:
            x=int(context.args[0])
            if x!=t.id: ref=x
        except ValueError: pass
    add_user(t.id,t.username,t.first_name,ref)
    kb=[[InlineKeyboardButton("অ্যাপ খুলুন",web_app=WebAppInfo(url=WEB_URL))]]
    await update.message.reply_text("স্বাগতম! বিজ্ঞাপন দেখে আয় করার অ্যাপটি খুলুন।",
                                     reply_markup=InlineKeyboardMarkup(kb))

async def balance(update:Update,context:ContextTypes.DEFAULT_TYPE):
    t=update.effective_user
    if not t: return
    if tg_blocked(t.id):
        await update.message.reply_text("আপনার অ্যাকাউন্টটি সাময়িকভাবে বন্ধ করা হয়েছে。"); return
    row=get_user(t.id); amount=float(row["balance"] or 0) if row else 0
    await update.message.reply_text(f"আপনার ব্যালেন্স: ৳{amount:.2f}")

async def withdraw(update:Update,context:ContextTypes.DEFAULT_TYPE):
    t=update.effective_user
    if t and tg_blocked(t.id):
        await update.message.reply_text("আপনার অ্যাকাউন্টটি সাময়িকভাবে বন্ধ করা হয়েছে।"); return
    await update.message.reply_text("উত্তোলনের জন্য Mini App খুলে Withdraw অপশন ব্যবহার করুন।")

def is_admin(update):
    return bool(update.effective_user and ADMIN_ID and update.effective_user.id==ADMIN_ID)

async def admin(update:Update,context:ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    await update.message.reply_text("Admin commands:\n/stats\n/setmin 50\n/setlimit 20\n/setreward 10\n/approve ID\n/reject ID\n/credit USER_ID AMOUNT\n/panel")

async def panel(update:Update,context:ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    kb=[[InlineKeyboardButton("🛠 Admin Panel খুলুন",web_app=WebAppInfo(url=WEB_URL.rstrip("/")+"/admin.html"))]]
    await update.message.reply_text("অ্যাডমিন প্যানেল খুলতে নিচের বাটনে চাপ দিন।",reply_markup=InlineKeyboardMarkup(kb))

async def stats(update:Update,context:ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    c=db()
    try:
        users=c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
        pending=c.execute("SELECT COUNT(*) n FROM withdrawals WHERE status='pending'").fetchone()["n"]
    finally: c.close()
    await update.message.reply_text(f"মোট ইউজার: {users}\nপেন্ডিং উত্তোলন: {pending}")

async def setmin(update:Update,context:ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    try:
        v=float(context.args[0])
        if v<=0: raise ValueError
        set_setting("min_withdraw",v); await update.message.reply_text(f"সর্বনিম্ন উত্তোলন ৳{v} করা হয়েছে")
    except (IndexError,ValueError): await update.message.reply_text("ব্যবহার: /setmin 50")

async def setlimit(update:Update,context:ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    try:
        v=int(context.args[0])
        if v<=0: raise ValueError
        set_setting("daily_limit",v); await update.message.reply_text(f"দৈনিক বিজ্ঞাপন সীমা {v} করা হয়েছে")
    except (IndexError,ValueError): await update.message.reply_text("ব্যবহার: /setlimit 20")

async def setreward(update:Update,context:ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    try:
        v=round(float(context.args[0]),2)
        if v<=0: raise ValueError
        set_setting("reward_amount",v); await update.message.reply_text(f"প্রতি বিজ্ঞাপনের পুরস্কার ৳{v} করা হয়েছে")
    except (IndexError,ValueError): await update.message.reply_text("ব্যবহার: /setreward 10")

async def credit(update:Update,context:ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    try:
        uid=int(context.args[0]); amount=round(float(context.args[1]),2)
        if amount<=0: raise ValueError
    except (IndexError,ValueError):
        await update.message.reply_text("ব্যবহার: /credit USER_ID AMOUNT"); return
    c=db()
    try:
        cur=c.execute("UPDATE users SET balance=balance+? WHERE user_id=?",(amount,uid)); c.commit()
        changed=cur.rowcount
    finally: c.close()
    await update.message.reply_text("ব্যালেন্স আপডেট করা হয়েছে।" if changed else "ইউজার পাওয়া যায়নি।")

async def approve(update:Update,context:ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    try: wid=int(context.args[0])
    except (IndexError,ValueError):
        await update.message.reply_text("ব্যবহার: /approve ID"); return
    try: ok,msg=withdrawal_action(wid,"approve")
    except Exception:
        app.logger.exception("Approve failed"); await update.message.reply_text("অনুরোধ সম্পন্ন করা যায়নি।"); return
    await update.message.reply_text(msg if ok else msg)

async def reject(update:Update,context:ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    try: wid=int(context.args[0])
    except (IndexError,ValueError):
        await update.message.reply_text("ব্যবহার: /reject ID"); return
    try: ok,msg=withdrawal_action(wid,"reject")
    except Exception:
        app.logger.exception("Reject failed"); await update.message.reply_text("অনুরোধ বাতিল করা যায়নি।"); return
    await update.message.reply_text(msg)

def run_web():
    app.run(host="0.0.0.0",port=PORT,use_reloader=False)

async def post_init(application):
    await application.bot.delete_webhook(drop_pending_updates=False)

def main():
    init_db()
    if not BOT_TOKEN: raise RuntimeError("BOT_TOKEN / TELEGRAM_BOT_TOKEN environment variable is missing")
    if not POSTBACK_SECRET: raise RuntimeError("POSTBACK_SECRET environment variable is missing")
    threading.Thread(target=run_web,daemon=True).start()
    application=Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    for name,fn in [
        ("start",start),("balance",balance),("withdraw",withdraw),("admin",admin),
        ("panel",panel),("stats",stats),("setmin",setmin),("setlimit",setlimit),
        ("setreward",setreward),("credit",credit),("approve",approve),("reject",reject)]:
        application.add_handler(CommandHandler(name,fn))
    application.run_polling()

if __name__=="__main__": main()
