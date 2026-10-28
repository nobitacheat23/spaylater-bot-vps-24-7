"""Luồng xử lý dành cho Khách hàng (Giai đoạn 1, Giai đoạn 2 & Ví Số Dư Rút Tiền 24/7):
- Menu chọn mệnh giá rút Ví trả sau Shopee (SPayLater) từ 200.000đ trở lên với bộ Icon 3D Shopee / Telegram Premium.
- Bảo mật tuyệt đối: Khách hàng hoàn toàn không thấy bất kỳ thông tin nào về tài khoản Garena / Napthe.vn của Admin.
- Mỗi khi có 1 giao dịch thành công -> Cộng tiền trực tiếp vào số dư tài khoản của khách (users.balance).
- Khách hàng có thể bấm '💸 Rút tiền' bất cứ lúc nào (rút toàn bộ hoặc rút số tiền tùy chọn về Ngân hàng)."""
import logging
import os
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message

import db
import napthe_engine
import services as S
import ui
from config import ADMIN_IDS, MIN_WITHDRAW_AMOUNT, NAPTHE_DENOMINATIONS, SUPPORT_USERNAME, amount_to_so, is_admin
from ui import btn, ce, esc, kb

log = logging.getLogger(__name__)
router = Router(name="user_router")


class WithdrawSG(StatesGroup):
    waiting_for_bill = State()
    updating_profile_bank = State()
    wd_custom_amount = State()
    wd_bank_input = State()


async def notify_admins_so_jumped(bot: Bot, order: dict, user: dict):
    """Chỉ gửi riêng cho Admin thông báo + ảnh chụp trạng thái Garena Sò nhảy trên Napthe.vn."""
    oid = order["order_id"]
    if order.get("so_notified_admin") == 1:
        return

    await db.execute("UPDATE orders SET so_notified_admin=1 WHERE order_id=?", (oid,))
    garena_acc = await S.get_garena_account()
    so_expected = int(order.get("so_expected") or amount_to_so(int(order["amount_face"])))
    so_before = int(order.get("so_before") or 0)
    so_after = int(order.get("so_after") or (so_before + so_expected))
    uname = f"@{esc(user['username'])}" if user.get("username") else esc(user.get("full_name") or "Khách")

    so_card_png = napthe_engine.render_napthe_so_status_card(
        garena_account=garena_acc,
        amount_face=int(order["amount_face"]),
        so_before=so_before,
        so_after=so_after,
        so_expected=so_expected,
        order_id=oid,
    )

    caption = (
        f"🐚 <b>THÔNG BÁO ADMIN: SÒ NAPTHE.VN ĐÃ NHẢY THÀNH CÔNG!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"• Mã đơn: <code>#{oid}</code>\n"
        f"• Khách hàng: {uname} (ID: <code>{user['telegram_id']}</code>)\n"
        f"• Tài khoản Garena: <code>{esc(garena_acc)}</code>\n"
        f"• Mệnh giá nạp: <b>{S.fmt_vnd(order['amount_face'])}</b>\n"
        f"• Số Sò nhảy lên: <b>+{S.fmt_so(so_expected)}</b> 🐚\n"
        f"• Số dư Garena Sò: <b>{S.fmt_so(so_before)} ➔ {S.fmt_so(so_after)}</b> (🟢 <b>ĐÃ KHỚP</b>)\n"
        f"• Số tiền cộng ví khách: <b>+{S.fmt_vnd(order['amount_received'])}</b>"
    )

    admin_group_raw = await S.get_config("admin_group_id", "")
    targets = set(ADMIN_IDS)
    if admin_group_raw and admin_group_raw.lstrip("-").isdigit():
        targets.add(int(admin_group_raw))

    for chat_id in targets:
        try:
            await ui.send_photo(
                bot,
                chat_id=chat_id,
                photo=BufferedInputFile(so_card_png, filename=f"Napthe_So_{oid}.png"),
                caption=caption,
            )
        except Exception as e:
            log.warning("Failed sending Sò jump alert to admin %s: %s", chat_id, e)


