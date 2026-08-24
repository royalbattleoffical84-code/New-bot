
import os, json, logging, asyncio, secrets, hmac, hashlib
from datetime import datetime
from decimal import Decimal, InvalidOperation
from contextlib import asynccontextmanager

import aiosqlite
import httpx
from fastapi import FastAPI, Request, HTTPException
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("sxo")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DB_PATH = os.getenv("DB_PATH", "sxo_store.db")
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", secrets.token_urlsafe(24))
DEFAULT_CURRENCY = "INR"

bot = Bot(BOT_TOKEN)
dp = Dispatcher()

# ---------- DB ----------
async def db():
    return await aiosqlite.connect(DB_PATH)

async def init_db():
    con = await db()
    await con.executescript("""
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS users(
      id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, lang TEXT DEFAULT 'hi',
      created_at TEXT DEFAULT CURRENT_TIMESTAMP, last_seen TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS staff(
      user_id INTEGER PRIMARY KEY, role TEXT NOT NULL, added_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS products(
      id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, description TEXT DEFAULT '',
      price REAL NOT NULL, file_id TEXT DEFAULT '', active INTEGER DEFAULT 1,
      created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS orders(
      id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, product_id INTEGER NOT NULL,
      amount REAL NOT NULL, status TEXT DEFAULT 'pending', gateway TEXT DEFAULT 'manual',
      gateway_order_id TEXT, gateway_payment_id TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
      paid_at TEXT
    );
    CREATE TABLE IF NOT EXISTS coupons(
      code TEXT PRIMARY KEY, kind TEXT NOT NULL, value REAL NOT NULL, uses INTEGER DEFAULT 0,
      max_uses INTEGER DEFAULT 0, active INTEGER DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS settings(
      key TEXT PRIMARY KEY, value TEXT DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS tickets(
      id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, text TEXT,
      status TEXT DEFAULT 'open', created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS broadcasts(
      id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT, sent INTEGER DEFAULT 0,
      failed INTEGER DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    """)
    await con.commit()
    await con.close()

async def q(sql, params=(), one=False, many=False):
    con = await db()
    cur = await con.execute(sql, params)
    rows = await cur.fetchall() if many or one else None
    await con.commit()
    await con.close()
    if one:
        return rows[0] if rows else None
    return rows

async def setting(key, default=""):
    r = await q("SELECT value FROM settings WHERE key=?", (key,), one=True)
    return r[0] if r else default

async def set_setting(key, value):
    await q("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

async def is_admin(uid): return uid in ADMIN_IDS or bool(await q("SELECT 1 FROM staff WHERE user_id=? AND role='owner'", (uid,), one=True))
async def is_staff(uid): return await is_admin(uid) or bool(await q("SELECT 1 FROM staff WHERE user_id=?", (uid,), one=True))

async def save_user(m: Message):
    u=m.from_user
    await q("""INSERT INTO users(id,username,first_name,last_seen) VALUES(?,?,?,CURRENT_TIMESTAMP)
              ON CONFLICT(id) DO UPDATE SET username=excluded.username,first_name=excluded.first_name,last_seen=CURRENT_TIMESTAMP""",
            (u.id,u.username or "",u.first_name or ""))

# ---------- keyboards ----------
def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t,d in row] for row in rows
    ])

def user_menu():
    return kb([
      [("🛍️ Products","u_products"),("📦 My Orders","u_orders")],
      [("🎟️ Coupon","u_coupon"),("👥 Referrals","u_ref")],
      [("💬 Support","u_support"),("🌐 Language","u_lang")],
    ])

def admin_menu():
    return kb([
      [("📊 Dashboard","a_dash"),("🛍️ Products","a_products")],
      [("📦 Orders","a_orders"),("👥 Users","a_users")],
      [("💳 Payment Gateway","a_gateway"),("🎟️ Coupons","a_coupons")],
      [("👨‍💼 Staff","a_staff"),("📣 Broadcast","a_broadcast")],
      [("⚙️ Settings","a_settings"),("🆘 Tickets","a_tickets")],
    ])

