import sqlite3
import os
import re
import asyncio
import threading
from flask import Flask
from telethon import TelegramClient, events, Button
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError, MessageNotModifiedError
from telethon.tl.functions.messages import SendReactionRequest
from telethon.tl.types import ReactionEmoji

# ================= FLASK WEB SERVER FOR RENDER (FREE TIER) =================
app = Flask('')

@app.route('/')
def home():
    return "Bot is running fine on Render Free Tier!"

def run_web():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

threading.Thread(target=run_web, daemon=True).start()
# ===========================================================================

# Secret Credentials (loaded from environment variables - NEVER hardcode secrets in code)
API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

if not API_ID or not API_HASH or not BOT_TOKEN:
    raise RuntimeError(
        "Missing required environment variables: API_ID, API_HASH, BOT_TOKEN. "
        "Set these in your Render dashboard under Environment."
    )

# Fix for Python 3.14: event loop is no longer auto-created in the main thread,
# so we must create and set one explicitly before starting the Telethon client.
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

bot = TelegramClient('babban_bot_session', API_ID, API_HASH).start(bot_token=BOT_TOKEN)

DATABASE_NAME = 'bot_bayanai_v4.db'
login_state = {}
channel_selection_state = {}
channel_list_cache = {}
active_user_clients = {}
text_input_state = {}  # generic state machine for all the /commands that need follow-up text

PHONE_NUMBER_PATTERN = r'(\+234\d{10})|(\b0\d{10}\b)'
LINK_PATTERN = r'https?://\S+|www\.\S+'
USERNAME_PATTERN = r'@\w+'


# ---------------------------------------------------------------------------
# DATABASE
# ---------------------------------------------------------------------------
def setup_database():
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS masu_amfani (
            user_id INTEGER PRIMARY KEY,
            phone_number TEXT,
            session_string TEXT,
            source_channel TEXT,
            source_name TEXT,
            target_channel TEXT,
            target_name TEXT,
            forwarding_status INTEGER DEFAULT 0,
            header_status INTEGER DEFAULT 0,
            media_forwarding INTEGER DEFAULT 1,
            url_preview INTEGER DEFAULT 0,
            cire_links INTEGER DEFAULT 0,
            cire_usernames INTEGER DEFAULT 0,
            repeat_post INTEGER DEFAULT 0,
            auto_delete_msg INTEGER DEFAULT 0,
            link_auto_replies INTEGER DEFAULT 0,
            amazon_converter INTEGER DEFAULT 0,
            disable_links INTEGER DEFAULT 0,
            mono_text INTEGER DEFAULT 0,
            protected_forwards INTEGER DEFAULT 0,
            auto_reaction INTEGER DEFAULT 0,
            blacklist_keywords TEXT DEFAULT '',
            whitelist_keywords TEXT DEFAULT '',
            trim_words TEXT DEFAULT '',
            replace_links TEXT DEFAULT '',
            replace_usernames TEXT DEFAULT '',
            replace_words TEXT DEFAULT '',
            add_header TEXT DEFAULT '',
            add_footer TEXT DEFAULT '',
            target_delay INTEGER DEFAULT 0
        )
    ''')
    conn.commit()

    new_columns = [
        ("remove_phone_numbers", "INTEGER DEFAULT 0"),
        ("replace_phone_numbers", "TEXT DEFAULT ''"),
        ("auto_delete_seconds", "INTEGER DEFAULT 60"),
        ("auto_reply_text", "TEXT DEFAULT 'Check the link above 👆'"),
        ("auto_reaction_emoji", "TEXT DEFAULT '👍'"),
    ]
    for col_name, col_type in new_columns:
        try:
            cursor.execute(f"ALTER TABLE masu_amfani ADD COLUMN {col_name} {col_type}")
            conn.commit()
        except sqlite3.OperationalError:
            pass

    conn.close()

setup_database()


def get_session(user_id):
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT session_string FROM masu_amfani WHERE user_id=?", (user_id,))
    res = cursor.fetchone()
    conn.close()
    return res[0] if res and res[0] else None


def save_session(user_id, phone, session_str):
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO masu_amfani (user_id, phone_number, session_string)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET phone_number=?, session_string=?
    ''', (user_id, phone, session_str, phone, session_str))
    conn.commit()
    conn.close()


def update_channel(user_id, kind, chat_id, chat_name):
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    if kind == 'source':
        cursor.execute("UPDATE masu_amfani SET source_channel=?, source_name=? WHERE user_id=?", (str(chat_id), chat_name, user_id))
    elif kind == 'target':
        cursor.execute("UPDATE masu_amfani SET target_channel=?, target_name=? WHERE user_id=?", (str(chat_id), chat_name, user_id))
    conn.commit()
    conn.close()


def get_full_config(user_id):
    conn = sqlite3.connect(DATABASE_NAME)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM masu_amfani WHERE user_id=?", (user_id,))
    res = cursor.fetchone()
    conn.close()
    return res


def toggle_setting(user_id, col_name, force_value=None):
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    if force_value is not None:
        cursor.execute(f"UPDATE masu_amfani SET {col_name}=? WHERE user_id=?", (force_value, user_id))
    else:
        cursor.execute(f"SELECT {col_name} FROM masu_amfani WHERE user_id=?", (user_id,))
        res = cursor.fetchone()
        if res:
            new_value = 0 if res[0] == 1 else 1
            cursor.execute(f"UPDATE masu_amfani SET {col_name}=? WHERE user_id=?", (new_value, user_id))
    conn.commit()
    conn.close()


def set_text_field(user_id, col_name, value):
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    cursor.execute(f"UPDATE masu_amfani SET {col_name}=? WHERE user_id=?", (value, user_id))
    conn.commit()
    conn.close()