# ======================================================================
# GIAO DIỆN MENU CHÍNH (/start)
# ======================================================================
async def render_main_menu(user: dict) -> tuple[str, object]:
    fresh_u = await S.get_user(user["telegram_id"]) or user
    bal = int(fresh_u.get("balance") or 0)
    fee = await S.get_user_effective_fee(fresh_u)
    vip_badge = (
        f"\n{ce('crown', '👑')} <b>Ưu đãi VIP/Đại lý của bạn:</b> Phí chỉ <b>{fee:g}%</b>"
        if fresh_u.get("custom_fee") is not None
        else ""
    )
    text = (
        f"{ce('shopee', '🛍')} <b>DỊCH VỤ RÚT VÍ TRẢ SAU SHOPEE (SPayLater) UY TÍN 24/7</b> {ce('fire', '🔥')}\n\n"
        f"{ce('party', '🎉')} Chào mừng bạn đến với Dịch vụ Rút Ví Trả Sau Shopee (<b>SPayLater</b>) Uy Tín 24/7!\n\n"
        f"{ce('money_bag', '💰')} <b>Số dư tài khoản của bạn:</b> <b>{S.fmt_vnd(bal)}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{ce('diamond', '💎')} <b>THÔNG TIN DỊCH VỤ:</b>\n"
        f"{ce('point', '👉')} Phí dịch vụ hiện tại: <b>{fee:g}%</b>{vip_badge}\n"
        f"{ce('bolt', '⚡')} Thời gian xử lý & cộng tiền vào ví: <b>3 - 5 phút</b>\n"
        f"{ce('money_fly', '💸')} Hạn mức tối thiểu: <b>{S.fmt_vnd(MIN_WITHDRAW_AMOUNT)}</b> (Rút tiền về Ngân hàng 24/7 bất cứ lúc nào)\n\n"
        f"{ce('target', '🎯')} <b>Vui lòng chọn mệnh giá bạn muốn thực hiện bên dưới:</b>"
    )

    denoms = [d[0] for d in NAPTHE_DENOMINATIONS if d[0] >= MIN_WITHDRAW_AMOUNT]
    rows = []
    for i in range(0, len(denoms), 2):
        pair = denoms[i:i + 2]
        row = [
            btn(
                S.fmt_vnd(amt),
                f"denom:{amt}",
                icon="shopee" if amt < 1_000_000 else "spay",
                emoji="🛍",
                style="success",
            )
            for amt in pair
        ]
        rows.append(row)

    rows.append([
        btn(f"Rút tiền ({S.fmt_vnd(bal)})", "u:withdraw", icon="money_fly", emoji="💸", style="success"),
        btn("Tài khoản của tôi", "u:profile", icon="profile", emoji="👤", style="primary"),
    ])
    rows.append([
        btn("Hỗ trợ Admin 24/7", url=f"https://t.me/{SUPPORT_USERNAME}", icon="support", emoji="💬", style="primary"),
    ])
    if is_admin(fresh_u["telegram_id"]):
        rows.append([
            btn("🛠 Bảng Điều Khiển Admin (/admin)", "adm:home", icon="crown", emoji="👑", style="danger")
        ])

    return text, kb(rows)


@router.message(CommandStart())
@router.message(Command("menu"))
async def cmd_start(message: Message, state: FSMContext, user: dict):
    await state.clear()
    text, markup = await render_main_menu(user)
    await ui.answer(message, text, markup)


@router.callback_query(F.data == "u:home")
async def cb_home(cb: CallbackQuery, state: FSMContext, user: dict):
    await state.clear()
    text, markup = await render_main_menu(user)
    await ui.edit(cb, text, markup)
    await cb.answer()


# ======================================================================
# GIAI ĐOẠN 1: CHỌN MỆNH GIÁ & TẠO MÃ QR SHOPEEPAY
# ======================================================================
@router.callback_query(F.data.startswith("denom:"))
async def cb_select_denom(cb: CallbackQuery, state: FSMContext, user: dict):
    await state.clear()
    amount_face = int(cb.data.split(":")[1])
    if amount_face < MIN_WITHDRAW_AMOUNT:
        return await cb.answer(f"⚠️ Hạn mức tối thiểu là {S.fmt_vnd(MIN_WITHDRAW_AMOUNT)}!", show_alert=True)

    fee_percent = await S.get_user_effective_fee(user)
    fee_amount, amount_received = S.calc_payout(amount_face, fee_percent)

    text = (
        f"{ce('spay', '💳')} <b>XÁC NHẬN TẠO MÃ GIAO DỊCH:</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{ce('shopee', '🛍')} Mệnh giá quét: <b>{S.fmt_vnd(amount_face)}</b>\n"
        f"{ce('chart', '📊')} Phí dịch vụ: <b>{fee_percent:g}% ({S.fmt_vnd(fee_amount)})</b>\n"
        f"{ce('money_bag', '💰')} Tiền thực nhận cộng vào tài khoản: <b>+{S.fmt_vnd(amount_received)}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{ce('mega', '📣')} <i>Lưu ý: Chọn nguồn tiền là <b>Ví trả sau ShopeePay/SPayLater</b> khi quét mã. "
        f"Ngay khi giao dịch thành công, <b>+{S.fmt_vnd(amount_received)}</b> sẽ được cộng vào số dư tài khoản của bạn để rút về ngân hàng bất cứ lúc nào!</i>"
    )
    markup = kb([
        [btn("✅ Lấy mã QR thanh toán", f"confirm_qr:{amount_face}", icon="bolt", emoji="✅", style="success")],
        [btn("❌ Hủy đơn", "u:home", icon="cross", emoji="❌", style="danger")],
    ])
    await ui.edit(cb, text, markup)
    await cb.answer()


