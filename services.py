"""Business Logic, Ví Số Dư Người Dùng (User Wallet), Napthe.vn Sò Tracker & Banking Engine:
- Tính phí chiết khấu SPayLater & Quy đổi Sò Garena tương ứng (500 VNĐ = 1 Sò).
- Tự động cộng tiền vào Ví tài khoản người dùng (users.balance) mỗi khi giao dịch thành công.
- Cho phép người dùng tạo lệnh Rút tiền từ Ví về Ngân hàng bất cứ lúc nào (withdrawals).
- Tạo mã VietQR chuyển tiền giải ngân 1 chạm cho Admin & Thống kê doanh thu."""
import base64
import io
import logging
import os
import re
import secrets
from urllib.parse import quote

import aiohttp
from PIL import Image, ImageDraw

import db
from config import DEFAULT_FEE_PERCENT, GARENA_ACCOUNT, QR_EXPIRE_SECONDS, amount_to_so

log = logging.getLogger(__name__)

# Bảng tra cứu tên ngân hàng -> Mã chuẩn VietQR Napas247
BANK_ALIASES = {
    "MB": "MB", "MBBANK": "MB", "QUANDOI": "MB",
    "VCB": "VCB", "VIETCOMBANK": "VCB",
    "TCB": "TCB", "TECHCOMBANK": "TCB",
    "ACB": "ACB", "ACBBANK": "ACB",
    "BIDV": "BIDV",
    "ICB": "ICB", "VIETINBANK": "ICB", "CTG": "ICB",
    "VPB": "VPB", "VPBANK": "VPB",
    "TPB": "TPB", "TPBANK": "TPB",
    "MSB": "MSB", "MARITIMEBANK": "MSB",
    "STB": "STB", "SACOMBANK": "STB",
    "VIB": "VIB",
    "OCB": "OCB",
    "SHB": "SHB",
    "EIB": "EIB", "EXIMBANK": "EIB",
    "HDB": "HDB", "HDBANK": "HDB",
    "LPB": "LPB", "LPBANK": "LPB", "LIENVIET": "LPB",
    "SEAB": "SEAB", "SEABANK": "SEAB",
    "CAKE": "CAKE",
    "TIMO": "TIMO",
    "AGRIBANK": "VBA", "VBA": "VBA",
}


def fmt_vnd(amount: int | float) -> str:
    return f"{int(round(amount)):,}".replace(",", ".") + " đ"


def fmt_so(so_amount: int | float) -> str:
    return f"{int(round(so_amount)):,}".replace(",", ".") + " Sò"


# ======================================================================
# SYSTEM CONFIG & QUẢN LÝ SỐ DƯ SÒ NAPTHE.VN
# ======================================================================
async def get_config(key: str, default: str = "") -> str:
    val = await db.fetchval("SELECT value FROM system_config WHERE key=?", (key,))
    return val if val is not None else default