# ---------- user ----------
@dp.message(CommandStart())
async def start(m: Message):
    await save_user(m)
    if await is_staff(m.from_user.id):
        await m.answer("👑 <b>SXO Store Bot</b>\n\nAdmin/Staff control panel:", parse_mode="HTML", reply_markup=admin_menu())
    else:
        await m.answer("🛍️ <b>SXO Store Bot</b>\n\nDigital products खरीदें, order status देखें और support लें.", parse_mode="HTML", reply_markup=user_menu())

@dp.callback_query(F.data=="u_products")
async def products(c: CallbackQuery):
    rows=await q("SELECT id,name,price FROM products WHERE active=1 ORDER BY id DESC", many=True)
    if not rows: return await c.message.edit_text("अभी कोई product उपलब्ध नहीं है।", reply_markup=user_menu())
    buttons=[[(f"🛒 {n} — ₹{p:g}",f"p_{i}")] for i,n,p in rows]
    buttons.append([("⬅️ Back","u_home")])
    await c.message.edit_text("📦 <b>Products</b>",parse_mode="HTML",reply_markup=kb(buttons))

@dp.callback_query(F.data.startswith("p_"))
async def product_detail(c: CallbackQuery):
    pid=int(c.data.split("_")[1])
    r=await q("SELECT id,name,description,price FROM products WHERE id=? AND active=1",(pid,),one=True)
    if not r: return await c.answer("Product नहीं मिला",show_alert=True)
    i,n,d,p=r
    await c.message.edit_text(f"📦 <b>{n}</b>\n\n{d}\n\n💰 Price: ₹{p:g}",parse_mode="HTML",
        reply_markup=kb([[("💳 Buy Now",f"buy_{i}")],[("⬅️ Back","u_products")]]))

@dp.callback_query(F.data.startswith("buy_"))
async def buy(c: CallbackQuery):
    pid=int(c.data.split("_")[1])
    r=await q("SELECT name,price FROM products WHERE id=? AND active=1",(pid,),one=True)
    if not r: return await c.answer("Product नहीं मिला",show_alert=True)
    n,p=r
    oid=await q("INSERT INTO orders(user_id,product_id,amount,gateway) VALUES(?,?,?,'manual')",(c.from_user.id,pid,p))
    order_id=oid.lastrowid if hasattr(oid,"lastrowid") else (await q("SELECT last_insert_rowid()",one=True))[0]
    gw=await setting("gateway","manual")
    if gw=="razorpay":
        link=await create_razorpay_payment_link(order_id,n,p)
        if link:
            return await c.message.edit_text(f"💳 <b>Payment</b>\n\nOrder: <code>#{order_id}</code>\nAmount: ₹{p:g}\n\nPayment link खोलें और payment करें.",parse_mode="HTML",
              reply_markup=kb([[("💳 Pay Now",link)], [("🔄 Check Order","u_orders"),("⬅️ Back","u_products")]]))
    await c.message.edit_text(f"🧾 <b>Order #{order_id}</b>\n\nProduct: {n}\nAmount: ₹{p:g}\n\nPayment method admin द्वारा configure किया गया है। Support में payment proof भेजें.",parse_mode="HTML",
        reply_markup=kb([[("💬 Send Payment Proof","u_support")],[("📦 My Orders","u_orders")]]))

@dp.callback_query(F.data=="u_orders")
async def orders(c: CallbackQuery):
    rows=await q("""SELECT o.id,p.name,o.amount,o.status,o.created_at FROM orders o JOIN products p ON p.id=o.product_id
                    WHERE o.user_id=? ORDER BY o.id DESC LIMIT 20""",(c.from_user.id,),many=True)
    if not rows: txt="📦 अभी कोई order नहीं है।"
    else:
        txt="📦 <b>My Orders</b>\n\n" + "\n".join(f"#{i} • {n} • ₹{a:g} • <b>{s}</b>" for i,n,a,s,_ in rows)
    await c.message.edit_text(txt,parse_mode="HTML",reply_markup=user_menu())

@dp.callback_query(F.data=="u_coupon")
async def coupon(c: CallbackQuery):
    await c.message.edit_text("🎟️ Coupon apply करने के लिए भेजें:\n<code>/coupon CODE</code>",parse_mode="HTML",reply_markup=kb([[("⬅️ Back","u_home")]]))