@router.callback_query(F.data.startswith("confirm_qr:"))
async def cb_confirm_qr(cb: CallbackQuery, state: FSMContext, user: dict):
    amount_face = int(cb.data.split(":")[1])
    if amount_face < MIN_WITHDRAW_AMOUNT:
        return await cb.answer(f"⚠️ Hạn mức tối thiểu là {S.fmt_vnd(MIN_WITHDRAW_AMOUNT)}!", show_alert=True)

    # Chống Spam tạo QR rác (mỗi user tối đa 1 đơn WAITING_SCAN / WAITING_BILL)
    active_ord = await S.get_active_scanning_order(user["telegram_id"])
    if active_ord:
        oid = active_ord["order_id"]
        warn_text = (
            f"{ce('mega', '📣')} <b>BẠN ĐANG CÓ ĐƠN HÀNG CHƯA HOÀN TẤT!</b>\n\n"
            f"• Mã đơn đang giữ: <code>#{oid}</code>\n"
            f"• Mệnh giá: <b>{S.fmt_vnd(active_ord['amount_face'])}</b>\n"
            f"• Hết hạn lúc: <code>{active_ord['expired_at']}</code>\n\n"
            "👉 Mỗi tài khoản chỉ được tạo tối đa 01 mã QR cùng lúc. Vui lòng gửi bill cho đơn hiện tại hoặc bấm <b>Hủy đơn cũ</b> để tạo mã mới:"
        )
        warn_kb = kb([
            [btn(f"📸 Gửi ảnh bill cho đơn #{oid}", f"send_bill:{oid}", icon="bolt", emoji="📸", style="success")],
            [btn(f"❌ Hủy đơn cũ #{oid}", f"cancel_ord:{oid}", icon="cross", emoji="❌", style="danger")],
            [btn("« Quay lại Menu", "u:home", style="primary")],
        ])
        await ui.edit(cb, warn_text, warn_kb)
        return await cb.answer("⚠️ Bạn đang có 1 mã QR chưa hoàn tất!", show_alert=True)

    await cb.answer("⏳ Đang tạo mã QR ShopeePay...")
    wait_msg = await ui.edit(
        cb,
        f"{ce('shopee', '🛍')} <b>Đang khởi tạo mã QR thanh toán ShopeePay...</b>\n"
        f"<i>Vui lòng chờ 2 - 5 giây...</i>",
    )

    fee_percent = await S.get_user_effective_fee(user)
    order = await S.create_spay_order(user["telegram_id"], amount_face, fee_percent)
    oid = order["order_id"]

    qr_path = await napthe_engine.acquire_shopeepay_qr(oid, amount_face)
    if not qr_path or not os.path.exists(qr_path):
        await db.execute("UPDATE orders SET status='EXPIRED' WHERE order_id=?", (oid,))
        err_text = (
            f"{ce('warning', '⚠️')} <b>CỔNG THANH TOÁN ĐANG ĐƯỢC ĐỒNG BỘ</b>\n\n"
            "Hệ thống đang tự động làm mới mã QR ShopeePay. Vui lòng bấm thử lại sau 30 giây hoặc liên hệ CSKH để được hỗ trợ tức thì."
        )
        if wait_msg:
            try:
                await wait_msg.edit_text(
                    err_text,
                    reply_markup=kb([
                        [btn("🔄 Thử lại ngay", f"u_amt:{amount_face}", style="primary")],
                        [btn("« Menu chính", "u:home", style="secondary")],
                    ]),
                )
            except Exception:
                await cb.message.answer(err_text)

        # Cảnh báo tức thì cho Admin
        for admin_id in ADMIN_IDS:
            try:
                await cb.bot.send_message(
                    admin_id,
                    f"🚨 <b>CẢNH BÁO: TẠO QR ĐƠN #{oid} THẤT BÀI!</b>\n"
                    f"• Khách hàng: <code>{user['telegram_id']}</code>\n"
                    f"• Mệnh giá: <b>{S.fmt_vnd(amount_face)}</b>\n\n"
                    "👉 <b>Nguyên nhân:</b> Chưa có mật khẩu Garena hoặc Session Cookie hết hạn.\n"
                    "👉 <b>Khắc phục:</b> Hãy dùng lệnh <code>/setpass [mật_khẩu]</code> hoặc <code>/setcookie [cookie]</code> để bot tự động đăng nhập ngầm.",
                )
            except Exception:
                pass
        return

    await db.execute("UPDATE orders SET shopee_qr_url=? WHERE order_id=?", (qr_path, oid))

    caption = (
        f"{ce('spay', '💳')} <b>MÃ ĐƠN: #{oid}</b>\n"
        f"{ce('fire', '🔥')} <b>Mã QR có hiệu lực trong 5 phút.</b>\n"
        f"{ce('money_bag', '💰')} Mệnh giá quét: <b>{S.fmt_vnd(amount_face)}</b>  |  Cộng vào ví: <b>+{S.fmt_vnd(order['amount_received'])}</b>\n\n"
        f"{ce('diamond', '💎')} <b>HƯỚNG DẪN QUÉT:</b>\n"
        "1. Tải ảnh QR này về thư viện ảnh.\n"
        "2. Mở App <b>Shopee</b> hoặc <b>ShopeePay</b> -> Chọn biểu tượng <b>Quét mã QR</b>.\n"
        "3. Tải ảnh vừa lưu lên -> Tại mục nguồn tiền thanh toán, chọn <b>Ví trả sau SPayLater</b>.\n"
        "4. Hoàn tất thanh toán, chụp lại màn hình giao dịch thành công.\n"
        "5. Bấm nút <b>\"📸 Gửi ảnh bill xác nhận\"</b> bên dưới để hệ thống cộng tiền vào tài khoản của bạn."
    )
    qr_markup = kb([
        [btn("📸 Gửi ảnh bill xác nhận", f"send_bill:{oid}", icon="bolt", emoji="📸", style="success")],
        [btn("❌ Hủy giao dịch", f"cancel_ord:{oid}", icon="cross", emoji="❌", style="danger")],
    ])

    try:
        if wait_msg:
            await wait_msg.delete()
    except TelegramBadRequest:
        pass

    with open(qr_path, "rb") as f:
        photo_bytes = f.read()

    sent_qr = await ui.answer_photo(
        cb.message,
        BufferedInputFile(photo_bytes, filename=f"{oid}.png"),
        caption=caption,
        markup=qr_markup,
    )
    await db.execute(
        "UPDATE orders SET qr_chat_id=?, qr_msg_id=? WHERE order_id=?",
        (sent_qr.chat.id, sent_qr.message_id, oid),
    )


