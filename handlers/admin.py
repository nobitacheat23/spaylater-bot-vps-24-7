"""Hệ thống Quản trị viên (Giai đoạn 3 - Admin Dispatcher, Duyệt Cộng Ví, Giải Ngân Rút Ví & Admin Panel):
- Check Sò trên Napthe.vn: Hiển thị biến động Sò nhảy theo mệnh giá nạp và báo Admin.
- Khi duyệt đơn quét SPayLater (#SPAY_XXXXXX): Tự động cộng tiền vào Ví tài khoản của khách (users.balance).
- Khi khách rút tiền từ Ví (#WD_XXXXXX): Xuất mã VietQR chuyển khoản 1 chạm, Xác nhận ĐÃ CHUYỂN TIỀN hoặc Từ chối (hoàn lại Ví).
- Bộ lệnh Admin chuẩn đặc tả: /setfee, /setvip, /user, /ban, /stats, /checkso, /addbal và bảng điều khiển /admin."""
import logging
import os
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message

import db
import napthe_engine
import services as S
import ui
from config import NAPTHE_DENOMINATIONS, QR_STORAGE_DIR, amount_to_so, is_admin
from handlers.user import notify_admins_so_jumped
from ui import btn, ce, esc, kb

log = logging.getLogger(__name__)
router = Router(name="admin_router")

PRESET_REJECT_REASONS = {
    "dup": "Bill cũ / trùng lặp hoặc đã từng sử dụng",
    "noso": "Hệ thống chưa nhận được thanh toán từ giao dịch này",
    "wrong_amt": "Sai mệnh giá quét hoặc chưa thanh toán qua SPayLater",
}


class AdminSG(StatesGroup):
    custom_reject_reason = State()
    set_fee_input = State()
    set_garena_input = State()
    set_so_balance_input = State()
    set_vip_input = State()
    adjust_bal_input = State()
    lookup_user_input = State()
    upload_qr_template = State()


def _check_admin(tg_id: int, username: str = None) -> bool:
    return is_admin(tg_id, username)


# ======================================================================
# CHECK SÒ NAPTHE.VN CHO ĐƠN HÀNG & TOÀN HỆ THỐNG
# ======================================================================
@router.callback_query(F.data.startswith("adm_check_so:"))
async def cb_admin_check_order_so(cb: CallbackQuery):
    if not _check_admin(cb.from_user.id):
        return await cb.answer("⛔ Bạn không có quyền Admin.", show_alert=True)

    oid = cb.data.split(":")[1]
    order = await S.get_order(oid)
    if not order:
        return await cb.answer("❌ Không tìm thấy đơn hàng.", show_alert=True)

    await cb.answer("🐚 Đang kiểm tra số dư Sò trên Napthe.vn...")
    live_so, _ = await napthe_engine.fetch_live_napthe_so()
    order, _ = await S.verify_and_jump_order_so(oid, live_so=live_so)
    garena_acc = await S.get_garena_account()

    so_expected = int(order.get("so_expected") or amount_to_so(int(order["amount_face"])))
    so_before = int(order.get("so_before") or 0)
    so_after = int(order.get("so_after") or (so_before + so_expected))

    so_png = napthe_engine.render_napthe_so_status_card(
        garena_account=garena_acc,
        amount_face=int(order["amount_face"]),
        so_before=so_before,
        so_after=so_after,
        so_expected=so_expected,
        order_id=oid,
    )

    caption = (
        f"🐚 <b>KẾT QUẢ CHECK SÒ NAPTHE.VN - ĐƠN #{oid}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"• Tài khoản Garena: <code>{esc(garena_acc)}</code>\n"
        f"• Mệnh giá đơn: <b>{S.fmt_vnd(order['amount_face'])}</b>\n"
        f"• Số Sò tương ứng: <b>+{S.fmt_so(so_expected)}</b> 🐚\n"
        f"• Số dư Sò trước nạp: <b>{S.fmt_so(so_before)}</b>\n"
        f"• Số dư Sò sau nạp: <b>{S.fmt_so(so_after)}</b> (🟢 <b>ĐÃ NHẢY ĐỦ SÒ</b>)\n"
        f"• Số tiền cộng vào Ví khách: <b>+{S.fmt_vnd(order['amount_received'])}</b>"
    )
    markup = kb([
        [
            btn(
                f"✅ Duyệt & Cộng +{S.fmt_vnd(order['amount_received'])} vào Ví Khách",
                f"adm_paid:{oid}",
                icon="up_green",
                emoji="✅",
                style="success",
            )
        ],
    ])
    await ui.answer_photo(
        cb.message,
        BufferedInputFile(so_png, filename=f"CheckSo_{oid}.png"),
        caption=caption,
        markup=markup,
    )


@router.message(Command("checkso", "so"))
async def cmd_check_so(message: Message):
    if not _check_admin(message.from_user.id):
        return
    await _send_so_overview(message)


@router.message(Command("setpass"))
async def cmd_set_garena_pass(message: Message):
    if not _check_admin(message.from_user.id):
        return
    parts = message.text.strip().split(maxsplit=1)
    if len(parts) < 2:
        return await message.answer(
            "⚠️ <b>Cú pháp:</b> <code>/setpass [mật_khẩu_garena]</code>\n"
            "Ví dụ: <code>/setpass MatKhauCuaBan123</code>"
        )
    pwd = parts[1].strip()
    await S.set_config("garena_password", pwd)
    acc = await S.get_garena_account()
    wait_msg = await message.answer(
        f"⏳ <b>Đã lưu mật khẩu Garena!</b>\n"
        f"Đang kích hoạt thử nghiệm tự động đăng nhập ngầm cho tài khoản <code>{esc(acc)}</code>..."
    )
    res = await napthe_engine.auto_login_and_check_so()
    icon = "✅" if res["success"] else "⚠️"
    so_text = f"\n• Số Sò hiện tại: <b>{S.fmt_so(res['so_balance'] or 0)} 🐚</b>" if res["so_balance"] is not None else ""
    await wait_msg.edit_text(
        f"{icon} <b>KẾT QUẢ ĐĂNG NHẬP GARENA TỰ ĐỘNG (100% NGẦM):</b>\n\n"
        f"• Tài khoản: <code>{esc(acc)}</code>\n"
        f"• Trạng thái: <b>{'THÀNH CÔNG' if res['success'] else 'CẦN KIỂM TRA LẠI'}</b>"
        f"{so_text}\n"
        f"• Thông báo: <i>{esc(res['message'])}</i>\n\n"
        f"👉 Giờ đây mọi đơn hàng tạo mã QR ShopeePay sẽ được bot tự động xử lý 100%!"
    )


@router.message(Command("setcookie"))
async def cmd_set_napthe_cookie(message: Message):
    if not _check_admin(message.from_user.id):
        return
    parts = message.text.strip().split(maxsplit=1)
    if len(parts) < 2:
        return await message.answer(
            "⚠️ <b>Cú pháp:</b> <code>/setcookie [chuỗi_cookie_napthe]</code>\n"
            "<i>(Sao chép từ F12 -> Network -> headers Cookie: trên trình duyệt bạn đã đăng nhập)</i>"
        )
    cookie_str = parts[1].strip()
    await S.set_config("napthe_cookie", cookie_str)
    wait_msg = await message.answer("⏳ Đang kiểm tra Cookie trên cổng Napthe.vn...")
    so_val, uname = await napthe_engine.fetch_live_napthe_so()
    if so_val is not None:
        await S.set_current_so_balance(so_val)
        await wait_msg.edit_text(
            f"✅ <b>KẾT NỐI NAPTHE.VN THÀNH CÔNG QUA COOKIE!</b>\n\n"
            f"• Tài khoản: <code>{esc(uname or 'Napthe User')}</code>\n"
            f"• Số dư Garena Sò hiện tại: <b>🐚 {S.fmt_so(so_val)} Sò</b>\n\n"
            "👉 Bot sẽ gọi trực tiếp API siêu tốc dưới 0.5s để lấy mã QR ShopeePay thật cho khách hàng!"
        )
    else:
        await wait_msg.edit_text(
            "⚠️ <b>Cookie đã lưu!</b>\n"
            "Nếu Cookie hợp lệ, bot sẽ ưu tiên gọi API tạo mã QR ShopeePay trực tiếp khi có đơn hàng."
        )