@dp.message(Command("coupon"))
async def coupon_cmd(m: Message):
    await save_user(m)
    parts=m.text.split(maxsplit=1)
    if len(parts)<2: return await m.answer("Format: /coupon CODE")
    r=await q("SELECT kind,value,uses,max_uses,active FROM coupons WHERE code=?",(parts[1].strip().upper(),),one=True)
    if not r or not r[4] or (r[3] and r[1]>=r[3]): return await m.answer("❌ Coupon invalid/expired.")
    await set_setting(f"coupon:{m.from_user.id}",parts[1].strip().upper())
    await m.answer(f"✅ Coupon <b>{parts[1].strip().upper()}</b> saved. Checkout पर लागू होगा.",parse_mode="HTML")

@dp.callback_query(F.data=="u_support")
async def support(c: CallbackQuery):
    await c.message.edit_text("💬 Support के लिए message भेजें।\n\nExample: <code>Order #12 payment issue</code>",parse_mode="HTML",reply_markup=kb([[("⬅️ Back","u_home")]]))

@dp.message()
async def general_message(m: Message):
    await save_user(m)
    # Admin input modes (including product document upload)
    mode=await setting(f"mode:{m.from_user.id}")
    if mode:
        if mode.startswith("await_file:") and m.document:
            pid=int(mode.split(":")[1])
            await q("UPDATE products SET file_id=? WHERE id=?", (m.document.file_id,pid))
            await set_setting(f"mode:{m.from_user.id}","")
            return await m.answer("✅ Product file saved. अब payment verify होने पर यही file auto-deliver होगी.", reply_markup=admin_menu())
        await handle_admin_input(m, mode)
        return
    if await is_staff(m.from_user.id):
        await m.answer("Admin panel:",reply_markup=admin_menu())
    else:
        # create support ticket
        await q("INSERT INTO tickets(user_id,text) VALUES(?,?)",(m.from_user.id,m.text or "[media]"))
        await m.answer("✅ आपका support message receive हो गया है। Staff जल्द reply करेगा.",reply_markup=user_menu())

# ---------- admin ----------
@dp.callback_query(F.data=="a_dash")
async def a_dash(c):
    users=(await q("SELECT COUNT(*) FROM users",one=True))[0]
    products=(await q("SELECT COUNT(*) FROM products WHERE active=1",one=True))[0]
    orders=(await q("SELECT COUNT(*) FROM orders",one=True))[0]
    revenue=(await q("SELECT COALESCE(SUM(amount),0) FROM orders WHERE status='paid'",one=True))[0]
    await c.message.edit_text(f"📊 <b>SXO Dashboard</b>\n\n👥 Users: {users}\n🛍️ Active Products: {products}\n📦 Orders: {orders}\n💰 Paid Revenue: ₹{revenue:g}",parse_mode="HTML",reply_markup=admin_menu())

@dp.callback_query(F.data=="a_products")
async def a_products(c):
    rows=await q("SELECT id,name,price,active FROM products ORDER BY id DESC",many=True)
    btn=[[(f"{'🟢' if a else '🔴'} {n} — ₹{p:g}",f"ae_{i}")] for i,n,p,a in rows]
    btn += [[("➕ Add Product","add_product")],[("⬅️ Admin","a_home")]]
    await c.message.edit_text("🛍️ <b>Product Manager</b>",parse_mode="HTML",reply_markup=kb(btn))

@dp.callback_query(F.data=="add_product")
async def add_product(c):
    await set_setting(f"mode:{c.from_user.id}","add_product")
    await c.message.edit_text("➕ Product add करने के लिए एक line में भेजें:\n\n<code>Name | Price | Description</code>\n\nफिर file/document भेजना optional है।",parse_mode="HTML")