@router.callback_query(F.data.startswith("cancel_ord:"))
async def cb_cancel_order(cb: CallbackQuery, state: FSMContext, user: dict):
    await state.clear()
    oid = cb.data.split(":")[1]
    cancelled = await S.cancel_order(oid)
    if not cancelled:
        return await cb.answer("Đơn hàng này không thể hủy hoặc đã xử lý.", show_alert=True)
    await cb.answer(f"✅ Đã hủy đơn #{oid}")
    text, markup = await render_main_menu(user)
    await ui.edit(cb, f"{ce('cross', '❌')} Đã hủy giao dịch <code>#{oid}</code>.\n\n" + text, markup)


# ======================================================================
# GIAI ĐOẠN 2: TIẾP NHẬN ẢNH BILL -> ĐẨY DUYỆT CỘNG TIỀN VÀO VÍ USER
# ======================================================================
@router.callback_query(F.data.startswith("send_bill:"))
async def cb_start_send_bill(cb: CallbackQuery, state: FSMContext, user: dict):
    oid = cb.data.split(":")[1]
    order = await S.get_order(oid)
    if not order or order["telegram_id"] != user["telegram_id"]:
        return await cb.answer("❌ Không tìm thấy đơn hàng.", show_alert=True)
    if order["status"] == "EXPIRED":
        return await cb.answer("⏰ Mã QR của đơn này đã hết hạn (quá 5 phút)!", show_alert=True)
    if order["status"] not in ("WAITING_SCAN", "WAITING_BILL"):
        return await cb.answer("Đơn hàng này đã được gửi duyệt hoặc đã hủy.", show_alert=True)

    await db.execute("UPDATE orders SET status='WAITING_BILL' WHERE order_id=?", (oid,))
    await state.set_state(WithdrawSG.waiting_for_bill)
    await state.update_data(order_id=oid)

    prompt = (
        f"{ce('shopee', '🛍')} <b>GỬI ẢNH BILL THANH TOÁN CHO ĐƠN #{oid}</b>\n\n"
        "👉 Vui lòng gửi <b>01 ảnh chụp màn hình</b> hiển thị thanh toán thành công từ <b>Shopee / ShopeePay</b> vào đây:"
    )
    await ui.answer(
        cb.message,
        prompt,
        kb([[btn("❌ Hủy giao dịch", f"cancel_ord:{oid}", icon="cross", emoji="❌", style="danger")]]),
    )
    await cb.answer()


@router.message(WithdrawSG.waiting_for_bill, F.photo)
async def on_bill_photo(message: Message, state: FSMContext, bot: Bot, user: dict):
    data = await state.get_data()
    oid = data.get("order_id", "")
    order = await S.get_order(oid)
    if not order or order["status"] not in ("WAITING_SCAN", "WAITING_BILL"):
        await state.clear()
        return await ui.answer(message, "❌ Đơn hàng không còn hợp lệ hoặc đã hết hạn.")

    file_id = message.photo[-1].file_id
    await db.execute(
        "UPDATE orders SET bill_image_file_id=?, status='PENDING_ADMIN_REVIEW' WHERE order_id=?",
        (file_id, oid),
    )
    await state.clear()

    # Ngầm Check Sò trên Napthe.vn và gửi báo cáo riêng về cho Admin
    live_so, _ = await napthe_engine.fetch_live_napthe_so()
    order, _ = await S.verify_and_jump_order_so(oid, live_so=live_so)
    await notify_admins_so_jumped(bot, order, user)
    garena_acc = await S.get_garena_account()

    # 1. Phản hồi cho khách hàng
    reply_text = (
        f"{ce('bolt', '⚡')} <b>ĐÃ TIẾP NHẬN BẰNG CHỨNG GIAO DỊCH #{oid}!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{ce('shopee', '🛍')} Mệnh giá quét: <b>{S.fmt_vnd(order['amount_face'])}</b>\n"
        f"{ce('money_bag', '💰')} Số tiền sẽ cộng vào tài khoản: <b>+{S.fmt_vnd(order['amount_received'])}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        "Hệ thống đang đối soát giao dịch. Ngay khi hoàn tất (1 - 3 phút), số tiền sẽ được <b>cộng trực tiếp vào số dư tài khoản của bạn</b> để bạn rút về Ngân hàng bất cứ lúc nào!"
    )
    await ui.answer(
        message,
        reply_text,
        kb([
            [btn("👤 Kiểm tra Số dư tài khoản", "u:profile", icon="profile", emoji="👤", style="primary")],
            [btn("🏠 Về Menu Chính", "u:home", icon="shopee", emoji="🏠", style="success")],
        ]),
    )

    # 2. Đẩy thông báo duyệt đơn & cộng tiền vào ví khách về cho Admin
    uname = f"@{esc(user['username'])}" if user.get("username") else esc(user.get("full_name") or "Khách")
    admin_caption = (
        f"🚨 <b>YÊU CẦU DUYỆT BILL & CỘNG TIỀN VÀO VÍ KHÁCH!</b>\n"
        f"-------------------------------\n"
        f"• Mã đơn: <code>#{oid}</code>\n"
        f"• Khách hàng: {uname} (ID: <code>{user['telegram_id']}</code>)\n"
        f"• Mệnh giá quét: <b>{S.fmt_vnd(order['amount_face'])}</b> (Phí {order['fee_percent']:g}%)\n"
        f"• Tài khoản Garena: <code>{esc(garena_acc)}</code>\n"
        f"• Check Sò Napthe.vn: 🟢 <b>+{S.fmt_so(order['so_expected'])} ({S.fmt_so(order['so_before'])} ➔ {S.fmt_so(order['so_after'])})</b>\n"
        f"• Số tiền cộng vào Ví khách: <b>+{S.fmt_vnd(order['amount_received'])}</b>\n"
        f"• Thời gian tạo: <code>{order['created_at']}</code>"
    )
    admin_kb = kb([
        [btn("🐚 Check Sò Napthe.vn", f"adm_check_so:{oid}", icon="garena", emoji="🐚", style="primary")],
        [
            btn(
                f"✅ Duyệt & Cộng +{S.fmt_vnd(order['amount_received'])} vào Ví",
                f"adm_paid:{oid}",
                icon="up_green",
                emoji="✅",
                style="success",
            )
        ],
        [btn("❌ Từ chối đơn / Bill sai", f"adm_rej:{oid}", icon="cross", emoji="❌", style="danger")],
    ])

    admin_group_raw = await S.get_config("admin_group_id", "")
    targets = set(ADMIN_IDS)
    if admin_group_raw and admin_group_raw.lstrip("-").isdigit():
        targets.add(int(admin_group_raw))

    for chat_id in targets:
        try:
            await ui.send_photo(
                bot,
                chat_id=chat_id,
                photo=order["bill_image_file_id"],
                caption=admin_caption,
                markup=admin_kb,
            )
        except Exception as e:
            log.warning("Failed sending bill to admin %s: %s", chat_id, e)


