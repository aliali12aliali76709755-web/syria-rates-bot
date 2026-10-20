"""Telegram bot: Syrian pound exchange rates. Arabic UI, inline-keyboard navigation,
history, charts and price alerts. Runs via long polling from FastAPI lifespan."""
import logging
import uuid
from datetime import datetime, timezone, timedelta

import re
import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters,
    ContextTypes,
)

import rates_service as rs
from charts import build_chart_png
from config import CURRENCIES, GOLD, CURRENCY_ORDER, GOLD_ORDER
from database import db, get_settings

logger = logging.getLogger("bot")

application: Application | None = None


# ----------------------------- WhatsApp Helpers -----------------------------
def html_to_whatsapp(text):
    # Convert <b>...</b> to *...*
    text = re.sub(r'<b>(.*?)</b>', r'*\1*', text)
    # Convert <code>...</code> to `...`
    text = re.sub(r'<code>(.*?)</code>', r'`\1`', text)
    # Convert <i>...</i> to _..._
    text = re.sub(r'<i>(.*?)</i>', r'_\1_', text)
    # Remove any other HTML tags
    text = re.sub(r'<[^>]+>', '', text)
    return text


async def send_whatsapp_message(text):
    settings = await get_settings()
    enabled = settings.get("whatsapp_enabled", False)
    if not enabled:
        return False
        
    instance_id = settings.get("whatsapp_instance_id")
    token = settings.get("whatsapp_token")
    chat_id = settings.get("whatsapp_chat_id")
    
    if not instance_id or not token or not chat_id:
        logger.warning("WhatsApp broadcasting is enabled but configurations are incomplete.")
        return False
        
    # Clean text to WhatsApp markdown
    wa_text = html_to_whatsapp(text)
    
    url = f"https://api.ultramsg.com/{instance_id}/messages/chat"
    payload = {
        "token": token,
        "to": chat_id,
        "body": wa_text,
        "priority": 10
    }
    
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(url, data=payload, timeout=15)
            if r.status_code == 200:
                logger.info(f"WhatsApp message successfully sent to {chat_id}")
                return True
            else:
                logger.error(f"Failed to send WhatsApp message. Status: {r.status_code}, Res: {r.text}")
                return False
    except Exception as e:
        logger.error(f"Exception raised while sending WhatsApp message: {e}")
        return False


def html_to_facebook(text):
    # Convert HTML formatting to clean plain text for Facebook feed posts
    text = re.sub(r'<b>(.*?)</b>', r'\1', text)
    text = re.sub(r'<code>(.*?)</code>', r'\1', text)
    text = re.sub(r'<i>(.*?)</i>', r'\1', text)
    text = re.sub(r'<[^>]+>', '', text)
    return text


async def send_facebook_post(text):
    settings = await get_settings()
    enabled = settings.get("facebook_enabled", False)
    if not enabled:
        return False
        
    page_id = settings.get("facebook_page_id")
    token = settings.get("facebook_access_token")
    
    if not page_id or not token:
        logger.warning("Facebook broadcasting is enabled but configurations are incomplete.")
        return False
        
    fb_text = html_to_facebook(text)
    
    url = f"https://graph.facebook.com/v20.0/{page_id}/feed"
    payload = {
        "message": fb_text,
        "access_token": token
    }
    
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(url, data=payload, timeout=15)
            if r.status_code in (200, 201):
                logger.info(f"Facebook post successfully published to Page {page_id}")
                return True
            else:
                logger.error(f"Failed to post to Facebook. Status: {r.status_code}, Res: {r.text}")
                return False
    except Exception as e:
        logger.error(f"Exception raised while posting to Facebook: {e}")
        return False


# ----------------------------- helpers -----------------------------
def fmt(v):
    if v is None:
        return "—"
    av = abs(v)
    if av >= 1000:
        return f"{v:,.0f}"
    if av >= 1:
        return f"{v:,.2f}"
    return f"{v:.4f}"


async def track_user(user):
    if not user:
        return
    now = datetime.now(timezone.utc).isoformat()
    await db.users.update_one(
        {"chat_id": user.id},
        {"$set": {"username": user.username, "first_name": user.first_name,
                  "last_seen": now},
         "$setOnInsert": {"chat_id": user.id, "created_at": now}},
        upsert=True,
    )


def market_mid_for(entry):
    return rs.market_mid(entry)


def market_buy_sell(entry):
    if entry.get("liranews") and entry["liranews"].get("buy") is not None:
        return entry["liranews"]["buy"], entry["liranews"]["sell"]
    if entry.get("lirascope_market") and entry["lirascope_market"].get("buy") is not None:
        return entry["lirascope_market"]["buy"], entry["lirascope_market"]["sell"]
    return None, None


async def is_user_admin(chat_id):
    if chat_id == 6641619062:
        return True
    try:
        settings = await get_settings()
        admins = settings.get("authorized_admins", [])
        return chat_id in admins
    except Exception:
        return False


async def check_mandatory_sub(user_id, bot):
    settings = await get_settings()
    chans = settings.get("mandatory_channels", [])
    if not chans:
        return True, []
    
    not_joined = []
    for chan in chans:
        try:
            member = await bot.get_chat_member(chat_id=chan, user_id=user_id)
            if member.status not in ["member", "administrator", "creator"]:
                not_joined.append(chan)
        except Exception as e:
            logger.warning(f"Failed to check membership for user {user_id} in {chan}: {e}")
            # If checking fails (bot not admin or channel private/invalid), we do not block the user.
    return len(not_joined) == 0, not_joined


def mandatory_sub_kb(not_joined):
    buttons = []
    for chan in not_joined:
        clean_name = chan.replace("@", "")
        buttons.append([InlineKeyboardButton(f"📢 اشترك في {chan}", url=f"https://t.me/{clean_name}")])
    buttons.append([InlineKeyboardButton("🔄 تحقق من الاشتراك", callback_data="m:verify_sub")])
    return InlineKeyboardMarkup(buttons)


async def enforce_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return True
    
    # Exclude admin verify callback, password inputs and "صويري" keyword from sub check
    is_verify_callback = update.callback_query and update.callback_query.data == "m:verify_sub"
    text = update.message.text.strip() if (update.message and update.message.text) else ""
    state = context.user_data.get("await")
    is_admin_flow = text == "صويري" or (state and state.get("type") == "admin_pw")
    
    # Exclude admin actions and admin panel queries from subscription enforcement
    is_admin_action = (update.callback_query and update.callback_query.data.startswith("admin:")) or (update.message and text.startswith("/admin"))
    
    if is_verify_callback or is_admin_flow:
        return True
        
    if is_admin_action and await is_user_admin(user.id):
        return True
        
    ok, not_joined = await check_mandatory_sub(user.id, context.bot)
    if not ok:
        msg_text = (
            "⚠️ <b>عذراً، يجب عليك الاشتراك في قنواتنا أولاً لتفعيل البوت:</b>\n\n"
            "الرجاء الاشتراك بالقنوات أدناه ثم اضغط على زر التحقق 👇"
        )
        if update.callback_query:
            await update.callback_query.message.reply_text(msg_text, parse_mode=ParseMode.HTML, reply_markup=mandatory_sub_kb(not_joined))
        else:
            await update.message.reply_text(msg_text, parse_mode=ParseMode.HTML, reply_markup=mandatory_sub_kb(not_joined))
        return False
    return True


# ----------------------------- keyboards -----------------------------
def main_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💵 أسعار الصرف", callback_data="m:rates")],
        [InlineKeyboardButton("🪙 أسعار الذهب", callback_data="m:gold"),
         InlineKeyboardButton("🔄 محوّل العملات", callback_data="m:convert")],
        [InlineKeyboardButton("🔔 التنبيهات", callback_data="m:alerts"),
         InlineKeyboardButton("ℹ️ مساعدة", callback_data="m:help")],
        [InlineKeyboardButton("🔗 قنواتنا وقروباتنا", callback_data="m:networks")]
    ])


def currency_grid(prefix, include_all=False):
    rows, row = [], []
    for code in CURRENCY_ORDER:
        cfg = CURRENCIES[code]
        row.append(InlineKeyboardButton(f"{cfg['flag']} {code}", callback_data=f"{prefix}:{code}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    if include_all:
        rows.append([InlineKeyboardButton("📋 عرض كل الأسعار", callback_data="r:ALL")])
    rows.append([InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="m:main")])
    return InlineKeyboardMarkup(rows)


def rate_detail_kb(code):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔔 ضبط تنبيه", callback_data=f"an:{code}"),
         InlineKeyboardButton("🔄 تحويل", callback_data=f"cv:{code}")],
        [InlineKeyboardButton("🔙 رجوع للأسعار", callback_data="m:rates")],
    ])


def timeframe_kb(code):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("يوم", callback_data=f"c:{code}:1"),
         InlineKeyboardButton("7 أيام", callback_data=f"c:{code}:7"),
         InlineKeyboardButton("30 يوم", callback_data=f"c:{code}:30")],
        [InlineKeyboardButton("3 أشهر", callback_data=f"c:{code}:90"),
         InlineKeyboardButton("سنة", callback_data=f"c:{code}:365")],
        [InlineKeyboardButton("🔙 رجوع", callback_data=f"r:{code}")],
    ])


