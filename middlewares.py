"""Middleware tự động đồng bộ thông tin khách hàng, kiểm tra khóa tài khoản (Ban) và chế độ Bảo trì."""
from typing import Any, Awaitable, Callable, Dict
from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

import services as S
import ui
from config import is_admin


class UserMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        tg_user = getattr(event, "from_user", None)
        if not tg_user:
            return await handler(event, data)

        full_name = f"{tg_user.first_name or ''} {tg_user.last_name or ''}".strip()
        u = await S.ensure_user(tg_user.id, tg_user.username or "", full_name)
        data["user"] = u

        # 1. Kiểm tra nếu người dùng bị khóa (Ban)
        if u.get("is_banned") and not is_admin(tg_user.id, tg_user.username):
            msg = f"{ui.ce('cross', '🚫')} <b>Tài khoản của bạn đã bị khóa do vi phạm quy định hệ thống.</b>"
            if isinstance(event, CallbackQuery):
                await event.answer("🚫 Tài khoản của bạn đã bị khóa!", show_alert=True)
            elif isinstance(event, Message):
                await ui.answer(event, msg)
            return

        # 2. Kiểm tra chế độ bảo trì hệ thống
        if await S.is_maintenance() and not is_admin(tg_user.id, tg_user.username):
            msg = (
                f"{ui.ce('mega', '🛠')} <b>HỆ THỐNG ĐANG BẢO TRÌ NÂNG CẤP</b>\n\n"
                "Hệ thống rút Ví trả sau Shopee (SPayLater) đang tạm bảo trì ít phút để nâng cấp đường truyền thanh toán.\n"
                "Vui lòng quay lại sau ít phút nhé!"
            )
            if isinstance(event, CallbackQuery):
                await event.answer("🛠 Hệ thống đang bảo trì, vui lòng thử lại sau ít phút!", show_alert=True)
            elif isinstance(event, Message):
                await ui.answer(event, msg)
            return

        return await handler(event, data)
