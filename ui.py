"""Xây dựng giao diện Bot API 7.0+ (icon_custom_emoji_id + style trên InlineKeyboardButton)
và bộ Icon 3D Shopee / SPayLater / Garena / Telegram Premium (chống lỗi ENTITY_TEXT_INVALID 100%)."""
import html
import logging
import re
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import BUTTON_STYLE, CUSTOM_EMOJI_IN_TEXT

log = logging.getLogger(__name__)

# ==================== BỘ ICON 3D SHOPEE & TELEGRAM PREMIUM ====================
ICONS = {
    "shopee": "6271742310564765894",     # 🛍 3D Shopee Orange Bag
    "spay": "6271478827206059641",       # 💳 3D SPayLater Badge
    "garena": "6271301771474247262",     # 🎮 3D Garena Sò Badge
    "fire": "5420315771991497307",       # 🔥 3D Animated Fire
    "bolt": "5431449001532594346",       # ⚡ 3D Animated Lightning
    "money_fly": "5472030678633684592",  # 💸 3D Flying Money
    "money_bag": "5375296873982604963",  # 💰 3D Money Bag
    "profile": "5373012449597335010",    # 👤 3D User Profile
    "support": "5465300082628763143",    # 💬 3D Chat Support
    "crown": "5467406098367521267",      # 👑 3D Gold Crown
    "chart": "5431577498364158238",      # 📊 3D Bar Chart
    "mega": "5469903029144657419",       # 📣 3D Orange Megaphone
    "cross": "5465665476971471368",      # ❌ 3D Red Cross
    "diamond": "5471952986970267163",    # 💎 3D Spinning Diamond
    "point": "5471978009449731768",      # 👉 3D Pointing Hand
    "party": "5436040291507247633",      # 🎉 3D Party Popper
    "cart": "5431499171045581032",       # 🛒 3D Shopping Cart
    "target": "5350460637182993292",     # 🎯 3D Target
    "up_green": "6271357258156744942",   # ⬆️ 3D Green Arrow
    "plus": "5226945370684140473",       # ➕ 3D Green Plus
}

# Emoji chuẩn bắt buộc nằm bên trong <tg-emoji> để Telegram không bao giờ báo ENTITY_TEXT_INVALID
DEFAULT_EMOJI = {
    "shopee": "🛍",
    "spay": "💳",
    "garena": "🎮",
    "fire": "🔥",
    "bolt": "⚡",
    "money_fly": "💸",
    "money_bag": "💰",
    "profile": "👤",
    "support": "💬",
    "crown": "👑",
    "chart": "📊",
    "mega": "📣",
    "cross": "❌",
    "diamond": "💎",
    "point": "👉",
    "party": "🎉",
    "cart": "🛒",
    "target": "🎯",
    "up_green": "⬆️",
    "plus": "➕",
}

_TG_EMOJI_RE = re.compile(r'<tg-emoji emoji-id="[^"]+">(.+?)</tg-emoji>')


def esc(s: object) -> str:
    return html.escape(str(s or ""))


def _is_valid_tg_inner_emoji(s: str) -> bool:
    """Kiểm tra chuỗi có phải là Unicode Emoji hợp lệ cho thẻ <tg-emoji> của Telegram không."""
    if not s or s in ("•", "-", "*", "·", " "):
        return False
    return any(ord(ch) >= 0x2600 or ord(ch) in (0x23F0, 0x23F3, 0x231B, 0x2B50) for ch in s)


def ce(key_or_id: str | None, fallback: str = "") -> str:
    """Trả về thẻ <tg-emoji> 3D chuẩn Telegram HTML (luôn bảo đảm ký tự bên trong là Emoji hợp lệ)."""
    std_emj = DEFAULT_EMOJI.get(key_or_id or "", "🔸")
    safe_inner = fallback if _is_valid_tg_inner_emoji(fallback) else std_emj
    if not key_or_id or not CUSTOM_EMOJI_IN_TEXT:
        return fallback or std_emj
    eid = ICONS.get(key_or_id, key_or_id)
    if str(eid).isdigit():
        return f'<tg-emoji emoji-id="{eid}">{safe_inner}</tg-emoji>'
    return fallback or std_emj