@dp.callback_query(F.data.startswith("ae_"))
async def edit_product(c):
    pid=int(c.data.split("_")[1]); r=await q("SELECT name,price,active FROM products WHERE id=?",(pid,),one=True)
    if not r:return
    n,p,a=r
    await c.message.edit_text(f"🛍️ <b>{n}</b>\n₹{p:g}\nStatus: {'Active' if a else 'Hidden'}",parse_mode="HTML",
        reply_markup=kb([[("🔄 Toggle","toggle_"+str(pid))],[("🗑️ Delete","del_"+str(pid))],[("⬅️ Back","a_products")]]))

@dp.callback_query(F.data.startswith("toggle_"))
async def toggle_product(c):
    pid=int(c.data.split("_")[1]); await q("UPDATE products SET active=1-active WHERE id=?",(pid,)); await a_products(c)

@dp.callback_query(F.data.startswith("del_"))
async def del_product(c):
    pid=int(c.data.split("_")[1]); await q("DELETE FROM products WHERE id=?",(pid,)); await a_products(c)

@dp.callback_query(F.data=="a_orders")
async def a_orders(c):
    rows=await q("""SELECT o.id,u.id,p.name,o.amount,o.status FROM orders o JOIN users u ON u.id=o.user_id
                    JOIN products p ON p.id=o.product_id ORDER BY o.id DESC LIMIT 30""",many=True)
    btn=[[(f"#{i} • {n} • ₹{a:g} • {s}",f"ord_{i}")] for i,uid,n,a,s in rows]
    btn.append([("⬅️ Admin","a_home")])
    await c.message.edit_text("📦 <b>Orders</b>",parse_mode="HTML",reply_markup=kb(btn or [[("⬅️ Admin","a_home")]]))

@dp.callback_query(F.data.startswith("ord_"))
async def order_detail(c):
    oid=int(c.data.split("_")[1]); r=await q("""SELECT o.user_id,p.name,o.amount,o.status,o.gateway_payment_id FROM orders o
      JOIN products p ON p.id=o.product_id WHERE o.id=?""",(oid,),one=True)
    if not r:return
    uid,n,a,s,pay=r
    await c.message.edit_text(f"📦 <b>Order #{oid}</b>\nUser: <code>{uid}</code>\nProduct: {n}\nAmount: ₹{a:g}\nStatus: {s}\nPayment ID: {pay or '-'}",
      parse_mode="HTML",reply_markup=kb([[("✅ Mark Paid",f"paid_{oid}"),("❌ Cancel",f"cancel_{oid}")],[("⬅️ Orders","a_orders")]]))

@dp.callback_query(F.data.startswith("paid_"))
async def mark_paid(c):
    oid=int(c.data.split("_")[1]); await fulfill_order(oid); await c.answer("Paid")
    await order_detail(c)

@dp.callback_query(F.data.startswith("cancel_"))
async def cancel(c):
    oid=int(c.data.split("_")[1]); await q("UPDATE orders SET status='cancelled' WHERE id=?",(oid,)); await c.answer("Cancelled"); await order_detail(c)

@dp.callback_query(F.data=="a_users")
async def a_users(c):
    r=(await q("SELECT COUNT(*) FROM users",one=True))[0]
    await c.message.edit_text(f"👥 <b>User Manager</b>\n\nTotal users: {r}\n\nUser actions can be added through staff workflow.",parse_mode="HTML",reply_markup=admin_menu())

@dp.callback_query(F.data=="a_gateway")
async def a_gateway(c):
    gw=await setting("gateway","manual")
    await c.message.edit_text(f"💳 <b>Payment Gateway</b>\n\nCurrent: <b>{gw}</b>\n\nChoose gateway and then enter credentials. Secrets are stored locally in DB; use environment encryption/secret management for production.",
      parse_mode="HTML",reply_markup=kb([[("🟢 Razorpay","gw_razorpay"),("🟡 Manual UPI","gw_manual")],[("⬅️ Admin","a_home")]]))

@dp.callback_query(F.data.startswith("gw_"))
async def choose_gateway(c):
    gw=c.data.split("_",1)[1]; await set_setting("gateway",gw)
    if gw=="manual":
        await c.message.edit_text("🟡 Manual UPI selected.\nAdmin payment verification will be manual.",reply_markup=admin_menu())
    else:
        await set_setting(f"mode:{c.from_user.id}","gateway_razorpay")
        await c.message.edit_text("Razorpay setup:\n\nSend one line:\n<code>KEY_ID | KEY_SECRET</code>",parse_mode="HTML")

