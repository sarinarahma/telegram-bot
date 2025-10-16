import os
import logging
import sqlite3
import hashlib
import hmac
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import uvicorn
import requests
import asyncio
from threading import Thread

# Logging setup
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# ============== CONFIGURATION ==============
# Setup di environment variables atau .env file
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8444255228:AAFMmpvAClPg24FL5FKvey9dWDPUfGS7mSQ")
MIDTRANS_SERVER_KEY = os.getenv("MIDTRANS_SERVER_KEY", "Mid-server-_bC43xeJO6EKwXmxe1jQT2_p")
MIDTRANS_CLIENT_KEY = os.getenv("MIDTRANS_CLIENT_KEY", "Mid-client-BU_MAFeF7t2Bf20p")
MIDTRANS_IS_PRODUCTION = os.getenv("MIDTRANS_IS_PRODUCTION", "false").lower() == "true"
ADMIN_TELEGRAM_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "123456789").split(",")]
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "https://your-app.railway.app")  # URL hosting kamu

# Midtrans API URLs
MIDTRANS_BASE_URL = "https://api.midtrans.com" if MIDTRANS_IS_PRODUCTION else "https://api.sandbox.midtrans.com"

# ============== DATABASE SETUP ==============
def init_db():
    conn = sqlite3.connect('bot_database.db')
    c = conn.cursor()
    
    # Products table
    c.execute('''CREATE TABLE IF NOT EXISTS products
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  name TEXT NOT NULL,
                  description TEXT,
                  price INTEGER NOT NULL,
                  stock INTEGER DEFAULT -1,
                  product_data TEXT,
                  active INTEGER DEFAULT 1)''')
    
    # Orders table
    c.execute('''CREATE TABLE IF NOT EXISTS orders
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  order_id TEXT UNIQUE NOT NULL,
                  user_id INTEGER NOT NULL,
                  username TEXT,
                  product_id INTEGER NOT NULL,
                  amount INTEGER NOT NULL,
                  status TEXT DEFAULT 'pending',
                  qris_url TEXT,
                  created_at TEXT,
                  paid_at TEXT,
                  expires_at TEXT,
                  FOREIGN KEY (product_id) REFERENCES products(id))''')
    
    conn.commit()
    conn.close()

# ============== DATABASE FUNCTIONS ==============
def get_db():
    return sqlite3.connect('bot_database.db')

def get_all_products():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM products WHERE active = 1")
    products = c.fetchall()
    conn.close()
    return products

def get_product(product_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM products WHERE id = ?", (product_id,))
    product = c.fetchone()
    conn.close()
    return product

def create_order(user_id, username, product_id, amount):
    conn = get_db()
    c = conn.cursor()
    order_id = f"ORDER-{user_id}-{int(datetime.now().timestamp())}"
    created_at = datetime.now().isoformat()
    expires_at = (datetime.now() + timedelta(minutes=15)).isoformat()
    
    c.execute("""INSERT INTO orders (order_id, user_id, username, product_id, amount, created_at, expires_at)
                 VALUES (?, ?, ?, ?, ?, ?, ?)""",
              (order_id, user_id, username, product_id, amount, created_at, expires_at))
    conn.commit()
    conn.close()
    return order_id

def update_order_payment(order_id, qris_url):
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE orders SET qris_url = ? WHERE order_id = ?", (qris_url, order_id))
    conn.commit()
    conn.close()

def get_order(order_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,))
    order = c.fetchone()
    conn.close()
    return order

def complete_order(order_id):
    conn = get_db()
    c = conn.cursor()
    paid_at = datetime.now().isoformat()
    c.execute("UPDATE orders SET status = 'paid', paid_at = ? WHERE order_id = ?", (paid_at, order_id))
    conn.commit()
    conn.close()

def add_product(name, description, price, stock, product_data):
    conn = get_db()
    c = conn.cursor()
    c.execute("""INSERT INTO products (name, description, price, stock, product_data)
                 VALUES (?, ?, ?, ?, ?)""",
              (name, description, price, stock, product_data))
    conn.commit()
    conn.close()

# ============== MIDTRANS FUNCTIONS ==============
def create_midtrans_transaction(order_id, amount, customer_name):
    url = f"{MIDTRANS_BASE_URL}/v2/charge"
    
    payload = {
        "payment_type": "qris",
        "transaction_details": {
            "order_id": order_id,
            "gross_amount": amount
        },
        "customer_details": {
            "first_name": customer_name,
        },
        "qris": {
            "acquirer": "gopay"
        }
    }
    
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Basic {get_midtrans_auth()}"
    }
    
    try:
        response = requests.post(url, json=payload, headers=headers)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logger.error(f"Midtrans API error: {e}")
        return None