@router.message(Command("setgarena"))
async def cmd_set_garena_creds(message: Message):
    if not _check_admin(message.from_user.id):
        return
    parts = message.text.strip().split(maxsplit=2)
    if len(parts) < 3:
        return await message.answer(
            "⚠️ <b>Cú pháp:</b> <code>/setgarena [tài_khoản] [mật_khẩu]</code>\n"
            "Ví dụ: <code>/setgarena nhat2301088 MatKhau123</code>"
        )
    acc = parts[1].strip()
    pwd = parts[2].strip()
    await S.set_config("garena_account", acc)
    await S.set_config("garena_password", pwd)
    wait_msg = await message.answer(f"⏳ Đang kích hoạt thử nghiệm đăng nhập tự động cho tài khoản <code>{esc(acc)}</code>...")
    res = await napthe_engine.auto_login_and_check_so()
    icon = "✅" if res["success"] else "⚠️"
    so_text = f"\n• Số Sò hiện tại: <b>{S.fmt_so(res['so_balance'] or 0)} 🐚</b>" if res["so_balance"] is not None else ""
    await wait_msg.edit_text(
        f"{icon} <b>KẾT QUẢ ĐĂNG NHẬP GARENA:</b>\n\n"
        f"• Tài khoản: <code>{esc(acc)}</code>\n"
        f"• Trạng thái: <b>{'THÀNH CÔNG' if res['success'] else 'THẤT BẠI'}</b>"
        f"{so_text}\n"
        f"• Thông báo: <i>{esc(res['message'])}</i>"
    )


@router.message(Command("login_napthe", "napthe"))
async def cmd_login_napthe(message: Message):
    if not _check_admin(message.from_user.id):
        return
    wait_msg = await message.answer("⏳ Đang chạy thử nghiệm đăng nhập ngầm 100% Napthe.vn...")
    res = await napthe_engine.auto_login_and_check_so()
    icon = "✅" if res["success"] else "⚠️"
    so_text = f"\n• Số Sò hiện tại: <b>{S.fmt_so(res['so_balance'] or 0)} 🐚</b>" if res["so_balance"] is not None else ""
    await wait_msg.edit_text(
        f"{icon} <b>KẾT QUẢ KIỂM TRA PHIÊN NAPTHE.VN:</b>\n\n"
        f"• Tài khoản: <code>{esc(res['username'])}</code>\n"
        f"• Phương thức: <code>{res['method']}</code>"
        f"{so_text}\n"
        f"• Chi tiết: <i>{esc(res['message'])}</i>"
    )


@router.callback_query(F.data == "adm:so_panel")
async def cb_adm_so_panel(cb: CallbackQuery):
    if not _check_admin(cb.from_user.id):
        return await cb.answer("⛔ Không có quyền Admin.", show_alert=True)
    await cb.answer("🐚 Đang kiểm tra Sò Napthe.vn...")
    await _send_so_overview(cb.message)


async def _send_so_overview(target_msg: Message):
    live_so, live_user = await napthe_engine.fetch_live_napthe_so()
    if live_so is not None:
        await S.set_current_so_balance(live_so)
    cur_so = await S.get_current_so_balance()
    garena_acc = live_user or await S.get_garena_account()

    active_orders = await db.fetchall(
        "SELECT * FROM orders WHERE status IN ('WAITING_SCAN', 'WAITING_BILL', 'PENDING_ADMIN_REVIEW') "
        "ORDER BY created_at DESC LIMIT 5"
    )

    so_png = napthe_engine.render_napthe_so_status_card(
        garena_account=garena_acc,
        amount_face=5_000_000,
        so_before=cur_so,
        so_after=cur_so,
        so_expected=10_000,
        order_id="NAPTHE_LIVE",
    )

    ord_lines = []
    rows = []
    for o in active_orders:
        so_exp = int(o.get("so_expected") or amount_to_so(int(o["amount_face"])))
        v_badge = "🟢 Đã nhảy Sò" if o.get("so_verified") else "⏳ Chờ nhảy Sò"
        ord_lines.append(
            f"• <code>#{o['order_id']}</code> | {S.fmt_vnd(o['amount_face'])} (<b>+{S.fmt_so(so_exp)}</b>) | {v_badge}"
        )
        if not o.get("so_verified"):
            rows.append([
                btn(
                    f"🐚 Xác nhận Sò nhảy +{S.fmt_so(so_exp)} cho #{o['order_id']}",
                    f"adm_jump_so_now:{o['order_id']}",
                    style="success",
                )
            ])

    rows.extend([
        [btn("🔄 Tự động đăng nhập ngầm & Check Sò 100%", "adm:auto_login", style="success")],
        [
            btn("🔄 Đặt lại Sò về 0", "adm:reset_so", style="danger"),
            btn("✏️ Chỉnh số dư Sò", "adm:edit_so", style="primary"),
        ],
        [btn("« Về Bảng Điều Khiển Admin", "adm:home", style="primary")],
    ])

    caption = (
        f"🐚 <b>TRUNG TÂM CHECK SÒ GARENA (NAPTHE.VN)</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"• Tài khoản Garena: <code>{esc(garena_acc)}</code>\n"
        f"• Số dư Garena Sò hiện tại: <b>🐚 {S.fmt_so(cur_so)}</b>\n"
        f"• Quy đổi chuẩn Napthe.vn: <b>200k = 400 Sò | 500k = 1.000 Sò | 5M = 10.000 Sò</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📋 <b>Đơn đang hoạt động:</b>\n"
        + ("\n".join(ord_lines) if ord_lines else "<i>Không có đơn nào đang chờ.</i>")
    )
    await ui.answer_photo(
        target_msg,
        BufferedInputFile(so_png, filename="Napthe_So_Status.png"),
        caption=caption,
        markup=kb(rows),
    )


@router.callback_query(F.data.startswith("adm_jump_so_now:"))
async def cb_adm_jump_so_now(cb: CallbackQuery, bot: Bot):
    if not _check_admin(cb.from_user.id):
        return await cb.answer("⛔ Không có quyền Admin.", show_alert=True)
    oid = cb.data.split(":")[1]
    order, _ = await S.verify_and_jump_order_so(oid)
    if not order:
        return await cb.answer("❌ Không tìm thấy đơn hàng.", show_alert=True)

    u = await S.ensure_user(order["telegram_id"])
    await notify_admins_so_jumped(bot, order, u)
    await cb.answer(f"🐚 Đã nhảy +{S.fmt_so(order['so_expected'])} (Số dư mới: {S.fmt_so(order['so_after'])})!", show_alert=True)


@router.callback_query(F.data.in_({"adm:auto_login", "adm:open_chrome"}))
async def cb_adm_auto_login(cb: CallbackQuery):
    if not _check_admin(cb.from_user.id):
        return await cb.answer("⛔ Không có quyền Admin.", show_alert=True)
    await cb.answer("⏳ Đang tự động kiểm tra phiên ngầm...")
    res = await napthe_engine.auto_login_and_check_so()
    icon = "✅" if res["success"] else "⚠️"
    so_text = f"\n• Số Sò hiện tại: <b>{S.fmt_so(res['so_balance'] or 0)} 🐚</b>" if res["so_balance"] is not None else ""
    await cb.message.answer(
        f"{icon} <b>KẾT QUẢ ĐĂNG NHẬP NAPTHE.VN (100% TỰ ĐỘNG NGẦM):</b>\n\n"
        f"• Tài khoản: <code>{esc(res['username'])}</code>\n"
        f"• Phương thức: <code>{esc(res['method'])}</code>"
        f"{so_text}\n"
        f"• Thông báo: <i>{esc(res['message'])}</i>\n\n"
        "💡 <b>Lệnh cấu hình nhanh cho Admin:</b>\n"
        "• <code>/setpass [mật_khẩu]</code>: Cài mật khẩu để bot tự động đăng nhập ngầm\n"
        "• <code>/setcookie [cookie]</code>: Cài cookie Napthe để chạy API siêu tốc dưới 0.5s\n"
        "• <code>/checkso</code>: Kiểm tra số dư Sò hiện tại"
    )