@dp.callback_query(F.data=="a_coupons")
async def a_coupons(c):
    rows=await q("SELECT code,kind,value,uses,max_uses,active FROM coupons ORDER BY code",many=True)
    txt="🎟️ <b>Coupons</b>\n\n" + ("\n".join(f"{code} • {kind} {v:g} • {uses}/{mx or '∞'}" for code,kind,v,uses,mx,a in rows) if rows else "No coupons.")
    await c.message.edit_text(txt,parse_mode="HTML",reply_markup=kb([[("➕ Add Coupon","add_coupon")],[("⬅️ Admin","a_home")]]))

@dp.callback_query(F.data=="add_coupon")
async def add_coupon(c):
    await set_setting(f"mode:{c.from_user.id}","add_coupon")
    await c.message.edit_text("Format:\n<code>CODE | percent/flat | VALUE | MAX_USES</code>",parse_mode="HTML")

@dp.callback_query(F.data=="a_staff")
async def a_staff(c):
    rows=await q("SELECT user_id,role FROM staff ORDER BY user_id",many=True)
    txt="👨‍💼 <b>Staff</b>\n\n"+("\n".join(f"<code>{u}</code> — {r}" for u,r in rows) if rows else "No extra staff.")
    await c.message.edit_text(txt,parse_mode="HTML",reply_markup=kb([[("➕ Add Staff","add_staff")],[("➖ Remove Staff","remove_staff")],[("⬅️ Admin","a_home")]]))

@dp.callback_query(F.data=="add_staff")
async def add_staff(c):
    await set_setting(f"mode:{c.from_user.id}","add_staff")
    await c.message.edit_text("Send:\n<code>TELEGRAM_USER_ID | manager/support</code>",parse_mode="HTML")

@dp.callback_query(F.data=="remove_staff")
async def remove_staff(c):
    await set_setting(f"mode:{c.from_user.id}","remove_staff")
    await c.message.edit_text("Staff का Telegram User ID भेजें।",reply_markup=admin_menu())

@dp.callback_query(F.data=="a_broadcast")
async def a_broadcast(c):
    await set_setting(f"mode:{c.from_user.id}","broadcast")
    await c.message.edit_text("📣 Broadcast text भेजें। Bot सभी registered users को भेजेगा।")

@dp.callback_query(F.data=="a_settings")
async def a_settings(c):
    brand=await setting("brand","SXO Store Bot")
    support=await setting("support_username","")
    await c.message.edit_text(f"⚙️ <b>Settings</b>\n\nBrand: {brand}\nSupport: {support or '-'}",
      parse_mode="HTML",reply_markup=kb([[("✏️ Edit Brand","set_brand"),("✏️ Support","set_support")],[("⬅️ Admin","a_home")]]))

@dp.callback_query(F.data=="set_brand")
async def set_brand(c):
    await set_setting(f"mode:{c.from_user.id}","set_brand"); await c.message.edit_text("New bot brand/name भेजें।")

@dp.callback_query(F.data=="set_support")
async def set_support(c):
    await set_setting(f"mode:{c.from_user.id}","set_support"); await c.message.edit_text("Support username/link भेजें।")

@dp.callback_query(F.data=="a_tickets")
async def a_tickets(c):
    rows=await q("SELECT id,user_id,text,status FROM tickets WHERE status='open' ORDER BY id DESC LIMIT 20",many=True)
    btn=[[(f"🎫 #{i} • {uid} • {text[:24]}",f"tic_{i}")] for i,uid,text,s in rows]
    btn.append([("⬅️ Admin","a_home")])
    await c.message.edit_text("🆘 <b>Open Tickets</b>",parse_mode="HTML",reply_markup=kb(btn))