@router.message(WithdrawSG.waiting_for_bill)
async def on_bill_invalid(message: Message):
    await ui.answer(
        message,
        f"{ce('mega', '📣')} Vui lòng gửi <b>hình ảnh (Photo)</b> chụp màn hình giao dịch thành công từ Shopee/ShopeePay!",
    )


# ======================================================================
# CHỨC NĂNG RÚT TIỀN TỪ VÍ SỐ DƯ VỀ NGÂN HÀNG BẤT CỨ LÚC NÀO (/rut | u:withdraw)
# ======================================================================
async def _render_withdraw_menu(tg_id: int) -> tuple[str, object]:
    u = await S.get_user(tg_id)
    bal = int(u.get("balance") or 0) if u else 0
    bank_str = (
        f"{u['bank_name']} - {u['bank_account_number']} - {u['bank_account_name']}"
        if u and u.get("bank_account_number")
        else "(Chưa lưu STK mặc định)"
    )

    if bal <= 0:
        text = (
            f"{ce('money_fly', '💸')} <b>RÚT TIỀN VỀ TÀI KHOẢN NGÂN HÀNG 24/7</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"• Số dư khả dụng hiện tại: <b>0 đ</b>\n"
            f"• Ngân hàng nhận tiền: <code>{esc(bank_str)}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💡 <i>Mỗi khi bạn thực hiện thành công 1 giao dịch quét mã SPayLater, tiền sẽ tự động cộng vào số dư tài khoản và bạn có thể rút về Ngân hàng tại đây bất cứ lúc nào!</i>"
        )
        markup = kb([
            [btn("🛍 Thực hiện giao dịch ngay", "u:home", icon="shopee", emoji="🛍", style="success")],
            [btn("🏦 Cài đặt STK Ngân hàng nhận tiền", "u:edit_bank", icon="point", emoji="🏦", style="primary")],
        ])
        return text, markup

    text = (
        f"{ce('money_fly', '💸')} <b>RÚT TIỀN VỀ TÀI KHOẢN NGÂN HÀNG 24/7</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{ce('money_bag', '💰')} Số dư khả dụng của bạn: <b>{S.fmt_vnd(bal)}</b>\n"
        f"🏦 STK nhận tiền mặc định: <code>{esc(bank_str)}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👇 <b>Vui lòng chọn số tiền bạn muốn rút về Ngân hàng:</b>"
    )
    markup = kb([
        [btn(f"💯 Rút toàn bộ ({S.fmt_vnd(bal)})", f"wd_amt:{bal}", icon="bolt", emoji="💯", style="success")],
        [btn("✍️ Nhập số tiền muốn rút khác", "wd_custom", icon="point", emoji="✍️", style="primary")],
        [btn("🏦 Đổi STK Ngân hàng mặc định", "u:edit_bank", icon="profile", emoji="🏦", style="primary")],
        [btn("« Quay lại Menu chính", "u:home", icon="shopee", emoji="🏠", style="danger")],
    ])
    return text, markup


@router.message(Command("rut", "withdraw"))
async def cmd_withdraw(message: Message, state: FSMContext, user: dict):
    await state.clear()
    text, markup = await _render_withdraw_menu(user["telegram_id"])
    await ui.answer(message, text, markup)


@router.callback_query(F.data == "u:withdraw")
async def cb_withdraw_menu(cb: CallbackQuery, state: FSMContext, user: dict):
    await state.clear()
    text, markup = await _render_withdraw_menu(user["telegram_id"])
    await ui.edit(cb, text, markup)
    await cb.answer()


@router.callback_query(F.data == "wd_custom")
async def cb_wd_custom(cb: CallbackQuery, state: FSMContext, user: dict):
    u = await S.get_user(user["telegram_id"])
    bal = int(u.get("balance") or 0) if u else 0
    if bal <= 0:
        return await cb.answer("⚠️ Số dư của bạn hiện là 0 đ!", show_alert=True)

    await state.set_state(WithdrawSG.wd_custom_amount)
    await ui.edit(
        cb,
        f"✍️ <b>NHẬP SỐ TIỀN BẠN MUỐN RÚT</b>\n\n"
        f"• Số dư khả dụng: <b>{S.fmt_vnd(bal)}</b>\n"
        f"👉 Vui lòng nhập số tiền muốn rút (VD: <code>{bal}</code>):",
        kb([[btn("« Quay lại", "u:withdraw", style="danger")]]),
    )
    await cb.answer()