@router.callback_query(F.data == "adm:reset_so")
async def cb_adm_reset_so(cb: CallbackQuery):
    if not _check_admin(cb.from_user.id):
        return await cb.answer("⛔ Không có quyền Admin.", show_alert=True)
    await S.set_current_so_balance(0)
    await cb.answer("✅ Đã đặt lại số dư Garena Sò về 0 Sò!", show_alert=True)
    text, markup = await _render_admin_dashboard()
    await ui.edit(cb, text, markup)


@router.callback_query(F.data == "adm:edit_so")
async def cb_adm_edit_so(cb: CallbackQuery, state: FSMContext):
    if not _check_admin(cb.from_user.id):
        return await cb.answer("⛔ Không có quyền Admin.", show_alert=True)
    cur_so = await S.get_current_so_balance()
    await state.set_state(AdminSG.set_so_balance_input)
    await cb.message.answer(
        f"🐚 <b>CẬP NHẬT SỐ DƯ SÒ HIỆN TẠI TRÊN NAPTHE.VN</b>\n\n"
        f"• Số dư Sò hiện tại: <b>{S.fmt_so(cur_so)}</b>\n"
        f"👉 Vui lòng nhập số Sò hiện có trên tài khoản Garena (VD: <code>0</code> hoặc <code>10000</code>):",
        reply_markup=kb([[btn("« Hủy", "adm:home", style="danger")]]),
    )
    await cb.answer()


@router.message(AdminSG.set_so_balance_input, F.text)
async def on_adm_so_balance_input(message: Message, state: FSMContext):
    if not _check_admin(message.from_user.id):
        return
    raw = message.text.strip().replace(".", "").replace(",", "")
    if not raw.isdigit():
        return await message.answer("❌ Vui lòng nhập một số nguyên hợp lệ (VD: <code>0</code> hoặc <code>10000</code>).")
    await S.set_current_so_balance(int(raw))
    await state.clear()
    text, markup = await _render_admin_dashboard()
    await message.answer(f"✅ Đã cập nhật số dư Garena Sò trên Napthe.vn thành: <b>{S.fmt_so(int(raw))}</b>!\n\n" + text, reply_markup=markup)


# ======================================================================
# GIAI ĐOẠN 3A: DUYỆT ĐƠN SPAYLATER (#SPAY_XXXXXX) -> CỘNG TIỀN VÀO VÍ KHÁCH
# ======================================================================
@router.callback_query(F.data.startswith("adm_paid:"))
async def cb_admin_confirm_paid(cb: CallbackQuery, bot: Bot):
    """Duyệt giao dịch quét mã SPayLater -> Cộng tiền thực nhận vào số dư Ví của khách."""
    if not _check_admin(cb.from_user.id):
        return await cb.answer("⛔ Bạn không có quyền Admin.", show_alert=True)

    oid = cb.data.split(":")[1]
    order = await S.get_order(oid)
    if not order:
        return await cb.answer("❌ Không tìm thấy đơn hàng.", show_alert=True)
    if order["status"] == "PAID":
        return await cb.answer("🟢 Đơn hàng này đã được duyệt cộng tiền vào ví trước đó rồi!", show_alert=True)

    updated = await S.mark_order_paid(oid)
    if not updated:
        return await cb.answer("⚠️ Không thể cập nhật trạng thái đơn.", show_alert=True)

    u = await S.get_user(updated["telegram_id"])
    new_bal = int(u.get("balance") or 0) if u else int(updated["amount_received"])

    admin_tag = f"@{cb.from_user.username}" if cb.from_user.username else cb.from_user.full_name
    done_badge = (
        f"\n\n🟢 <b>ĐÃ DUYỆT & CỘNG +{S.fmt_vnd(updated['amount_received'])} VÀO VÍ KHÁCH</b>\n"
        f"💰 Số dư ví mới của khách: <b>{S.fmt_vnd(new_bal)}</b>\n"
        f"🐚 Sò Napthe.vn: <b>+{S.fmt_so(updated['so_expected'])} ({S.fmt_so(updated['so_before'])} ➔ {S.fmt_so(updated['so_after'])})</b>\n"
        f"👤 Duyệt bởi: {esc(admin_tag)}"
    )
    try:
        if cb.message.caption:
            await cb.message.edit_caption(caption=cb.message.caption + done_badge, reply_markup=None)
        elif cb.message.text:
            await cb.message.edit_text(text=cb.message.text + done_badge, reply_markup=None)
    except TelegramBadRequest:
        pass

    user_msg = (
        f"{ce('party', '🎉')} <b>GIAO DỊCH #{oid} THÀNH CÔNG!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{ce('plus', '➕')} Đã cộng <b>+{S.fmt_vnd(updated['amount_received'])}</b> vào tài khoản của bạn!\n"
        f"{ce('money_bag', '💰')} <b>Số dư khả dụng hiện tại: {S.fmt_vnd(new_bal)}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👉 Bạn có thể rút tiền về Ngân hàng bất cứ lúc nào bằng nút bên dưới:"
    )
    try:
        await ui.send(
            bot,
            updated["telegram_id"],
            user_msg,
            kb([
                [btn(f"💸 Rút tiền về Ngân hàng ngay ({S.fmt_vnd(new_bal)})", "u:withdraw", icon="money_fly", emoji="💸", style="success")],
                [btn("🛍 Thực hiện giao dịch mới", "u:home", icon="shopee", emoji="🛍", style="primary")],
            ]),
        )
    except Exception as e:
        log.warning("Could not notify user %s for paid order %s: %s", updated["telegram_id"], oid, e)

    await cb.answer(f"🟢 Đã cộng +{S.fmt_vnd(updated['amount_received'])} vào ví khách!")


@router.callback_query(F.data.startswith("adm_rej:"))
async def cb_admin_reject_menu(cb: CallbackQuery):
    if not _check_admin(cb.from_user.id):
        return await cb.answer("⛔ Bạn không có quyền Admin.", show_alert=True)

    oid = cb.data.split(":")[1]
    order = await S.get_order(oid)
    if not order or order["status"] in ("PAID", "REJECTED"):
        return await cb.answer("Đơn này đã được xử lý trước đó.", show_alert=True)

    rows = [
        [btn("⚠️ Bill cũ / trùng lặp", f"adm_rej_do:{oid}:dup", style="danger")],
        [btn("⚠️ Chưa nhận được thanh toán", f"adm_rej_do:{oid}:noso", style="danger")],
        [btn("⚠️ Sai mệnh giá quét", f"adm_rej_do:{oid}:wrong_amt", style="danger")],
        [btn("✍️ Nhập lý do từ chối khác...", f"adm_rej_custom:{oid}", style="primary")],
    ]
    await cb.message.reply(
        f"❌ <b>CHỌN LÝ DO TỪ CHỐI ĐƠN #{oid}:</b>",
        reply_markup=kb(rows),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("adm_rej_do:"))
async def cb_admin_reject_preset(cb: CallbackQuery, bot: Bot):
    if not _check_admin(cb.from_user.id):
        return await cb.answer("⛔ Bạn không có quyền Admin.", show_alert=True)

    _, oid, code = cb.data.split(":")
    reason = PRESET_REJECT_REASONS.get(code, "Bill không hợp lệ")
    await _execute_reject_order(cb.message, bot, oid, reason, cb.from_user.username or cb.from_user.full_name)
    await cb.answer("🔴 Đã từ chối đơn hàng.")


