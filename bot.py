import telebot
import time
import requests
import re
import html
from bs4 import BeautifulSoup
import threading
from datetime import datetime
import os
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import concurrent.futures
from con_ns import (
    BOT_TOKEN,
    CHAT_ID,
    ADMIN_ID,
    LOGIN_URL,
    PORTAL_URL,
    SMS_URL,
    EMAIL,
    PASSWORD,
    START_DATE,
    country_codes
)
# ================= CONFIG ================


POLL_INTERVAL_SECONDS = 4
MAX_WORKERS = 8

RANGES_DIR = "ranges"

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# 🔧
session = requests.Session()

# Retry setup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

retry_strategy = Retry(total=4, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
adapter = HTTPAdapter(max_retries=retry_strategy)
session.mount("http://", adapter)
session.mount("https://", adapter)

csrf_token = None
seen_otps = {}
user_states = {}

if not os.path.exists(RANGES_DIR):
    os.makedirs(RANGES_DIR)

# দেশের কোড + নাম + ফ্ল্যাগ

def login_and_get_csrf():
    global csrf_token
    try:
        r = session.get(LOGIN_URL, timeout=30)
        soup = BeautifulSoup(r.text, "html.parser")
        token_tag = soup.find("input", {"name": "_token"})
        if not token_tag or "value" not in token_tag.attrs:
            return False
        initial_token = token_tag["value"]

        payload = {"_token": initial_token, "email": EMAIL, "password": PASSWORD}
        r_post = session.post(LOGIN_URL, data=payload, timeout=30)

        if r_post.status_code != 200 or "login" in r_post.url.lower():
            return False

        r_portal = session.get(PORTAL_URL, timeout=30)
        soup_portal = BeautifulSoup(r_portal.text, "html.parser")

        meta = soup_portal.find("meta", {"name": "csrf-token"})
        if meta and "content" in meta.attrs:
            csrf_token = meta["content"]
            return True

        input_tag = soup_portal.find("input", {"name": "_token"})
        if input_tag and "value" in input_tag.attrs:
            csrf_token = input_tag["value"]
            return True

        return False
    except Exception as e:
        print(f"[LOGIN/CSRF ERROR]: {str(e)}")
        return False

def fetch_otps(number, range_name):
    global csrf_token
    if not csrf_token and not login_and_get_csrf():
        return None, "লগইন/CSRF সমস্যা"

    payload = {
        "start": START_DATE,
        "end": time.strftime("%Y-%m-%d"),
        "Number": number,
        "Range": range_name
    }

    headers = {
        "X-CSRF-TOKEN": csrf_token,
        "X-Requested-With": "XMLHttpRequest",
        "Referer": PORTAL_URL,
        "Accept": "*/*",
        "Content-Type": "application/x-www-form-urlencoded"
    }

    try:
        r = session.post(SMS_URL, data=payload, headers=headers, timeout=35)

        if r.status_code == 419:
            if login_and_get_csrf():
                headers["X-CSRF-TOKEN"] = csrf_token
                r = session.post(SMS_URL, data=payload, headers=headers, timeout=35)
            else:
                return None, "419 - CSRF fail"

        if r.status_code != 200:
            return None, f"HTTP {r.status_code}"

        soup = BeautifulSoup(r.text, "html.parser")
        sms_cards = soup.find_all('div', class_=lambda v: v and 'card-body' in v.split())

        messages = []
        for card in sms_cards:
            p = card.select_one('p.mb-0.pb-0')
            if not p: continue
            raw = p.get_text(separator=" ", strip=True)
            full_text = html.unescape(raw).strip()
            if len(full_text) < 5: continue

            otp_patterns = [
                r'(?:code|OTP|কোড|ওটিপি|Your WhatsApp code)[:\s-]*(\d{3,8}(?:-\d{3})?)',
                r'(\d{3,8}(?:-\d{3})?)\s*(?:is your|your code|code|OTP)',
                r'\b(\d{3,8}(?:-\d{3})?)\b'
            ]

            otp = None
            for pat in otp_patterns:
                m = re.search(pat, full_text, re.IGNORECASE)
                if m:
                    otp = m.group(1).replace("-", "")
                    break

            if otp:
                messages.append({"otp": otp, "full_body": full_text})
            else:
                messages.append({"otp": None, "full_body": full_text})

        return messages, None

    except Exception as e:
        print(f"[FETCH ERR] {number}: {e}")
        return None, "এরর"

def fetch_and_post_new_otps(number, range_name):
    msgs, err = fetch_otps(number, range_name)
    if err:
        return

    new_msgs = []
    for msg in msgs:
        key = f"{number}:{msg['otp']}"
        if key not in seen_otps:
            seen_otps[key] = time.time()
            new_msgs.append(msg)

    if not new_msgs:
        return

    # দেশ ডিটেক্ট
    country_name = "Unknown"
    flag = "🏍"
    clean_num = number.lstrip("+0")
    for length in [3, 2, 1]:
        prefix = clean_num[:length]
        if prefix in country_codes:
            country_name, flag = country_codes[prefix]
            break

    hidden_num = number[:4] + "★★★" + number[-4:] if len(number) >= 8 else number

    for msg in new_msgs:
        otp = msg['otp']
        otp_text = otp if otp else "❌ OTP NOT FOUND"
        full_body = msg['full_body']

        # ক্লায়েন্ট ডিটেক্ট
        client = "Service"
        lower = full_body.lower()
        if any(x in lower for x in ["facebook", "fb"]):
            client = "Facebook"
        elif "whatsapp" in lower:
            client = "WhatsApp"
        elif "telegram" in lower:
            client = "Telegram"
        elif any(x in lower for x in ["instagram", "ig"]):
            client = "Instagram"
        elif any(x in lower for x in ["google", "gmail", "youtube"]):
            client = "Google"
        elif "tiktok" in lower:
            client = "TikTok"
        elif "twitter" in lower or "x.com" in lower:
            client = "X/Twitter"

        # full_body সেফ করা — HTML escape + # escape
        safe_body = html.escape(full_body).replace("#", "\\#").replace("<", "&lt;").replace(">", "&gt;")

        message_text = f"""🔩🔩. <b>{flag} {client.upper()} 🅰🅷 🅼🅴🆃🅷🅾🅳 </b>.🔪🔪
﹐﹐﹐﹐﹐﹐﹐﹐﹐﹐﹐﹐﹐﹐
<blockquote>{flag} 𝗖𝗼𝘂𝗻𝘁𝗿𝘆 » {country_name}
☎️ 𝗡𝘂𝗺𝗯𝗲𝗿 » {hidden_num}</blockquote>
🔑𝗢𝗧𝗣 » <code>{otp_text}</code>
<blockquote><code>{safe_body}</code></blockquote>

— 𝗔𝗛 𝗠𝗘𝗧𝗛𝗢𝗗 𝗧𝗘𝗔𝗠"""

        try:
            bot.send_message(CHAT_ID, message_text)
            print(f"[SENT] {client} OTP {otp} for {number} ({country_name})")
        except Exception as e:
            print(f"[SEND ERR] {number} OTP {otp}: {e}")

def load_all_numbers():
    all_items = []
    for fn in os.listdir(RANGES_DIR):
        if fn.endswith(".txt"):
            range_name = fn[:-4].replace("_", " ")
            path = os.path.join(RANGES_DIR, fn)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    nums = [line.strip() for line in f if line.strip()]
                for n in nums:
                    all_items.append({"number": n, "range": range_name})
            except Exception as e:
                print(f"File read error {fn}: {e}")
    return all_items

def polling_loop():
    print(f"[POLLING] শুরু — প্রতি ~{POLL_INTERVAL_SECONDS} সেকেন্ডে (workers={MAX_WORKERS})")
    while True:
        cycle_start = time.time()
        try:
            items = load_all_numbers()
            count = len(items)
            print(f"[POLL] {count} নম্বর চেক হচ্ছে...")

            if count > 0:
                with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                    futures = [executor.submit(fetch_and_post_new_otps, item["number"], item["range"]) for item in items]
                    concurrent.futures.wait(futures)

            elapsed = time.time() - cycle_start
            sleep_time = max(POLL_INTERVAL_SECONDS - elapsed, 1.0)
            print(f"[POLL] সময় লাগেছে {elapsed:.2f}s → {sleep_time:.2f}s অপেক্ষা")
            time.sleep(sleep_time)

        except Exception as ex:
            print(f"[POLL ERR]: {ex}")
            time.sleep(10)

# --------------------- অ্যাডমিন প্যানেল ---------------------

def get_range_buttons():
    markup = InlineKeyboardMarkup(row_width=2)
    for fn in os.listdir(RANGES_DIR):
        if fn.endswith(".txt"):
            range_name = fn[:-4].replace("_", " ")
            markup.add(InlineKeyboardButton(range_name, callback_data=f"upload_{range_name}"))
    markup.add(InlineKeyboardButton("➕ ADD New Range", callback_data="add_range"))
    return markup

def get_delete_buttons():
    markup = InlineKeyboardMarkup(row_width=2)
    for fn in os.listdir(RANGES_DIR):
        if fn.endswith(".txt"):
            range_name = fn[:-4].replace("_", " ")
            markup.add(InlineKeyboardButton(f"🗑️ {range_name}", callback_data=f"delete_{range_name}"))
    markup.add(InlineKeyboardButton("« Back", callback_data="back_to_menu"))
    return markup

@bot.message_handler(commands=["start"])
def start(message):
    if message.from_user.id != ADMIN_ID:
        bot.reply_to(message, "বট চালু আছে।")
        return

    markup = get_range_buttons()
    bot.reply_to(message, "<b>অ্যাডমিন প্যানেল</b>\nরেঞ্জ সিলেক্ট করো বা নতুন অ্যাড করো:", reply_markup=markup)

@bot.message_handler(commands=["delete"])
def delete_cmd(message):
    if message.from_user.id != ADMIN_ID:
        return
    if not os.listdir(RANGES_DIR):
        bot.reply_to(message, "কোনো রেঞ্জ নেই।")
        return
    markup = get_delete_buttons()
    bot.reply_to(message, "<b>যে রেঞ্জ ডিলিট করতে চাও সিলেক্ট করো:</b>", reply_markup=markup)

@bot.message_handler(commands=["get"])
def manual_get(message):
    if message.from_user.id != ADMIN_ID:
        return
    bot.reply_to(message, "ম্যানুয়াল চেক শুরু...")
    items = load_all_numbers()
    for item in items:
        fetch_and_post_new_otps(item["number"], item["range"])
    bot.reply_to(message, f"চেক শেষ ({len(items)} নম্বর)।")

@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "শুধু অ্যাডমিন!", show_alert=True)
        return

    if call.data == "add_range":
        user_states[call.from_user.id] = {"state": "waiting_range_name"}
        bot.answer_callback_query(call.id)
        bot.send_message(call.message.chat.id, "নতুন রেঞ্জের নাম দাও (যেমন: BENIN 379)")

    elif call.data.startswith("upload_"):
        range_name = call.data.replace("upload_", "")
        user_states[call.from_user.id] = {"state": "waiting_file", "range_name": range_name}
        bot.answer_callback_query(call.id)
        bot.send_message(call.message.chat.id, f"এখন '{range_name}' এর জন্য TXT ফাইল আপলোড করো।")

    elif call.data.startswith("delete_"):
        range_name = call.data.replace("delete_", "")
        safe_fn = range_name.replace(" ", "_").replace("/", "-") + ".txt"
        path = os.path.join(RANGES_DIR, safe_fn)
        if os.path.exists(path):
            os.remove(path)
            bot.answer_callback_query(call.id, f"'{range_name}' ডিলিট হয়েছে!", show_alert=True)
            bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id,
                                  text="রেঞ্জ ডিলিট সফল।", reply_markup=get_range_buttons())
        else:
            bot.answer_callback_query(call.id, "রেঞ্জ পাওয়া যায়নি!", show_alert=True)

    elif call.data == "back_to_menu":
        bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id,
                              text="<b>অ্যাডমিন প্যানেল</b>", reply_markup=get_range_buttons())