def back_main_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="m:main")]])


def gold_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔔 تنبيه ذهب عيار 24", callback_data="an:G24"),
         InlineKeyboardButton("🔔 تنبيه ذهب عيار 21", callback_data="an:G21")],
        [InlineKeyboardButton("🔔 تنبيه ذهب عيار 18", callback_data="an:G18")],
        [InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="m:main")]
    ])


# ----------------------------- text builders -----------------------------
WELCOME = (
    "🇸🇾 <b>بوت أسعار صرف الليرة السورية</b>\n\n"
    "أهلاً بك! متابعة لحظية ومباشرة لأسعار الصرف في السوق والمصرف المركزي وأسعار الذهب.\n\n"
    "اختر من القائمة بالأسفل 👇"
)

HELP = (
    "<b>ℹ️ الأوامر المتاحة</b>\n\n"
    "/start — القائمة الرئيسية\n"
    "/rates — عرض كل الأسعار\n"
    "/rate USD — سعر عملة محددة\n"
    "/gold — أسعار الذهب\n"
    "/convert — محوّل العملات\n"
    "/alerts — إدارة التنبيهات\n"
    "/help — هذه الرسالة\n\n"
    "💡 يمكنك أيضاً استخدام الأزرار للتنقّل بسهولة.\n"
    "🔔 التنبيهات: اختر عملة ثم «ضبط تنبيه» وأرسل السعر المستهدف."
)


def _src_line(label, d):
    if not d or d.get("mid") is None:
        return None
    return f"{label}: شراء <b>{fmt(d.get('buy'))}</b> | مبيع <b>{fmt(d.get('sell'))}</b> | وسطي <b>{fmt(d.get('mid'))}</b>"


async def get_trend_indicator(code, current_price, entry_or_item):
    ln = entry_or_item.get("liranews")
    if ln and ln.get("last_close") is not None and ln.get("last_close") > 0:
        lc = ln["last_close"]
        change = current_price - lc
        pct = (change / lc) * 100
        sign = "+" if change > 0 else ""
        emoji = "🔺" if change > 0 else ("🔻" if change < 0 else "➖")
        if change == 0:
            return " ➖ 0.00%"
        return f" {emoji} {sign}{pct:.2f}%"
    
    try:
        day_ago = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        cursor = db.rate_snapshots.find({"code": code, "ts": {"$lte": day_ago}}).sort("ts", -1).limit(1)
        res = await cursor.to_list(1)
        if res:
            prev = res[0]["mid"]
            if prev > 0:
                change = current_price - prev
                pct = (change / prev) * 100
                sign = "+" if change > 0 else ""
                emoji = "🔺" if change > 0 else ("🔻" if change < 0 else "➖")
                if change == 0:
                    return " ➖ 0.00%"
                return f" {emoji} {sign}{pct:.2f}%"
    except Exception as e:
        logger.warning(f"Error fetching 24h trend for {code}: {e}")
        
    return ""


def rate_detail_text(entry):
    lines = [f"{entry['flag']} <b>{entry['name_ar']} ({entry['code']})</b> مقابل الليرة السورية\n"]
    cbs = _src_line("🏦 المصرف المركزي", entry.get("lirascope_cbs"))
    mkt = _src_line("💹 السوق الموازي", entry.get("liranews") or entry.get("lirascope_market"))
    any_data = False
    for ln in (cbs, mkt):
        if ln:
            lines.append(ln)
            any_data = True
    if not any_data:
        lines.append("لا تتوفر بيانات لهذه العملة حالياً.")
    lines.append("\n<i>الأسعار بالليرة السورية لكل وحدة عملة.</i>")
    return "\n".join(lines)


async def get_trend_emoji(code, current_price, entry_or_item):
    if current_price is None:
        return ""
    ln = entry_or_item.get("liranews")
    if ln and ln.get("last_close") is not None and ln.get("last_close") > 0:
        lc = ln["last_close"]
        change = current_price - lc
        emoji = " 🔺" if change > 0 else (" 🔻" if change < 0 else " ➖")
        return emoji
    
    try:
        day_ago = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        cursor = db.rate_snapshots.find({"code": code, "ts": {"$lte": day_ago}}).sort("ts", -1).limit(1)
        res = await cursor.to_list(1)
        if res:
            prev = res[0]["mid"]
            if prev > 0:
                change = current_price - prev
                emoji = " 🔺" if change > 0 else (" 🔻" if change < 0 else " ➖")
                return emoji
    except Exception as e:
        logger.warning(f"Error fetching 24h trend emoji for {code}: {e}")
        
    return ""


async def all_rates_text(data):
    lines = ["💵 <b>أسعار الصرف مقابل الليرة السورية</b>\n"]
    for code in CURRENCY_ORDER:
        if code == "LBP":
            continue
        e = data["currencies"][code]
        buy, sell = market_buy_sell(e)
        mid = market_mid_for(e)
        trend = await get_trend_emoji(code, mid, e)
        name = e["name_ar"]
        
        if code == "IQD":
            name = "1000 دينار عراقي"
            if buy is not None:
                buy = buy * 1000
            if sell is not None:
                sell = sell * 1000
            if mid is not None:
                mid = mid * 1000
                
        if buy is not None and sell is not None:
            lines.append(f"{e['flag']} <b>{name}</b> — شراء: <b>{fmt(buy)}</b> | مبيع: <b>{fmt(sell)}</b>{trend}")
        else:
            lines.append(f"{e['flag']} <b>{name}</b> — وسطي: <b>{fmt(mid)}</b>{trend}")
            
    lines.append("\n🪙 <b>الذهب (غرام)</b>")
    for code in GOLD_ORDER:
        if code == "G24":
            continue
        g = data["gold"][code]
        v = g.get("liranews")
        mid = v["mid"] if v else None
        trend = await get_trend_emoji(code, mid, g) if mid else ""
        lines.append(f"• {g['name_ar']}: <b>{fmt(mid)}</b>{trend}")
        
    return "\n".join(lines)


async def whatsapp_rates_text(data):
    lines = ["💵 <b>أسعار الصرف مقابل الليرة السورية</b>\n"]
    
    # USD only
    code = "USD"
    e = data["currencies"][code]
    buy, sell = market_buy_sell(e)
    mid = market_mid_for(e)
    trend = await get_trend_emoji(code, mid, e)
    name = e["name_ar"]
    if buy is not None and sell is not None:
        lines.append(f"{e['flag']} <b>{name}</b> — شراء: <b>{fmt(buy)}</b> | مبيع: <b>{fmt(sell)}</b>{trend}")
    else:
        lines.append(f"{e['flag']} <b>{name}</b> — وسطي: <b>{fmt(mid)}</b>{trend}")
        
    lines.append("\n🪙 <b>الذهب (غرام)</b>")
    for code in GOLD_ORDER:
        if code == "G24":
            continue
        g = data["gold"][code]
        v = g.get("liranews")
        mid = v["mid"] if v else None
        trend = await get_trend_emoji(code, mid, g) if mid else ""
        lines.append(f"• {g['name_ar']}: <b>{fmt(mid)}</b>{trend}")
        
    lines.append(
        "\n📌 لمتابعة أسعار صرف الليرة السورية مقابل بقية العملات الأجنبية وتفاصيل الذهب، يرجى الانتقال إلى بوت التليجرام الخاص بنا المتاح دائماً وتلقائياً:\n"
        "🔗 http://t.me/SyriaRates4Bot"
    )
    return "\n".join(lines)


async def facebook_rates_text(data):
    lines = ["💵 أسعار الصرف مقابل الليرة السورية\n"]
    
    # USD and EUR
    for code in ["USD", "EUR"]:
        e = data["currencies"][code]
        buy, sell = market_buy_sell(e)
        mid = market_mid_for(e)
        trend = await get_trend_emoji(code, mid, e)
        name = e["name_ar"]
        if buy is not None and sell is not None:
            lines.append(f"{e['flag']} {name} — شراء: {fmt(buy)} | مبيع: {fmt(sell)}{trend}")
        else:
            lines.append(f"{e['flag']} {name} — وسطي: {fmt(mid)}{trend}")
            
    lines.append("\n🪙 الذهب (غرام)")
    for code in GOLD_ORDER:
        if code == "G24":
            continue
        g = data["gold"][code]
        v = g.get("liranews")
        mid = v["mid"] if v else None
        trend = await get_trend_emoji(code, mid, g) if mid else ""
        lines.append(f"• {g['name_ar']}: {fmt(mid)}{trend}")
        
    lines.append(
        "\n📌 لمتابعة بقية العملات والتحديثات اللحظية، يرجى الانتقال إلى بوت التليجرام الخاص بنا:\n"
        "🔗 http://t.me/SyriaRates4Bot"
    )
    return "\n".join(lines)