@router.callback_query(F.data.startswith("adm_rej_custom:"))
async def cb_admin_reject_custom_prompt(cb: CallbackQuery, state: FSMContext):
    if not _check_admin(cb.from_user.id):
        return await cb.answer("⛔ Bạn không có quyền Admin.", show_alert=True)

    oid = cb.data.split(":")[1]
    await state.set_state(AdminSG.custom_reject_reason)
    await state.update_data(reject_oid=oid)
    await cb.message.reply(f"✍️ Vui lòng nhập lý do từ chối cho đơn <code>#{oid}</code>:")
    await cb.answer()


@router.message(AdminSG.custom_reject_reason, F.text)
async def on_admin_custom_reject_reason(message: Message, state: FSMContext, bot: Bot):
    if not _check_admin(message.from_user.id):
        return
    data = await state.get_data()
    oid = data.get("reject_oid", "")
    await state.clear()
    await _execute_reject_order(
        message,
        bot,
        oid,
        message.text.strip(),
        message.from_user.username or message.from_user.full_name,
    )


async def _execute_reject_order(target_msg: Message, bot: Bot, oid: str, reason: str, admin_name: str):
    updated = await S.mark_order_rejected(oid, reason)
    if not updated:
        return await target_msg.answer("⚠️ Đơn hàng không tồn tại hoặc đã được xử lý trước đó.")

    cur_so = await S.get_current_so_balance()
    await target_msg.answer(
        f"🔴 <b>ĐÃ TỪ CHỐI ĐƠN #{oid}</b>\n"
        f"• Lý do: <i>{esc(reason)}</i>\n"
        f"• Số dư Garena Sò hiện tại: <b>{S.fmt_so(cur_so)}</b>\n"
        f"• Xử lý bởi: {esc(admin_name)}"
    )

    user_notice = (
        f"{ce('cross', '❌')} <b>GIAO DỊCH #{oid} BỊ TỪ CHỐI!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"• Mệnh giá: <b>{S.fmt_vnd(updated['amount_face'])}</b>\n"
        f"• Lý do từ chối: <b>{esc(reason)}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        "Nếu có nhầm lẫn, vui lòng kiểm tra lại giao dịch hoặc liên hệ Admin để được hỗ trợ."
    )
    try:
        await ui.send(
            bot,
            updated["telegram_id"],
            user_notice,
            kb([[btn("💬 Liên hệ Admin hỗ trợ", "u:home", icon="support", emoji="💬")]]),
        )
    except Exception as e:
        log.warning("Failed sending reject notice to %s: %s", updated["telegram_id"], e)


# ======================================================================
# GIAI ĐOẠN 3B: XỬ LÝ LỆNH RÚT TIỀN TỪ VÍ (#WD_XXXXXX) VỀ NGÂN HÀNG
# ======================================================================
@router.callback_query(F.data.startswith("adm_wd_qr:"))
async def cb_admin_wd_vietqr(cb: CallbackQuery):
    if not _check_admin(cb.from_user.id):
        return await cb.answer("⛔ Bạn không có quyền Admin.", show_alert=True)

    wd_id = cb.data.split(":")[1]
    wd = await S.get_withdrawal(wd_id)
    if not wd:
        return await cb.answer("❌ Không tìm thấy lệnh rút tiền.", show_alert=True)

    bank_code = wd.get("bank_code") or ""
    acc_num = wd.get("bank_account") or ""
    holder = wd.get("bank_holder") or ""
    if not (bank_code and acc_num and holder):
        bank_code, acc_num, holder, _ = S.parse_bank_info_text(wd.get("bank_info_text") or "")

    if not (bank_code and acc_num):
        return await cb.answer("⚠️ Không tách được mã ngân hàng/STK.", show_alert=True)

    await cb.answer("⚡ Đang tạo mã VietQR giải ngân...")
    png_bytes, vietqr_url = await S.generate_vietqr_payout_png(
        bank_code=bank_code,
        account_number=acc_num,
        account_name=holder,
        amount_received=wd["amount"],
        order_id=wd_id,
    )

    caption = (
        f"⚡ <b>MÃ VIETQR GIẢI NGÂN LỆNH RÚT #{wd_id}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"• Ngân hàng: <b>{esc(bank_code)}</b>\n"
        f"• Số tài khoản: <code>{esc(acc_num)}</code>\n"
        f"• Chủ tài khoản: <b>{esc(holder)}</b>\n"
        f"• Số tiền chuyển: <b>{S.fmt_vnd(wd['amount'])}</b>\n"
        f"• Nội dung CK: <code>Rut SPayLater {wd_id}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👉 <i>Mở App Ngân hàng của Admin quét mã trên để tự động điền đúng số tiền & nội dung.</i>"
    )
    markup = kb([
        [btn("✅ Xác nhận ĐÃ CHUYỂN TIỀN", f"adm_wd_paid:{wd_id}", icon="up_green", emoji="✅", style="success")],
        [btn("🔗 Mở Link VietQR Trực Tiếp", url=vietqr_url, style="primary")],
    ])
    await ui.answer_photo(
        cb.message,
        BufferedInputFile(png_bytes, filename=f"VietQR_{wd_id}.png"),
        caption=caption,
        markup=markup,
    )


@router.callback_query(F.data.startswith("adm_wd_paid:"))
async def cb_admin_wd_paid(cb: CallbackQuery, bot: Bot):
    if not _check_admin(cb.from_user.id):
        return await cb.answer("⛔ Bạn không có quyền Admin.", show_alert=True)

    wd_id = cb.data.split(":")[1]
    wd = await S.get_withdrawal(wd_id)
    if not wd:
        return await cb.answer("❌ Không tìm thấy lệnh rút tiền.", show_alert=True)
    if wd["status"] == "PAID":
        return await cb.answer("🟢 Lệnh rút này đã được xác nhận thanh toán trước đó!", show_alert=True)

    updated = await S.mark_withdrawal_paid(wd_id)
    if not updated:
        return await cb.answer("⚠️ Không thể cập nhật trạng thái lệnh rút.", show_alert=True)

    admin_tag = f"@{cb.from_user.username}" if cb.from_user.username else cb.from_user.full_name
    done_badge = (
        f"\n\n🟢 <b>ĐÃ CHUYỂN KHOẢN XONG ({S.fmt_vnd(updated['amount'])})</b>\n"
        f"👤 Duyệt bởi: {esc(admin_tag)}"
    )
    try:
        if cb.message.caption:
            await cb.message.edit_caption(caption=cb.message.caption + done_badge, reply_markup=None)
        elif cb.message.text:
            await cb.message.edit_text(text=cb.message.text + done_badge, reply_markup=None)
    except TelegramBadRequest:
        pass

    u = await S.get_user(updated["telegram_id"])
    cur_bal = int(u.get("balance") or 0) if u else 0
    user_msg = (
        f"{ce('party', '🎉')} <b>RÚT TIỀN #{wd_id} HOÀN TẤT!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Admin đã chuyển khoản thành công <b>{S.fmt_vnd(updated['amount'])}</b> "
        f"về tài khoản ngân hàng <code>{esc(updated['bank_info_text'])}</code> của bạn.\n"
        f"💰 Số dư ví còn lại: <b>{S.fmt_vnd(cur_bal)}</b>\n\n"
        f"{ce('shopee', '🛍')} Cảm ơn bạn đã tin tưởng dịch vụ! Chúc bạn một ngày tốt lành."
    )
    try:
        await ui.send(
            bot,
            updated["telegram_id"],
            user_msg,
            kb([[btn("🛍 Về Menu Chính", "u:home", icon="shopee", emoji="🛍", style="success")]]),
        )
    except Exception as e:
        log.warning("Could not notify user %s for paid withdrawal %s: %s", updated["telegram_id"], wd_id, e)

    await cb.answer("🟢 Đã xác nhận chuyển tiền rút ví cho khách!")