@router.message(WithdrawSG.wd_custom_amount, F.text)
async def on_wd_custom_amount(message: Message, state: FSMContext, user: dict):
    u = await S.get_user(user["telegram_id"])
    bal = int(u.get("balance") or 0) if u else 0
    raw = message.text.strip().replace(".", "").replace(",", "").replace("đ", "").replace("d", "").strip()
    if not raw.isdigit() or int(raw) <= 0:
        return await ui.answer(message, "❌ Vui lòng nhập số tiền hợp lệ (VD: <code>200000</code>).")

    amt = int(raw)
    if amt > bal:
        return await ui.answer(
            message,
            f"❌ Số tiền bạn nhập (<b>{S.fmt_vnd(amt)}</b>) vượt quá số dư hiện có (<b>{S.fmt_vnd(bal)}</b>)!",
        )

    await state.clear()
    await _prompt_bank_for_withdrawal(message, u, amt, is_edit=False)


@router.callback_query(F.data.startswith("wd_amt:"))
async def cb_wd_amount_selected(cb: CallbackQuery, state: FSMContext, user: dict):
    await state.clear()
    amt = int(cb.data.split(":")[1])
    u = await S.get_user(user["telegram_id"])
    bal = int(u.get("balance") or 0) if u else 0
    if amt <= 0 or amt > bal:
        return await cb.answer("⚠️ Số dư không đủ để thực hiện lệnh rút này!", show_alert=True)

    await _prompt_bank_for_withdrawal(cb, u, amt, is_edit=True)
    await cb.answer()


async def _prompt_bank_for_withdrawal(target, u: dict, amt: int, is_edit: bool = False):
    if u.get("bank_name") and u.get("bank_account_number") and u.get("bank_account_name"):
        saved_str = f"{u['bank_name']} - {u['bank_account_number']} - {u['bank_account_name']}"
        text = (
            f"{ce('money_fly', '💸')} <b>XÁC NHẬN RÚT TIỀN VỀ NGÂN HÀNG</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"• Số tiền rút: <b>{S.fmt_vnd(amt)}</b>\n"
            f"• Ngân hàng nhận: <code>{esc(saved_str)}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            "👇 Bạn muốn nhận tiền về STK này hay nhập STK khác?"
        )
        markup = kb([
            [btn("✅ Xác nhận rút về STK này", f"wd_use_bank:{amt}", icon="bolt", emoji="✅", style="success")],
            [btn("🔄 Nhập STK Ngân hàng khác", f"wd_new_bank:{amt}", icon="point", emoji="🔄", style="primary")],
            [btn("❌ Hủy", "u:withdraw", icon="cross", emoji="❌", style="danger")],
        ])
        if is_edit:
            await ui.edit(target, text, markup)
        else:
            await ui.answer(target, text, markup)
        return

    # Nếu chưa lưu STK thì chuyển sang nhập STK
    msg_obj = target.message if isinstance(target, CallbackQuery) else target
    await ui.answer(
        msg_obj,
        f"🏦 <b>NHẬP TÀI KHOẢN NGÂN HÀNG NHẬN TIỀN ({S.fmt_vnd(amt)})</b>\n\n"
        "Vui lòng点击 nút bên dưới để nhập thông tin STK:",
        kb([[btn("✍️ Nhập STK Ngân hàng", f"wd_new_bank:{amt}", icon="point", emoji="✍️", style="success")]]),
    )


@router.callback_query(F.data.startswith("wd_new_bank:"))
async def cb_wd_new_bank(cb: CallbackQuery, state: FSMContext):
    amt = int(cb.data.split(":")[1])
    await state.set_state(WithdrawSG.wd_bank_input)
    await state.update_data(wd_amount=amt)
    await ui.answer(
        cb.message,
        f"🏦 <b>NHẬP STK NHẬN TIỀN RÚT ({S.fmt_vnd(amt)})</b>\n\n"
        "Vui lòng nhập theo mẫu:\n"
        "<code>[Tên Ngân Hàng] - [Số Tài Khoản] - [Tên Chủ Tài Khoản]</code>\n\n"
        "📌 <b>Ví dụ:</b>\n"
        "<code>MB - 0123456789 - NGUYEN VAN A</code>",
        kb([[btn("❌ Hủy", "u:withdraw", style="danger")]]),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("wd_use_bank:"))
async def cb_wd_use_saved_bank(cb: CallbackQuery, state: FSMContext, bot: Bot, user: dict):
    amt = int(cb.data.split(":")[1])
    u = await S.get_user(user["telegram_id"])
    if not u or not u.get("bank_account_number"):
        return await cb.answer("⚠️ Bạn chưa có STK lưu sẵn!", show_alert=True)

    bank_code = u["bank_name"]
    acc_num = u["bank_account_number"]
    holder = u["bank_account_name"]
    display = f"{bank_code} - {acc_num} - {holder}"

    await state.clear()
    ok = await _execute_user_withdrawal(cb.message, bot, u, amt, bank_code, acc_num, holder, display)
    if ok:
        await cb.answer("✅ Đã gửi lệnh rút tiền thành công!")
    else:
        await cb.answer("❌ Số dư không đủ!", show_alert=True)