async def gold_text(data):
    lines = ["🪙 <b>أسعار الذهب (سعر الغرام بالليرة السورية)</b>\n"]
    for code in GOLD_ORDER:
        g = data["gold"][code]
        v = g.get("liranews")
        if v and v.get("mid") is not None:
            mid = v["mid"]
            trend = await get_trend_indicator(code, mid, g)
            lines.append(f"• <b>{g['name_ar']}</b>: {fmt(mid)}{trend}  "
                          f"(شراء {fmt(v.get('buy'))} / مبيع {fmt(v.get('sell'))})")
        else:
            lines.append(f"• {g['name_ar']}: —")
    lines.append("\n<i>يتم التحديث لحظياً بشكل تلقائي.</i>")
    return "\n".join(lines)


# ----------------------------- command handlers -----------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await track_user(update.effective_user)
    if not await enforce_subscription(update, context):
        return
    await update.message.reply_text(WELCOME, parse_mode=ParseMode.HTML, reply_markup=main_menu_kb())


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP, parse_mode=ParseMode.HTML, reply_markup=back_main_kb())


async def cmd_rates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await enforce_subscription(update, context):
        return
    await track_user(update.effective_user)
    data = await rs.get_rates()
    await update.message.reply_text(await all_rates_text(data), parse_mode=ParseMode.HTML,
                                    reply_markup=currency_grid("r"))


async def cmd_gold(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await enforce_subscription(update, context):
        return
    data = await rs.get_rates()
    await update.message.reply_text(await gold_text(data), parse_mode=ParseMode.HTML,
                                    reply_markup=gold_kb())


def _arg_code(context):
    if context.args:
        return context.args[0].upper()
    return None


async def cmd_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await enforce_subscription(update, context):
        return
    code = _arg_code(context)
    data = await rs.get_rates()
    if code and code in data["currencies"]:
        e = data["currencies"][code]
        await update.message.reply_text(rate_detail_text(e), parse_mode=ParseMode.HTML,
                                        reply_markup=rate_detail_kb(code))
    else:
        await update.message.reply_text("اختر العملة:", reply_markup=currency_grid("r", include_all=True))


async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("عذراً، تم إلغاء هذه الميزة من قبل الإدارة.")


async def cmd_chart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("عذراً، تم إلغاء هذه الميزة من قبل الإدارة.")


async def cmd_convert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await enforce_subscription(update, context):
        return
    await update.message.reply_text("🔄 اختر العملة لبدء عملية التحويل منها أو إليها:",
                                    reply_markup=currency_grid("cv"))


async def cmd_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await enforce_subscription(update, context):
        return
    await track_user(update.effective_user)
    text, kb = await alerts_view(update.effective_user.id)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def cmd_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await enforce_subscription(update, context):
        return
    text = (
        "📞 <b>التواصل والدعم الفني</b>\n\n"
        "للتواصل مع إدارة البوت أو الإبلاغ عن مشكلة، يرجى مراسلتنا على الحساب التالي:\n"
        "👤 <b>الآدمن:</b> @d91ik\n\n"
        "نسعد بخدمتكم في أي وقت!"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=back_main_kb())


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if await is_user_admin(user_id):
        await send_admin_panel(update, context)
    else:
        # Non-admins get no response so it appears the command does not exist
        return


# ----------------------------- shared senders -----------------------------
async def send_history(chat_id, context, code):
    pts, src = await rs.get_history(code, days=30)
    cfg = CURRENCIES[code]
    if not pts:
        await context.bot.send_message(
            chat_id,
            f"📈 لا يوجد سجل كافٍ لـ {cfg['flag']} {code} بعد.\n"
            "يتم تجميع البيانات التاريخية تلقائياً كل 15 دقيقة — عد لاحقاً.",
            reply_markup=back_main_kb())
        return
    recent = pts[-15:]
    lines = [f"📈 <b>سجل {cfg['name_ar']} ({code})</b> — آخر النقاط\n"]
    for dt, v in recent:
        lines.append(f"• {dt.strftime('%m-%d %H:%M')} — <b>{fmt(v)}</b>")
    first, last = pts[0][1], pts[-1][1]
    change = last - first
    pct = (change / first * 100) if first else 0
    sign = "🔺" if change >= 0 else "🔻"
    lines.append(f"\n{sign} التغيّر: <b>{fmt(change)}</b> ({pct:+.2f}%) خلال الفترة المسجلة")
    await context.bot.send_message(chat_id, "\n".join(lines), parse_mode=ParseMode.HTML,
                                   reply_markup=rate_detail_kb(code))


async def send_chart(chat_id, context, code, days, query_to_delete=None):
    pts, src = await rs.get_history(code, days=days)
    cfg = CURRENCIES[code]
    
    if query_to_delete:
        try:
            await query_to_delete.delete_message()
        except Exception:
            pass
            
    if len(pts) < 2:
        await context.bot.send_message(
            chat_id,
            f"📊 لا توجد بيانات كافية لرسم {cfg['flag']} {code} خلال هذه الفترة.\n"
            "يتم تجميع البيانات تلقائياً كل 15 دقيقة — عد لاحقاً.",
            reply_markup=timeframe_kb(code))
        return
    buf = build_chart_png(pts, code, days)
    await context.bot.send_photo(
        chat_id, photo=buf,
        caption=f"📊 {cfg['flag']} {cfg['name_ar']} ({code}) / SYP — آخر {days} يوم",
        reply_markup=timeframe_kb(code))


async def alerts_view(chat_id):
    alerts = await db.alerts.find({"chat_id": chat_id, "active": True}, {"_id": 0}).to_list(100)
    if not alerts:
        text = ("🔔 <b>التنبيهات</b>\n\nلا توجد تنبيهات فعّالة.\n\n"
                "لضبط تنبيه: افتح «أسعار الصرف» → اختر عملة → «ضبط تنبيه»، ثم أرسل السعر المستهدف.")
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("➕ إضافة تنبيه", callback_data="m:rates")],
                                   [InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="m:main")]])
        return text, kb
    lines = ["🔔 <b>تنبيهاتك الفعّالة</b>\n"]
    rows = []
    for a in alerts:
        cfg = CURRENCIES.get(a["code"]) or GOLD.get(a["code"], {"flag": "", "name_ar": a["code"]})
        dir_txt = "يرتفع إلى" if a["direction"] == "above" else "ينخفض إلى"
        flag_str = cfg.get("flag") + " " if cfg.get("flag") else ""
        lines.append(f"• {flag_str}<b>{cfg['name_ar']}</b> — عندما {dir_txt} <b>{fmt(a['target'])}</b>")
        rows.append([InlineKeyboardButton(f"🗑 حذف تنبيه {cfg['name_ar']} @ {fmt(a['target'])}",
                                          callback_data=f"al:del:{a['id']}")])
    rows.append([InlineKeyboardButton("➕ إضافة تنبيه", callback_data="m:rates")])
    rows.append([InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="m:main")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


# ----------------------------- admin panel builders -----------------------------
async def send_admin_panel(update_or_query, context, edit=False):
    total = await db.users.count_documents({})
    
    # Calculate stats based on created_at string comparison
    now = datetime.now(timezone.utc)
    today_iso = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    week_iso = (now - timedelta(days=7)).isoformat()
    month_iso = (now - timedelta(days=30)).isoformat()
    year_iso = (now - timedelta(days=365)).isoformat()
    
    today_users = await db.users.count_documents({"created_at": {"$gte": today_iso}})
    week_users = await db.users.count_documents({"created_at": {"$gte": week_iso}})
    month_users = await db.users.count_documents({"created_at": {"$gte": month_iso}})
    year_users = await db.users.count_documents({"created_at": {"$gte": year_iso}})
    
    settings = await get_settings()
    bc_count = len(settings.get("broadcast_channels", []))
    md_count = len(settings.get("mandatory_channels", []))
    ln_count = len(settings.get("recommended_links", []))
    
    wa_enabled = settings.get("whatsapp_enabled", False)
    wa_status_str = "🟢 مفعل" if wa_enabled else "🔴 معطل"
    wa_chat = settings.get("whatsapp_chat_id", "غير محدد")
    
    fb_enabled = settings.get("facebook_enabled", False)
    fb_status_str = "🟢 مفعل" if fb_enabled else "🔴 معطل"
    fb_page = settings.get("facebook_page_id", "غير محدد")
    
    text = (
        "⚙️ <b>لوحة التحكم للآدمن (صويري)</b>\n\n"
        "📊 <b>إحصائيات المستخدمين:</b>\n"
        f"• إجمالي المستخدمين: <b>{total}</b>\n"
        f"• مستخدمي اليوم (UTC): <b>{today_users}</b>\n"
        f"• مستخدمي الأسبوع: <b>{week_users}</b>\n"
        f"• مستخدمي الشهر: <b>{month_users}</b>\n"
        f"• مستخدمي السنة: <b>{year_users}</b>\n\n"
        "📣 <b>الإعدادات الحالية:</b>\n"
        f"• قنوات البث التلقائي للأسعار: <b>{bc_count}</b>\n"
        f"• القنوات المجبرة (الاشتراك الإجباري): <b>{md_count}</b>\n"
        f"• الروابط وقنوات التوجيه الموصى بها: <b>{ln_count}</b>\n"
        f"• نشر الواتساب التلقائي: <b>{wa_status_str}</b> (الدردشة: <code>{wa_chat}</code>)\n"
        f"• نشر الفيسبوك التلقائي: <b>{fb_status_str}</b> (الصفحة: <code>{fb_page}</code>)"
    )
    
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📣 قنوات البث التلقائي", callback_data="admin:broadcast_ch")],
        [InlineKeyboardButton("🔐 قنوات الاشتراك الإجباري", callback_data="admin:mandatory_ch")],
        [InlineKeyboardButton("🔗 الروابط الموصى بها", callback_data="admin:links")],
        [InlineKeyboardButton("👥 إدارة المشرفين", callback_data="admin:admins_mgr")],
        [InlineKeyboardButton("💬 إعدادات الواتساب", callback_data="admin:wa_settings")],
        [InlineKeyboardButton("📘 إعدادات فيسبوك", callback_data="admin:fb_settings")],
        [InlineKeyboardButton("✉️ إذاعة جماعية (Broadcast)", callback_data="admin:broadcast_msg")],
        [InlineKeyboardButton("❌ إغلاق لوحة التحكم", callback_data="admin:close")]
    ])
    
    if edit and isinstance(update_or_query, Update) and update_or_query.callback_query:
        await update_or_query.callback_query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
    elif edit and not isinstance(update_or_query, Update):
        await update_or_query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
    else:
        if isinstance(update_or_query, Update):
            await update_or_query.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        else:
            await update_or_query.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


