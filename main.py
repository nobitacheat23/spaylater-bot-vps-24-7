"""Điểm khởi chạy chính của Bot Telegram Rút Ví Trả Sau Shopee (SPayLater) qua cổng Napthe.vn."""
import asyncio
import contextlib
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

import db
import napthe_engine
import services as S
import ui
from config import BOT_TOKEN, amount_to_so
from handlers import admin, user
from handlers.user import notify_admins_so_jumped
from middlewares import UserMiddleware
from ui import btn, ce, kb

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("spaylater_bot")


async def _background_napthe_and_expiry_worker(bot: Bot):
    """Tác vụ chạy ngầm:
    1. Kiểm tra tự động biến động Sò trên Napthe.vn cho các đơn đang hoạt động; nếu Sò nhảy đủ -> Báo Admin ngay.
    2. Kiểm tra và tự động hủy các mã QR quá hạn 5 phút (300 giây), dọn dẹp file ảnh QR trong storage/qrcodes/."""
    while True:
        try:
            # 1. Tự động check Sò trên Napthe.vn cho các đơn đang chờ quét / chờ gửi bill
            active_orders = await db.fetchall(
                "SELECT * FROM orders WHERE status IN ('WAITING_SCAN', 'WAITING_BILL') AND so_verified=0"
            )
            if active_orders:
                live_so, _ = await napthe_engine.fetch_live_napthe_so()
                if live_so is not None:
                    for ao in active_orders:
                        so_exp = int(ao.get("so_expected") or amount_to_so(int(ao["amount_face"])))
                        so_bef = int(ao.get("so_before") or 0)
                        if live_so >= so_bef + so_exp:
                            updated, newly = await S.verify_and_jump_order_so(ao["order_id"], live_so=live_so)
                            if updated and newly:
                                u = await S.ensure_user(updated["telegram_id"])
                                await notify_admins_so_jumped(bot, updated, u)

            # 2. Hủy các đơn quá hạn 5 phút
            expired_orders = await S.expire_overdue_orders()
            for o in expired_orders:
                oid = o["order_id"]
                log.info("Auto-expired overdue QR order #%s (user %s)", oid, o["telegram_id"])
                if o.get("qr_chat_id") and o.get("qr_msg_id"):
                    with contextlib.suppress(Exception):
                        await bot.delete_message(chat_id=o["qr_chat_id"], message_id=o["qr_msg_id"])
                with contextlib.suppress(Exception):
                    await ui.send(
                        bot,
                        o["telegram_id"],
                        f"{ce('cross', '❌')} <b>MÃ QR ĐƠN #{oid} ĐÃ HẾT HẠN (QUÁ 5 PHÚT)!</b>\n\n"
                        f"• Mệnh giá: <b>{S.fmt_vnd(o['amount_face'])}</b>\n"
                        "• Để đảm bảo an toàn giao dịch, mỗi mã QR ShopeePay chỉ có hiệu lực trong 5 phút.\n"
                        "👉 Nếu bạn vẫn muốn rút tiền, vui lòng bấm nút bên dưới để lấy mã QR mới:",
                        kb([[btn("🛍 Tạo mã QR mới", f"denom:{o['amount_face']}", icon="shopee", emoji="🛍", style="success")]]),
                    )
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.warning("Background worker error: %s", e)
        await asyncio.sleep(15)


async def main():
    if not BOT_TOKEN:
        raise RuntimeError("Thiếu BOT_TOKEN trong file .env!")

    await db.init_db()

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())

    auth_mw = UserMiddleware()
    dp.message.middleware(auth_mw)
    dp.callback_query.middleware(auth_mw)

    dp.include_router(admin.router)
    dp.include_router(user.router)

    await bot.set_my_commands([
        BotCommand(command="start", description="🛍 Rút Ví Trả Sau Shopee (SPayLater) 24/7"),
        BotCommand(command="rut", description="💸 Rút tiền từ số dư tài khoản về Ngân hàng"),
        BotCommand(command="admin", description="👑 Bảng điều khiển Quản trị viên"),
        BotCommand(command="setpass", description="🔑 Cài mật khẩu Garena để bot tự đăng nhập ngầm"),
        BotCommand(command="setcookie", description="🍪 Cài cookie Napthe.vn chạy API siêu tốc"),
        BotCommand(command="login_napthe", description="🔄 Thử đăng nhập tự động ngầm 100%"),
        BotCommand(command="checkso", description="🐚 Check số dư Garena Sò trên Napthe.vn"),
        BotCommand(command="addbal", description="💰 Cộng/trừ số dư ví khách hàng (Admin)"),
        BotCommand(command="stats", description="📊 Thống kê doanh thu & lợi nhuận (Admin)"),
        BotCommand(command="setfee", description="💸 Đổi phí dịch vụ toàn hệ thống (Admin)"),
        BotCommand(command="setvip", description="👑 Cài phí VIP riêng cho khách (Admin)"),
        BotCommand(command="user", description="🔍 Kiểm tra hồ sơ khách hàng (Admin)"),
        BotCommand(command="ban", description="🔒 Khóa/Mở khóa người dùng (Admin)"),
    ])

    await bot.delete_webhook(drop_pending_updates=True)
    me = await bot.get_me()
    log.info("🚀 SPayLater Withdrawal Bot started: @%s (ID: %s)", me.username, me.id)

    bg_task = asyncio.create_task(_background_napthe_and_expiry_worker(bot))
    try:
        await dp.start_polling(bot)
    finally:
        bg_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await bg_task
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