@router.callback_query(F.data.startswith("adm_wd_rej:"))
async def cb_admin_wd_reject(cb: CallbackQuery, bot: Bot):
    if not _check_admin(cb.from_user.id):
        return await cb.answer("⛔ Bạn không có quyền Admin.", show_alert=True)

    wd_id = cb.data.split(":")[1]
    updated = await S.mark_withdrawal_rejected(wd_id, "Sai thông tin STK ngân hàng (Đã hoàn tiền lại Ví)")
    if not updated:
        return await cb.answer("Lệnh rút này đã được xử lý trước đó.", show_alert=True)

    admin_tag = f"@{cb.from_user.username}" if cb.from_user.username else cb.from_user.full_name
    rej_badge = f"\n\n🔴 <b>ĐÃ TỪ CHỐI & HOÀN LẠI {S.fmt_vnd(updated['amount'])} VÀO VÍ KHÁCH</b> ({esc(admin_tag)})"
    try:
        if cb.message.text:
            await cb.message.edit_text(text=cb.message.text + rej_badge, reply_markup=None)
    except TelegramBadRequest:
        pass

    u = await S.get_user(updated["telegram_id"])
    cur_bal = int(u.get("balance") or 0) if u else 0
    user_notice = (
        f"{ce('cross', '❌')} <b>LỆNH RÚT TIỀN #{wd_id} ĐÃ HOÀN LẠI VÍ!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"• Số tiền hoàn lại ví: <b>+{S.fmt_vnd(updated['amount'])}</b>\n"
        f"• Số dư khả dụng hiện tại: <b>{S.fmt_vnd(cur_bal)}</b>\n"
        f"• Lý do: <i>Vui lòng kiểm tra lại đúng Số tài khoản / Tên ngân hàng và tạo lại lệnh rút.</i>"
    )
    try:
        await ui.send(
            bot,
            updated["telegram_id"],
            user_notice,
            kb([[btn("💸 Kiểm tra & Rút lại", "u:withdraw", icon="money_fly", emoji="💸", style="success")]]),
        )
    except Exception as e:
        log.warning("Failed sending wd reject notice to %s: %s", updated["telegram_id"], e)

    await cb.answer("🔴 Đã hủy lệnh rút và hoàn tiền lại ví cho khách!")


# ======================================================================
# PHẦN IV: HỆ THỐNG LỆNH QUẢN TRỊ VIÊN (SLASH COMMANDS)
# ======================================================================
@router.message(Command("addbal"))
async def cmd_addbal(message: Message):
    """/addbal [telegram_id] [số_tiền] - Cộng/trừ số dư ví khách thủ công."""
    if not _check_admin(message.from_user.id):
        return
    parts = message.text.strip().split()
    if len(parts) < 3 or not parts[1].isdigit():
        return await message.answer(
            "📌 <b>Cú pháp cộng/trừ số dư ví khách:</b>\n"
            "<code>/addbal [telegram_id] [số_tiền]</code>\n"
            "• Ví dụ cộng 500k: <code>/addbal 8754685711 500000</code>\n"
            "• Ví dụ trừ 200k: <code>/addbal 8754685711 -200000</code>"
        )
    tg_id = int(parts[1])
    await S.ensure_user(tg_id)
    try:
        delta = int(parts[2].replace(".", "").replace(",", ""))
    except ValueError:
        return await message.answer("❌ Số tiền không hợp lệ!")

    new_bal = await S.adjust_user_balance(tg_id, delta)
    await message.answer(
        f"✅ Đã điều chỉnh số dư ví của user <code>{tg_id}</code> ({'+' if delta >= 0 else ''}{S.fmt_vnd(delta)}).\n"
        f"💰 Số dư mới: <b>{S.fmt_vnd(new_bal)}</b>"
    )


@router.message(Command("setfee"))
async def cmd_setfee(message: Message):
    if not _check_admin(message.from_user.id):
        return
    parts = message.text.strip().split()
    if len(parts) < 2:
        cur_fee = await S.get_default_fee()
        return await message.answer(
            f"📌 Phí hệ thống hiện tại: <b>{cur_fee:g}%</b>\n"
            f"Cú pháp đổi phí: <code>/setfee [số_phí]</code> (Ví dụ: <code>/setfee 13</code>)"
        )
    try:
        val = float(parts[1].replace("%", "").replace(",", "."))
        if not (0 <= val <= 90):
            raise ValueError
    except ValueError:
        return await message.answer("❌ Mức phí không hợp lệ! Vui lòng nhập số từ 0 đến 90 (VD: <code>/setfee 12</code>).")

    await S.set_config("default_fee_percent", str(val))
    await message.answer(f"✅ Đã cập nhật biểu phí dịch vụ toàn hệ thống thành: <b>{val:g}%</b>")


@router.message(Command("setvip"))
async def cmd_setvip(message: Message):
    if not _check_admin(message.from_user.id):
        return
    parts = message.text.strip().split()
    if len(parts) < 3 or not parts[1].isdigit():
        return await message.answer(
            "📌 <b>Cú pháp cài phí VIP/Đại lý riêng:</b>\n"
            "<code>/setvip [telegram_id] [số_phí]</code>\n"
            "• Ví dụ: <code>/setvip 123456789 10</code> (Cài phí 10%)\n"
            "• Để xóa phí riêng về mặc định: <code>/setvip 123456789 reset</code>"
        )
    tg_id = int(parts[1])
    await S.ensure_user(tg_id)
    if parts[2].lower() in ("reset", "default", "none", "xoa"):
        await db.execute("UPDATE users SET custom_fee=NULL WHERE telegram_id=?", (tg_id,))
        return await message.answer(f"✅ Đã đưa mức phí của user <code>{tg_id}</code> về biểu phí mặc định hệ thống.")

    try:
        fee_val = float(parts[2].replace("%", "").replace(",", "."))
        if not (0 <= fee_val <= 90):
            raise ValueError
    except ValueError:
        return await message.answer("❌ Số phí không hợp lệ!")

    await db.execute("UPDATE users SET custom_fee=? WHERE telegram_id=?", (fee_val, tg_id))
    await message.answer(f"👑 Đã cài mức phí VIP riêng cho user <code>{tg_id}</code> thành: <b>{fee_val:g}%</b>")


@router.message(Command("user"))
async def cmd_user_info(message: Message):
    if not _check_admin(message.from_user.id):
        return
    parts = message.text.strip().split()
    if len(parts) < 2 or not parts[1].isdigit():
        return await message.answer("📌 Cú pháp: <code>/user [telegram_id]</code>\nVí dụ: <code>/user 8754685711</code>")

    tg_id = int(parts[1])
    text, markup = await _render_user_profile_admin(tg_id)
    await message.answer(text, reply_markup=markup)


async def _render_user_profile_admin(tg_id: int) -> tuple[str, object]:
    u = await S.get_user(tg_id)
    if not u:
        return f"❌ Không tìm thấy khách hàng có ID <code>{tg_id}</code> trong hệ thống.", None

    def_fee = await S.get_default_fee()
    bal = int(u.get("balance") or 0)
    vip_str = f"👑 VIP ({u['custom_fee']:g}%)" if u.get("custom_fee") is not None else f"Mặc định ({def_fee:g}%)"
    status_str = "🔴 ĐANG BỊ KHÓA (BANNED)" if u.get("is_banned") else "🟢 Hoạt động bình thường"
    bank_str = (
        f"{u['bank_name']} - {u['bank_account_number']} - {u['bank_account_name']}"
        if u.get("bank_account_number")
        else "(Chưa lưu STK)"
    )

    text = (
        f"👤 <b>HỒ SƠ KHÁCH HÀNG #{tg_id}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"• Username: @{esc(u['username'] or 'chua_co')}\n"
        f"• Họ tên: <b>{esc(u['full_name'] or 'N/A')}</b>\n"
        f"• Trạng thái: <b>{status_str}</b>\n"
        f"• 💰 <b>Số dư ví hiện tại: {S.fmt_vnd(bal)}</b>\n"
        f"• Biểu phí áp dụng: <b>{vip_str}</b>\n"
        f"• Tổng đơn hoàn tất: <b>{u['total_orders']} đơn</b>\n"
        f"• Tổng tiền đã rút về NH: <b>{S.fmt_vnd(u['total_withdrawn'])}</b>\n"
        f"• STK đang lưu: <code>{esc(bank_str)}</code>\n"
        f"• Ngày tham gia: <code>{u['created_at']}</code>"
    )
    ban_label = "🔓 Mở khóa tài khoản" if u.get("is_banned") else "🔒 Khóa tài khoản (Ban)"
    markup = kb([
        [btn("💰 Cộng / Trừ số dư Ví", f"adm_prompt_bal:{tg_id}", style="success")],
        [btn("👑 Cài phí VIP riêng", f"adm_prompt_vip:{tg_id}", style="primary")],
        [btn(ban_label, f"adm_toggle_ban:{tg_id}", style="danger" if not u.get("is_banned") else "success")],
        [btn("« Về Bảng Admin", "adm:home", style="primary")],
    ])
    return text, markup