# ----------------------------- admin view helpers -----------------------------
async def show_admins_view(q, settings):
    admins = settings.get("authorized_admins", [])
    text = (
        "👥 <b>إدارة المشرفين والحسابات المخولة</b>\n\n"
        "المشرفون الذين يملكون صلاحية دخول لوحة التحكم:\n"
        "• <code>6641619062</code> (المالك الأساسي 👑)\n"
    )
    rows = []
    for admin_id in admins:
        text += f"• <code>{admin_id}</code>\n"
        rows.append([InlineKeyboardButton(f"🗑 سحب الصلاحية من {admin_id}", callback_data=f"admin:del_admin:{admin_id}")])
    
    rows.append([InlineKeyboardButton("➕ إضافة مشرف جديد", callback_data="admin:add_admin")])
    rows.append([InlineKeyboardButton("🔙 رجوع للوحة التحكم", callback_data="admin:main")])
    await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))


async def show_whatsapp_settings_view(q, settings):
    wa_enabled = settings.get("whatsapp_enabled", False)
    wa_status_str = "🟢 مفعل" if wa_enabled else "🔴 معطل"
    wa_instance = settings.get("whatsapp_instance_id", "غير محدد")
    wa_token = settings.get("whatsapp_token", "غير محدد")
    wa_chat = settings.get("whatsapp_chat_id", "غير محدد")
    
    text = (
        "💬 <b>إعدادات نشر الواتساب التلقائي (UltraMsg)</b>\n\n"
        "لربط البوت بمجموعة أو قناة واتساب للنشر التلقائي عند تغير الأسعار:\n\n"
        f"• <b>حالة النشر التلقائي:</b> {wa_status_str}\n"
        f"• <b>معرف المثيل (Instance ID):</b> <code>{wa_instance}</code>\n"
        f"• <b>رمز الوصول (Token):</b> <code>{wa_token}</code>\n"
        f"• <b>معرف المجموعة/الدردشة (Chat ID):</b> <code>{wa_chat}</code>\n\n"
        "⚠️ <i>ملاحظة:</i> يمكنك الحصول على هذه البيانات مجاناً من موقع ultramsg.com بعد إنشاء حساب ومسح رمز QR الخاص بالواتساب."
    )
    
    toggle_text = "🔴 تعطيل النشر" if wa_enabled else "🟢 تفعيل النشر"
    
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(toggle_text, callback_data="admin:wa_toggle")],
        [InlineKeyboardButton("📝 تعيين Instance ID", callback_data="admin:wa_set_instance"),
         InlineKeyboardButton("📝 تعيين Token", callback_data="admin:wa_set_token")],
        [InlineKeyboardButton("📝 تعيين Chat ID", callback_data="admin:wa_set_chat")],
        [InlineKeyboardButton("🧪 إرسال رسالة تجريبية", callback_data="admin:wa_test")],
        [InlineKeyboardButton("🔙 رجوع للوحة التحكم", callback_data="admin:main")]
    ])
    await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb, disable_web_page_preview=True)


async def show_facebook_settings_view(q, settings):
    fb_enabled = settings.get("facebook_enabled", False)
    fb_status_str = "🟢 مفعل" if fb_enabled else "🔴 معطل"
    fb_page_id = settings.get("facebook_page_id", "غير محدد")
    fb_token = settings.get("facebook_access_token", "غير محدد")
    
    text = (
        "📘 <b>إعدادات نشر فيسبوك التلقائي (Facebook Page API)</b>\n\n"
        "لربط البوت بصفحة فيسبوك للنشر التلقائي للأسعار والذهب:\n\n"
        f"• <b>حالة النشر التلقائي:</b> {fb_status_str}\n"
        f"• <b>معرف الصفحة (Page ID):</b> <code>{fb_page_id}</code>\n"
        f"• <b>رمز وصول الصفحة (Access Token):</b> <code>{fb_token}</code>\n\n"
        "⚠️ <i>ملاحظة:</i> للحصول على رمز الوصول الدائم ومعرف الصفحة، يرجى اتباع التعليمات المخصصة على بوابة مطوري فيسبوك (Meta developers)."
    )
    
    toggle_text = "🔴 تعطيل النشر" if fb_enabled else "🟢 تفعيل النشر"
    
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(toggle_text, callback_data="admin:fb_toggle")],
        [InlineKeyboardButton("📝 تعيين معرف الصفحة (Page ID)", callback_data="admin:fb_set_page")],
        [InlineKeyboardButton("📝 تعيين رمز الوصول (Access Token)", callback_data="admin:fb_set_token")],
        [InlineKeyboardButton("🧪 تجربة النشر على فيسبوك", callback_data="admin:fb_test")],
        [InlineKeyboardButton("🔙 رجوع للوحة التحكم", callback_data="admin:main")]
    ])
    await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb, disable_web_page_preview=True)


async def show_broadcast_ch_view(q, settings):
    chans = settings.get("broadcast_channels", [])
    text = "📣 <b>قنوات البث التلقائي للأسعار</b>\n\nالقنوات المضافة حالياً:\n"
    rows = []
    for c in chans:
        text += f"• <code>{c}</code>\n"
        rows.append([InlineKeyboardButton(f"🗑 حذف {c}", callback_data=f"admin:del_bc:{c}")])
    text += "\nالبوت يقوم بالإرسال تلقائياً في الساعة 9:00 صباحاً وعند أي تغير بالأسعار."
    rows.append([InlineKeyboardButton("➕ إضافة قناة بث", callback_data="admin:add_bc")])
    rows.append([InlineKeyboardButton("🔙 رجوع للوحة التحكم", callback_data="admin:main")])
    await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))

async def show_mandatory_ch_view(q, settings):
    chans = settings.get("mandatory_channels", [])
    text = "🔐 <b>قنوات الاشتراك الإجباري</b>\n\nيجب على المستخدمين الاشتراك في هذه القنوات لاستخدام البوت:\n"
    rows = []
    for c in chans:
        text += f"• <code>{c}</code>\n"
        rows.append([InlineKeyboardButton(f"🗑 حذف {c}", callback_data=f"admin:del_md:{c}")])
    rows.append([InlineKeyboardButton("➕ إضافة قناة مجبرة", callback_data="admin:add_md")])
    rows.append([InlineKeyboardButton("🔙 رجوع للوحة التحكم", callback_data="admin:main")])
    await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))

async def show_links_view(q, settings):
    links = settings.get("recommended_links", [])
    text = "🔗 <b>الروابط وقنوات التوجيه الموصى بها</b>\n\nالروابط الحالية:\n"
    rows = []
    for i, l in enumerate(links):
        text += f"• {l['category']} — <a href='{l['url']}'>{l['title']}</a>\n"
        rows.append([InlineKeyboardButton(f"🗑 حذف {l['title']}", callback_data=f"admin:del_link:{i}")])
    rows.append([InlineKeyboardButton("➕ إضافة رابط", callback_data="admin:add_link")])
    rows.append([InlineKeyboardButton("🔙 رجوع للوحة التحكم", callback_data="admin:main")])
    await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))