def get_midtrans_auth():
    import base64
    auth_string = f"{MIDTRANS_SERVER_KEY}:"
    return base64.b64encode(auth_string.encode()).decode()

def verify_midtrans_signature(order_id, status_code, gross_amount, server_key):
    """Verify webhook signature from Midtrans"""
    signature_string = f"{order_id}{status_code}{gross_amount}{server_key}"
    return hashlib.sha512(signature_string.encode()).hexdigest()

# ============== TELEGRAM BOT HANDLERS ==============
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🛍️ Lihat Produk", callback_data="show_products")],
        [InlineKeyboardButton("📋 Pesanan Saya", callback_data="my_orders")],
        [InlineKeyboardButton("ℹ️ Bantuan", callback_data="help")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    welcome_text = """
🤖 *Selamat Datang di Auto Order Bot!*

Bot ini menyediakan pembelian produk digital otomatis dengan pembayaran QRIS.

✅ Pembayaran via QRIS (semua e-wallet & bank)
✅ Verifikasi otomatis & instant delivery
✅ Aktif 24/7

Silakan pilih menu di bawah:
    """
    
    await update.message.reply_text(welcome_text, parse_mode='Markdown', reply_markup=reply_markup)

async def show_products(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    products = get_all_products()
    
    if not products:
        await query.edit_message_text("Maaf, belum ada produk tersedia saat ini.")
        return
    
    keyboard = []
    for product in products:
        product_id, name, desc, price, stock, _, _ = product
        stock_text = f"(Stok: {stock})" if stock > 0 else "(Stok: ∞)"
        button_text = f"{name} - Rp {price:,} {stock_text}"
        keyboard.append([InlineKeyboardButton(button_text, callback_data=f"product_{product_id}")])
    
    keyboard.append([InlineKeyboardButton("« Kembali", callback_data="back_to_menu")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text("📦 *Pilih Produk:*", parse_mode='Markdown', reply_markup=reply_markup)

async def show_product_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    product_id = int(query.data.split("_")[1])
    product = get_product(product_id)
    
    if not product:
        await query.edit_message_text("Produk tidak ditemukan.")
        return
    
    _, name, desc, price, stock, _, _ = product
    
    stock_text = f"Stok: {stock}" if stock > 0 else "Stok: Unlimited"
    
    text = f"""
📦 *{name}*

{desc}

💰 Harga: Rp {price:,}
📊 {stock_text}

Klik tombol di bawah untuk membeli:
    """
    
    keyboard = [
        [InlineKeyboardButton("🛒 Beli Sekarang", callback_data=f"buy_{product_id}")],
        [InlineKeyboardButton("« Kembali", callback_data="show_products")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(text, parse_mode='Markdown', reply_markup=reply_markup)

async def process_purchase(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    product_id = int(query.data.split("_")[1])
    product = get_product(product_id)
    
    if not product:
        await query.edit_message_text("Produk tidak ditemukan.")
        return
    
    _, name, desc, price, stock, product_data, _ = product
    
    # Check stock
    if stock == 0:
        await query.edit_message_text("Maaf, produk habis.")
        return
    
    user_id = query.from_user.id
    username = query.from_user.username or query.from_user.first_name
    
    # Create order
    order_id = create_order(user_id, username, product_id, price)
    
    # Create Midtrans transaction
    midtrans_response = create_midtrans_transaction(order_id, price, username)
    
    if not midtrans_response or midtrans_response.get("status_code") != "201":
        await query.edit_message_text("⚠️ Gagal membuat pembayaran. Silakan coba lagi.")
        return
    
    # Get QRIS URL
    qris_url = midtrans_response.get("actions", [{}])[0].get("url", "")
    
    # Update order with QRIS
    update_order_payment(order_id, qris_url)
    
    text = f"""
✅ *Pesanan Dibuat!*

📦 Produk: {name}
💰 Total: Rp {price:,}
🆔 Order ID: `{order_id}`

⏰ Bayar dalam 15 menit

Scan QRIS di bawah atau klik tombol untuk membuka QRIS:
    """
    
    keyboard = [
        [InlineKeyboardButton("📱 Buka QRIS", url=qris_url)],
        [InlineKeyboardButton("« Kembali", callback_data="show_products")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(text, parse_mode='Markdown', reply_markup=reply_markup)

async def back_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    keyboard = [
        [InlineKeyboardButton("🛍️ Lihat Produk", callback_data="show_products")],
        [InlineKeyboardButton("📋 Pesanan Saya", callback_data="my_orders")],
        [InlineKeyboardButton("ℹ️ Bantuan", callback_data="help")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text("Pilih menu:", reply_markup=reply_markup)

async def my_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    await query.edit_message_text("Fitur pesanan saya sedang dalam pengembangan.")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    text = """
ℹ️ *Bantuan*

Cara order:
1. Pilih produk
2. Klik "Beli Sekarang"
3. Scan QRIS & bayar
4. Produk otomatis dikirim setelah pembayaran

⏰ Pembayaran valid 15 menit

💬 Kontak admin: @youradmin
    """
    
    keyboard = [[InlineKeyboardButton("« Kembali", callback_data="back_to_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(text, parse_mode='Markdown', reply_markup=reply_markup)

# Admin commands
async def admin_add_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_TELEGRAM_IDS:
        await update.message.reply_text("⛔ Unauthorized")
        return
    
    text = """
➕ *Tambah Produk*

Format:
/addproduct Nama|Deskripsi|Harga|Stok|Data

Contoh:
/addproduct Netflix Premium|Akun Netflix 1 bulan|50000|10|email:pass

Stok -1 untuk unlimited
    """
    await update.message.reply_text(text, parse_mode='Markdown')

async def process_add_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_TELEGRAM_IDS:
        return
    
    try:
        data = update.message.text.replace("/addproduct ", "").split("|")
        name, desc, price, stock, product_data = data
        
        add_product(name.strip(), desc.strip(), int(price), int(stock), product_data.strip())
        await update.message.reply_text("✅ Produk berhasil ditambahkan!")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

# ============== FASTAPI WEBHOOK ==============
app = FastAPI()

@app.post("/webhook/midtrans")
async def midtrans_webhook(request: Request):
    try:
        data = await request.json()
        
        order_id = data.get("order_id")
        status_code = data.get("status_code")
        gross_amount = data.get("gross_amount")
        signature_key = data.get("signature_key")
        transaction_status = data.get("transaction_status")
        
        # Verify signature
        expected_signature = verify_midtrans_signature(
            order_id, status_code, gross_amount, MIDTRANS_SERVER_KEY
        )
        
        if signature_key != expected_signature:
            raise HTTPException(status_code=403, detail="Invalid signature")
        
        # Process payment
        if transaction_status in ["capture", "settlement"]:
            order = get_order(order_id)
            if order and order[6] == "pending":  # status column
                complete_order(order_id)
                
                # Send product to user
                user_id = order[2]
                product_id = order[4]
                product = get_product(product_id)
                
                if product:
                    product_data = product[5]
                    message = f"""
✅ *Pembayaran Berhasil!*

Terima kasih telah berbelanja!

📦 Produk: {product[1]}
🎁 Data Produk:
`{product_data}`

Order ID: `{order_id}`
                    """
                    
                    # Send to user via bot
                    bot = context.application.bot
                    await bot.send_message(chat_id=user_id, text=message, parse_mode='Markdown')
        
        return JSONResponse({"status": "ok"})
    
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/")
async def root():
    return {"status": "Bot is running"}

# ============== MAIN ==============
def run_fastapi():
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)

async def main():
    # Initialize database
    init_db()
    
    # Create bot application
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("addproduct", process_add_product))
    application.add_handler(CallbackQueryHandler(show_products, pattern="^show_products$"))
    application.add_handler(CallbackQueryHandler(show_product_detail, pattern="^product_"))
    application.add_handler(CallbackQueryHandler(process_purchase, pattern="^buy_"))
    application.add_handler(CallbackQueryHandler(back_to_menu, pattern="^back_to_menu$"))
    application.add_handler(CallbackQueryHandler(my_orders, pattern="^my_orders$"))
    application.add_handler(CallbackQueryHandler(help_command, pattern="^help$"))
    
    # Store bot in app state for webhook
    app.state.bot = application.bot
    
    # Run FastAPI in separate thread
    fastapi_thread = Thread(target=run_fastapi, daemon=True)
    fastapi_thread.start()
    
    # Start bot
    await application.run_polling()

if __name__ == "__main__":
    asyncio.run(main())