@bot.message_handler(func=lambda m: m.from_user.id in user_states and user_states[m.from_user.id].get("state") == "waiting_range_name")
def handle_range_name(message):
    range_name = message.text.strip()
    if not range_name:
        bot.reply_to(message, "নাম দিতে হবে।")
        return

    safe_fn = range_name.replace(" ", "_").replace("/", "-") + ".txt"
    path = os.path.join(RANGES_DIR, safe_fn)

    created = False
    if not os.path.exists(path):
        open(path, 'a', encoding='utf-8').close()
        created = True

    msg = f"রেঞ্জ '{range_name}' {'তৈরি হয়েছে' if created else 'আগে থেকেই আছে'}!\n\n"
    msg += f"এখন '{range_name}' এর জন্য TXT ফাইল আপলোড করো।"
    bot.reply_to(message, msg)

    user_states[message.from_user.id] = {"state": "waiting_file", "range_name": range_name}

@bot.message_handler(content_types=['document'])
def handle_document(message):
    if message.from_user.id != ADMIN_ID:
        bot.reply_to(message, "শুধু অ্যাডমিন!")
        return

    if not message.document.file_name.lower().endswith('.txt'):
        bot.reply_to(message, "শুধু .txt ফাইল!")
        return

    if message.from_user.id not in user_states or user_states[message.from_user.id].get("state") != "waiting_file":
        bot.reply_to(message, "প্রথমে রেঞ্জ সিলেক্ট / ADD করো।")
        return

    range_name = user_states[message.from_user.id]["range_name"]
    safe_fn = range_name.replace(" ", "_").replace("/", "-") + ".txt"
    path = os.path.join(RANGES_DIR, safe_fn)

    if not os.path.exists(path):
        bot.reply_to(message, f"রেঞ্জ '{range_name}' পাওয়া যায়নি!")
        del user_states[message.from_user.id]
        return

    file_info = bot.get_file(message.document.file_id)
    downloaded = bot.download_file(file_info.file_path)
    new_nums = [line.strip() for line in downloaded.decode('utf-8').splitlines() if line.strip()]

    existing = set()
    if os.path.getsize(path) > 0:
        with open(path, 'r', encoding='utf-8') as f:
            existing = set(line.strip() for line in f if line.strip())

    added = 0
    with open(path, 'a', encoding='utf-8') as f:
        for num in new_nums:
            if num not in existing:
                f.write(num + '\n')
                existing.add(num)
                added += 1

    bot.reply_to(message, f"সফল! '{range_name}' এ {added} টি নতুন নম্বর যোগ হয়েছে।")

    del user_states[message.from_user.id]
    bot.send_message(message.chat.id, "রেঞ্জ লিস্ট আপডেট:", reply_markup=get_range_buttons())

if __name__ == "__main__":
    if not login_and_get_csrf():
        print("Initial login failed — চেক করো")

    threading.Thread(target=polling_loop, daemon=True).start()

    print("BOT STARTED → প্রতি ~৪ সেকেন্ডে পোলিং চলছে")

    while True:
        try:
            bot.infinity_polling(timeout=30, long_polling_timeout=60)
        except Exception as e:
            print(f"[POLLING CRASH] {e}")
            time.sleep(10)