# ----------------------------- callback router -----------------------------
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    
    user_id = update.effective_user.id
    dataparts = q.data.split(":")
    head = dataparts[0]
    chat_id = q.message.chat_id
    
    # 1. Check Mandatory sub (Verify Sub callback is allowed without block)
    if head == "m" and dataparts[1] == "verify_sub":
        ok, not_joined = await check_mandatory_sub(user_id, context.bot)
        if ok:
            await q.edit_message_text("🎉 تم التحقق من الاشتراك بنجاح! يمكنك الآن استخدام البوت بكامل ميزاته.", reply_markup=back_main_kb())
        else:
            await q.answer("❌ لم تشترك في جميع القنوات بعد! يرجى الاشتراك ثم الضغط مرة أخرى.", show_alert=True)
        return

    # Rest of the callbacks require verification
    if not await enforce_subscription(update, context):
        return

    # Admin actions
    if head == "admin":
        if not await is_user_admin(user_id):
            await q.answer()
            return
            
        action = dataparts[1]
        settings = await get_settings()
        
        if action == "main":
            await send_admin_panel(q, context, edit=True)
            
        elif action == "broadcast_ch":
            await show_broadcast_ch_view(q, settings)
            
        elif action == "add_bc":
            context.user_data["await"] = {"type": "add_bc"}
            await q.edit_message_text("📣 <b>إضافة قناة بث تلقائي</b>\n\nأرسل يوزر القناة العام يبدأ بـ @ (مثال: <code>@SyriaRates</code>):\n\n⚠️ <i>تأكد من ترقية البوت مشرفاً في القناة أولاً لتفعيل الإرسال.</i>", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 إلغاء", callback_data="admin:broadcast_ch")]]))
            
        elif action.startswith("del_bc:"):
            target_chan = q.data[13:] # admin:del_bc:
            await db.settings.update_one({"_id": "settings"}, {"$pull": {"broadcast_channels": target_chan}})
            await q.answer("تم حذف قناة البث بنجاح")
            fresh_settings = await get_settings()
            await show_broadcast_ch_view(q, fresh_settings)
            
        elif action == "mandatory_ch":
            await show_mandatory_ch_view(q, settings)
            
        elif action == "add_md":
            context.user_data["await"] = {"type": "add_md"}
            await q.edit_message_text("🔐 <b>إضافة قناة اشتراك إجباري</b>\n\nأرسل يوزر القناة يبدأ بـ @ (مثال: <code>@MyChannel</code>):", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 إلغاء", callback_data="admin:mandatory_ch")]]))
            
        elif action.startswith("del_md:"):
            target_chan = q.data[13:]
            await db.settings.update_one({"_id": "settings"}, {"$pull": {"mandatory_channels": target_chan}})
            await q.answer("تم حذف قناة الاشتراك الإجباري")
            fresh_settings = await get_settings()
            await show_mandatory_ch_view(q, fresh_settings)
            
        elif action == "links":
            await show_links_view(q, settings)
            
        elif action == "add_link":
            context.user_data["await"] = {"type": "add_link_name"}
            await q.edit_message_text(
                "🔗 <b>إضافة رابط موصى به — خطوة 1 من 3</b>\n\n"
                "أرسل اسم القناة أو المجموعة (مثال: <code>قناة أسعار الصرف</code>):",
                parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 إلغاء", callback_data="admin:links")]]))
            
        elif action == "save_link":
            cat = dataparts[2]
            temp = context.user_data.pop("temp_link", None)
            context.user_data.pop("await", None)
            if temp:
                link_doc = {
                    "title": temp["title"],
                    "url": temp["url"],
                    "category": cat
                }
                await db.settings.update_one({"_id": "settings"}, {"$push": {"recommended_links": link_doc}}, upsert=True)
                await q.answer(f"✅ تم إضافة الرابط: {temp['title']}")
            fresh_settings = await get_settings()
            await show_links_view(q, fresh_settings)
            
        elif action.startswith("del_link:"):
            idx = int(dataparts[2])
            links = settings.get("recommended_links", [])
            if 0 <= idx < len(links):
                del links[idx]
                await db.settings.update_one({"_id": "settings"}, {"$set": {"recommended_links": links}})
                await q.answer("تم حذف الرابط بنجاح")
            fresh_settings = await get_settings()
            await show_links_view(q, fresh_settings)
            
        elif action == "broadcast_msg":
            context.user_data["await"] = {"type": "broadcast_msg"}
            await q.edit_message_text("✉️ <b>إذاعة جماعية</b>\n\nأرسل نص الرسالة التي ترغب في إذاعتها لجميع مستخدمي البوت المسجلين:", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 إلغاء", callback_data="admin:main")]]))
            
        elif action == "admins_mgr":
            await show_admins_view(q, settings)
            
        elif action == "add_admin":
            context.user_data["await"] = {"type": "add_admin_id"}
            await q.edit_message_text("👥 <b>إضافة مشرف جديد</b>\n\nأرسل الآيدي الرقمي (Telegram ID) الخاص بالحساب الذي ترغب في ترقيته لمشرف (مثال: <code>123456789</code>):", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 إلغاء", callback_data="admin:admins_mgr")]]))
            
        elif action.startswith("del_admin:"):
            target_admin = int(dataparts[2])
            await db.settings.update_one({"_id": "settings"}, {"$pull": {"authorized_admins": target_admin}})
            await q.answer(f"تم سحب الصلاحية من {target_admin}")
            fresh_settings = await get_settings()
            await show_admins_view(q, fresh_settings)
            
        elif action == "wa_settings":
            await show_whatsapp_settings_view(q, settings)
            
        elif action == "wa_toggle":
            enabled = not settings.get("whatsapp_enabled", False)
            await db.settings.update_one({"_id": "settings"}, {"$set": {"whatsapp_enabled": enabled}}, upsert=True)
            await q.answer("تم تغيير حالة النشر التلقائي للواتساب")
            fresh_settings = await get_settings()
            await show_whatsapp_settings_view(q, fresh_settings)
            
        elif action == "wa_set_instance":
            context.user_data["await"] = {"type": "wa_instance_id"}
            await q.edit_message_text("📝 <b>إدخال Instance ID (معرف المثيل)</b>\n\nيرجى إرسال معرف المثيل من UltraMsg (مثال: <code>instance12345</code>):", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 إلغاء", callback_data="admin:wa_settings")]]))
            
        elif action == "wa_set_token":
            context.user_data["await"] = {"type": "wa_token"}
            await q.edit_message_text("📝 <b>إدخال Token (رمز الوصول)</b>\n\nيرجى إرسال رمز الوصول الخاص بـ UltraMsg:", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 إلغاء", callback_data="admin:wa_settings")]]))
            
        elif action == "wa_set_chat":
            context.user_data["await"] = {"type": "wa_chat_id"}
            await q.edit_message_text(
                "📝 <b>إدخال Chat ID (معرف المجموعة/الدردشة)</b>\n\n"
                "يرجى إرسال معرف الواتساب المستهدف:\n"
                "• للمجموعات: ينتهي بـ <code>@g.us</code> (مثال: <code>12036319703700@g.us</code>)\n"
                "• للقنوات/النشرات: ينتهي بـ <code>@newsletter</code> (مثال: <code>12036319703700@newsletter</code>)\n"
                "• لرقم هاتف عادي: اكتب الرقم بصيغة دولية بدون أصفار أو علامة + (مثال: <code>963999999999</code>)", 
                parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 إلغاء", callback_data="admin:wa_settings")]]))
                
        elif action == "wa_test":
            await q.answer("جاري إرسال الرسالة التجريبية...")
            success = await send_whatsapp_message("🧪 <b>رسالة تجريبية من بوت أسعار الصرف السورية!</b>\n\nتم ربط البوت بالواتساب بنجاح.")
            if success:
                await q.answer("✅ تم إرسال الرسالة التجريبية بنجاح!", show_alert=True)
            else:
                await q.answer("❌ فشل إرسال الرسالة. يرجى التحقق من صحة البيانات وحالة المثيل في UltraMsg.", show_alert=True)
                
        elif action == "fb_settings":
            await show_facebook_settings_view(q, settings)
            
        elif action == "fb_toggle":
            enabled = not settings.get("facebook_enabled", False)
            await db.settings.update_one({"_id": "settings"}, {"$set": {"facebook_enabled": enabled}}, upsert=True)
            await q.answer("تم تغيير حالة النشر التلقائي للفيسبوك")
            fresh_settings = await get_settings()
            await show_facebook_settings_view(q, fresh_settings)
            
        elif action == "fb_set_page":
            context.user_data["await"] = {"type": "fb_page_id"}
            await q.edit_message_text("📝 <b>إدخال Page ID (معرف الصفحة)</b>\n\nيرجى إرسال معرف صفحة فيسبوك المستهدفة (مثال: <code>123456789012345</code>):", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 إلغاء", callback_data="admin:fb_settings")]]))
            
        elif action == "fb_set_token":
            context.user_data["await"] = {"type": "fb_access_token"}
            await q.edit_message_text("📝 <b>إدخال Access Token (رمز وصول الصفحة)</b>\n\nيرجى إرسال رمز الوصول الدائم الخاص بصفحتك على فيسبوك:", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 إلغاء", callback_data="admin:fb_settings")]]))
            
        elif action == "fb_test":
            await q.answer("جاري النشر التجريبي على فيسبوك...")
            success = await send_facebook_post("🧪 <b>منشور تجريبي من بوت أسعار الصرف السورية!</b>\n\nتم ربط البوت بصفحة الفيسبوك بنجاح.")
            if success:
                await q.answer("✅ تم نشر المنشور التجريبي بنجاح!", show_alert=True)
            else:
                await q.answer("❌ فشل النشر. يرجى التحقق من صحة معرف الصفحة ورمز الوصول.", show_alert=True)
            
        elif action == "close":
            await q.delete_message()
        return

    # Regular callbacks
    if head == "m":
        section = dataparts[1]
        if section == "main":
            await q.edit_message_text(WELCOME, parse_mode=ParseMode.HTML, reply_markup=main_menu_kb())
        elif section == "rates":
            data = await rs.get_rates()
            await q.edit_message_text(await all_rates_text(data), parse_mode=ParseMode.HTML,
                                      reply_markup=currency_grid("r"))
        elif section == "gold":
            data = await rs.get_rates()
            await q.edit_message_text(await gold_text(data), parse_mode=ParseMode.HTML,
                                      reply_markup=gold_kb())
        elif section == "convert":
            await q.edit_message_text("🔄 <b>محوّل العملات</b>\n\nاختر العملة لبدء عملية التحويل منها أو إليها:",
                                       parse_mode=ParseMode.HTML, reply_markup=currency_grid("cv"))
        elif section == "charts":
            await q.edit_message_text("📊 اختر العملة لعرض الرسم البياني:",
                                      reply_markup=currency_grid("chp"))
        elif section == "history":
            await q.edit_message_text("📈 اختر العملة لعرض السجل التاريخي:",
                                      reply_markup=currency_grid("h"))
        elif section == "alerts":
            text, kb = await alerts_view(chat_id)
            await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        elif section == "help":
            await q.edit_message_text(HELP, parse_mode=ParseMode.HTML, reply_markup=back_main_kb())
        elif section == "networks":
            settings = await get_settings()
            links = settings.get("recommended_links", [])
            if not links:
                await q.edit_message_text("🔗 <b>قنواتنا وقروباتنا الموصى بها</b>\n\nلا توجد قنوات موصى بها حالياً.", parse_mode=ParseMode.HTML, reply_markup=back_main_kb())
                return
            
            # Group by category
            cats = {}
            for l in links:
                cats.setdefault(l["category"], []).append(l)
                
            text = "🔗 <b>قنواتنا وشبكاتنا الموصى بها:</b>\n\n"
            for cat, items in cats.items():
                text += f"▪️ <b>{cat}:</b>\n"
                for item in items:
                    text += f" • <a href='{item['url']}'>{item['title']}</a>\n"
                text += "\n"
            await q.edit_message_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True, reply_markup=back_main_kb())
        return

    if head == "r":
        code = dataparts[1]
        data = await rs.get_rates()
        if code == "ALL":
            text_to_send = await all_rates_text(data)
            kb_to_send = currency_grid("r")
            try:
                await q.edit_message_text(text_to_send, parse_mode=ParseMode.HTML, reply_markup=kb_to_send)
            except Exception:
                try:
                    await q.delete_message()
                except Exception:
                    pass
                await context.bot.send_message(chat_id=chat_id, text=text_to_send, parse_mode=ParseMode.HTML, reply_markup=kb_to_send)
        elif code in data["currencies"]:
            text_to_send = rate_detail_text(data["currencies"][code])
            kb_to_send = rate_detail_kb(code)
            try:
                await q.edit_message_text(text_to_send, parse_mode=ParseMode.HTML, reply_markup=kb_to_send)
            except Exception:
                try:
                    await q.delete_message()
                except Exception:
                    pass
                await context.bot.send_message(chat_id=chat_id, text=text_to_send, parse_mode=ParseMode.HTML, reply_markup=kb_to_send)
        return

    if head == "h":
        await send_history(chat_id, context, dataparts[1])
        return

    if head == "chp":
        code = dataparts[1]
        cfg = CURRENCIES[code]
        text_to_send = f"📊 {cfg['flag']} {cfg['name_ar']} ({code}) — اختر الفترة:"
        kb_to_send = timeframe_kb(code)
        try:
            await q.edit_message_text(text_to_send, reply_markup=kb_to_send)
        except Exception:
            try:
                await q.delete_message()
            except Exception:
                pass
            await context.bot.send_message(chat_id=chat_id, text=text_to_send, reply_markup=kb_to_send)
        return

    if head == "c":
        code, days = dataparts[1], int(dataparts[2])
        await send_chart(chat_id, context, code, days, query_to_delete=q)
        return

    if head == "cv":
        code = dataparts[1]
        cfg = CURRENCIES[code]
        text_msg = (
            f"🔄 <b>محوّل العملات — {cfg['name_ar']} ({code})</b>\n\n"
            "اختر اتجاه التحويل المطلوب:"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"💵 من {cfg['name_ar']} إلى ليرة سورية 🇸🇾", callback_data=f"cvt:{code}:to_syp")],
            [InlineKeyboardButton(f"🇸🇾 من ليرة سورية إلى {cfg['name_ar']} 💵", callback_data=f"cvt:{code}:to_ccy")],
            [InlineKeyboardButton("🔙 رجوع", callback_data="m:convert")]
        ])
        await q.edit_message_text(text_msg, parse_mode=ParseMode.HTML, reply_markup=kb)
        return

    if head == "cvt":
        code = dataparts[1]
        direction = dataparts[2]
        cfg = CURRENCIES[code]
        if direction == "to_syp":
            context.user_data["await"] = {"type": "convert_to_syp", "code": code}
            await q.edit_message_text(
                f"🔄 <b>تحويل من {cfg['name_ar']} إلى ليرة سورية</b>\n\n"
                f"أرسل المبلغ بـ <b>{cfg['name_ar']}</b> وسأقوم بتحويله تلقائياً:\n"
                "مثال: <code>100</code>",
                parse_mode=ParseMode.HTML, reply_markup=back_main_kb())
        else:
            context.user_data["await"] = {"type": "convert_to_ccy", "code": code}
            await q.edit_message_text(
                f"🔄 <b>تحويل من ليرة سورية إلى {cfg['name_ar']}</b>\n\n"
                f"أرسل المبلغ بـ <b>ل.س</b> وسأقوم بتحويله تلقائياً:\n"
                "مثال: <code>500000</code>",
                parse_mode=ParseMode.HTML, reply_markup=back_main_kb())
        return

    if head == "an":
        code = dataparts[1]
        context.user_data["await"] = {"type": "alert", "code": code}
        cfg = CURRENCIES.get(code) or GOLD.get(code)
        data = await rs.get_rates()
        if code in data["currencies"]:
            cur = market_mid_for(data["currencies"][code])
        else:
            v = data["gold"][code].get("liranews")
            cur = v["mid"] if v else None
        
        flag_str = f" {cfg.get('flag', '')}" if cfg.get("flag") else ""
        await q.edit_message_text(
            f"🔔 السعر الحالي لـ{flag_str} <b>{cfg['name_ar']}</b> هو <b>{fmt(cur)}</b> ليرة.\n\n"
            "أرسل السعر المستهدف وسأنبّهك عند بلوغه.\n"
            "مثال: <code>150</code>", parse_mode=ParseMode.HTML, reply_markup=back_main_kb())
        return

    if head == "an_save":
        code = dataparts[1]
        target_val = float(dataparts[2])
        direction = dataparts[3]
        
        cfg = CURRENCIES.get(code) or GOLD.get(code)
        alert = {
            "id": str(uuid.uuid4()),
            "chat_id": chat_id,
            "code": code,
            "target": target_val,
            "direction": direction,
            "active": True,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        await db.alerts.insert_one(alert)
        
        dir_txt = "يرتفع إلى أو يتجاوز" if direction == "above" else "ينخفض إلى أو يتجاوز"
        flag_str = f" {cfg.get('flag', '')}" if cfg.get("flag") else ""
        
        await q.edit_message_text(
            f"✅ <b>تم ضبط التنبيه بنجاح!</b>\n\n"
            f"سأنبّهك عندما {dir_txt} سعر{flag_str} <b>{cfg['name_ar']}</b> "
            f"القيمة <b>{fmt(target_val)}</b> ليرة.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔔 تنبيهاتي", callback_data="m:alerts")],
                [InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="m:main")]]))
        return

    if head == "al":
        if dataparts[1] == "del":
            await db.alerts.update_one({"id": dataparts[2], "chat_id": chat_id},
                                       {"$set": {"active": False}})
            text, kb = await alerts_view(chat_id)
            await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        return


# ----------------------------- text (state) handler -----------------------------
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    
    # 1. Admin Auth Trigger
    if text == "صويري":
        context.user_data["await"] = {"type": "admin_pw"}
        await update.message.reply_text("🔒 الرجاء إدخال كلمة المرور للوحة التحكم:")
        return

    state = context.user_data.get("await")
    
    # 2. Check Password State
    if state and state.get("type") == "admin_pw":
        context.user_data.pop("await", None)
        if text == "76891796":
            settings = await get_settings()
            auths = settings.get("authorized_admins", [])
            if update.effective_user.id == 6641619062 or update.effective_user.id in auths or str(update.effective_user.id) in auths:
                await db.users.update_one(
                    {"chat_id": update.effective_user.id},
                    {"$set": {"is_admin": True}},
                    upsert=True
                )
                await update.message.reply_text("✅ تم الدخول بنجاح وترقيتك كمشرف (Admin) بشكل دائم!")
                await send_admin_panel(update, context)
            else:
                await update.message.reply_text("❌ عذراً، حسابك غير مدرج في قائمة المشرفين المصرح لهم بالدخول.")
        else:
            await update.message.reply_text("❌ كلمة المرور خاطئة. تم إلغاء العملية.")
        return

    # Regular sub enforcement
    if not await enforce_subscription(update, context):
        return

    # Admin Panel Interactions (Awaiting Text Inputs)
    if state and state.get("type") in ["add_bc", "add_md", "add_link_name", "add_link_url", "add_link_cat", "broadcast_msg", "add_admin_id", "wa_instance_id", "wa_token", "wa_chat_id", "fb_page_id", "fb_access_token"]:
        if not await is_user_admin(update.effective_user.id):
            context.user_data.pop("await", None)
            return
            
        itype = state["type"]
        
        if itype == "add_bc":
            context.user_data.pop("await", None)
            if not text.startswith("@"):
                await update.message.reply_text("⚠️ خطأ: يجب أن يبدأ المعرّف بـ @")
                return
            await db.settings.update_one({"_id": "settings"}, {"$addToSet": {"broadcast_channels": text}}, upsert=True)
            await update.message.reply_text(f"✅ تم إضافة القناة <code>{text}</code> للبث التلقائي.", parse_mode=ParseMode.HTML)
            await send_admin_panel(update, context)
            
        elif itype == "add_md":
            context.user_data.pop("await", None)
            if not text.startswith("@"):
                await update.message.reply_text("⚠️ خطأ: يجب أن يبدأ المعرّف بـ @")
                return
            await db.settings.update_one({"_id": "settings"}, {"$addToSet": {"mandatory_channels": text}}, upsert=True)
            await update.message.reply_text(f"✅ تم إضافة القناة <code>{text}</code> للاشتراك الإجباري.", parse_mode=ParseMode.HTML)
            await send_admin_panel(update, context)
            
        elif itype == "add_link_name":
            context.user_data["temp_link"] = {"title": text}
            context.user_data["await"] = {"type": "add_link_url"}
            await update.message.reply_text(
                "🔗 <b>إضافة رابط موصى به — خطوة 2 من 3</b>\n\n"
                "أرسل الآن رابط القناة أو المجموعة (مثال: <code>https://t.me/SyrianSAA</code>):",
                parse_mode=ParseMode.HTML)
                
        elif itype == "add_link_url":
            temp = context.user_data.get("temp_link")
            if temp:
                temp["url"] = text
                context.user_data["await"] = {"type": "add_link_cat"}
                text_msg = (
                    "🔗 <b>إضافة رابط موصى به — خطوة 3 من 3</b>\n\n"
                    f"الاسم: {temp['title']}\n"
                    f"الرابط: {temp['url']}\n\n"
                    "اختر قسم هذا الرابط من الأزرار بالأسفل، أو أرسل اسم القسم كتابةً:"
                )
                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("📺 قنوات", callback_data="admin:save_link:قنوات"),
                     InlineKeyboardButton("💬 قروبات", callback_data="admin:save_link:قروبات")],
                    [InlineKeyboardButton("🟢 واتساب", callback_data="admin:save_link:واتساب"),
                     InlineKeyboardButton("ℹ️ أخرى", callback_data="admin:save_link:أخرى")],
                    [InlineKeyboardButton("🔙 إلغاء", callback_data="admin:links")]
                ])
                await update.message.reply_text(text_msg, parse_mode=ParseMode.HTML, reply_markup=kb)
            else:
                context.user_data.pop("await", None)
                await update.message.reply_text("⚠️ حدث خطأ في جلسة الإدخال. يرجى البدء من جديد.")
                
        elif itype == "add_link_cat":
            context.user_data.pop("await", None)
            temp = context.user_data.pop("temp_link", None)
            if temp:
                link_doc = {
                    "title": temp["title"],
                    "url": temp["url"],
                    "category": text
                }
                await db.settings.update_one({"_id": "settings"}, {"$push": {"recommended_links": link_doc}}, upsert=True)
                await update.message.reply_text(f"✅ تم إضافة الرابط: <b>{link_doc['title']}</b> في قسم <b>{link_doc['category']}</b>", parse_mode=ParseMode.HTML)
                await send_admin_panel(update, context)
            else:
                await update.message.reply_text("⚠️ حدث خطأ في جلسة الإدخال. يرجى البدء من جديد.")
            
        elif itype == "broadcast_msg":
            context.user_data.pop("await", None)
            users = await db.users.find({}, {"chat_id": 1}).to_list(100000)
            success = 0
            failed = 0
            await update.message.reply_text(f"📣 جارٍ الإذاعة إلى {len(users)} مستخدم...")
            for u in users:
                cid = u.get("chat_id")
                if not cid:
                    continue
                try:
                    await context.bot.send_message(chat_id=cid, text=text)
                    success += 1
                except Exception:
                    failed += 1
            await update.message.reply_text(f"✅ انتهت الإذاعة!\n\n• تم الإرسال بنجاح إلى: <b>{success}</b> مستخدم.\n• فشل الإرسال (بوت محظور/مغلق) لـ: <b>{failed}</b> مستخدم.", parse_mode=ParseMode.HTML)
            await send_admin_panel(update, context)
            
        elif itype == "add_admin_id":
            context.user_data.pop("await", None)
            try:
                new_admin_id = int(text)
            except ValueError:
                await update.message.reply_text("⚠️ خطأ: يرجى إدخال آيدي رقمي صحيح للمشرف الجديد.")
                return
            
            await db.settings.update_one({"_id": "settings"}, {"$addToSet": {"authorized_admins": new_admin_id}}, upsert=True)
            await update.message.reply_text(f"✅ تم إضافة الحساب <code>{new_admin_id}</code> كمشرف بنجاح.", parse_mode=ParseMode.HTML)
            settings = await get_settings()
            await send_admin_panel(update, context)
            
        elif itype == "wa_instance_id":
            context.user_data.pop("await", None)
            await db.settings.update_one({"_id": "settings"}, {"$set": {"whatsapp_instance_id": text}}, upsert=True)
            await update.message.reply_text("✅ تم حفظ معرف المثيل (Instance ID) بنجاح.")
            settings = await get_settings()
            await send_admin_panel(update, context)
            
        elif itype == "wa_token":
            context.user_data.pop("await", None)
            await db.settings.update_one({"_id": "settings"}, {"$set": {"whatsapp_token": text}}, upsert=True)
            await update.message.reply_text("✅ تم حفظ رمز الوصول (Token) بنجاح.")
            settings = await get_settings()
            await send_admin_panel(update, context)
            
        elif itype == "wa_chat_id":
            context.user_data.pop("await", None)
            clean_chat = text.replace("+", "").replace(" ", "")
            await db.settings.update_one({"_id": "settings"}, {"$set": {"whatsapp_chat_id": clean_chat}}, upsert=True)
            await update.message.reply_text(f"✅ تم حفظ معرف الواتساب <code>{clean_chat}</code> بنجاح.", parse_mode=ParseMode.HTML)
            settings = await get_settings()
            await send_admin_panel(update, context)
            
        elif itype == "fb_page_id":
            context.user_data.pop("await", None)
            await db.settings.update_one({"_id": "settings"}, {"$set": {"facebook_page_id": text}}, upsert=True)
            await update.message.reply_text("✅ تم حفظ معرف صفحة فيسبوك (Page ID) بنجاح.")
            settings = await get_settings()
            await send_admin_panel(update, context)
            
        elif itype == "fb_access_token":
            context.user_data.pop("await", None)
            await db.settings.update_one({"_id": "settings"}, {"$set": {"facebook_access_token": text}}, upsert=True)
            await update.message.reply_text("✅ تم حفظ رمز وصول فيسبوك (Access Token) بنجاح.")
            settings = await get_settings()
            await send_admin_panel(update, context)
        return

    # Original Alert/Conversion values checking
    if not state:
        await update.message.reply_text("استخدم /start لعرض القائمة، أو /help للأوامر.",
                                        reply_markup=main_menu_kb())
        return
        
    raw = update.message.text.strip().replace(",", "")
    try:
        amount = float(raw)
    except ValueError:
        await update.message.reply_text("⚠️ الرجاء إرسال رقم صحيح. مثال: 100")
        return

    code = state["code"]
    data = await rs.get_rates()
    cfg = CURRENCIES.get(code) or GOLD.get(code)
    if code in data["currencies"]:
        cur = market_mid_for(data["currencies"][code])
    else:
        v = data["gold"][code].get("liranews")
        cur = v["mid"] if v else None

    if state["type"] == "convert_to_syp":
        context.user_data.pop("await", None)
        if cur is None:
            await update.message.reply_text("تعذّر جلب السعر حالياً، حاول لاحقاً.")
            return
        result = amount * cur
        await update.message.reply_text(
            f"🔄 <b>نتيجة التحويل</b> (بسعر السوق {fmt(cur)})\n\n"
            f"{cfg['flag']} <b>{fmt(amount)} {code}</b> = <b>{fmt(result)}</b> ل.س",
            parse_mode=ParseMode.HTML, reply_markup=rate_detail_kb(code))
        return

    if state["type"] == "convert_to_ccy":
        context.user_data.pop("await", None)
        if cur is None:
            await update.message.reply_text("تعذّر جلب السعر حالياً، حاول لاحقاً.")
            return
        result = amount / cur if cur else 0
        await update.message.reply_text(
            f"🔄 <b>نتيجة التحويل</b> (بسعر السوق {fmt(cur)})\n\n"
            f"🇸🇾 <b>{fmt(amount)} ل.س</b> = <b>{fmt(result)} {code}</b> {cfg['flag']}",
            parse_mode=ParseMode.HTML, reply_markup=rate_detail_kb(code))
        return

    if state["type"] == "alert":
        context.user_data.pop("await", None)
        flag_str = f" {cfg.get('flag', '')}" if cfg.get("flag") else ""
        text_msg = (
            f"🔔 <b>ضبط تنبيه لـ{flag_str} {cfg['name_ar']}</b>\n\n"
            f"السعر المستهدف: <b>{fmt(amount)}</b> ليرة.\n\n"
            "يرجى تحديد متى ترغب في أن ينبهك البوت:"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔺 عند الارتفاع (فوق السعر)", callback_data=f"an_save:{code}:{amount}:above")],
            [InlineKeyboardButton("🔻 عند الانخفاض (تحت السعر)", callback_data=f"an_save:{code}:{amount}:below")],
            [InlineKeyboardButton("🔙 إلغاء", callback_data="m:main")]
        ])
        await update.message.reply_text(text_msg, parse_mode=ParseMode.HTML, reply_markup=kb)
        return


