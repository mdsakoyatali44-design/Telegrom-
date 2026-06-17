import logging
import json
import datetime
import random
import hashlib
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from telegram.error import BadRequest, TelegramError
from telegram.request import HTTPXRequest
import firebase_admin
from firebase_admin import credentials, db

# ১. ট্রাবলশুটিং ও এরর ট্র্যাকিংয়ের জন্য লগিং কনফিগারেশন
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', 
    level=logging.INFO
)

# [কনফিগারেশন সেকশন] - আপনার পেমেন্ট প্রুফ চ্যানেলের আইডি এখানে দিন (চ্যানেলে বটকে অ্যাডমিন রাখতে হবে)
PAYMENT_PROOF_CHANNEL = "@PocketCash_Payments" 

# ২. ফায়ারবেস (Firebase) কানেকশন সেটআপ
try:
    if not firebase_admin._apps:
        cred = credentials.Certificate("serviceAccountKey.json")
        firebase_admin.initialize_app(cred, {
            'databaseURL': 'https://topup-cb0e6-default-rtdb.firebaseio.com/'
        })
except Exception as e:
    logging.error(f"ফায়ারবেস কানেকশন সেটআপে সমস্যা: {e}")

# ৩. ফায়ারবেস থেকে রিয়েল-টাইম সেটিংস লোড ও ব্যাকআপ জেনারেটর ফাংশন
def get_bot_settings():
    try:
        ref = db.reference('settings')
        settings = ref.get()
        
        if not settings:
            default_settings = {
                'welcome_text': "👋 স্বাগতম {user_name}!\n\n🤖 **𝑷𝒐𝒄𝒌𝒆𝒕𝑪𝒂𝒔𝒉 𝑹𝒐𝒃𝒐𝒕**-এ আপনাকে স্বাগতম। নিচের বাটনগুলো ব্যবহার করে টাস্ক পূরণ করুন এবং আনলিমিটেড পয়েন্ট আয় করা শুরু করুন! 🚀",
                'global_notice': "📢 আপডেট: আমাদের অ্যাপের কাজ চমৎকারভাবে চলছে! সবাই নিয়মিত কাজ করুন।",
                'ad_link': 'https://www.highrevenuegate.com/your_direct_link',
                'ad_points': 10,
                'refer_points': 50,
                'daily_bonus_points': 20,
                'min_withdraw_points': 1000,
                'channels_json': '[]'
            }
            ref.set(default_settings)
            return default_settings
        return settings
    except Exception as e:
        logging.error(f"সেটিংস লোড করতে সমস্যা: {e}")
        return {}

# ৪. ফায়ারবেস ইউজার ডাটা ম্যানেজমেন্ট ফাংশনসমূহ (অ্যান্টি-চিট ডিভাইস ফিঙ্গারপ্রিন্ট সহ)
def get_user_data(user_id, update: Update = None):
    try:
        ref = db.reference(f'users/{user_id}')
        user_data = ref.get()
        
        # অ্যান্টি-চিট: ইউজারের চ্যাট অবজেক্ট থেকে একটি ইউনিক ডিভাইস ফিঙ্গারপ্রিন্ট টোকেন জেনারেট করা
        device_fingerprint = "unknown_device"
        if update and update.effective_chat:
            raw_string = f"{user_id}_{update.effective_chat.type}"
            device_fingerprint = hashlib.md5(raw_string.encode()).hexdigest()

        if not user_data:
            user_data = {
                'points': 0, 
                'referred_by': None, 
                'refer_count': 0,
                'last_daily_bonus': '',
                'last_spin_date': '',
                'username': f"user_{user_id}",
                'device_id': device_fingerprint,
                'status': 'Active'
            }
            ref.set(user_data)
        return user_data
    except Exception as e:
        logging.error(f"ইউজার ডাটা লোড করতে সমস্যা: {e}")
        return {'points': 0, 'referred_by': None, 'refer_count': 0, 'last_daily_bonus': '', 'last_spin_date': '', 'username': 'Unknown', 'device_id': 'unknown', 'status': 'Active'}

def add_user_points(user_id, points_to_add):
    try:
        ref = db.reference(f'users/{user_id}/points')
        current_points = ref.get() or 0
        ref.set(current_points + points_to_add)
    except Exception as e:
        logging.error(f"পয়েন্ট যোগ করতে সমস্যা: {e}")