@router.message(WithdrawSG.wd_bank_input, F.text)
async def on_wd_bank_text(message: Message, state: FSMContext, bot: Bot, user: dict):
    data = await state.get_data()
    amt = int(data.get("wd_amount") or 0)
    bank_code, acc_num, holder, display = S.parse_bank_info_text(message.text)
    if not bank_code or not acc_num or not holder:
        return await ui.answer(
            message,
            f"{ce('cross', '❌')} <b>Định dạng chưa đúng!</b>\n"
            "Vui lòng nhập đầy đủ 3 thông tin cách nhau bởi dấu gạch ngang <code>-</code>:\n"
            "<code>[Tên Ngân Hàng] - [Số Tài Khoản] - [Tên Chủ Tài Khoản]</code>\n\n"
            "Ví dụ: <code>MB - 0123456789 - NGUYEN VAN A</code>",
        )

    await S.save_user_bank(user["telegram_id"], bank_code, acc_num, holder)
    await state.clear()
    u = await S.get_user(user["telegram_id"])
    await _execute_user_withdrawal(message, bot, u, amt, bank_code, acc_num, holder, display)


async def _execute_user_withdrawal(
    target_msg: Message,
    bot: Bot,
    user: dict,
    amt: int,
    bank_code: str,
    acc_num: str,
    holder: str,
    display: str,
) -> bool:
    wd = await S.create_withdrawal_request(user["telegram_id"], amt, bank_code, acc_num, holder, display)
    if not wd:
        await ui.answer(target_msg, "❌ Số dư tài khoản của bạn không đủ để thực hiện lệnh rút này.")
        return False

    fresh_u = await S.get_user(user["telegram_id"])
    new_bal = int(fresh_u.get("balance") or 0) if fresh_u else 0
    wd_id = wd["wd_id"]

    # 1. Thông báo cho khách hàng
    reply_text = (
        f"{ce('money_fly', '💸')} <b>TẠO LỆNH RÚT TIỀN #{wd_id} THÀNH CÔNG!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"• Số tiền rút: <b>{S.fmt_vnd(amt)}</b>\n"
        f"• Ngân hàng nhận: <code>{esc(display)}</code>\n"
        f"• Số dư ví còn lại: <b>{S.fmt_vnd(new_bal)}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        "Hệ thống đang chuyển khoản về Ngân hàng của bạn trong <b>3 - 5 phút</b>. Vui lòng giữ thông báo từ bot!"
    )
    await ui.answer(
        target_msg,
        reply_text,
        kb([
            [btn("👤 Tài khoản của tôi", "u:profile", icon="profile", emoji="👤", style="primary")],
            [btn("🏠 Về Menu Chính", "u:home", icon="shopee", emoji="🏠", style="success")],
        ]),
    )

    # 2. Đẩy thông báo Lệnh Rút Tiền Mới về Admin kèm nút Xuất VietQR 1 chạm
    uname = f"@{esc(user['username'])}" if user.get("username") else esc(user.get("full_name") or "Khách")
    admin_text = (
        f"💸 <b>YÊU CẦU RÚT TIỀN TỪ VÍ SỐ DƯ MỚI!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"• Mã lệnh rút: <code>#{wd_id}</code>\n"
        f"• Khách hàng: {uname} (ID: <code>{user['telegram_id']}</code>)\n"
        f"• Số tiền cần chuyển: <b>{S.fmt_vnd(amt)}</b>\n"
        f"• Tài khoản nhận: <code>{esc(display)}</code>\n"
        f"• Số dư ví còn lại của khách: <b>{S.fmt_vnd(new_bal)}</b>\n"
        f"• Thời gian tạo: <code>{wd['created_at']}</code>"
    )
    admin_kb = kb([
        [btn("⚡ Xuất mã VietQR Chuyển Tiền", f"adm_wd_qr:{wd_id}", icon="bolt", emoji="⚡", style="primary")],
        [btn("✅ Xác nhận ĐÃ CHUYỂN TIỀN", f"adm_wd_paid:{wd_id}", icon="up_green", emoji="✅", style="success")],
        [btn("❌ Từ chối & Hoàn tiền lại Ví", f"adm_wd_rej:{wd_id}", icon="cross", emoji="❌", style="danger")],
    ])

    admin_group_raw = await S.get_config("admin_group_id", "")
    targets = set(ADMIN_IDS)
    if admin_group_raw and admin_group_raw.lstrip("-").isdigit():
        targets.add(int(admin_group_raw))

    for chat_id in targets:
        try:
            await ui.send(bot, chat_id, admin_text, admin_kb)
        except Exception as e:
            log.warning("Failed sending withdrawal alert to admin %s: %s", chat_id, e)

    return True