# ----------------------------- channel broadcasting helper -----------------------------
async def broadcast_rates_to_channels(bot):
    settings = await get_settings()
    channels = settings.get("broadcast_channels", [])
    wa_enabled = settings.get("whatsapp_enabled", False)
    fb_enabled = settings.get("facebook_enabled", False)
    
    if not channels and not wa_enabled and not fb_enabled:
        return
        
    data = await rs.get_rates(force=True)
    text = await all_rates_text(data)
    for chan in channels:
        try:
            await bot.send_message(chat_id=chan, text=text, parse_mode=ParseMode.HTML)
            logger.info("Sent rates update to channel: %s", chan)
        except Exception as e:
            logger.error("Failed to send rates to channel %s: %s", chan, e)
            
    # Send to WhatsApp
    if wa_enabled:
        wa_text = await whatsapp_rates_text(data)
        await send_whatsapp_message(wa_text)
        
    # Send to Facebook
    if fb_enabled:
        fb_text = await facebook_rates_text(data)
        await send_facebook_post(fb_text)


async def check_and_broadcast_rate_changes(bot):
    settings = await get_settings()
    channels = settings.get("broadcast_channels", [])
    wa_enabled = settings.get("whatsapp_enabled", False)
    fb_enabled = settings.get("facebook_enabled", False)
    
    if not channels and not wa_enabled and not fb_enabled:
        return
        
    data = await rs.get_rates(force=True)
    usd_entry = data["currencies"].get("USD")
    if not usd_entry:
        return
        
    current_usd = market_mid_for(usd_entry)
    if current_usd is None:
        return
        
    last_usd = settings.get("last_broadcasted_usd")
    if last_usd != current_usd:
        # Save new broadcasted price
        await db.settings.update_one({"_id": "settings"}, {"$set": {"last_broadcasted_usd": current_usd}}, upsert=True)
        # Only broadcast if it is NOT the first startup write (to avoid spamming on start)
        if last_usd is not None:
            logger.info(f"USD rate changed from {last_usd} to {current_usd}. Broadcasting updates...")
            text = f"🚨 <b>تغير أسعار الصرف مقابل الليرة السورية</b>\n\n• سعر الدولار السابق: {fmt(last_usd)} ل.س\n• سعر الدولار الحالي: <b>{fmt(current_usd)}</b> ل.س\n\n" + await all_rates_text(data)
            for chan in channels:
                try:
                    await bot.send_message(chat_id=chan, text=text, parse_mode=ParseMode.HTML)
                except Exception as e:
                    logger.error(f"Failed to send rate change alert to channel {chan}: {e}")
            
            # Broadcast to WhatsApp
            if wa_enabled:
                wa_text = f"🚨 <b>تغير أسعار الصرف مقابل الليرة السورية</b>\n\n• سعر الدولار السابق: {fmt(last_usd)} ل.س\n• سعر الدولار الحالي: <b>{fmt(current_usd)}</b> ل.س\n\n" + await whatsapp_rates_text(data)
                await send_whatsapp_message(wa_text)
                
            # Broadcast to Facebook
            if fb_enabled:
                fb_text = f"🚨 تغير أسعار الصرف مقابل الليرة السورية\n\n• سعر الدولار السابق: {fmt(last_usd)} ل.س\n• سعر الدولار الحالي: {fmt(current_usd)} ل.س\n\n" + await facebook_rates_text(data)
                await send_facebook_post(fb_text)