@dp.callback_query(F.data.startswith("tic_"))
async def ticket(c):
    tid=int(c.data.split("_")[1]); r=await q("SELECT user_id,text FROM tickets WHERE id=?",(tid,),one=True)
    if not r:return
    uid,text=r
    await c.message.edit_text(f"🎫 Ticket #{tid}\nUser: <code>{uid}</code>\n\n{text}",parse_mode="HTML",
      reply_markup=kb([[("✉️ Reply","reply_"+str(tid)),("✅ Close",f"close_{tid}")],[("⬅️ Tickets","a_tickets")]]))

@dp.callback_query(F.data.startswith("reply_"))
async def reply_ticket(c):
    tid=int(c.data.split("_")[1]); await set_setting(f"mode:{c.from_user.id}",f"reply_ticket:{tid}")
    await c.message.edit_text("User को भेजने वाला reply लिखें।")

@dp.callback_query(F.data.startswith("close_"))
async def close_ticket(c):
    tid=int(c.data.split("_")[1]); await q("UPDATE tickets SET status='closed' WHERE id=?",(tid,)); await a_tickets(c)

@dp.callback_query(F.data=="a_home")
async def a_home(c): await c.message.edit_text("👑 <b>SXO Store Bot Admin</b>",parse_mode="HTML",reply_markup=admin_menu())
@dp.callback_query(F.data=="u_home")
async def u_home(c): await c.message.edit_text("🛍️ <b>SXO Store Bot</b>",parse_mode="HTML",reply_markup=user_menu())

# ---------- admin text workflows ----------
async def handle_admin_input(m, mode):
    await set_setting(f"mode:{m.from_user.id}","")
    try:
        if mode=="add_product":
            parts=[x.strip() for x in (m.text or "").split("|",2)]
            if len(parts)<3: raise ValueError("Use Name | Price | Description")
            name,price,desc=parts; price=float(price)
            await q("INSERT INTO products(name,price,description) VALUES(?,?,?)",(name,price,desc))
            r=await q("SELECT id FROM products WHERE name=? ORDER BY id DESC LIMIT 1",(name,),one=True)
            pid=r[0]
            await set_setting(f"mode:{m.from_user.id}",f"await_file:{pid}")
            return await m.answer("✅ Product added. अब उसी product की APK/ZIP/PDF/file Telegram में भेजें, मैं उसका file_id save कर दूँगा.",reply_markup=admin_menu())
        if mode=="gateway_razorpay":
            parts=[x.strip() for x in (m.text or "").split("|",1)]
            if len(parts)!=2: raise ValueError("Use KEY_ID | KEY_SECRET")
            await set_setting("razorpay_key_id",parts[0]); await set_setting("razorpay_key_secret",parts[1])
            return await m.answer("✅ Razorpay configured.",reply_markup=admin_menu())
        if mode=="add_coupon":
            p=[x.strip() for x in (m.text or "").split("|")]
            if len(p)!=4: raise ValueError("CODE | percent/flat | VALUE | MAX_USES")
            await q("INSERT OR REPLACE INTO coupons(code,kind,value,max_uses) VALUES(?,?,?,?)",(p[0].upper(),p[1],float(p[2]),int(p[3])))
            return await m.answer("✅ Coupon added.",reply_markup=admin_menu())
        if mode=="add_staff":
            p=[x.strip() for x in (m.text or "").split("|")]
            await q("INSERT OR REPLACE INTO staff(user_id,role) VALUES(?,?)",(int(p[0]),p[1]))
            return await m.answer("✅ Staff added.",reply_markup=admin_menu())
        if mode=="remove_staff":
            await q("DELETE FROM staff WHERE user_id=?",(int(m.text.strip()),)); return await m.answer("✅ Staff removed.",reply_markup=admin_menu())
        if mode=="set_brand":
            await set_setting("brand",m.text.strip()); return await m.answer("✅ Brand updated.",reply_markup=admin_menu())
        if mode=="set_support":
            await set_setting("support_username",m.text.strip()); return await m.answer("✅ Support updated.",reply_markup=admin_menu())
        if mode=="broadcast":
            rows=await q("SELECT id FROM users",many=True); sent=failed=0
            for (uid,) in rows:
                try: await bot.send_message(uid,m.text); sent+=1
                except Exception: failed+=1
                await asyncio.sleep(.03)
            return await m.answer(f"📣 Done.\nSent: {sent}\nFailed: {failed}",reply_markup=admin_menu())
        if mode.startswith("reply_ticket:"):
            tid=int(mode.split(":")[1]); r=await q("SELECT user_id FROM tickets WHERE id=?",(tid,),one=True)
            if r:
                await bot.send_message(r[0],f"💬 <b>Support Reply</b>\n\n{m.text}",parse_mode="HTML")
                await q("UPDATE tickets SET status='closed' WHERE id=?",(tid,))
            return await m.answer("✅ Reply sent.",reply_markup=admin_menu())
    except (ValueError, InvalidOperation) as e:
        await m.answer(f"❌ Format error: {e}")
    except Exception as e:
        log.exception(e); await m.answer("❌ Operation failed.")