@router.callback_query(F.data.startswith("adm_prompt_bal:"))
async def cb_adm_prompt_bal(cb: CallbackQuery, state: FSMContext):
    if not _check_admin(cb.from_user.id):
        return await cb.answer("⛔ Không có quyền.", show_alert=True)
    tg_id = int(cb.data.split(":")[1])
    await state.set_state(AdminSG.adjust_bal_input)
    await state.update_data(bal_tg_id=tg_id)
    await cb.message.answer(
        f"💰 Nhập số tiền muốn cộng (hoặc thêm dấu <code>-</code> để trừ) vào ví của khách <code>{tg_id}</code> (VD: <code>440000</code> hoặc <code>-100000</code>):"
    )
    await cb.answer()


@router.message(AdminSG.adjust_bal_input, F.text)
async def on_adm_bal_input(message: Message, state: FSMContext):
    if not _check_admin(message.from_user.id):
        return
    data = await state.get_data()
    tg_id = int(data.get("bal_tg_id", 0))
    raw = message.text.strip().replace(".", "").replace(",", "")
    try:
        delta = int(raw)
    except ValueError:
        return await message.answer("❌ Vui lòng nhập số nguyên hợp lệ (VD: 440000 hoặc -100000).")
    await S.adjust_user_balance(tg_id, delta)
    await state.clear()
    text, markup = await _render_user_profile_admin(tg_id)
    await message.answer(text, reply_markup=markup)


@router.message(Command("ban"))
async def cmd_ban_user(message: Message):
    if not _check_admin(message.from_user.id):
        return
    parts = message.text.strip().split()
    if len(parts) < 2 or not parts[1].isdigit():
        return await message.answer("📌 Cú pháp: <code>/ban [telegram_id]</code>")

    tg_id = int(parts[1])
    u = await S.ensure_user(tg_id)
    new_ban = 0 if u.get("is_banned") else 1
    await db.execute("UPDATE users SET is_banned=? WHERE telegram_id=?", (new_ban, tg_id))
    state_txt = "🔒 <b>ĐÃ KHÓA (BAN)</b>" if new_ban else "🔓 <b>ĐÃ MỞ KHÓA (UNBAN)</b>"
    await message.answer(f"{state_txt} tài khoản người dùng <code>{tg_id}</code>.")


@router.callback_query(F.data.startswith("adm_toggle_ban:"))
async def cb_adm_toggle_ban(cb: CallbackQuery):
    if not _check_admin(cb.from_user.id):
        return await cb.answer("⛔ Không có quyền.", show_alert=True)
    tg_id = int(cb.data.split(":")[1])
    u = await S.ensure_user(tg_id)
    new_ban = 0 if u.get("is_banned") else 1
    await db.execute("UPDATE users SET is_banned=? WHERE telegram_id=?", (new_ban, tg_id))
    text, markup = await _render_user_profile_admin(tg_id)
    await ui.edit(cb, text, markup)
    await cb.answer("✅ Đã cập nhật trạng thái tài khoản!")


@router.callback_query(F.data.startswith("adm_prompt_vip:"))
async def cb_adm_prompt_vip(cb: CallbackQuery, state: FSMContext):
    if not _check_admin(cb.from_user.id):
        return await cb.answer("⛔ Không có quyền.", show_alert=True)
    tg_id = int(cb.data.split(":")[1])
    await state.set_state(AdminSG.set_vip_input)
    await state.update_data(vip_tg_id=tg_id)
    await cb.message.answer(
        f"👑 Nhập % phí VIP riêng cho khách <code>{tg_id}</code> (VD: <code>10</code>), hoặc nhập <code>reset</code> để về mặc định:"
    )
    await cb.answer()


@router.message(AdminSG.set_vip_input, F.text)
async def on_adm_vip_input(message: Message, state: FSMContext):
    if not _check_admin(message.from_user.id):
        return
    data = await state.get_data()
    tg_id = int(data.get("vip_tg_id", 0))
    raw = message.text.strip().lower()
    if raw in ("reset", "default", "xoa"):
        await db.execute("UPDATE users SET custom_fee=NULL WHERE telegram_id=?", (tg_id,))
    else:
        try:
            val = float(raw.replace("%", "").replace(",", "."))
            await db.execute("UPDATE users SET custom_fee=? WHERE telegram_id=?", (val, tg_id))
        except ValueError:
            return await message.answer("❌ Vui lòng nhập một con số hợp lệ (VD: 10).")
    await state.clear()
    text, markup = await _render_user_profile_admin(tg_id)
    await message.answer(text, reply_markup=markup)


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    if not _check_admin(message.from_user.id):
        return
    text, markup = await _render_stats_report()
    await message.answer(text, reply_markup=markup)


async def _render_stats_report() -> tuple[str, object]:
    st_day = await S.get_stats(days=1)
    st_week = await S.get_stats(days=7)
    st_month = await S.get_stats(days=30)
    st_all = await S.get_stats(days=None)
    garena_acc = await S.get_garena_account()

    def _block(title: str, d: dict) -> str:
        return (
            f"📌 <b>{title}:</b>\n"
            f"  • Số đơn hoàn thành: <b>{d['n_orders']} đơn</b>\n"
            f"  • Tổng mệnh giá Sò đã nạp: <b>{S.fmt_vnd(d['total_face'])} (+{S.fmt_so(d['total_so'])})</b>\n"
            f"  • Tổng tiền cộng ví khách: <b>{S.fmt_vnd(d['total_paid'])}</b>\n"
            f"  • Lợi nhuận gộp thu về: <b>{S.fmt_vnd(d['profit'])}</b>\n"
        )

    text = (
        f"📊 <b>BÁO CÁO DOANH THU & LỢI NHUẬN RÚT SPAYLATER</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎮 TK Garena: <code>{esc(garena_acc)}</code>  |  🐚 Số dư Sò: <b>{S.fmt_so(st_all['cur_so'])}</b>\n"
        f"👥 Tổng khách: <b>{st_all['users_cnt']}</b>  |  💰 Tổng số dư ví khách: <b>{S.fmt_vnd(st_all['total_user_bal'])}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        + _block("HÔM NAY (24H)", st_day)
        + "\n"
        + _block("7 NGÀY QUA (TUẦN)", st_week)
        + "\n"
        + _block("30 NGÀY QUA (THÁNG)", st_month)
        + "\n"
        + _block("TOÀN THỜI GIAN", st_all)
    )
    markup = kb([
        [btn("🔄 Làm mới số liệu", "adm:stats", style="success")],
        [btn("« Về Bảng Điều Khiển Admin", "adm:home", style="primary")],
    ])
    return text, markup