# ----------------------------- alert checking (scheduler) -----------------------------
async def check_alerts():
    if application is None:
        return
    active = await db.alerts.find({"active": True}, {"_id": 0}).to_list(2000)
    if not active:
        return
    data = await rs.get_rates()
    for a in active:
        if a["code"] in data["currencies"]:
            entry = data["currencies"][a["code"]]
            cur = market_mid_for(entry)
            cfg = CURRENCIES[a["code"]]
        elif a["code"] in data["gold"]:
            item = data["gold"][a["code"]]
            v = item.get("liranews")
            cur = v["mid"] if v else None
            cfg = GOLD[a["code"]]
        else:
            continue
            
        if cur is None:
            continue
        hit = (a["direction"] == "above" and cur >= a["target"]) or \
              (a["direction"] == "below" and cur <= a["target"])
        if hit:
            flag_str = f" {cfg.get('flag', '')}" if cfg.get("flag") else ""
            try:
                await application.bot.send_message(
                    a["chat_id"],
                    f"🚨 <b>تنبيه سعر!</b>\n\n{flag_str}<b>{cfg['name_ar']}</b> بلغ "
                    f"<b>{fmt(cur)}</b> ليرة (الهدف: {fmt(a['target'])}).",
                    parse_mode=ParseMode.HTML)
            except Exception as e:
                logger.warning("Failed to send alert to %s: %s", a["chat_id"], e)
            await db.alerts.update_one(
                {"id": a["id"]},
                {"$set": {"active": False,
                          "triggered_at": datetime.now(timezone.utc).isoformat(),
                          "triggered_rate": cur}})