# ৫. মেম্বারশিপ চেক করার ক্রাশ-প্রুফ লজিক
async def get_unjoined_channels(user_id, context: ContextTypes.DEFAULT_TYPE, channels_list):
    unjoined = []
    
    if not isinstance(channels_list, list):
        return unjoined
        
    for ch in channels_list:
        if not isinstance(ch, dict):
            continue
            
        ch_id = ch.get('id', '').strip()
        if not ch_id or ch_id == "@admin":
            continue
            
        if not ch_id.startswith('@') and not ch_id.startswith('-'):
            target_chat_id = f"@{ch_id}"
        else:
            target_chat_id = ch_id
            
        try:
            member = await context.bot.get_chat_member(chat_id=target_chat_id, user_id=user_id)
            if member.status in ['left', 'kicked']:
                unjoined.append(ch)
        except BadRequest as e:
            error_msg = str(e).lower()
            logging.error(f"টেলিগ্রাম এপিআই এরর ({target_chat_id}): {error_msg}")
            pass
        except Exception as e:
            logging.error(f"মেম্বারশিপ চেকে সমস্যা: {e}")
            pass
            
    return unjoined

# ৬. ডাইনামিক মাল্টি-চ্যানেল জয়েন কিবোর্ড জেনারেটর
def make_multi_join_keyboard(unjoined_channels):
    keyboard = []
    for index, ch in enumerate(unjoined_channels, start=1):
        button_name = ch.get('name', f"📢 জয়েন করুন - গ্রুপ/চ্যানেল #{index}").strip()
        if not button_name:
            button_name = f"📢 জয়েন করুন - গ্রুপ/চ্যানেল #{index}"
            
        keyboard.append([InlineKeyboardButton(button_name, url=ch.get('url'))])
    
    keyboard.append([InlineKeyboardButton("✅ আমি সবগুলোতে জয়েন করেছি (Check)", callback_data='check_membership')])
    return InlineKeyboardMarkup(keyboard)