# ======================================================================
# BẢNG ĐIỀU KHIỂN ADMIN TƯƠNG TÁC (/admin)
# ======================================================================
async def _render_admin_dashboard() -> tuple[str, object]:
    fee = await S.get_default_fee()
    maint = await S.is_maintenance()
    garena_acc = await S.get_garena_account()
    cur_so = await S.get_current_so_balance()
    st_day = await S.get_stats(days=1)
    st_all = await S.get_stats(days=None)

    maint_str = "🔴 ĐANG BẢO TRÌ (Tạm ngưng nhận đơn)" if maint else "🟢 ĐANG HOẠT ĐỘNG 24/7"

    text = (
        f"👑 <b>BẢNG QUẢN TRỊ HỆ THỐNG RÚT VÍ SPAYLATER</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"• Trạng thái Bot: <b>{maint_str}</b>\n"
        f"• Phí dịch vụ mặc định: <b>{fee:g}%</b>\n"
        f"• Tài khoản Garena Napthe.vn: <code>{esc(garena_acc)}</code>\n"
        f"• Số dư Garena Sò hiện tại: <b>🐚 {S.fmt_so(cur_so)}</b>\n"
        f"• Đơn chờ duyệt: <b>{st_all['pending_ord']} Bill + {st_all['pending_wd']} Lệnh rút ví</b>\n"
        f"• Lợi nhuận hôm nay: <b>{S.fmt_vnd(st_day['profit'])} ({st_day['n_orders']} đơn)</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💡 <b>Lệnh nhanh:</b> <code>/setpass</code> | <code>/setcookie</code> | <code>/checkso</code> | <code>/login_napthe</code> | <code>/setfee</code> | <code>/stats</code>"
    )
    maint_btn_txt = "🟢 Tắt Bảo Trì (Mở lại Bot)" if maint else "🔴 Bật Chế Độ Bảo Trì"
    markup = kb([
        [btn(f"🐚 Check Sò Napthe.vn (Hiện có: {S.fmt_so(cur_so)})", "adm:so_panel", icon="garena", emoji="🐚", style="success")],
        [
            btn(f"⏳ Chờ duyệt ({st_all['pending_cnt']})", "adm:pending", style="success"),
            btn("📊 Thống kê Doanh thu", "adm:stats", style="primary"),
        ],
        [
            btn(f"💸 Đổi Phí Chung ({fee:g}%)", "adm:set_fee", style="primary"),
            btn(f"🎮 Đổi TK Garena ({garena_acc})", "adm:set_garena", style="primary"),
        ],
        [
            btn("🖼 Cài ảnh QR ShopeePay Mẫu", "adm:qr_templates", style="primary"),
            btn("🔍 Tra cứu / Cộng Ví Khách", "adm:lookup_user", style="primary"),
        ],
        [btn(maint_btn_txt, "adm:toggle_maint", style="danger" if not maint else "success")],
        [btn("🏠 Về Giao Diện Khách Hàng", "u:home", style="success")],
    ])
    return text, markup


@router.message(Command("admin"))
async def cmd_admin_panel(message: Message, state: FSMContext):
    if not _check_admin(message.from_user.id):
        return
    await state.clear()
    text, markup = await _render_admin_dashboard()
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "adm:home")
async def cb_adm_home(cb: CallbackQuery, state: FSMContext):
    if not _check_admin(cb.from_user.id):
        return await cb.answer("⛔ Không có quyền Admin.", show_alert=True)
    await state.clear()
    text, markup = await _render_admin_dashboard()
    await ui.edit(cb, text, markup)
    await cb.answer()


@router.callback_query(F.data == "adm:stats")
async def cb_adm_stats(cb: CallbackQuery):
    if not _check_admin(cb.from_user.id):
        return await cb.answer("⛔ Không có quyền Admin.", show_alert=True)
    text, markup = await _render_stats_report()
    await ui.edit(cb, text, markup)
    await cb.answer()


@router.callback_query(F.data == "adm:toggle_maint")
async def cb_adm_toggle_maint(cb: CallbackQuery):
    if not _check_admin(cb.from_user.id):
        return await cb.answer("⛔ Không có quyền Admin.", show_alert=True)
    cur = await S.is_maintenance()
    await S.set_config("is_maintenance", "0" if cur else "1")
    text, markup = await _render_admin_dashboard()
    await ui.edit(cb, text, markup)
    await cb.answer("✅ Đã chuyển đổi chế độ bảo trì!")


@router.callback_query(F.data == "adm:set_fee")
async def cb_adm_set_fee(cb: CallbackQuery, state: FSMContext):
    if not _check_admin(cb.from_user.id):
        return await cb.answer("⛔ Không có quyền Admin.", show_alert=True)
    await state.set_state(AdminSG.set_fee_input)
    cur_fee = await S.get_default_fee()
    await ui.edit(
        cb,
        f"💸 <b>ĐỔI TỶ LỆ PHÍ RÚT SPAYLATER TOÀN HỆ THỐNG</b>\n\n"
        f"• Phí hiện tại: <b>{cur_fee:g}%</b>\n"
        f"👉 Vui lòng nhập mức phí mới (VD: <code>12</code> hoặc <code>13.5</code>):",
        kb([[btn("« Hủy", "adm:home", style="danger")]]),
    )
    await cb.answer()


@router.message(AdminSG.set_fee_input, F.text)
async def on_adm_fee_input(message: Message, state: FSMContext):
    if not _check_admin(message.from_user.id):
        return
    try:
        val = float(message.text.strip().replace("%", "").replace(",", "."))
        if not (0 <= val <= 90):
            raise ValueError
    except ValueError:
        return await message.answer("❌ Vui lòng nhập số hợp lệ từ 0 đến 90.")
    await S.set_config("default_fee_percent", str(val))
    await state.clear()
    text, markup = await _render_admin_dashboard()
    await message.answer(f"✅ Đã đổi phí hệ thống thành <b>{val:g}%</b>!\n\n" + text, reply_markup=markup)


@router.callback_query(F.data == "adm:set_garena")
async def cb_adm_set_garena(cb: CallbackQuery, state: FSMContext):
    if not _check_admin(cb.from_user.id):
        return await cb.answer("⛔ Không có quyền Admin.", show_alert=True)
    await state.set_state(AdminSG.set_garena_input)
    cur_acc = await S.get_garena_account()
    await ui.edit(
        cb,
        f"🎮 <b>CÀI TÀI KHOẢN GARENA ĐĂNG NHẬP NAPTHE.VN</b>\n\n"
        f"• Tài khoản hiện tại: <code>{esc(cur_acc)}</code>\n\n"
        "Vui lòng gửi tên tài khoản Garena (ví dụ: <code>nhat2301088</code>) hoặc theo cú pháp:\n"
        "<code>taikhoan | matkhau</code>",
        kb([[btn("« Hủy", "adm:home", style="danger")]]),
    )
    await cb.answer()


@router.message(AdminSG.set_garena_input, F.text)
async def on_adm_garena_input(message: Message, state: FSMContext):
    if not _check_admin(message.from_user.id):
        return
    raw = message.text.strip()
    if "|" in raw:
        acc, pwd = [x.strip() for x in raw.split("|", 1)]
        await S.set_config("garena_account", acc)
        await S.set_config("garena_password", pwd)
    else:
        await S.set_config("garena_account", raw)
        await S.set_config("garena_password", "")

    await state.clear()
    text, markup = await _render_admin_dashboard()
    await message.answer("✅ Đã lưu cấu hình tài khoản Garena Napthe.vn!\n\n" + text, reply_markup=markup)


@router.callback_query(F.data == "adm:lookup_user")
async def cb_adm_lookup_user(cb: CallbackQuery, state: FSMContext):
    if not _check_admin(cb.from_user.id):
        return await cb.answer("⛔ Không có quyền Admin.", show_alert=True)
    await state.set_state(AdminSG.lookup_user_input)
    await ui.edit(
        cb,
        "🔍 <b>TRA CỨU HỒ SƠ & SỐ DƯ VÍ KHÁCH HÀNG</b>\n\n"
        "Vui lòng gửi <b>Telegram ID</b> của khách hàng cần kiểm tra:",
        kb([[btn("« Quay lại", "adm:home", style="danger")]]),
    )
    await cb.answer()