def strip_tg_emoji(text: str) -> str:
    return _TG_EMOJI_RE.sub(r"\1", text or "")


def btn(
    text: str,
    callback_data: str | None = None,
    *,
    url: str | None = None,
    icon: str | None = None,
    emoji: str = "",
    style: str | None = BUTTON_STYLE,
) -> InlineKeyboardButton:
    eid = ICONS.get(icon, icon) if icon else None
    kw = {}
    if callback_data is not None:
        kw["callback_data"] = callback_data
    if url is not None:
        kw["url"] = url
    if style:
        kw["style"] = style
    if eid and str(eid).isdigit():
        kw["icon_custom_emoji_id"] = str(eid)
        label = text
    else:
        label = f"{emoji} {text}".strip() if emoji and not text.startswith(emoji) else text
    return InlineKeyboardButton(text=label, **kw)


def kb(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _strip_markup_icons(markup: InlineKeyboardMarkup | None) -> InlineKeyboardMarkup | None:
    if not markup:
        return None
    new_rows = []
    for row in markup.inline_keyboard:
        new_row = []
        for b in row:
            d = b.model_dump(exclude_none=True)
            d.pop("icon_custom_emoji_id", None)
            d.pop("style", None)
            new_row.append(InlineKeyboardButton(**d))
        new_rows.append(new_row)
    return InlineKeyboardMarkup(inline_keyboard=new_rows)


def _is_icon_err(e: Exception) -> bool:
    s = str(e).upper()
    return any(
        k in s
        for k in (
            "CUSTOM_EMOJI",
            "BUTTON_ICON",
            "DOCUMENT_INVALID",
            "STYLE",
            "ENTITY_TEXT_INVALID",
            "CANT_PARSE_ENTITIES",
        )
    )


async def answer(message: Message, text: str, markup: InlineKeyboardMarkup | None = None, **kw) -> Message:
    try:
        return await message.answer(text, reply_markup=markup, **kw)
    except TelegramBadRequest as e:
        if _is_icon_err(e):
            return await message.answer(strip_tg_emoji(text), reply_markup=_strip_markup_icons(markup), **kw)
        raise


async def send(bot: Bot, chat_id: int, text: str, markup: InlineKeyboardMarkup | None = None, **kw) -> Message:
    try:
        return await bot.send_message(chat_id, text, reply_markup=markup, **kw)
    except TelegramBadRequest as e:
        if _is_icon_err(e):
            return await bot.send_message(chat_id, strip_tg_emoji(text), reply_markup=_strip_markup_icons(markup), **kw)
        raise


async def answer_photo(message: Message, photo, caption: str = "", markup: InlineKeyboardMarkup | None = None, **kw) -> Message:
    try:
        return await message.answer_photo(photo, caption=caption, reply_markup=markup, **kw)
    except TelegramBadRequest as e:
        if _is_icon_err(e):
            return await message.answer_photo(photo, caption=strip_tg_emoji(caption), reply_markup=_strip_markup_icons(markup), **kw)
        raise


async def send_photo(bot: Bot, chat_id: int, photo, caption: str = "", markup: InlineKeyboardMarkup | None = None, **kw) -> Message:
    try:
        return await bot.send_photo(chat_id=chat_id, photo=photo, caption=caption, reply_markup=markup, **kw)
    except TelegramBadRequest as e:
        if _is_icon_err(e):
            return await bot.send_photo(chat_id=chat_id, photo=photo, caption=strip_tg_emoji(caption), reply_markup=_strip_markup_icons(markup), **kw)
        raise


async def edit(cb: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None = None, **kw):
    try:
        if cb.message.photo or cb.message.document:
            try:
                await cb.message.delete()
            except TelegramBadRequest:
                pass
            return await answer(cb.message, text, markup, **kw)
        return await cb.message.edit_text(text, reply_markup=markup, **kw)
    except TelegramBadRequest as e:
        if "message is not modified" in str(e).lower():
            return
        if _is_icon_err(e):
            try:
                return await cb.message.edit_text(strip_tg_emoji(text), reply_markup=_strip_markup_icons(markup), **kw)
            except TelegramBadRequest:
                return await answer(cb.message, strip_tg_emoji(text), _strip_markup_icons(markup), **kw)
        return await answer(cb.message, text, markup, **kw)