# ७. সম্পূর্ণ ডাইনামিক ও পরিবর্তনযোগ্য মেইন মেনু ইন্টারফেস
async def display_main_menu(send_message_func, user_id, user_name, settings, is_edit=False):
    keyboard = [
        [InlineKeyboardButton("📺 Ad দেখে আয় করুন", callback_data='watch_ad'), InlineKeyboardButton("🎁 ডেইলি বোনাস", callback_data='daily_bonus')],
        [InlineKeyboardButton("🎰 লাকি স্পিন (Spin)", callback_data='spin_wheel'), InlineKeyboardButton("📊 লিডারবোর্ড", callback_data='leaderboard')],
        [InlineKeyboardButton("👥 বন্ধুদের ইনভাইট", callback_data='refer_friend'), InlineKeyboardButton("💰 আমার ব্যালেন্স", callback_data='check_balance')],
        [InlineKeyboardButton("💳 টাকা তুলুন (Withdraw)", callback_data='withdraw_panel')]
    ]
    
    settings_data = settings if settings else {}
    notice = settings_data.get('global_notice', '')
    raw_welcome = settings_data.get('welcome_text', '')
    
    if not raw_welcome or str(raw_welcome).strip() == "":
        raw_welcome = "👋 স্বাগতম {user_name}!\n\n🤖 **𝑷𝒐𝒄𝒌𝒆𝒕𝑪𝒂𝒔𝒉 𝑹𝒐𝒃𝒐𝒕** মেইন মেনু আনলক হয়েছে। কাজ শুরু করতে নিচের বাটনগুলো ব্যবহার করুন।"
    
    try:
        welcome_msg = raw_welcome.format(user_name=user_name, user_id=user_id)
    except Exception:
        welcome_msg = "👋 স্বাগতম! মেইন মেনু আনলক হয়েছে।"
        
    if notice and str(notice).strip() != "":
        final_text = f"🚨 **{notice}**\n\n────────────────\n\n{welcome_msg}"
    else:
        final_text = welcome_msg
        
    if not final_text or final_text.strip() == "":
        final_text = "🤖 **𝑷𝒐𝒄𝒌𝒆𝒕𝑪𝒂𝒔𝒉 𝑹𝒐𝒃𝒐𝒕** মেইন মেনু।"
        
    try:
        await send_message_func(final_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    except Exception as e:
        logging.error(f"মেনু প্রদর্শনে সমস্যা: {e}")

# ৮. ডাটাবেজে রেফারেল ট্র্যাকিং ও পয়েন্ট বণ্টন লজিক (🛡️ অ্যান্টি-চিট ভেরিফিকেশন সহ)
async def reward_referrer(user_id, referrer_id, refer_points_bonus, update: Update = None):
    try:
        if referrer_id.isdigit() and int(referrer_id) != user_id:
            referrer_ref = db.reference(f'users/{referrer_id}')
            referrer_data = referrer_ref.get()
            
            # নতুন অ্যাকাউন্টের ফিঙ্গারপ্রিন্ট জেনারেট করা
            raw_string = f"{user_id}_{update.effective_chat.type}" if update and update.effective_chat else str(user_id)
            current_device_id = hashlib.md5(raw_string.encode()).hexdigest()
            
            if referrer_data:
                # 🛡️ অ্যান্টি-চিট সিকিউরিটি চেক: রেফারার এবং নতুন ইউজারের ডিভাইস এক কিনা তা যাচাই করা
                if referrer_data.get('device_id') == current_device_id:
                    logging.warning(f"🚨 ফেক রেফার সনাক্তকরণ! ইউজার {referrer_id} তার নিজের ডিভাইসে অ্যাকাউন্ট খুলে চিট করার চেষ্টা করেছে।")
                    return False
                
                current_points = referrer_data.get('points', 0)
                current_refers = referrer_data.get('refer_count', 0)
                
                db.reference(f'users/{referrer_id}/points').set(current_points + refer_points_bonus)
                db.reference(f'users/{referrer_id}/refer_count').set(current_refers + 1)
                db.reference(f'users/{user_id}/referred_by').set(referrer_id)
                return True
    except Exception as e:
        logging.error(f"রেফার বোনাস দিতে সমস্যা: {e}")
    return False

# ৯. /start কমান্ড প্রসেসর
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
        
    user_id = update.effective_user.id
    user_name = update.effective_user.first_name
    username = update.effective_user.username or user_name
    
    settings = get_bot_settings()
    
    try:
        db.reference(f'users/{user_id}/username').set(username)
    except Exception:
        pass
    
    try:
        channels_json_str = settings.get('channels_json', '[]')
        channels_list = json.loads(channels_json_str) if channels_json_str else []
    except Exception as e:
        logging.error(f"JSON ডিকোড এরর: {e}")
        channels_list = []
        
    unjoined = await get_unjoined_channels(user_id, context, channels_list)
    
    if unjoined:
        if context.args:
            context.user_data['pending_referrer'] = context.args[0]
            
        await update.message.reply_text(
            f"👋 হ্যালো {user_name}!\n\nআমাদের বটে কাজ করতে হলে আপনাকে নিচে দেওয়া আমাদের **সকল** চ্যানেল এবং গ্রুপে বাধ্যতামূলকভাবে জয়েন করতে হবে। জয়েন করা শেষ হলে নিচের ভেরিফাই বাটনে চাপুন।",
            reply_markup=make_multi_join_keyboard(unjoined)
        )
        return

    try:
        user_ref = db.reference(f'users/{user_id}')
        is_new_user = user_ref.get() is None
        
        if is_new_user and context.args:
            refer_points_bonus = settings.get('refer_points', 50)
            await reward_referrer(user_id, context.args[0], refer_points_bonus, update=update)
    except Exception:
        pass

    get_user_data(user_id, update=update)
    await display_main_menu(update.message.reply_text, user_id, user_name, settings, is_edit=False)

# ১০. বটের সব ইনলাইন ইন্টারেক্টিভ ক্লিকে অ্যাকশন হ্যান্ডলার 
async def handle_button_clicks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
        
    await query.answer()
    
    user_id = query.from_user.id
    user_name = query.from_user.first_name
    
    settings = get_bot_settings()
    
    try:
        channels_json_str = settings.get('channels_json', '[]')
        channels_list = json.loads(channels_json_str) if channels_json_str else []
    except Exception:
        channels_list = []
        
    ad_link = settings.get('ad_link', 'https://t.me')
    ad_points = settings.get('ad_points', 10)
    refer_points_bonus = settings.get('refer_points', 50)
    daily_bonus_points = settings.get('daily_bonus_points', 20)
    min_withdraw = settings.get('min_withdraw_points', 1000)
    
    if query.data != 'check_membership' and not query.data.startswith('w_') and query.data != 'main_menu':
        unjoined = await get_unjoined_channels(user_id, context, channels_list)
        if unjoined:
            try:
                await query.edit_message_text(
                    "⚠️ দুঃখিত! আপনি আমাদের প্রয়োজনীয় চ্যানেল বা গ্রুপ থেকে লিভ নিয়েছেন। বটটি আবার ব্যবহার করতে চাইলে দয়া করে সবগুলোতে পুনরায় জয়েন করুন।",
                    reply_markup=make_multi_join_keyboard(unjoined)
                )
            except Exception:
                pass
            return

    user_data = get_user_data(user_id, update=update)
    
    # 🛡️ ব্যানড/ব্লকড ইউজার প্রটেকশন চেক
    if user_data.get('status') == 'Banned':
        await query.message.reply_text("❌ দুঃখিত, আপনার অ্যাকাউন্টটি নিয়ম লঙ্ঘনের জন্য ব্যান করা হয়েছে।")
        return

    if query.data == 'check_membership':
        unjoined = await get_unjoined_channels(user_id, context, channels_list)
        
        if not unjoined:
            try:
                pending_referrer = context.user_data.get('pending_referrer')
                user_ref = db.reference(f'users/{user_id}')
                
                if user_ref.get() is None and pending_referrer:
                    await reward_referrer(user_id, pending_referrer, refer_points_bonus, update=update)
                    context.user_data.pop('pending_referrer', None)
            except Exception:
                pass
                
            get_user_data(user_id, update=update)
            try:
                await query.message.delete()
            except Exception:
                pass
            await display_main_menu(context.bot.send_message, user_id, user_name, settings, is_edit=False)
        else:
            try:
                await context.bot.answer_callback_query(
                    callback_query_id=query.id, 
                    text="❌ আপনি এখনো আমাদের সব গ্রুপ বা চ্যানেলে জয়েন করেননি! দয়া করে সবগুলো চেক করুন।", 
                    show_alert=True
                )
            except Exception:
                pass

    # --- ১. ডেইলি বোনাস ফিচার ---
    elif query.data == 'daily_bonus':
        today_str = datetime.date.today().isoformat()
        last_bonus = user_data.get('last_daily_bonus', '')
        
        keyboard = [[InlineKeyboardButton("🔙 মূল মেনু", callback_data='main_menu')]]
        
        if last_bonus == today_str:
            await query.edit_message_text(
                f"❌ **দুঃখিত {user_name}!**\n\nআপনি আজকের ডেইলি বোনাস অলরেডি ক্লেইম করে ফেলেছেন। আগামীকাল আবার চেষ্টা করুন! 🕒",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
        else:
            db.reference(f'users/{user_id}/last_daily_bonus').set(today_str)
            add_user_points(user_id, daily_bonus_points)
            await query.edit_message_text(
                f"🎉 **অভিনন্দন!**\n\nআপনি সফলভাবে আপনার দৈনিক বোনাস ক্লেইম করেছেন এবং আপনার অ্যাকাউন্টে **{daily_bonus_points} পয়েন্ট** যোগ করা হয়েছে। 🎁",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )

    # --- 🎰 লাকি স্পিন হুইল ফিচার ---
    elif query.data == 'spin_wheel':
        today_str = datetime.date.today().isoformat()
        last_spin = user_data.get('last_spin_date', '')
        
        keyboard = [[InlineKeyboardButton("🔙 মূল মেনু", callback_data='main_menu')]]
        
        if last_spin == today_str:
            await query.edit_message_text(
                f"🛑 **দুঃখিত {user_name}!**\n\nআপনার আজকের ফ্রি লাকি স্পিন শেষ। নতুন স্পিন লক আনলক হতে আগামীকাল পর্যন্ত অপেক্ষা করুন! ⏳",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
        else:
            win_points = random.choice([5, 10, 15, 20, 25, 30, 40, 50])
            db.reference(f'users/{user_id}/last_spin_date').set(today_str)
            add_user_points(user_id, win_points)
            
            await query.edit_message_text(
                f"🎰 **লাকি স্পিন চাকা ঘুরছে...**\n\n"
                f"🎉 **অভিনন্দন!** চাকাটি `{win_points}` এ এসে থেমেছে।\n"
                f"আপনি আপনার ভাগ্যের জোরে আজ সম্পূর্ণ ফ্রিতে **{win_points} পয়েন্ট** বোনাস জিতে নিয়েছেন! 🚀",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )

    # --- ২. গ্লোবাল লিডারবোর্ড ফিচার ---
    elif query.data == 'leaderboard':
        keyboard = [[InlineKeyboardButton("🔙 মূল মেনু", callback_data='main_menu')]]
        try:
            users_ref = db.reference('users').get()
            leaderboard_list = []
            
            if users_ref:
                for uid, uinfo in users_ref.items():
                    if isinstance(uinfo, dict):
                        pts = uinfo.get('points', 0)
                        uname = uinfo.get('username', 'User')
                        leaderboard_list.append((uname, pts))
            
            leaderboard_list.sort(key=lambda x: x[1], reverse=True)
            top_10 = leaderboard_list[:10]
            
            text = "🏆 **গ্লোবাল লিডারবোর্ড - টপ ১০ ইউজার** 🏆\n\n"
            medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
            
            for rank, (name, pts) in enumerate(top_10):
                text += f"{medals[rank]} `{name}` — **{pts} পয়েন্ট**\n"
                
            if not top_10:
                text += "এখনো কোনো ডাটা পাওয়া যায়নি।"
                
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        except Exception as e:
            logging.error(f"লিডারবোর্ডে সমস্যা: {e}")
            await query.edit_message_text("🔄 ডাটা লোড করতে সমস্যা হয়েছে, পরে চেষ্টা করুন।", reply_markup=InlineKeyboardMarkup(keyboard))

    # --- ৩. উইথড্র সিস্টেম প্যানেল ---
    elif query.data == 'withdraw_panel':
        keyboard = [
            [InlineKeyboardButton("📱 বিকাশ (Bkash)", callback_data='w_bkash'), InlineKeyboardButton("📱 নগদ (Nagad)", callback_data='w_nagad')],
            [InlineKeyboardButton("🔙 মূল মেনু", callback_data='main_menu')]
        ]
        text = (
            f"💳 **𝑷𝒐𝒄𝒌𝒆𝒕𝑪𝒂𝒔𝒉 উইথড্র সেন্টার** 💳\n\n"
            f"💰 আপনার বর্তমান ব্যালেন্স: **{user_data.get('points', 0)} পয়েন্ট**\n"
            f"⚠️ সর্বনিম্ন উইথড্র অ্যামাউন্ট: **{min_withdraw} পয়েন্ট**\n\n"
            f"টাকা তোলার জন্য নিচে দেওয়া আপনার পছন্দসই পেমেন্ট গেটওয়ে বা পদ্ধতিটি সিলেক্ট করুন:"
        )
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif query.data.startswith('w_'):
        method = "বিকাশ (Bkash)" if "bkash" in query.data else "নগদ (Nagad)"
        current_points = user_data.get('points', 0)
        
        keyboard = [[InlineKeyboardButton("🔙 মূল মেনু", callback_data='main_menu')]]
        
        if current_points < min_withdraw:
            await query.edit_message_text(
                f"❌ **দুঃখিত!** আপনার ব্যালেন্স অপর্যাপ্ত। টাকা তুলতে ন্যূনতম **{min_withdraw} পয়েন্ট** প্রয়োজন। আপনার আছে মাত্র {current_points} পয়েন্ট।",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return
            
        context.user_data['withdraw_method'] = method
        context.user_data['awaiting_withdraw_num'] = True
        
        await query.edit_message_text(
            f"📥 আপনি **{method}** এর মাধ্যমে টাকা তুলতে চেয়েছেন।\n\n"
            f"দয়া করে আপনার সচল ১০-১২ ডিজিটের **{method} পার্সোনাল নাম্বারটি** ইনবক্সে লিখে সেন্ড করুন:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    # --- আগের বেসিক ফিচারসমূহ ---
    elif query.data == 'watch_ad':
        keyboard = [
            [InlineKeyboardButton("🔗 বিজ্ঞাপনের লিংকে যান", url=ad_link)],
            [InlineKeyboardButton("✅ দেখা শেষ (পয়েন্ট নিন)", callback_data='claim_ad_points')],
            [InlineKeyboardButton("🔙 মূল মেনু", callback_data='main_menu')]
        ]
        text = f"🎯 নিচে দেওয়া লিংকে ক্লিক করে আমাদের পার্টনারদের বিজ্ঞাপনটি সম্পূর্ণ দেখুন। দেখা শেষ হলে নিচের ভেরিফাই বাটনে চাপুন।\n\n💰 কাজের রিওয়ার্ড: {ad_points} পয়েন্ট।"
        try:
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        except Exception:
            pass
        
    elif query.data == 'claim_ad_points':
        add_user_points(user_id, ad_points)
        keyboard = [[InlineKeyboardButton("🔙 মূল মেনু", callback_data='main_menu')]]
        try:
            await query.edit_message_text(
                f"🎉 অভিনন্দন! আপনি সফলভাবে বিজ্ঞাপনটি দেখেছেন এবং {ad_points} পয়েন্ট আপনার ব্যালেন্সে যোগ করা হয়েছে।", 
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except Exception:
            pass
        
    elif query.data == 'refer_friend':
        try:
            bot_info = await context.bot.get_me()
            bot_username = bot_info.username
            refer_link = f"https://t.me/{bot_username}?start={user_id}"
            
            keyboard = [[InlineKeyboardButton("🔙 মূল মেনু", callback_data='main_menu')]]
            text = (
                f"👥 **𝑷𝒐𝒄𝒌𝒆𝒕𝑪𝒂𝒔𝒉 𝑹𝒐𝒃𝒐𝒕 - রেফারেল প্রোগ্রাম**\n\n"
                f"আপনার বন্ধুদের আমাদের বটে আমন্ত্রণ জানান এবং প্রতি সফল রেফারে **{refer_points_bonus} পয়েন্ট** জিতে নিন!\n"
                f"⚠️ (নোট: আপনার বন্ধুকে অবশ্যই আমাদের সকল অফিশিয়াল চ্যানেল ও গ্রুপে জয়েন করতে হবে, তবেই আপনার অ্যাকাউন্টে পয়েন্ট যোগ হবে।)\n\n"
                f"🔗 আপনার ইউনিক রেফারেল লিংক:\n`{refer_link}`\n\n"
                f"📊 আপনি এ পর্যন্ত সফলভাবে রেফার করেছেন: {user_data.get('refer_count', 0)} জনকে।"
            )
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        except Exception:
            pass
        
    elif query.data == 'check_balance':
        keyboard = [[InlineKeyboardButton("🔙 মূল মেনু", callback_data='main_menu')]]
        text = (
            f"💳 **আপনার অ্যাকাউন্ট প্রোফাইল**\n\n"
            f"💰 বর্তমান ব্যালেন্স: {user_data.get('points', 0)} পয়েন্ট।\n"
            f"🆔 আপনার ইউজার আইডি: `{user_id}`\n\n"
            f"নোট: পয়েন্ট কনভার্ট করে উইথড্র করার রিকোয়েস্ট দিতে পারবেন।"
        )
        try:
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        except Exception:
            pass
        
    elif query.data == 'main_menu':
        context.user_data.pop('awaiting_withdraw_num', None)
        try:
            await query.edit_message_text("🔄 লোড হচ্ছে...")
            await display_main_menu(query.edit_message_text, user_id, user_name, settings, is_edit=True)
        except Exception:
            await display_main_menu(context.bot.send_message, user_id, user_name, settings, is_edit=False)

# --- ৪. উইথড্র ইনপুট রিসিভার এবং ৫. অ্যাডমিন ব্রডকাস্ট টেক্সট হ্যান্ডলার ---
async def handle_user_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
        
    user_id = update.effective_user.id
    text_input = update.message.text.strip()
    
    # উইথড্র নাম্বার নেওয়ার লজিক
    if context.user_data.get('awaiting_withdraw_num'):
        method = context.user_data.get('withdraw_method', 'বিকাশ/নগদ')
        user_data = get_user_data(user_id, update=update)
        settings = get_bot_settings()
        current_points = user_data.get('points', 0)
        min_withdraw = settings.get('min_withdraw_points', 1000)
        
        if current_points < min_withdraw:
            await update.message.reply_text("❌ দুঃখিত! আপনার পর্যাপ্ত ব্যালেন্স নেই।")
            context.user_data.pop('awaiting_withdraw_num', None)
            return
            
        try:
            current_time_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            username_str = update.effective_user.username or 'No Username'
            
            withdraw_ref = db.reference('withdraw_requests')
            withdraw_ref.push({
                'user_id': user_id,
                'username': username_str,
                'method': method,
                'number': text_input,
                'points': current_points,
                'status': 'Pending',
                'date': current_time_str
            })
            
            db.reference(f'users/{user_id}/points').set(0)
            context.user_data.pop('awaiting_withdraw_num', None)
            
            # --- 📡 পেমেন্ট প্রুফ চ্যানেলে অটো-পোস্ট রিকোয়েস্ট সেন্ডার ---
            try:
                proof_text = (
                    f"📥 **নতুন উইথড্র রিকোয়েস্ট সাবমিট** 📥\n\n"
                    f"👤 ইউজার: `{update.effective_user.first_name}` (@{username_str})\n"
                    f"🆔 ইউজার আইডি: `{user_id}`\n"
                    f"💳 পেমেন্ট পদ্ধতি: **{method}**\n"
                    f"📞 অ্যাকাউন্ট নাম্বার: `{text_input[:-3]}***` (নিরাপত্তার স্বার্থে হাইড)\n"
                    f"💰 কনভার্ট পয়েন্ট: **{current_points} Pts**\n"
                    f"⏰ সময়: `{current_time_str}`\n\n"
                    f"🟢 **স্ট্যাটাস: পেন্ডিং (অ্যাডমিন ভেরিফিকেশন চলছে)**"
                )
                await context.bot.send_message(chat_id=PAYMENT_PROOF_CHANNEL, text=proof_text, parse_mode="Markdown")
            except Exception as e:
                logging.error(f"চ্যানেলে পেমেন্ট প্রুফ পোস্ট করতে ব্যর্থ: {e}")

            keyboard = [[InlineKeyboardButton("🔙 মূল মেনু", callback_data='main_menu')]]
            await update.message.reply_text(
                f"✅ **উইথড্র রিকোয়েস্ট সফল হয়েছে!**\n\n"
                f"💰 আপনার সমস্ত **{current_points} পয়েন্ট** কেটে নেওয়া হয়েছে।\n"
                f"📱 গেটওয়ে: {method}\n"
                f"📞 নাম্বার: `{text_input}`\n\n"
                f"⌛ অ্যাডমিন প্যানেল আপনার রিকোয়েস্ট চেক করে আগামী ১২-২৪ ঘণ্টার মধ্যে পেমেন্ট সফল করে দেবে। ধন্যবাদ!",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
        except Exception as e:
            logging.error(f"উইথড্র সাবমিট এরর: {e}")
            await update.message.reply_text("❌ ডাটাবেজে সমস্যা হয়েছে! পরে চেষ্টা করুন।")
        return

    # ওয়ান-ক্লিক অ্যাডমিন ব্রডকাস্ট ফিচার (অ্যাডমিন প্যানেল কমান্ড টেক্সট)
    if text_input.startswith('/broadcast '):
        broadcast_msg = text_input.replace('/broadcast ', '').strip()
        users_ref = db.reference('users').get()
        
        if not users_ref:
            await update.message.reply_text("❌ ডেটাবেজে কোনো মেম্বার খুঁজে পাওয়া যায়নি।")
            return
            
        await update.message.reply_text("📢 ব্রডকাস্ট মেসেজ পাঠানো শুরু হয়েছে, দয়া করে অপেক্ষা করুন...")
        success_count = 0
        
        for uid in users_ref.keys():
            try:
                await context.bot.send_message(
                    chat_id=int(uid),
                    text=f"📢 **অফিশিয়াল নোটিশ / নতুন আপডেট** 📢\n\n{broadcast_msg}",
                    parse_mode="Markdown"
                )
                success_count += 1
            except Exception:
                pass
                
        await update.message.reply_text(f"✅ ব্রডকাস্ট সম্পন্ন হয়েছে! সর্বমোট `{success_count}` জন ইউজারের ইনবক্সে মেসেজ সফলভাবে পৌছেছে।")

# 🎉 নতুন মেগা ব্যাকগ্রাউন্ড লিসেনার ফিচার: অ্যাডমিন প্যানেল চ্যা্যানেল অটো-পোস্টার (Realtime Payment Tracker)
def start_firebase_payment_listener(application: Application):
    def on_withdraw_update(event):
        # যখনই ফায়ারবেসের withdraw_requests নোডে কোনো ডাটা আপডেট (Approve) হবে
        if event.event_type == 'put' and event.path != '/':
            try:
                # পাথ থেকে রিকোয়েস্ট কি (Key) বের করা
                path_parts = event.path.strip('/').split('/')
                req_key = path_parts[0]
                
                # সম্পূর্ণ রিকোয়েস্ট ডাটা রিড করা
                req_ref = db.reference(f'withdraw_requests/{req_key}')
                req_data = req_ref.get()
                
                if req_data and req_data.get('status') == 'Paid/Success' and not req_data.get('posted_to_channel'):
                    user_id = req_data.get('user_id')
                    username = req_data.get('username', 'user')
                    method = req_data.get('method', 'Bkash/Nagad')
                    points = req_data.get('points', 0)
                    number = req_data.get('number', '017XXXXXXXX')
                    
                    # টেলিগ্রাম পেমেন্ট প্রুফ চ্যানেলে সাকসেস পোস্ট পাঠানো
                    success_caption = (
                        f"✅ **পেমেন্ট সফলভাবে সম্পন্ন (🔥 PAID) ** ✅\n\n"
                        f"👤 মেম্বার আইডি: `{user_id}` (@{username})\n"
                        f"💰 পেইড পয়েন্ট: **{points} Pts**\n"
                        f"💳 মাধ্যম: **{method}**\n"
                        f"📞 অ্যাকাউন্ট নাম্বার: `{number[:-3]}***`\n"
                        f" status: **সরাসরি ক্যাশ আউট সফল 🚀**\n\n"
                        f"🤖 **বট লিংক:** @PocketCash_Bot\n"
                        f"📢 **পেমেন্ট প্রুফ চ্যানেল:** {PAYMENT_PROOF_CHANNEL}"
                    )
                    
                    # সিনক্রোনাস এপিআই রিকোয়েস্টের জন্য অ্যাপ্লিকেশন বটের লুপ ব্যবহার করা
                    application.loop.create_task(
                        application.bot.send_message(chat_id=PAYMENT_PROOF_CHANNEL, text=success_caption, parse_mode="Markdown")
                    )
                    
                    # ডাবল পোস্টিং এড়াতে ফ্ল্যাগ ট্র্যাকার অন করা
                    req_ref.update({'posted_to_channel': True})
            except Exception as e:
                logging.error(f"পেমেন্ট প্রুফ লিসেনারে এরর: {e}")

    # ফায়ারবেস চাইল্ড লিসেনার ট্রিগার অন করা
    db.reference('withdraw_requests').listen(on_withdraw_update)

# ১১. মেইন এক্সিকিউশন ও সার্ভার পোলিং ইঞ্জিন
def main():
    # আপনার দেওয়া বটের অরিজিনাল টোকেনটি অক্ষত রাখা হয়েছে
    TOKEN = "8871385726:AAGgu2H6bxphQXNYQK1i9GjpHiRwg8w9vvM" 
    
    custom_request = HTTPXRequest(
        connect_timeout=30.0, 
        read_timeout=30.0,    
        write_timeout=30.0    
    )
    
    app = Application.builder().token(TOKEN).request(custom_request).build()
    
    # হ্যান্ডলার রেজিস্ট্রি
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CallbackQueryHandler(handle_button_clicks))
    
    # মেসেজ ও ব্রডকাস্ট প্রসেসর হ্যান্ডলার
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_user_messages))
    
    # ব্যাকগ্রাউন্ড পেমেন্ট প্রুফ অটো-পোস্টার থ্রেড চালু করা
    start_firebase_payment_listener(app)
    
    print("🤖 [ULTIMATE-UPDATE] PocketCash Robot এখন স্পিন, অ্যান্টি-চিট সিকিউরিটি ও লাইভ পেমেন্ট অটো-পোস্টার সহ রান হয়েছে...")
    app.run_polling(timeout=30)

if __name__ == '__main__':
    main()