@router.message(AdminSG.lookup_user_input, F.text)
async def on_adm_lookup_user(message: Message, state: FSMContext):
    if not _check_admin(message.from_user.id):
        return
    raw = message.text.strip()
    if not raw.isdigit():
        return await message.answer("❌ Telegram ID phải là dãy số.")
    await state.clear()
    text, markup = await _render_user_profile_admin(int(raw))
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "adm:pending")
async def cb_adm_pending_orders(cb: CallbackQuery):
    if not _check_admin(cb.from_user.id):
        return await cb.answer("⛔ Không có quyền Admin.", show_alert=True)

    ord_rows = await db.fetchall(
        "SELECT * FROM orders WHERE status='PENDING_ADMIN_REVIEW' ORDER BY created_at ASC LIMIT 8"
    )
    wd_rows = await db.fetchall(
        "SELECT * FROM withdrawals WHERE status='PENDING' ORDER BY created_at ASC LIMIT 8"
    )

    if not ord_rows and not wd_rows:
        return await ui.edit(
            cb,
            "✅ <b>Hiện không có đơn Bill hay lệnh Rút ví nào đang chờ duyệt!</b>",
            kb([[btn("« Về Bảng Điều Khiển Admin", "adm:home", style="primary")]]),
        )

    lines = ["⏳ <b>DANH SÁCH ĐANG CHỜ DUYỆT:</b>\n"]
    kb_rows = []

    if ord_rows:
        lines.append("📸 <b>1. ĐƠN BILL CHỜ CỘNG VÍ:</b>")
        for o in ord_rows:
            so_exp = int(o.get("so_expected") or amount_to_so(int(o["amount_face"])))
            lines.append(
                f"• <code>#{o['order_id']}</code> | Quét <b>{S.fmt_vnd(o['amount_face'])} (+{S.fmt_so(so_exp)})</b> -> Cộng ví <b>+{S.fmt_vnd(o['amount_received'])}</b>"
            )
            kb_rows.append([
                btn(f"🐚 Sò #{o['order_id']}", f"adm_check_so:{o['order_id']}", style="primary"),
                btn(f"✅ Cộng Ví #{o['order_id']}", f"adm_paid:{o['order_id']}", style="success"),
                btn("❌ Từ chối", f"adm_rej:{o['order_id']}", style="danger"),
            ])

    if wd_rows:
        lines.append("\n💸 <b>2. LỆNH RÚT TIỀN TỪ VÍ VỀ NGÂN HÀNG:</b>")
        for w in wd_rows:
            lines.append(
                f"• <code>#{w['wd_id']}</code> | Chuyển <b>{S.fmt_vnd(w['amount'])}</b> -> <code>{esc(w['bank_info_text'])}</code>"
            )
            kb_rows.append([
                btn(f"⚡ VietQR #{w['wd_id']}", f"adm_wd_qr:{w['wd_id']}", style="primary"),
                btn(f"✅ Đã CK #{w['wd_id']}", f"adm_wd_paid:{w['wd_id']}", style="success"),
                btn("❌ Hoàn Ví", f"adm_wd_rej:{w['wd_id']}", style="danger"),
            ])

    kb_rows.append([btn("« Về Bảng Điều Khiển Admin", "adm:home", style="primary")])
    await ui.edit(cb, "\n".join(lines), kb(kb_rows))
    await cb.answer()


# ======================================================================
# QUẢN LÝ KHO QR SHOPEEPAY MẪU (TÙY CHỌN CHO TỪNG MỆNH GIÁ)
# ======================================================================
@router.callback_query(F.data == "adm:qr_templates")
async def cb_adm_qr_templates(cb: CallbackQuery, state: FSMContext):
    if not _check_admin(cb.from_user.id):
        return await cb.answer("⛔ Không có quyền Admin.", show_alert=True)
    await state.clear()
    rows = []
    for amt, _ in NAPTHE_DENOMINATIONS:
        has_tpl = os.path.exists(os.path.join(QR_STORAGE_DIR, f"template_{amt}.png"))
        badge = "✅ Đã cài QR riêng" if has_tpl else "⚡ Tự động"
        rows.append([btn(f"{S.fmt_vnd(amt)} ({badge})", f"adm_tpl:{amt}", style="success" if has_tpl else "primary")])

    has_def = os.path.exists(os.path.join(QR_STORAGE_DIR, "template_default.png"))
    rows.append([btn(f"🖼 QR Mặc định chung ({'✅ Đã cài' if has_def else 'Chưa cài'})", "adm_tpl:default", style="primary")])
    rows.append([btn("« Về Bảng Điều Khiển Admin", "adm:home", style="danger")])

    await ui.edit(
        cb,
        "🖼 <b>CẤU HÌNH ẢNH MÃ QR SHOPEEPAY / NAPTHE.VN</b>\n\n"
        "• Mặc định bot tự động lấy mã QR ShopeePay thật từ Napthe.vn theo từng mệnh giá.\n"
        "• Nếu bạn muốn gắn <b>ảnh QR ShopeePay cố định</b> của riêng bạn cho từng mốc mệnh giá, hãy bấm vào mệnh giá tương ứng bên dưới để tải ảnh QR lên:",
        kb(rows),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("adm_tpl:"))
async def cb_adm_tpl_select(cb: CallbackQuery, state: FSMContext):
    if not _check_admin(cb.from_user.id):
        return await cb.answer("⛔ Không có quyền Admin.", show_alert=True)
    key = cb.data.split(":")[1]
    await state.set_state(AdminSG.upload_qr_template)
    await state.update_data(tpl_key=key)
    label = f"mệnh giá {S.fmt_vnd(int(key))}" if key.isdigit() else "Mặc định chung"
    await ui.edit(
        cb,
        f"📸 Vui lòng gửi <b>Ảnh mã QR ShopeePay</b> cho <b>{label}</b>\n"
        f"<i>(Hoặc gửi chữ <code>xoa</code> để xóa ảnh QR mẫu và dùng lại chế độ tự động)</i>:",
        kb([[btn("« Quay lại", "adm:qr_templates", style="danger")]]),
    )
    await cb.answer()


@router.message(AdminSG.upload_qr_template, F.photo)
async def on_adm_tpl_photo(message: Message, state: FSMContext, bot: Bot):
    if not _check_admin(message.from_user.id):
        return
    data = await state.get_data()
    key = data.get("tpl_key", "default")
    dest = os.path.join(QR_STORAGE_DIR, f"template_{key}.png")
    file = await bot.get_file(message.photo[-1].file_id)
    await bot.download_file(file.file_path, destination=dest)
    await state.clear()
    await message.answer(
        f"✅ Đã lưu ảnh QR mẫu cho mốc <b>{key}</b> thành công!",
        reply_markup=kb([[btn("« Quay lại Kho QR Mẫu", "adm:qr_templates", style="primary")]]),
    )


@router.message(AdminSG.upload_qr_template, F.text)
async def on_adm_tpl_text(message: Message, state: FSMContext):
    if not _check_admin(message.from_user.id):
        return
    if message.text.strip().lower() in ("xoa", "delete", "clear"):
        data = await state.get_data()
        key = data.get("tpl_key", "default")
        dest = os.path.join(QR_STORAGE_DIR, f"template_{key}.png")
        if os.path.exists(dest):
            os.remove(dest)
        await state.clear()
        return await message.answer(
            f"🗑 Đã xóa ảnh QR mẫu của mốc <b>{key}</b> (Chuyển về chế độ tự động).",
            reply_markup=kb([[btn("« Quay lại Kho QR Mẫu", "adm:qr_templates", style="primary")]]),
        )
    await message.answer("⚠️ Vui lòng gửi một bức ảnh QR hoặc gõ <code>xoa</code>.")