# ---------- payment ----------
async def create_razorpay_payment_link(order_id, name, amount):
    key=await setting("razorpay_key_id"); secret=await setting("razorpay_key_secret")
    if not key or not secret: return None
    payload={"amount":int(round(amount*100)),"currency":"INR","description":name,
             "reference_id":f"SXO-{order_id}","expire_by":None,"notify":{"sms":False,"email":False}}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r=await client.post("https://api.razorpay.com/v1/payment_links",auth=(key,secret),json=payload)
            r.raise_for_status(); return r.json().get("short_url")
    except Exception as e:
        log.error("Razorpay: %s",e); return None

async def fulfill_order(oid):
    r=await q("SELECT user_id,product_id FROM orders WHERE id=?",(oid,),one=True)
    if not r:return
    uid,pid=r
    await q("UPDATE orders SET status='paid',paid_at=CURRENT_TIMESTAMP WHERE id=?",(oid,))
    file=await q("SELECT file_id,name FROM products WHERE id=?",(pid,),one=True)
    if file and file[0]:
        try: await bot.send_document(uid,file[0],caption=f"✅ Payment verified!\n\n📦 {file[1]}\nOrder #{oid}")
        except Exception: pass
    else:
        await bot.send_message(uid,f"✅ Order #{oid} paid.\n\nProduct delivery file अभी configured नहीं है; support से contact करें.")

# ---------- FastAPI webhook/health ----------
@asynccontextmanager
async def lifespan(app):
    await init_db()
    if PUBLIC_URL:
        await bot.set_webhook(f"{PUBLIC_URL}/telegram/webhook", secret_token=WEBHOOK_SECRET)
    else:
        asyncio.create_task(dp.start_polling(bot))
    yield
    if PUBLIC_URL:
        await bot.delete_webhook()
    await bot.session.close()

app=FastAPI(title="SXO Store Bot",lifespan=lifespan)

@app.get("/")
async def root(): return {"ok":True,"bot":"SXO Store Bot"}

@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        raise HTTPException(status_code=403)
    data=await request.json()
    from aiogram.types import Update
    await dp.feed_update(bot,Update.model_validate(data))
    return {"ok":True}

@app.post("/razorpay/webhook")
async def razorpay_webhook(request: Request):
    # Set RAZORPAY_WEBHOOK_SECRET in env and configure the same secret in Razorpay.
    secret=os.getenv("RAZORPAY_WEBHOOK_SECRET","")
    body=await request.body()
    sig=request.headers.get("X-Razorpay-Signature","")
    if secret and not hmac.compare_digest(sig,hmac.new(secret.encode(),body,hashlib.sha256).hexdigest()):
        raise HTTPException(status_code=403)
    data=json.loads(body)
    if data.get("event")=="payment_link.paid":
        pl=data["payload"]["payment_link"]["entity"]
        ref=pl.get("reference_id","")
        if ref.startswith("SXO-"):
            oid=int(ref[4:])
            await q("UPDATE orders SET status='paid',gateway_payment_id=? WHERE id=?",(data["payload"]["payment"]["entity"].get("id",""),oid))
            await fulfill_order(oid)
    return {"ok":True}

if __name__=="__main__":
    import uvicorn
    uvicorn.run(app,host="0.0.0.0",port=int(os.getenv("PORT","8000")))