def remove_channel(user_id, kind):
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    if kind == 'source':
        cursor.execute("UPDATE masu_amfani SET source_channel='', source_name='Not set' WHERE user_id=?", (user_id,))
    elif kind == 'target':
        cursor.execute("UPDATE masu_amfani SET target_channel='', target_name='Not set' WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()


def reset_all_config(user_id):
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE masu_amfani SET
        source_channel='', source_name='Not set',
        target_channel='', target_name='Not set',
        forwarding_status=0, header_status=0, media_forwarding=1,
        url_preview=0, cire_links=0, cire_usernames=0, mono_text=0,
        repeat_post=0, auto_delete_msg=0, link_auto_replies=0, disable_links=0,
        protected_forwards=0, auto_reaction=0, remove_phone_numbers=0,
        blacklist_keywords='', whitelist_keywords='', trim_words='',
        replace_links='', replace_usernames='', replace_words='',
        replace_phone_numbers='', add_header='', add_footer='', target_delay=0
        WHERE user_id=?
    ''', (user_id,))
    conn.commit()
    conn.close()


def delete_user(user_id):
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM masu_amfani WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# TEXT / LIST HELPERS
# ---------------------------------------------------------------------------
def parse_csv_list(text):
    return [item.strip() for item in re.split(r'[,\n]', text) if item.strip()]


def merge_csv_list(existing, new_text):
    existing_items = parse_csv_list(existing) if existing else []
    for item in parse_csv_list(new_text):
        if item.lower() not in [e.lower() for e in existing_items]:
            existing_items.append(item)
    return ', '.join(existing_items)


def remove_from_csv_list(existing, remove_text):
    if remove_text.strip().lower() == 'all':
        return ''
    existing_items = parse_csv_list(existing) if existing else []
    remove_lower = [x.lower() for x in parse_csv_list(remove_text)]
    kept = [item for item in existing_items if item.lower() not in remove_lower]
    return ', '.join(kept)


def parse_pairs(text):
    pairs = []
    for line in text.splitlines():
        if '=>' in line:
            old, new = line.split('=>', 1)
            old, new = old.strip(), new.strip()
            if old:
                pairs.append((old, new))
    return pairs


def merge_pairs(existing, new_text):
    existing_pairs = {}
    if existing:
        for line in existing.splitlines():
            if '=>' in line:
                o, n = line.split('=>', 1)
                existing_pairs[o.strip()] = n.strip()
    for old, new in parse_pairs(new_text):
        existing_pairs[old] = new
    return '\n'.join(f"{o}=>{n}" for o, n in existing_pairs.items())


def apply_pairs(text, pairs_str):
    if not pairs_str or not text:
        return text
    for line in pairs_str.splitlines():
        if '=>' in line:
            old, new = line.split('=>', 1)
            old, new = old.strip(), new.strip()
            if old:
                text = text.replace(old, new)
    return text


def keyword_present(text, keywords_csv):
    if not keywords_csv or not text:
        return False
    keywords = parse_csv_list(keywords_csv)
    lowered = text.lower()
    return any(k.lower() in lowered for k in keywords)


def strip_links(text, replacement):
    return re.sub(LINK_PATTERN, replacement, text)


def strip_usernames(text, replacement):
    return re.sub(USERNAME_PATTERN, replacement, text)


def disable_link_clicks(text):
    # Insert a zero-width space right after the scheme so Telegram stops
    # auto-linking the URL, while the text itself stays human-readable.
    return re.sub(r'(https?://)', lambda m: m.group(1) + '\u200b', text)


def format_display(value, empty_label="Empty"):
    return f"`{value}`" if value else f"❌ [{empty_label}]"


# ---------------------------------------------------------------------------
# HOME / DASHBOARD (/start, /lconfig)
# ---------------------------------------------------------------------------
@bot.on(events.NewMessage(pattern='/start'))
async def start_cmd(event):
    user_id = event.sender_id
    res = get_full_config(user_id)
    if not res:
        conn = sqlite3.connect(DATABASE_NAME)
        cursor = conn.cursor()
        cursor.execute("INSERT OR IGNORE INTO masu_amfani (user_id) VALUES (?)", (user_id,))
        conn.commit()
        conn.close()
    await lconfig_cmd(event)


@bot.on(events.NewMessage(pattern='/lconfig'))
async def lconfig_cmd(event):
    user_id = event.sender_id
    res = get_full_config(user_id)

    src_name = res['source_name'] if res and res['source_name'] else 'Not set'
    trg_name = res['target_name'] if res and res['target_name'] else 'Not set'

    def status_icon(val):
        return "🟢 ON" if val == 1 else "🔴 OFF"

    f_status = status_icon(res['forwarding_status']) if res else "🔴 OFF"
    h_status = status_icon(res['header_status']) if res else "🔴 OFF"
    m_status = status_icon(res['media_forwarding']) if res else "🟢 ON"
    u_status = status_icon(res['url_preview']) if res else "🔴 OFF"
    l_status = status_icon(res['cire_links']) if res else "🔴 OFF"
    un_status = status_icon(res['cire_usernames']) if res else "🔴 OFF"
    ph_status = status_icon(res['remove_phone_numbers']) if res else "🔴 OFF"

    dashboard = f"""🛠️ **Your Current Configuration Settings**
━━━━━━━━━━━━━━━━━━━
📥 **Source Channel for Copying Posts**
   └─ • `{src_name}`

🎯 **Target Channel for Forwarding**
   └─ • `{trg_name}`

⚙️ **General Settings**
  ┌─ Forwarding Status: {f_status}
  ├─ Header Status: {h_status}
  ├─ Media Forwarding: {m_status}
  ├─ URL Preview: {u_status}
  ├─ Remove Links: {l_status}
  ├─ Remove Usernames: {un_status}
  └─ Remove Phone Numbers: {ph_status}

💬 **Help Center:** @musamk11
━━━━━━━━━━━━━━━━━━━
🛠️ **Full filters, replacements & advanced toggles:** /forward_settings
🚀 *Quick actions below:*"""

    keyboard = [
        [Button.inline("🚀 Start Login", b"fara_login"), Button.inline("❌ Logout Account", b"logout_acc")],
        [Button.inline("📥 Set Source", b"saita_source"), Button.inline("🎯 Set Target", b"saita_target")],
        [Button.inline("🔄 Forwarding Status", b"tog_forwarding_status"), Button.inline("🔼 Header Status", b"tog_header_status")],
        [Button.inline("🖼️ Media Forwarding", b"tog_media_forwarding"), Button.inline("🌐 URL Preview", b"tog_url_preview")],
        [Button.inline("🔗 Remove Links", b"tog_cire_links"), Button.inline("👤 Remove Usernames", b"tog_cire_usernames")],
        [Button.inline("📞 Remove Phone Numbers", b"tog_remove_phone_numbers")],
        [Button.inline("⚙️ Filters & Replacements", b"refresh_fset")],
        [Button.inline("📞 Contact Support", b"tuntuba_admin"), Button.inline("🔄 Refresh Menu", b"refresh_dashboard")]
    ]

    if isinstance(event, events.CallbackQuery.Event):
        try:
            await event.edit(dashboard, buttons=keyboard)
        except MessageNotModifiedError:
            pass
    else:
        await event.respond(dashboard, buttons=keyboard)


# ---------------------------------------------------------------------------
# FULL SETTINGS DASHBOARD (/forward_settings)
# ---------------------------------------------------------------------------
@bot.on(events.NewMessage(pattern='/forward_settings'))
async def forward_settings_cmd(event):
    user_id = event.sender_id
    res = get_full_config(user_id)
    if not res:
        await event.respond("❌ Please send /start first.")
        return

    def s(val):
        return "🟢 ON" if val == 1 else "🔴 OFF"

    text = f"""⚙️ **Message Forwarding Configuration Settings**
━━━━━━━━━━━━━━━━━━━
Header Status: {s(res['header_status'])}
Media Forwarding: {s(res['media_forwarding'])}
URL Preview: {s(res['url_preview'])}
Remove Links: {s(res['cire_links'])}
Remove Usernames: {s(res['cire_usernames'])}
Remove Phone Numbers: {s(res['remove_phone_numbers'])}
Repeat Post: {s(res['repeat_post'])}
Auto Delete Messages: {s(res['auto_delete_msg'])} (after {res['auto_delete_seconds']}s)
Link Auto Replies: {s(res['link_auto_replies'])}
Disable Links: {s(res['disable_links'])}
Mono Text: {s(res['mono_text'])}
Protected Forwards: {s(res['protected_forwards'])}
Auto Reaction: {s(res['auto_reaction'])} ({res['auto_reaction_emoji']})

👇 **Filters & Replacements**
Blacklist Keywords: {format_display(res['blacklist_keywords'])}
Whitelist Keywords: {format_display(res['whitelist_keywords'])}
Trim Words: {format_display(res['trim_words'])}
Replace Links: {format_display(res['replace_links'])}
Replace Usernames: {format_display(res['replace_usernames'])}
Replace Phone Numbers: {format_display(res['replace_phone_numbers'])}
Replace Words: {format_display(res['replace_words'])}
Add Header: {format_display(res['add_header'])}
Add Footer: {format_display(res['add_footer'])}
Target Delay Timer: [{res['target_delay']} Seconds]
━━━━━━━━━━━━━━━━━━━
🎛️ **Toggle switches:** /lconfig

👇 **Tap a button below to set or clear each filter — no typing commands needed:**"""

    keyboard = [
        [Button.inline("🚫 Blacklist", b"ask_blacklist"), Button.inline("🗑️ Clear", b"clr_blacklist")],
        [Button.inline("✅ Whitelist", b"ask_whitelist"), Button.inline("🗑️ Clear", b"clr_whitelist")],
        [Button.inline("✂️ Trim Words", b"ask_trim"), Button.inline("🗑️ Clear", b"clr_trim")],
        [Button.inline("🔗 Replace Links", b"ask_repl_links"), Button.inline("🗑️ Clear", b"clr_repl_links")],
        [Button.inline("👤 Replace Usernames", b"ask_repl_username"), Button.inline("🗑️ Clear", b"clr_repl_username")],
        [Button.inline("📝 Replace Words", b"ask_repl_words"), Button.inline("🗑️ Clear", b"clr_repl_words")],
        [Button.inline("📞 Replace Phone Nos.", b"ask_repl_phone")],
        [Button.inline("🔼 Add Header", b"ask_header"), Button.inline("🗑️ Clear", b"clr_header")],
        [Button.inline("🔽 Add Footer", b"ask_footer"), Button.inline("🗑️ Clear", b"clr_footer")],
        [Button.inline("⏱️ Target Delay", b"ask_delay"), Button.inline("⏳ Auto Delete Time", b"ask_auto_delete")],
        [Button.inline("💬 Auto Reply Text", b"ask_reply_text"), Button.inline("😀 Reaction Emoji", b"ask_reaction")],
        [Button.inline("🔄 Refresh", b"refresh_fset")],
    ]

    if isinstance(event, events.CallbackQuery.Event):
        try:
            await event.edit(text, buttons=keyboard)
        except MessageNotModifiedError:
            pass
    else:
        await event.respond(text, buttons=keyboard)


# ---------------------------------------------------------------------------
# SOURCE / TARGET CHANNEL SELECTION
# ---------------------------------------------------------------------------
@bot.on(events.NewMessage(pattern='/source'))
async def source_cmd(event):
    user_id = event.sender_id
    u_client = active_user_clients.get(user_id)
    if not u_client:
        sess_str = get_session(user_id)
        if sess_str:
            u_client = TelegramClient(StringSession(sess_str), API_ID, API_HASH)
            await u_client.connect()
            active_user_clients[user_id] = u_client
        else:
            await event.respond("❌ You haven't logged in yet! Send /start to sign in with your account.")
            return

    await event.respond("⏳ Scanning all channels/groups in this account, please wait a moment...")
    try:
        channels = []
        async for dialog in u_client.iter_dialogs():
            if dialog.is_channel or dialog.is_group:
                channels.append({'id': dialog.id, 'title': dialog.title})

        if not channels:
            await event.respond("❌ No channel or group was found in your account.")
            return

        channel_list_cache[user_id] = channels
        channel_selection_state[user_id] = 'source'

        message = "📥 **Pick the number of the channel below to set as Source:**\n━━━━━━━━━━━━━━━━━━━\n"
        for i, ch in enumerate(channels[:50], 1):
            message += f"{i}. {ch['title']}\n"
        message += "\n━━━━━━━━━━━━━━━━━━━\n✍️ **Type the number below:**"
        await event.respond(message)
    except Exception as e:
        await event.respond(f"❌ Error while fetching channels: {str(e)}")


@bot.on(events.NewMessage(pattern='/target'))
async def target_cmd(event):
    user_id = event.sender_id
    u_client = active_user_clients.get(user_id)
    if not u_client:
        sess_str = get_session(user_id)
        if sess_str:
            u_client = TelegramClient(StringSession(sess_str), API_ID, API_HASH)
            await u_client.connect()
            active_user_clients[user_id] = u_client
        else:
            await event.respond("❌ You haven't logged in yet! Send /start to sign in with your account.")
            return

    await event.respond("⏳ Scanning all channels/groups in this account, please wait a moment...")
    try:
        channels = []
        async for dialog in u_client.iter_dialogs():
            if dialog.is_channel or dialog.is_group:
                channels.append({'id': dialog.id, 'title': dialog.title})

        if not channels:
            await event.respond("❌ No channel or group was found in your account.")
            return

        channel_list_cache[user_id] = channels
        channel_selection_state[user_id] = 'target'

        message = "🎯 **Pick the number of the channel below to set as Target:**\n━━━━━━━━━━━━━━━━━━━\n"
        for i, ch in enumerate(channels[:50], 1):
            message += f"{i}. {ch['title']}\n"
        message += "\n━━━━━━━━━━━━━━━━━━━\n✍️ **Type the number below:**"
        await event.respond(message)
    except Exception as e:
        await event.respond(f"❌ Error while fetching channels: {str(e)}")


@bot.on(events.NewMessage(pattern='/start_forwarding'))
async def start_forwarding_cmd(event):
    toggle_setting(event.sender_id, 'forwarding_status', 1)
    await event.respond("🟢 **Auto Forwarding** has been enabled successfully!")


@bot.on(events.NewMessage(pattern='/stop_forwarding'))
async def stop_forwarding_cmd(event):
    toggle_setting(event.sender_id, 'forwarding_status', 0)
    await event.respond("🔴 **Auto Forwarding** has been disabled successfully!")


@bot.on(events.NewMessage(pattern='/remove_source'))
async def remove_source_cmd(event):
    remove_channel(event.sender_id, 'source')
    await event.respond("🗑️ Your Source Channel has been removed.")


@bot.on(events.NewMessage(pattern='/remove_target'))
async def remove_target_cmd(event):
    remove_channel(event.sender_id, 'target')
    await event.respond("🗑️ Your Target Channel has been removed.")


@bot.on(events.NewMessage(pattern='/reset_config'))
async def reset_config_cmd(event):
    reset_all_config(event.sender_id)
    await event.respond("🔄 All your settings have been reset back to default.")


@bot.on(events.NewMessage(pattern='/help'))
async def help_cmd(event):
    await event.respond("💬 Contact our support admin here: @musamk11")


@bot.on(events.NewMessage(pattern='/affiliate'))
async def affiliate_cmd(event):
    await event.respond(
        "💰 **Refer & Earn Money**\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "Share this bot with your friends! Contact @musamk11 to get your "
        "referral link and learn how you can start earning."
    )


@bot.on(events.NewMessage(pattern='/tutorial'))
async def tutorial_cmd(event):
    await event.respond(
        "🎬 **How to Setup (Video Guide)**\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "A step-by-step video guide is coming soon. In the meantime, contact "
        "@musamk11 for setup help."
    )


@bot.on(events.NewMessage(pattern='/logout'))
async def logout_cmd(event):
    user_id = event.sender_id
    if user_id in active_user_clients:
        try:
            await active_user_clients[user_id].disconnect()
        except:
            pass
        del active_user_clients[user_id]
    delete_user(user_id)
    await event.respond("✅ You have been logged out and all your session data has been deleted from the bot.")


# ---------------------------------------------------------------------------
# FILTERS & REPLACEMENTS COMMANDS
# ---------------------------------------------------------------------------

async def require_config(event):
    res = get_full_config(event.sender_id)
    if not res:
        await event.respond("❌ Please send /start first.")
        return False
    return True


@bot.on(events.NewMessage(pattern='/blacklist'))
async def blacklist_cmd(event):
    if not await require_config(event):
        return
    text_input_state[event.sender_id] = {'action': 'add_list', 'field': 'blacklist_keywords'}
    await event.respond("🚫 Send the word(s) you want to add to the blacklist, separated by commas.\n\nAny message containing one of these words will NOT be forwarded.")

@bot.on(events.NewMessage(pattern='/remove_blacklist'))
async def remove_blacklist_cmd(event):
    if not await require_config(event):
        return
    text_input_state[event.sender_id] = {'action': 'remove_list', 'field': 'blacklist_keywords'}
    await event.respond("🗑️ Send the word(s) to remove from the blacklist (comma separated), or type `all` to clear the whole list.")


@bot.on(events.NewMessage(pattern='/whitelist'))
async def whitelist_cmd(event):
    if not await require_config(event):
        return
    text_input_state[event.sender_id] = {'action': 'add_list', 'field': 'whitelist_keywords'}
    await event.respond("✅ Send the word(s) you want to add to the whitelist, separated by commas.\n\nIf this list is not empty, ONLY messages containing at least one of these words will be forwarded.")

@bot.on(events.NewMessage(pattern='/remove_whitelist'))
async def remove_whitelist_cmd(event):
    if not await require_config(event):
        return
    text_input_state[event.sender_id] = {'action': 'remove_list', 'field': 'whitelist_keywords'}
    await event.respond("🗑️ Send the word(s) to remove from the whitelist (comma separated), or type `all` to clear the whole list.")


@bot.on(events.NewMessage(pattern='/trim_words'))
async def trim_words_cmd(event):
    if not await require_config(event):
        return
    text_input_state[event.sender_id] = {'action': 'add_list', 'field': 'trim_words'}
    await event.respond("✂️ Send the word(s) or phrase(s) you want removed from every forwarded message, separated by commas.")

@bot.on(events.NewMessage(pattern='/delete_trim'))
async def delete_trim_cmd(event):
    if not await require_config(event):
        return
    set_text_field(event.sender_id, 'trim_words', '')
    await event.respond("🗑️ The Trim Words list has been emptied.")


@bot.on(events.NewMessage(pattern='/replace_username'))
async def replace_username_cmd(event):
    if not await require_config(event):
        return
    text_input_state[event.sender_id] = {'action': 'add_pairs', 'field': 'replace_usernames'}
    await event.respond("👤 Send username replacement pairs, one per line, like this:\n`@old_username=>@new_username`")

@bot.on(events.NewMessage(pattern='/delete_username'))
async def delete_username_cmd(event):
    if not await require_config(event):
        return
    set_text_field(event.sender_id, 'replace_usernames', '')
    await event.respond("🗑️ The Usernames replacement list has been deleted.")


@bot.on(events.NewMessage(pattern='/replace_links'))
async def replace_links_cmd(event):
    if not await require_config(event):
        return
    text_input_state[event.sender_id] = {'action': 'add_pairs', 'field': 'replace_links'}
    await event.respond("🔗 Send link replacement pairs, one per line, like this:\n`https://old-link.com=>https://new-link.com`")

@bot.on(events.NewMessage(pattern='/delete_links'))
async def delete_links_cmd(event):
    if not await require_config(event):
        return
    set_text_field(event.sender_id, 'replace_links', '')
    await event.respond("🗑️ The Links replacement list has been deleted.")


@bot.on(events.NewMessage(pattern='/replace_words'))
async def replace_words_cmd(event):
    if not await require_config(event):
        return
    text_input_state[event.sender_id] = {'action': 'add_pairs', 'field': 'replace_words'}
    await event.respond("📝 Send word replacement pairs, one per line, like this:\n`old word=>new word`")

@bot.on(events.NewMessage(pattern='/delete_words'))
async def delete_words_cmd(event):
    if not await require_config(event):
        return
    set_text_field(event.sender_id, 'replace_words', '')
    await event.respond("🗑️ The Words replacement list has been deleted.")


@bot.on(events.NewMessage(pattern='/set_replace_phone'))
async def set_replace_phone_cmd(event):
    if not await require_config(event):
        return
    text_input_state[event.sender_id] = {'action': 'set_value', 'field': 'replace_phone_numbers'}
    await event.respond("📞 Send the text you want phone numbers replaced with (or send `clear` to just remove them with no replacement).")


@bot.on(events.NewMessage(pattern='/add_header'))
async def add_header_cmd(event):
    if not await require_config(event):
        return
    text_input_state[event.sender_id] = {'action': 'set_value', 'field': 'add_header', 'also_enable': 'header_status'}
    await event.respond("🔼 Send the header text you want added to the top of every forwarded message.")

@bot.on(events.NewMessage(pattern='/delete_header'))
async def delete_header_cmd(event):
    if not await require_config(event):
        return
    set_text_field(event.sender_id, 'add_header', '')
    toggle_setting(event.sender_id, 'header_status', 0)
    await event.respond("🗑️ The Header Text has been deleted.")

@bot.on(events.NewMessage(pattern='/add_footer'))
async def add_footer_cmd(event):
    if not await require_config(event):
        return
    text_input_state[event.sender_id] = {'action': 'set_value', 'field': 'add_footer'}
    await event.respond("🔽 Send the footer text you want added to the bottom of every forwarded message.")

@bot.on(events.NewMessage(pattern='/delete_footer'))
async def delete_footer_cmd(event):
    if not await require_config(event):
        return
    set_text_field(event.sender_id, 'add_footer', '')
    await event.respond("🗑️ The Footer Text has been deleted.")


@bot.on(events.NewMessage(pattern=r'/set_delay(?: (.+))?'))
async def set_delay_cmd(event):
    if not await require_config(event):
        return
    arg = event.pattern_match.group(1)
    if not arg or not arg.strip().isdigit():
        await event.respond("⏱️ Usage: `/set_delay 10` (seconds to wait before forwarding each message).")
        return
    set_text_field(event.sender_id, 'target_delay', int(arg.strip()))
    await event.respond(f"✅ Target Delay Timer set to {arg.strip()} seconds.")

@bot.on(events.NewMessage(pattern=r'/set_auto_delete(?: (.+))?'))
async def set_auto_delete_cmd(event):
    if not await require_config(event):
        return
    arg = event.pattern_match.group(1)
    if not arg or not arg.strip().isdigit():
        await event.respond("⏱️ Usage: `/set_auto_delete 60` (seconds before the forwarded message auto-deletes).")
        return
    set_text_field(event.sender_id, 'auto_delete_seconds', int(arg.strip()))
    await event.respond(f"✅ Auto Delete Timer set to {arg.strip()} seconds.")

@bot.on(events.NewMessage(pattern=r'/set_reply_text(?: (.+))?'))
async def set_reply_text_cmd(event):
    if not await require_config(event):
        return
    arg = event.pattern_match.group(1)
    if not arg:
        await event.respond("✍️ Usage: `/set_reply_text Check the link above 👆`")
        return
    set_text_field(event.sender_id, 'auto_reply_text', arg.strip())
    await event.respond("✅ Link Auto Reply text updated.")

@bot.on(events.NewMessage(pattern=r'/set_reaction(?: (.+))?'))
async def set_reaction_cmd(event):
    if not await require_config(event):
        return
    arg = event.pattern_match.group(1)
    if not arg:
        await event.respond("✍️ Usage: `/set_reaction 👍`")
        return
    set_text_field(event.sender_id, 'auto_reaction_emoji', arg.strip())
    await event.respond("✅ Auto Reaction emoji updated.")


# ---------------------------------------------------------------------------
# CALLBACK BUTTON HANDLERS
# ---------------------------------------------------------------------------
@bot.on(events.CallbackQuery(data=b"refresh_dashboard"))
async def refresh_db_click(event):
    await lconfig_cmd(event)

@bot.on(events.CallbackQuery(data=re.compile(b"tog_(.*)")))
async def toggle_settings_click(event):
    user_id = event.sender_id
    col_name = event.data.decode().split('_', 1)[1]
    res = get_full_config(user_id)
    if not res:
        await event.respond("❌ Please log in first before changing settings.", alert=True)
        return
    toggle_setting(user_id, col_name)
    await lconfig_cmd(event)

@bot.on(events.CallbackQuery(data=b"fara_login"))
async def fara_login_click(event):
    login_state[event.sender_id] = {'step': 'phone'}
    await event.respond("📞 Please send your phone number with the country code (e.g., +2348012345678):")

@bot.on(events.CallbackQuery(data=b"logout_acc"))
async def logout_acc_click(event):
    await logout_cmd(event)

@bot.on(events.CallbackQuery(data=b"saita_source"))
async def saita_source_click(event):
    await source_cmd(event)

@bot.on(events.CallbackQuery(data=b"saita_target"))
async def saita_target_click(event):
    await target_cmd(event)

@bot.on(events.CallbackQuery(data=b"tuntuba_admin"))
async def tuntuba_admin_click(event):
    await help_cmd(event)


@bot.on(events.CallbackQuery(data=b"refresh_fset"))
async def refresh_fset_click(event):
    await forward_settings_cmd(event)


# --- Buttons that ask for text input (reuse the same /command logic) ---
@bot.on(events.CallbackQuery(data=b"ask_blacklist"))
async def ask_blacklist_click(event):
    await blacklist_cmd(event)

@bot.on(events.CallbackQuery(data=b"clr_blacklist"))
async def clr_blacklist_click(event):
    set_text_field(event.sender_id, 'blacklist_keywords', '')
    await event.answer("🗑️ Blacklist cleared.")
    await forward_settings_cmd(event)

@bot.on(events.CallbackQuery(data=b"ask_whitelist"))
async def ask_whitelist_click(event):
    await whitelist_cmd(event)

@bot.on(events.CallbackQuery(data=b"clr_whitelist"))
async def clr_whitelist_click(event):
    set_text_field(event.sender_id, 'whitelist_keywords', '')
    await event.answer("🗑️ Whitelist cleared.")
    await forward_settings_cmd(event)

@bot.on(events.CallbackQuery(data=b"ask_trim"))
async def ask_trim_click(event):
    await trim_words_cmd(event)

@bot.on(events.CallbackQuery(data=b"clr_trim"))
async def clr_trim_click(event):
    set_text_field(event.sender_id, 'trim_words', '')
    await event.answer("🗑️ Trim words cleared.")
    await forward_settings_cmd(event)

@bot.on(events.CallbackQuery(data=b"ask_repl_links"))
async def ask_repl_links_click(event):
    await replace_links_cmd(event)

@bot.on(events.CallbackQuery(data=b"clr_repl_links"))
async def clr_repl_links_click(event):
    set_text_field(event.sender_id, 'replace_links', '')
    await event.answer("🗑️ Link replacements cleared.")
    await forward_settings_cmd(event)

@bot.on(events.CallbackQuery(data=b"ask_repl_username"))
async def ask_repl_username_click(event):
    await replace_username_cmd(event)

@bot.on(events.CallbackQuery(data=b"clr_repl_username"))
async def clr_repl_username_click(event):
    set_text_field(event.sender_id, 'replace_usernames', '')
    await event.answer("🗑️ Username replacements cleared.")
    await forward_settings_cmd(event)

@bot.on(events.CallbackQuery(data=b"ask_repl_words"))
async def ask_repl_words_click(event):
    await replace_words_cmd(event)

@bot.on(events.CallbackQuery(data=b"clr_repl_words"))
async def clr_repl_words_click(event):
    set_text_field(event.sender_id, 'replace_words', '')
    await event.answer("🗑️ Word replacements cleared.")
    await forward_settings_cmd(event)

@bot.on(events.CallbackQuery(data=b"ask_repl_phone"))
async def ask_repl_phone_click(event):
    await set_replace_phone_cmd(event)

@bot.on(events.CallbackQuery(data=b"ask_header"))
async def ask_header_click(event):
    await add_header_cmd(event)

@bot.on(events.CallbackQuery(data=b"clr_header"))
async def clr_header_click(event):
    set_text_field(event.sender_id, 'add_header', '')
    toggle_setting(event.sender_id, 'header_status', 0)
    await event.answer("🗑️ Header cleared.")
    await forward_settings_cmd(event)

@bot.on(events.CallbackQuery(data=b"ask_footer"))
async def ask_footer_click(event):
    await add_footer_cmd(event)

@bot.on(events.CallbackQuery(data=b"clr_footer"))
async def clr_footer_click(event):
    set_text_field(event.sender_id, 'add_footer', '')
    await event.answer("🗑️ Footer cleared.")
    await forward_settings_cmd(event)

@bot.on(events.CallbackQuery(data=b"ask_delay"))
async def ask_delay_click(event):
    if not await require_config(event):
        return
    text_input_state[event.sender_id] = {'action': 'set_int', 'field': 'target_delay'}
    await event.respond("⏱️ Send the number of seconds to wait before forwarding each message (e.g., `10`).")

@bot.on(events.CallbackQuery(data=b"ask_auto_delete"))
async def ask_auto_delete_click(event):
    if not await require_config(event):
        return
    text_input_state[event.sender_id] = {'action': 'set_int', 'field': 'auto_delete_seconds'}
    await event.respond("⏳ Send the number of seconds before a forwarded message auto-deletes (e.g., `60`).")

@bot.on(events.CallbackQuery(data=b"ask_reply_text"))
async def ask_reply_text_click(event):
    if not await require_config(event):
        return
    text_input_state[event.sender_id] = {'action': 'set_value', 'field': 'auto_reply_text'}
    await event.respond("💬 Send the text you want auto-replied whenever a link is forwarded.")

@bot.on(events.CallbackQuery(data=b"ask_reaction"))
async def ask_reaction_click(event):
    if not await require_config(event):
        return
    text_input_state[event.sender_id] = {'action': 'set_value', 'field': 'auto_reaction_emoji'}
    await event.respond("😀 Send the emoji you want used for auto-reactions (e.g., 👍).")


# ---------------------------------------------------------------------------
# PROCESSING FOLLOW-UP TEXT (channel picking, login flow, filter input)
# ---------------------------------------------------------------------------
@bot.on(events.NewMessage)
async def handle_text_input(event):
    if event.text.startswith('/'):
        return

    user_id = event.sender_id
    text = event.text.strip()

    if user_id in text_input_state:
        state = text_input_state[user_id]
        field = state['field']
        action = state['action']
        current = get_full_config(user_id)
        current_value = current[field] if current else ''

        if action == 'add_list':
            new_value = merge_csv_list(current_value, text)
            set_text_field(user_id, field, new_value)
            await event.respond(f"✅ Added! Current list: {format_display(new_value)}")

        elif action == 'remove_list':
            new_value = remove_from_csv_list(current_value, text)
            set_text_field(user_id, field, new_value)
            await event.respond(f"✅ Updated! Current list: {format_display(new_value)}")

        elif action == 'add_pairs':
            new_value = merge_pairs(current_value, text)
            set_text_field(user_id, field, new_value)
            await event.respond(f"✅ Saved! Current replacements:\n`{new_value}`")

        elif action == 'set_value':
            if text.strip().lower() == 'clear':
                set_text_field(user_id, field, '')
                await event.respond("✅ Cleared.")
            else:
                set_text_field(user_id, field, text)
                if state.get('also_enable'):
                    toggle_setting(user_id, state['also_enable'], 1)
                await event.respond(f"✅ Saved: `{text}`")

        elif action == 'set_int':
            if not text.strip().isdigit():
                await event.respond("❌ Please send a number only (e.g., `10`). Try again:")
                return
            set_text_field(user_id, field, int(text.strip()))
            await event.respond(f"✅ Saved: {text.strip()} seconds.")

        del text_input_state[user_id]
        return

    if user_id in channel_selection_state and user_id in channel_list_cache:
        kind = channel_selection_state[user_id]
        channels = channel_list_cache[user_id]

        if text.isdigit():
            number = int(text)
            if 1 <= number <= len(channels):
                chosen = channels[number - 1]
                update_channel(user_id, kind, chosen['id'], chosen['title'])
                await event.respond(f"✅ Successfully set `{chosen['title']}` as **{kind.upper()}**!")
                del channel_selection_state[user_id]
                del channel_list_cache[user_id]
                await lconfig_cmd(event)
                return
            else:
                await event.respond(f"❌ That number isn't in the list I sent you. Please pick a number between 1 and {len(channels)}:")
                return
        else:
            await event.respond("❌ Please type the channel number only (e.g., 5):")
            return

    if user_id in login_state:
        data = login_state[user_id]

        if data['step'] == 'phone':
            data['phone'] = text
            u_client = TelegramClient(StringSession(), API_ID, API_HASH)
            await u_client.connect()
            try:
                sent_code = await u_client.send_code_request(text)
                data['phone_code_hash'] = sent_code.phone_code_hash
                data['client'] = u_client
                data['step'] = 'otp'
                login_state[user_id] = data

                otp_message = (
                    "📩 **The OTP code has been sent successfully!**\n\n"
                    "⚠️ **HOW TO ENTER YOUR OTP:**\n"
                    "Telegram doesn't allow sending a 5-digit OTP code directly inside a bot. "
                    "So, you need to attach the word **BAFBOT** to the front of your code with no space.\n\n"
                    "📝 **Example:** If your OTP code is `12345`, type it like this:\n"
                    "👉 **`BAFBOT12345`**\n\n"
                    "✍️ *Please type yours now and send it:* "
                )
                await event.respond(otp_message)
            except Exception as e:
                await event.respond(f"❌ Error while sending OTP: {str(e)}")
                await u_client.disconnect()
                del login_state[user_id]

        elif data['step'] == 'otp':
            u_client = data['client']
            clean_otp = text.upper().replace('BAFBOT', '').strip()
            try:
                await u_client.sign_in(data['phone'], clean_otp, phone_code_hash=data['phone_code_hash'])
                sess_str = u_client.session.save()
                save_session(user_id, data['phone'], sess_str)
                active_user_clients[user_id] = u_client
                await start_forwarding_engine(user_id, sess_str)
                await event.respond("🟢 Great! You've successfully logged into your account.")
                del login_state[user_id]
                await lconfig_cmd(event)
            except SessionPasswordNeededError:
                data['step'] = 'password'
                login_state[user_id] = data
                await event.respond("🔐 Your account has Two-Step Verification (2FA) enabled. Please send your password:")
            except Exception as e:
                await event.respond(f"❌ Error while verifying OTP: {str(e)}\n⚠️ Make sure you typed it as `BAFBOT12345`")
                await u_client.disconnect()
                del login_state[user_id]

        elif data['step'] == 'password':
            u_client = data['client']
            try:
                await u_client.sign_in(password=text)
                sess_str = u_client.session.save()
                save_session(user_id, data['phone'], sess_str)
                active_user_clients[user_id] = u_client
                await start_forwarding_engine(user_id, sess_str)
                await event.respond("🟢 Great! You've logged in successfully with your 2FA password.")
                del login_state[user_id]
                await lconfig_cmd(event)
            except Exception as e:
                await event.respond(f"❌ Error while verifying 2FA password: {str(e)}")
                await u_client.disconnect()
                del login_state[user_id]


# ---------------------------------------------------------------------------
# LIVE AUTOMATED FORWARDER ENGINE
# ---------------------------------------------------------------------------
async def start_forwarding_engine(user_id, session_string):
    if user_id in active_user_clients:
        client = active_user_clients[user_id]
    else:
        client = TelegramClient(StringSession(session_string), API_ID, API_HASH)
        await client.connect()
        active_user_clients[user_id] = client

    try:
        client.list_event_handlers().clear()
    except Exception:
        pass

    async def automated_forward_handler(event):
        config = get_full_config(user_id)
        if not config or not config['forwarding_status']:
            return

        src_id = config['source_channel']
        trg_id = config['target_channel']
        if not src_id or not trg_id:
            return

        current_chat_id = str(event.chat_id)
        if current_chat_id != src_id and f"-100{current_chat_id}" != src_id and src_id != current_chat_id.replace("-100", ""):
            return

        if event.message.media and not config['media_forwarding']:
            return

        original_text = event.message.text or ""

        if keyword_present(original_text, config['blacklist_keywords']):
            return
        if config['whitelist_keywords'] and not keyword_present(original_text, config['whitelist_keywords']):
            return

        message_text = original_text
        message_text = apply_pairs(message_text, config['replace_words'])
        message_text = apply_pairs(message_text, config['replace_links'])
        message_text = apply_pairs(message_text, config['replace_usernames'])

        if config['trim_words']:
            for word in parse_csv_list(config['trim_words']):
                message_text = message_text.replace(word, '')

        if config['cire_links']:
            message_text = strip_links(message_text, '')

        if config['cire_usernames']:
            message_text = strip_usernames(message_text, '')

        if config['remove_phone_numbers']:
            replacement = config['replace_phone_numbers'] if config['replace_phone_numbers'] else ''
            message_text = re.sub(PHONE_NUMBER_PATTERN, replacement, message_text)

        if config['disable_links']:
            message_text = disable_link_clicks(message_text)

        if config['mono_text'] and message_text:
            message_text = f"`{message_text}`"

        if config['header_status'] and config['add_header']:
            message_text = f"{config['add_header']}\n{message_text}"
        if config['add_footer']:
            message_text = f"{message_text}\n{config['add_footer']}"

        if config['target_delay'] and config['target_delay'] > 0:
            await asyncio.sleep(config['target_delay'])

        try:
            target_entity = int(trg_id) if trg_id.replace('-', '').isdigit() else trg_id

            send_kwargs = dict(
                file=event.message.media if config['media_forwarding'] else None,
                link_preview=bool(config['url_preview']),
            )

            sent = None
            if config['protected_forwards']:
                try:
                    sent = await client.send_message(target_entity, message_text, noforwards=True, **send_kwargs)
                except TypeError:
                    sent = await client.send_message(target_entity, message_text, **send_kwargs)
            else:
                sent = await client.send_message(target_entity, message_text, **send_kwargs)

            if config['repeat_post']:
                try:
                    await client.send_message(target_entity, message_text, **send_kwargs)
                except Exception:
                    pass

            if config['auto_delete_msg'] and sent:
                delay = config['auto_delete_seconds'] or 60

                async def delete_later(entity, msg_id, wait_for):
                    await asyncio.sleep(wait_for)
                    try:
                        await client.delete_messages(entity, msg_id)
                    except Exception:
                        pass

                asyncio.create_task(delete_later(target_entity, sent.id, delay))

            if config['link_auto_replies'] and re.search(LINK_PATTERN, original_text) and sent:
                try:
                    reply_text = config['auto_reply_text'] or 'Check the link above 👆'
                    await client.send_message(target_entity, reply_text, reply_to=sent.id)
                except Exception:
                    pass

            if config['auto_reaction'] and sent:
                try:
                    emoji = config['auto_reaction_emoji'] or '👍'
                    await client(SendReactionRequest(
                        peer=target_entity,
                        msg_id=sent.id,
                        reaction=[ReactionEmoji(emoticon=emoji)]
                    ))
                except Exception:
                    pass

        except Exception:
            pass

    client.add_event_handler(automated_forward_handler, events.NewMessage)


async def boot_all_active_sessions():
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, session_string FROM masu_amfani WHERE session_string IS NOT NULL")
    rows = cursor.fetchall()
    conn.close()
    for row in rows:
        uid = row[0]
        s_str = row[1]
        try:
            asyncio.create_task(start_forwarding_engine(uid, s_str))
        except Exception:
            pass


if __name__ == '__main__':
    bot.loop.run_until_complete(boot_all_active_sessions())
    print("🚀 All updates applied! The bot is up and running.")
    bot.run_until_disconnected()