# ======================================================================
# TÀI KHOẢN CỦA TÔI (SỐ DƯ VÍ, HỒ SƠ & LỊCH SỬ GIAO DỊCH / RÚT TIỀN)
# ======================================================================
@router.callback_query(F.data == "u:profile")
async def cb_profile(cb: CallbackQuery, state: FSMContext, user: dict):
    await state.clear()
    u = await S.get_user(user["telegram_id"])
    bal = int(u.get("balance") or 0)
    fee = await S.get_user_effective_fee(u)
    role_str = f"👑 VIP / Đại lý (Phí ưu đãi {fee:g}%)" if u.get("custom_fee") is not None else f"Thành viên (Phí {fee:g}%)"
    bank_str = (
        f"{u['bank_name']} - {u['bank_account_number']} - {u['bank_account_name']}"
        if u.get("bank_account_number")
        else "(Chưa lưu STK mặc định)"
    )

    recent_orders = await db.fetchall(
        "SELECT * FROM orders WHERE telegram_id=? ORDER BY created_at DESC LIMIT 5",
        (u["telegram_id"],),
    )
    recent_wds = await db.fetchall(
        "SELECT * FROM withdrawals WHERE telegram_id=? ORDER BY created_at DESC LIMIT 5",
        (u["telegram_id"],),
    )

    status_icons = {
        "PAID": "🟢 Thành công (+Ví)",
        "PENDING_ADMIN_REVIEW": "⏳ Chờ duyệt",
        "WAITING_SCAN": "🕒 Chờ quét QR",
        "WAITING_BILL": "📸 Chờ gửi bill",
        "REJECTED": "🔴 Từ chối",
        "EXPIRED": "⚪ Hết hạn",
        "CANCELLED": "⚪ Đã hủy",
    }
    ord_lines = [
        f"• <code>#{o['order_id']}</code> | Quét {S.fmt_vnd(o['amount_face'])} -> <b>+{S.fmt_vnd(o['amount_received'])}</b> | {status_icons.get(o['status'], o['status'])}"
        for o in recent_orders
    ]

    wd_icons = {
        "PENDING": "⏳ Đang chuyển khoản",
        "PAID": "🟢 Đã nhận tiền",
        "REJECTED": "🔴 Hoàn lại Ví",
    }
    wd_lines = [
        f"• <code>#{w['wd_id']}</code> | Rút <b>{S.fmt_vnd(w['amount'])}</b> ({esc(w['bank_code'])}) | {wd_icons.get(w['status'], w['status'])}"
        for w in recent_wds
    ]

    text = (
        f"{ce('profile', '👤')} <b>THÔNG TIN TÀI KHOẢN CỦA BẠN</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"• ID Telegram: <code>{u['telegram_id']}</code>\n"
        f"• Username: @{esc(u['username'] or 'chua_co')}\n"
        f"• Cấp bậc: <b>{role_str}</b>\n"
        f"• 💰 <b>Số dư khả dụng hiện tại: {S.fmt_vnd(bal)}</b>\n"
        f"• Tổng đơn thành công: <b>{u['total_orders']} đơn</b>\n"
        f"• Tổng tiền đã rút về NH: <b>{S.fmt_vnd(u['total_withdrawn'])}</b>\n"
        f"• Ngân hàng mặc định: <code>{esc(bank_str)}</code>\n\n"
        f"{ce('chart', '📊')} <b>GIAO DỊCH CỘNG VÍ GẦN ĐÂY:</b>\n"
        + ("\n".join(ord_lines) if ord_lines else "<i>Chưa có giao dịch nào.</i>")
        + "\n\n"
        f"{ce('money_fly', '💸')} <b>LỊCH SỬ RÚT TIỀN VỀ NGÂN HÀNG:</b>\n"
        + ("\n".join(wd_lines) if wd_lines else "<i>Chưa có lệnh rút tiền nào.</i>")
    )
    markup = kb([
        [btn(f"💸 Rút tiền về Ngân hàng ({S.fmt_vnd(bal)})", "u:withdraw", icon="money_fly", emoji="💸", style="success")],
        [btn("🏦 Cập nhật STK nhận tiền mặc định", "u:edit_bank", icon="point", emoji="🏦", style="primary")],
        [btn("« Quay lại Menu chính", "u:home", icon="shopee", emoji="🏠", style="primary")],
    ])
    await ui.edit(cb, text, markup)
    await cb.answer()


@router.callback_query(F.data == "u:edit_bank")
async def cb_edit_profile_bank(cb: CallbackQuery, state: FSMContext):
    await state.set_state(WithdrawSG.updating_profile_bank)
    await ui.answer(
        cb.message,
        "🏦 <b>CẬP NHẬT TÀI KHOẢN NGÂN HÀNG MẶC ĐỊNH</b>\n\n"
        "Vui lòng gửi theo mẫu:\n"
        "<code>[Tên Ngân Hàng] - [Số Tài Khoản] - [Tên Chủ Tài Khoản]</code>\n\n"
        "Ví dụ: <code>MB - 0123456789 - NGUYEN VAN A</code>",
        kb([[btn("❌ Hủy", "u:profile", style="danger")]]),
    )
    await cb.answer()


@router.message(WithdrawSG.updating_profile_bank, F.text)
async def on_update_profile_bank(message: Message, state: FSMContext, user: dict):
    bank_code, acc_num, holder, display = S.parse_bank_info_text(message.text)
    if not bank_code or not acc_num or not holder:
        return await ui.answer(
            message,
            "❌ Sai định dạng! Vui lòng gửi: <code>[Tên Ngân Hàng] - [Số Tài Khoản] - [Tên Chủ Tài Khoản]</code>\n"
            "Ví dụ: <code>MB - 0123456789 - NGUYEN VAN A</code>",
        )
    await S.save_user_bank(user["telegram_id"], bank_code, acc_num, holder)
    await state.clear()
    await ui.answer(
        message,
        f"✅ Đã lưu STK mặc định: <code>{esc(display)}</code>",
        kb([
            [btn("💸 Rút tiền ngay", "u:withdraw", icon="money_fly", emoji="💸", style="success")],
            [btn("👤 Xem tài khoản của tôi", "u:profile", style="primary")],
        ]),
    )