async def set_config(key: str, value: str):
    await db.execute(
        "INSERT INTO system_config(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


async def get_default_fee() -> float:
    raw = await get_config("default_fee_percent", str(DEFAULT_FEE_PERCENT))
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_FEE_PERCENT


async def get_garena_account() -> str:
    acc = await get_config("garena_account", GARENA_ACCOUNT)
    return acc or GARENA_ACCOUNT


async def get_current_so_balance() -> int:
    raw = await get_config("current_so_balance", "0")
    try:
        return int(float(raw))
    except ValueError:
        return 0


async def set_current_so_balance(so_val: int):
    await set_config("current_so_balance", str(max(0, int(so_val))))


async def is_maintenance() -> bool:
    return (await get_config("is_maintenance", "0")) == "1"


# ======================================================================
# USERS & WALLET BALANCE
# ======================================================================
async def ensure_user(tg_id: int, username: str = "", full_name: str = "") -> dict:
    u = await db.fetchone("SELECT * FROM users WHERE telegram_id=?", (tg_id,))
    if not u:
        await db.execute(
            "INSERT INTO users(telegram_id, username, full_name, balance) VALUES (?, ?, ?, 0)",
            (tg_id, username or "", full_name or ""),
        )
        u = await db.fetchone("SELECT * FROM users WHERE telegram_id=?", (tg_id,))
    else:
        if (username and u["username"] != username) or (full_name and u["full_name"] != full_name):
            await db.execute(
                "UPDATE users SET username=?, full_name=? WHERE telegram_id=?",
                (username or u["username"], full_name or u["full_name"], tg_id),
            )
            u["username"] = username or u["username"]
            u["full_name"] = full_name or u["full_name"]
    return u


async def get_user(tg_id: int) -> dict | None:
    return await db.fetchone("SELECT * FROM users WHERE telegram_id=?", (tg_id,))


async def get_user_effective_fee(user: dict) -> float:
    if user and user.get("custom_fee") is not None:
        return float(user["custom_fee"])
    return await get_default_fee()


async def adjust_user_balance(tg_id: int, delta: int) -> int:
    """Cộng hoặc trừ số dư ví của user (ACID transaction). Trả về số dư mới."""
    async with db.tx() as conn:
        await conn.execute(
            "UPDATE users SET balance = MAX(0, COALESCE(balance, 0) + ?) WHERE telegram_id=?",
            (int(delta), tg_id),
        )
        async with conn.execute("SELECT balance FROM users WHERE telegram_id=?", (tg_id,)) as cur:
            row = await cur.fetchone()
            return int(row[0] or 0) if row else 0


def calc_payout(amount_face: int, fee_percent: float) -> tuple[int, int]:
    """Trả về (tiền_phí, tiền_khách_thực_nhận)."""
    fee_amount = int(round(amount_face * (fee_percent / 100.0)))
    received = max(0, amount_face - fee_amount)
    return fee_amount, received


def parse_bank_info_text(raw: str) -> tuple[str, str, str, str]:
    """Phân tách chuỗi '[Tên Ngân Hàng] - [Số Tài Khoản] - [Tên Chủ Tài Khoản]'.
    Trả về (bank_code, account_number, account_holder, formatted_display)."""
    cleaned = raw.strip()
    parts = [p.strip() for p in re.split(r"[-|/\n]+", cleaned) if p.strip()]
    if len(parts) >= 3:
        raw_bank, raw_acc, raw_name = parts[0], parts[1], " ".join(parts[2:])
    else:
        m = re.match(r"^([A-Za-zÀ-ỹ\s]+?)\s+(\d{6,20})\s+(.+)$", cleaned)
        if m:
            raw_bank, raw_acc, raw_name = m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
        else:
            return "", "", "", cleaned

    key = re.sub(r"[^A-Z0-9]", "", raw_bank.upper())
    bank_code = BANK_ALIASES.get(key, raw_bank.upper().strip().replace(" ", ""))
    acc_num = re.sub(r"\s+", "", raw_acc)
    holder = raw_name.upper().strip()
    display = f"{bank_code} - {acc_num} - {holder}"
    return bank_code, acc_num, holder, display


async def save_user_bank(tg_id: int, bank_code: str, acc_num: str, holder: str):
    await db.execute(
        "UPDATE users SET bank_name=?, bank_account_number=?, bank_account_name=? WHERE telegram_id=?",
        (bank_code, acc_num, holder, tg_id),
    )


# ======================================================================
# ORDERS (GIAO DỊCH QUÉT QR SPAYLATER -> CỘNG TIỀN VÀO VÍ USER)
# ======================================================================
async def get_active_scanning_order(tg_id: int) -> dict | None:
    """Kiểm tra xem user có đang giữ đơn WAITING_SCAN / WAITING_BILL chưa hết hạn không (Chống spam QR)."""
    return await db.fetchone(
        "SELECT * FROM orders WHERE telegram_id=? AND status IN ('WAITING_SCAN', 'WAITING_BILL') "
        "AND expired_at > datetime('now', 'localtime') ORDER BY created_at DESC LIMIT 1",
        (tg_id,),
    )


async def create_spay_order(tg_id: int, amount_face: int, fee_percent: float) -> dict:
    _, amount_received = calc_payout(amount_face, fee_percent)
    so_expected = amount_to_so(amount_face)
    so_before = await get_current_so_balance()

    async with db.tx() as conn:
        for _ in range(10):
            oid = "SPAY_" + secrets.token_hex(3).upper()
            async with conn.execute("SELECT 1 FROM orders WHERE order_id=?", (oid,)) as c:
                if not await c.fetchone():
                    break
        await conn.execute(
            "INSERT INTO orders(order_id, telegram_id, amount_face, fee_percent, amount_received, "
            "so_expected, so_before, so_after, so_verified, status, expired_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 'WAITING_SCAN', datetime('now', 'localtime', ?))",
            (oid, tg_id, amount_face, fee_percent, amount_received, so_expected, so_before, so_before, f"+{QR_EXPIRE_SECONDS} seconds"),
        )
    return await get_order(oid)


async def get_order(order_id: str) -> dict | None:
    clean_id = order_id.strip().lstrip("#").upper()
    return await db.fetchone("SELECT * FROM orders WHERE order_id=?", (clean_id,))


async def verify_and_jump_order_so(order_id: str, live_so: int | None = None) -> tuple[dict | None, bool]:
    """Xác nhận đơn nạp Sò thành công trên Napthe.vn:
    - Cộng số Sò tương ứng với mệnh giá nạp vào số dư Sò hiện tại.
    - Trả về (order_dict, newly_jumped: bool)."""
    clean_id = order_id.strip().lstrip("#").upper()
    async with db.tx() as conn:
        async with conn.execute("SELECT * FROM orders WHERE order_id=?", (clean_id,)) as cur:
            row = await cur.fetchone()
            if not row:
                return None, False
            o = dict(row)

        if o.get("so_verified") == 1:
            return o, False

        so_expected = int(o.get("so_expected") or amount_to_so(int(o["amount_face"])))
        async with conn.execute("SELECT value FROM system_config WHERE key='current_so_balance'") as c2:
            r2 = await c2.fetchone()
            cur_so = int(float(r2[0])) if r2 and r2[0] else 0

        so_before = cur_so
        so_after = live_so if (live_so is not None and live_so >= so_before + so_expected) else (so_before + so_expected)

        await conn.execute(
            "UPDATE orders SET so_expected=?, so_before=?, so_after=?, so_verified=1 WHERE order_id=?",
            (so_expected, so_before, so_after, clean_id),
        )
        await conn.execute(
            "INSERT INTO system_config(key, value) VALUES ('current_so_balance', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(so_after),),
        )

    return await get_order(clean_id), True


async def cancel_order(order_id: str) -> dict | None:
    o = await get_order(order_id)
    if not o or o["status"] not in ("WAITING_SCAN", "WAITING_BILL"):
        return None
    await db.execute("UPDATE orders SET status='CANCELLED' WHERE order_id=?", (o["order_id"],))
    if o.get("shopee_qr_url") and os.path.exists(o["shopee_qr_url"]):
        try:
            os.remove(o["shopee_qr_url"])
        except OSError:
            pass
    return await get_order(o["order_id"])


async def mark_order_paid(order_id: str) -> dict | None:
    """Khi 1 giao dịch SPayLater thành công:
    - Chuyển trạng thái đơn sang PAID.
    - Cộng số tiền thực nhận (amount_received) trực tiếp vào số dư Ví của user (users.balance)
      để user có thể rút về ngân hàng bất cứ lúc nào!"""
    clean_id = order_id.strip().lstrip("#").upper()
    await verify_and_jump_order_so(clean_id)
    async with db.tx() as conn:
        async with conn.execute("SELECT * FROM orders WHERE order_id=?", (clean_id,)) as cur:
            row = await cur.fetchone()
            if not row:
                return None
            o = dict(row)
        if o["status"] == "PAID":
            return None
        await conn.execute("UPDATE orders SET status='PAID' WHERE order_id=?", (clean_id,))
        await conn.execute(
            "UPDATE users SET balance = COALESCE(balance, 0) + ?, total_orders = total_orders + 1 "
            "WHERE telegram_id=?",
            (o["amount_received"], o["telegram_id"]),
        )
    return await get_order(clean_id)


async def mark_order_rejected(order_id: str, reason: str) -> dict | None:
    clean_id = order_id.strip().lstrip("#").upper()
    o = await get_order(clean_id)
    if not o or o["status"] in ("PAID", "REJECTED"):
        return None
    if o.get("so_verified") == 1:
        cur_so = await get_current_so_balance()
        so_exp = int(o.get("so_expected") or amount_to_so(int(o["amount_face"])))
        await set_current_so_balance(max(0, cur_so - so_exp))
    await db.execute(
        "UPDATE orders SET status='REJECTED', reject_reason=?, so_verified=0 WHERE order_id=?",
        (reason, clean_id),
    )
    return await get_order(clean_id)


async def expire_overdue_orders() -> list[dict]:
    """Tìm các đơn WAITING_SCAN / WAITING_BILL đã quá 5 phút, chuyển sang EXPIRED và xóa ảnh QR."""
    rows = await db.fetchall(
        "SELECT * FROM orders WHERE status IN ('WAITING_SCAN', 'WAITING_BILL') "
        "AND expired_at <= datetime('now', 'localtime')"
    )
    expired = []
    for o in rows:
        await db.execute("UPDATE orders SET status='EXPIRED' WHERE order_id=?", (o["order_id"],))
        qr_path = o.get("shopee_qr_url") or ""
        if qr_path and os.path.exists(qr_path):
            try:
                os.remove(qr_path)
            except OSError:
                pass
        expired.append(o)
    return expired


# ======================================================================
# WITHDRAWALS (RÚT TIỀN TỪ VÍ SỐ DƯ USER VỀ NGÂN HÀNG BẤT CỨ LÚC NÀO)
# ======================================================================
async def create_withdrawal_request(
    tg_id: int,
    amount: int,
    bank_code: str,
    acc_num: str,
    holder: str,
    display: str,
) -> dict | None:
    """Trừ số dư Ví của user và tạo lệnh rút tiền #WD_XXXXXX về Ngân hàng (ACID Transaction)."""
    if amount <= 0:
        return None
    async with db.tx() as conn:
        async with conn.execute("SELECT COALESCE(balance, 0) FROM users WHERE telegram_id=?", (tg_id,)) as cur:
            row = await cur.fetchone()
            cur_bal = int(row[0] or 0) if row else 0
        if cur_bal < amount:
            return None

        for _ in range(10):
            wd_id = "WD_" + secrets.token_hex(3).upper()
            async with conn.execute("SELECT 1 FROM withdrawals WHERE wd_id=?", (wd_id,)) as c2:
                if not await c2.fetchone():
                    break

        await conn.execute(
            "UPDATE users SET balance = balance - ? WHERE telegram_id=?",
            (amount, tg_id),
        )
        await conn.execute(
            "INSERT INTO withdrawals(wd_id, telegram_id, amount, bank_info_text, bank_code, bank_account, bank_holder, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING')",
            (wd_id, tg_id, amount, display, bank_code, acc_num, holder),
        )
    return await get_withdrawal(wd_id)


async def get_withdrawal(wd_id: str) -> dict | None:
    clean_id = wd_id.strip().lstrip("#").upper()
    return await db.fetchone("SELECT * FROM withdrawals WHERE wd_id=?", (clean_id,))


async def mark_withdrawal_paid(wd_id: str) -> dict | None:
    """Admin xác nhận đã chuyển khoản lệnh rút #WD_XXXXXX cho khách."""
    clean_id = wd_id.strip().lstrip("#").upper()
    async with db.tx() as conn:
        async with conn.execute("SELECT * FROM withdrawals WHERE wd_id=?", (clean_id,)) as cur:
            row = await cur.fetchone()
            if not row:
                return None
            w = dict(row)
        if w["status"] == "PAID":
            return None
        await conn.execute("UPDATE withdrawals SET status='PAID' WHERE wd_id=?", (clean_id,))
        await conn.execute(
            "UPDATE users SET total_withdrawn = total_withdrawn + ? WHERE telegram_id=?",
            (w["amount"], w["telegram_id"]),
        )
    return await get_withdrawal(clean_id)


async def mark_withdrawal_rejected(wd_id: str, reason: str) -> dict | None:
    """Admin từ chối lệnh rút (VD: sai STK) -> Tự động hoàn lại tiền vào số dư Ví của khách!"""
    clean_id = wd_id.strip().lstrip("#").upper()
    async with db.tx() as conn:
        async with conn.execute("SELECT * FROM withdrawals WHERE wd_id=?", (clean_id,)) as cur:
            row = await cur.fetchone()
            if not row:
                return None
            w = dict(row)
        if w["status"] in ("PAID", "REJECTED"):
            return None
        await conn.execute(
            "UPDATE withdrawals SET status='REJECTED', reject_reason=? WHERE wd_id=?",
            (reason, clean_id),
        )
        await conn.execute(
            "UPDATE users SET balance = COALESCE(balance, 0) + ? WHERE telegram_id=?",
            (w["amount"], w["telegram_id"]),
        )
    return await get_withdrawal(clean_id)


# ======================================================================
# VIETQR PAYOUT ENGINE CHO ADMIN QUÉT GIẢI NGÂN 1 CHẠM
# ======================================================================
def _render_vietqr_svg_to_png(svg_bytes: bytes, account_name: str, account: str, amount: int, content: str) -> bytes | None:
    text = svg_bytes.decode("utf-8", errors="ignore")
    imgs = re.findall(r'href="data:image/(?:png|jpeg);base64,([A-Za-z0-9+/=\s]+)"', text)
    if not imgs:
        return None
    base = Image.open(io.BytesIO(base64.b64decode(imgs[0]))).convert("RGBA")
    bg = Image.new("RGBA", base.size, (255, 255, 255, 255))
    bg.alpha_composite(base)
    draw = ImageDraw.Draw(bg)

    m_tr = re.search(r'transform="translate\(([\d.]+)\s+([\d.]+)\)\s*scale\(([\d.]+)\)"', text)
    m_path = re.search(r'<path[^>]*d="([^"]+)"', text)
    if m_tr and m_path:
        tx, ty, sc = float(m_tr.group(1)), float(m_tr.group(2)), float(m_tr.group(3))
        for mx, my, mw in re.findall(r"M(\d+)\s+(\d+)h(\d+)v1", m_path.group(1)):
            x0 = tx + int(mx) * sc
            y0 = ty + int(my) * sc
            draw.rectangle((x0, y0, x0 + int(mw) * sc, y0 + sc), fill=(17, 17, 17, 255))

        if len(imgs) > 1:
            m_logo = re.search(
                r'<image[^>]*id="vietqr-logo"[^>]*x="([\d.]+)"[^>]*y="([\d.]+)"[^>]*width="([\d.]+)"[^>]*height="([\d.]+)"',
                text,
            )
            if m_logo:
                lx = int(round(tx + float(m_logo.group(1)) * sc))
                ly = int(round(ty + float(m_logo.group(2)) * sc))
                lw = max(1, int(round(float(m_logo.group(3)) * sc)))
                lh = max(1, int(round(float(m_logo.group(4)) * sc)))
                try:
                    logo_im = Image.open(io.BytesIO(base64.b64decode(imgs[1]))).convert("RGBA").resize((lw, lh), Image.Resampling.LANCZOS)
                    bg.paste(logo_im, (lx, ly), logo_im)
                except Exception:
                    pass

    try:
        from PIL import ImageFont
        font_b = ImageFont.truetype("arialbd.ttf", 17)
        font_r = ImageFont.truetype("arial.ttf", 16)
    except Exception:
        from PIL import ImageFont
        font_b = font_r = ImageFont.load_default()

    w = bg.width
    footer = [
        (account_name, 552, font_b, (40, 40, 40, 255)),
        (f"STK: {account}", 576, font_r, (70, 70, 70, 255)),
        (f"Giai ngan: {amount:,} VND | {content}".replace(",", "."), 600, font_b, (238, 77, 45, 255)),
    ]
    for line, y, fnt, color in footer:
        bbox = draw.textbbox((0, 0), line, font=fnt)
        draw.text(((w - (bbox[2] - bbox[0])) / 2, y), line, font=fnt, fill=color)

    out = io.BytesIO()
    bg.convert("RGB").save(out, format="PNG")
    return out.getvalue()


async def generate_vietqr_payout_png(bank_code: str, account_number: str, account_name: str, amount_received: int, order_id: str) -> tuple[bytes, str]:
    """Tạo ảnh PNG VietQR chuẩn giải ngân cho Admin quét 1 chạm."""
    add_info = f"Rut SPayLater {order_id}"
    vietqr_url = (
        f"https://img.vietqr.io/image/{quote(bank_code)}-{quote(account_number)}-compact2.png"
        f"?amount={amount_received}&addInfo={quote(add_info)}&accountName={quote(account_name)}"
    )
    async with aiohttp.ClientSession() as s:
        try:
            async with s.get(vietqr_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=aiohttp.ClientTimeout(total=12)) as r:
                if r.status == 200:
                    raw = await r.read()
                    if b"<svg" in raw[:200]:
                        png = _render_vietqr_svg_to_png(raw, account_name, account_number, amount_received, add_info)
                        if png:
                            return png, vietqr_url
                    else:
                        im = Image.open(io.BytesIO(raw)).convert("RGB")
                        out = io.BytesIO()
                        im.save(out, format="PNG")
                        return out.getvalue(), vietqr_url
        except Exception as e:
            log.warning("VietQR primary error: %s", e)

        try:
            u2 = f"https://qr.sepay.vn/img?acc={quote(account_number)}&bank={quote(bank_code)}&amount={amount_received}&des={quote(add_info)}&template=compact"
            async with s.get(u2, timeout=aiohttp.ClientTimeout(total=12)) as r2:
                if r2.status == 200:
                    im2 = Image.open(io.BytesIO(await r2.read())).convert("RGB")
                    out2 = io.BytesIO()
                    im2.save(out2, format="PNG")
                    return out2.getvalue(), vietqr_url
        except Exception as e2:
            log.warning("VietQR sepay fallback error: %s", e2)

    import qrcode
    qr_im = qrcode.make(f"{bank_code}|{account_number}|{account_name}|{amount_received}|{add_info}").convert("RGB").resize((360, 360))
    card = Image.new("RGB", (540, 560), (255, 255, 255))
    d = ImageDraw.Draw(card)
    d.rounded_rectangle((16, 16, 524, 544), radius=20, outline=(238, 77, 45), width=4)
    card.paste(qr_im, (90, 40))
    d.text((60, 420), f"NH: {bank_code} - STK: {account_number}", fill=(20, 20, 20))
    d.text((60, 450), f"Chu TK: {account_name}", fill=(20, 20, 20))
    d.text((60, 480), f"So tien: {amount_received:,} VND | ND: {add_info}", fill=(238, 77, 45))
    out = io.BytesIO()
    card.save(out, format="PNG")
    return out.getvalue(), vietqr_url


# ======================================================================
# THỐNG KÊ DOANH THU (/stats)
# ======================================================================
async def get_stats(days: int | None = None) -> dict:
    where = "WHERE status='PAID'"
    params = ()
    if days is not None:
        where += " AND date(created_at) >= date('now', 'localtime', ?)"
        params = (f"-{days - 1} days",)

    row = await db.fetchone(
        f"SELECT COUNT(*) n_orders, COALESCE(SUM(amount_face), 0) total_face, "
        f"COALESCE(SUM(amount_received), 0) total_paid, COALESCE(SUM(so_expected), 0) total_so "
        f"FROM orders {where}",
        params,
    )
    total_face = int(row["total_face"] or 0)
    total_paid = int(row["total_paid"] or 0)
    total_so = int(row["total_so"] or 0)
    profit = total_face - total_paid
    pending_ord = await db.fetchval("SELECT COUNT(*) FROM orders WHERE status='PENDING_ADMIN_REVIEW'", default=0)
    pending_wd = await db.fetchval("SELECT COUNT(*) FROM withdrawals WHERE status='PENDING'", default=0)
    users_cnt = await db.fetchval("SELECT COUNT(*) FROM users", default=0)
    total_user_bal = await db.fetchval("SELECT COALESCE(SUM(balance), 0) FROM users", default=0)
    cur_so = await get_current_so_balance()
    return {
        "n_orders": int(row["n_orders"] or 0),
        "total_face": total_face,
        "total_paid": total_paid,
        "total_so": total_so,
        "cur_so": cur_so,
        "profit": profit,
        "pending_cnt": int(pending_ord or 0) + int(pending_wd or 0),
        "pending_ord": int(pending_ord or 0),
        "pending_wd": int(pending_wd or 0),
        "total_user_bal": int(total_user_bal or 0),
        "users_cnt": int(users_cnt or 0),
    }