async def set_bot_commands(bot):
    from telegram import BotCommand
    commands = [
        BotCommand("start", "تشغيل البوت والتحقق 🇸🇾"),
        BotCommand("rates", "عرض أسعار الصرف في سوريا 📈"),
        BotCommand("gold", "سعر غرام الذهب عيار 18/21/24 🪙"),
        BotCommand("convert", "محول العملات الذكي (اتجاهين) 🔄"),
        BotCommand("alerts", "ضبط وإدارة تنبيهات الأسعار 🔔"),
        BotCommand("contact", "تواصل مع الإدارة / إبلاغ عن مشكلة 📞"),
        BotCommand("help", "مساعدة وتعليمات الاستخدام ℹ️")
    ]
    try:
        await bot.set_my_commands(commands)
    except Exception as e:
        logger.warning(f"Failed to set bot commands: {e}")


# ----------------------------- app builder -----------------------------
def build_application():
    global application
    token = __import__("os").environ.get("TELEGRAM_TOKEN") or __import__("os").environ.get("BOT_TOKEN")
    application = Application.builder().token(token).build()
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("rates", cmd_rates))
    application.add_handler(CommandHandler("rate", cmd_rate))
    application.add_handler(CommandHandler("gold", cmd_gold))
    application.add_handler(CommandHandler("history", cmd_history))
    application.add_handler(CommandHandler("chart", cmd_chart))
    application.add_handler(CommandHandler("convert", cmd_convert))
    application.add_handler(CommandHandler("alerts", cmd_alerts))
    application.add_handler(CommandHandler("contact", cmd_contact))
    application.add_handler(CommandHandler("support", cmd_contact))
    application.add_handler(CommandHandler("admin", cmd_admin))
    application.add_handler(CallbackQueryHandler(on_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return application
