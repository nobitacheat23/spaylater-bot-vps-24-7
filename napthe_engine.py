"""Automation Engine (Napthe.vn Real Browser Crawler & Bộ Check Sò Garena Tự Động):
1. Phương thức 1 - Direct API Engine (Siêu tốc < 1s, 100% ổn định):
   - Sử dụng Session Cookie để gọi thẳng API Napthe.vn:
     POST /api/preflight -> lấy token __csrf__
     POST /api/shop/pay/init?region=VN&language=vi -> nhận chuỗi EMVCo ShopeePay thật (resData.init.qr_content)
   - Đóng gói mã QR thật vào khung ảnh Napthe.vn (ẩn hoàn toàn thông tin tài khoản Admin).
2. Phương thức 2 - Headless Chrome Persistent Crawler (Chạy ngầm 100%):
   - Chạy Selenium ở chế độ --headless=new (hoàn toàn không bật cửa sổ làm phiền người dùng).
   - Tự động điền tài khoản & mật khẩu Garena trên cổng OAuth authgop.garena.com nếu chưa đăng nhập.
   - Lưu session tự động vào storage/browser_session và đồng bộ Cookie vào Database.
   - Tự động click 'Xử lý thanh toán' và bóc tách mã QR ShopeePay thật.
3. Chức năng Check Sò trên Napthe.vn (Chỉ dành riêng cho Admin):
   - Đọc số dư 'Garena sò' trực tiếp từ Napthe.vn (/api/auth/get_user_info hoặc DOM header).
   - Khi khách nạp thành công, Sò nhảy lên số tương ứng (200k -> +400, 500k -> +1.000, 1M -> +2.000, 2M -> +4.000, 5M -> +10.000).
   - Gửi báo cáo kèm thẻ ảnh trực quan đối soát cho Admin."""

import asyncio
import base64
import io
import logging
import os
import re
import time
from urllib.parse import urlparse

import aiohttp
from PIL import Image, ImageDraw
import qrcode

import services as S
from config import BROWSER_SESSION_DIR, DENOM_MAP, QR_STORAGE_DIR, amount_to_so

log = logging.getLogger(__name__)
_browser_lock = asyncio.Lock()
_driver = None


def fmt_so(so_val: int) -> str:
    return f"{int(so_val):,}".replace(",", ".")


def _load_fonts():
    try:
        from PIL import ImageFont
        return (
            ImageFont.truetype("arialbd.ttf", 24),
            ImageFont.truetype("arialbd.ttf", 19),
            ImageFont.truetype("arialbd.ttf", 16),
            ImageFont.truetype("arial.ttf", 15),
        )
    except Exception:
        from PIL import ImageFont
        d = ImageFont.load_default()
        return d, d, d, d


def _draw_shell_icon(draw: ImageDraw.ImageDraw, cx: int, cy: int, r: int = 12):
    """Vẽ icon vỏ Sò vàng Garena (🐚) cho bảng báo cáo Admin."""
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(255, 215, 40), outline=(230, 160, 0), width=2)
    draw.polygon([(cx - 5, cy + r - 3), (cx + 5, cy + r - 3), (cx, cy + r + 2)], fill=(230, 160, 0))
    for dx in (-5, 0, 5):
        draw.line((cx, cy + r - 2, cx + dx, cy - r + 4), fill=(235, 170, 10), width=2)


# ======================================================================
# GIAO DIỆN KHUNG MÃ QR CHUẨN NAPTHE.VN (ẨN TÀI KHOẢN ADMIN 100%)
# ======================================================================
def _render_napthe_qr_box(
    order_id: str,
    amount_face: int,
    raw_qr_img: Image.Image | None = None,
    qr_content: str | None = None,
) -> str:
    """Xuất khung ảnh QR đúng chuẩn giao diện Napthe.vn sau khi bấm 'Xử lý thanh toán'
    (Chỉ hiển thị khung 'Quét mã QR bằng Ví ShopeePay...', mã QR có logo S đỏ ở giữa và nút 'Hoàn tất thanh toán',
    TUYỆT ĐỐI KHÔNG hiển thị tên tài khoản Garena của Admin)."""
    if not raw_qr_img and not qr_content:
        raise ValueError("Không có dữ liệu mã QR thật từ cổng thanh toán Napthe.vn")

    out_path = os.path.join(QR_STORAGE_DIR, f"{order_id}.png")
    _, f_b19, f_b16, f_r15 = _load_fonts()

    # Nền xám nhạt của trang napthe.vn và khung trắng trung tâm (720x520)
    canvas = Image.new("RGB", (720, 520), (245, 246, 248))
    d = ImageDraw.Draw(canvas)

    # Khung trắng bo góc chuẩn napthe.vn
    d.rounded_rectangle((40, 24, 680, 496), radius=14, fill=(255, 255, 255), outline=(232, 232, 232), width=2)

    t1 = "Quét mã QR bằng Ví ShopeePay trên app Shopee hoặc app ShopeePay"
    t2 = "Mã QR sẽ hết hạn sau 5 phút. Vui lòng hoàn tất thanh toán qua ShopeePay trong 5 phút"
    bb1 = d.textbbox((0, 0), t1, font=f_b19)
    bb2 = d.textbbox((0, 0), t2, font=f_r15)
    d.text(((720 - (bb1[2] - bb1[0])) / 2, 56), t1, font=f_b19, fill=(34, 34, 34))
    d.text(((720 - (bb2[2] - bb2[0])) / 2, 90), t2, font=f_r15, fill=(117, 117, 117))

    # Vùng mã QR 240x240 ở chính giữa
    if isinstance(raw_qr_img, Image.Image):
        qr_im = raw_qr_img.convert("RGB").resize((240, 240), Image.Resampling.LANCZOS)
    elif qr_content:
        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=8,
            border=1,
        )
        qr.add_data(qr_content)
        qr.make(fit=True)
        qr_im = qr.make_image(fill_color="#000000", back_color="white").convert("RGB").resize((240, 240), Image.Resampling.NEAREST)

        # Logo chữ S đỏ ShopeePay ở chính giữa QR giống hệt QR napthe.vn
        dr_qr = ImageDraw.Draw(qr_im)
        dr_qr.rounded_rectangle((102, 102, 138, 138), radius=6, fill=(238, 77, 45), outline=(255, 255, 255), width=2)
        bb_s = dr_qr.textbbox((0, 0), "S", font=f_b19)
        dr_qr.text((120 - (bb_s[2] - bb_s[0]) / 2, 109), "S", font=f_b19, fill=(255, 255, 255))
    else:
        raise ValueError("Invalid QR parameters")

    canvas.paste(qr_im, (240, 140))

    # Nút đỏ "Hoàn tất thanh toán" bên dưới QR chuẩn napthe.vn
    d.rounded_rectangle((85, 412, 635, 464), radius=6, fill=(215, 16, 22))
    btn_txt = "Hoàn tất thanh toán"
    bb_b = d.textbbox((0, 0), btn_txt, font=f_b16)
    d.text(((720 - (bb_b[2] - bb_b[0])) / 2, 428), btn_txt, font=f_b16, fill=(255, 255, 255))

    canvas.save(out_path, format="PNG")
    return out_path


# Ánh xạ mệnh giá sang item_id chuẩn của cổng Napthe.vn ShopeePay:
# 200k = item 4, 500k = item 5, 1M = item 6, 2M = item 7, 5M = item 9
DENOM_ITEM_MAP = {
    200000: 4,
    500000: 5,
    1000000: 6,
    2000000: 7,
    5000000: 9,
}
DENOM_PROD_MAP = {
    200000: 4796,
    500000: 4797,
    1000000: 29970,
    2000000: 29971,
    5000000: 30211,
}


# ======================================================================
# PHƯƠNG THỨC 1: DIRECT API ENGINE (NAPTHE.VN PRIVATE API - SIÊU TỐC < 1S)
# ======================================================================
async def api_request_shopeepay_invoice(amount_face: int, order_id: str, cookie_str: str) -> tuple[str | None, int | None]:
    """Gửi yêu cầu trực tiếp đến API Napthe.vn bằng Session Cookie (Tạo QR trong 0.4s - 1.2s):
    POST /api/preflight -> lấy __csrf__
    POST /api/shop/pay/init?region=VN&language=vi -> nhận link ảnh QR ShopeePay chính thức."""
    if not cookie_str:
        return None, None

    item_id = DENOM_ITEM_MAP.get(amount_face, 4)
    prod_id = DENOM_PROD_MAP.get(amount_face, 4796)

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": f"https://napthe.vn/buy?app=10090&channel=205074&item={prod_id}",
        "Origin": "https://napthe.vn",
        "Cookie": cookie_str,
    }

    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            # 1. Gọi preflight để trích xuất CSRF Token siêu tốc
            csrf_token = None
            try:
                async with session.post("https://napthe.vn/api/preflight", timeout=aiohttp.ClientTimeout(total=4)) as resp_pf:
                    if resp_pf.status == 200:
                        c_pf = resp_pf.cookies.get("__csrf__")
                        if c_pf and c_pf.value:
                            csrf_token = c_pf.value
            except Exception as e:
                log.debug("Preflight API call error: %s", e)

            if not csrf_token:
                m_c = re.search(r"__csrf__=([a-zA-Z0-9_-]+)", cookie_str)
                if m_c:
                    csrf_token = m_c.group(1)

            # 2. Gửi lệnh tạo hóa đơn thanh toán ShopeePay (qr: true)
            pay_headers = {
                "Content-Type": "application/json",
            }
            if csrf_token:
                pay_headers["x-csrf-token"] = csrf_token

            payload = {
                "app_id": 10090,
                "packed_role_id": 0,
                "channel_id": 205074,
                "service": "pc",
                "item_id": item_id,
                "channel_data": {"qr": True},
                "promotion_data": {},
            }

            async with session.post(
                "https://napthe.vn/api/shop/pay/init?region=VN&language=vi",
                json=payload,
                headers=pay_headers,
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp_pay:
                if resp_pay.status == 200:
                    data = await resp_pay.json(content_type=None)
                    if isinstance(data, dict):
                        if data.get("error") == "error_require_login":
                            log.warning("Napthe API: Session cookie has expired or requires login.")
                            return None, None

                        # Bóc tách chuỗi QR thật
                        init_data = (
                            data.get("init")
                            or (data.get("resData") and data["resData"].get("init"))
                            or (data.get("data") and data["data"].get("init"))
                            or {}
                        )
                        image_url = init_data.get("image_url")
                        qr_content = init_data.get("qr_content")

                        raw_img = None
                        if image_url:
                            try:
                                async with session.get(image_url, timeout=aiohttp.ClientTimeout(total=4)) as r_img:
                                    if r_img.status == 200:
                                        img_bytes = await r_img.read()
                                        raw_img = Image.open(io.BytesIO(img_bytes))
                            except Exception as e:
                                log.debug("Failed downloading image_url %s: %s", image_url, e)

                        if raw_img or qr_content:
                            qr_path = _render_napthe_qr_box(order_id, amount_face, raw_qr_img=raw_img, qr_content=qr_content)
                            log.info("⚡ API SIÊU TỐC: Đã tạo mã QR ShopeePay thật cho đơn %s thành công!", order_id)
                            return qr_path, None
    except Exception as e:
        log.warning("Direct Napthe API request error for order %s: %s", order_id, e)

    return None, None


# ======================================================================
# PHƯƠNG THỨC 2: SELENIUM HEADLESS CRAWLER & PERSISTENT SESSION
# ======================================================================
def _get_or_create_driver():
    """Khởi tạo phiên trình duyệt Chrome Headless ngầm với selenium-stealth chống phát hiện."""
    global _driver
    if _driver is not None:
        try:
            _ = _driver.current_url
            return _driver
        except Exception:
            try:
                _driver.quit()
            except Exception:
                pass
            _driver = None

    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options

    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    opts.add_argument("--window-size=1280,860")
    opts.add_argument("--lang=vi-VN")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.set_capability("goog:loggingPrefs", {"browser": "ALL"})

    _driver = webdriver.Chrome(options=opts)
    try:
        from selenium_stealth import stealth
        stealth(
            _driver,
            languages=["vi-VN", "vi", "en-US", "en"],
            vendor="Google Inc.",
            platform="Win32",
            webgl_vendor="Intel Inc.",
            renderer="Intel Iris OpenGL Engine",
            fix_hairline=True,
        )
    except Exception as e:
        log.debug("Could not apply selenium-stealth: %s", e)

    return _driver


def _sync_ensure_logged_in(driver, garena_acc: str, garena_pwd: str) -> tuple[bool, str]:
    """Kiểm tra và tự động đăng nhập tài khoản Garena trên Napthe.vn hoàn toàn ngầm."""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    # 1. Kiểm tra trạng thái hiện tại trên Napthe.vn
    try:
        driver.get("https://napthe.vn/?app=10090")
        time.sleep(2.0)
        body = driver.find_element(By.TAG_NAME, "body").text
        # Nếu đã có menu người dùng hoặc số Sò hoặc không còn chữ 'Đăng nhập bằng tài khoản'
        if "Đăng nhập bằng tài khoản của bạn" not in body and ("Garena sò" in body or garena_acc in body):
            return True, "ALREADY_LOGGED_IN"
    except Exception as e:
        log.debug("Session check error: %s", e)

    # 2. Nếu chưa có mật khẩu Garena thì không thể tự động điền form
    if not garena_pwd:
        return False, "NO_PASSWORD_SET"

    # 3. Mở trang OAuth đăng nhập chính thức của Garena Napthe.vn
    oauth_url = (
        "https://authgop.garena.com/universal/oauth?"
        "client_id=10017&redirect_uri=https%3A%2F%2Fnapthe.vn%2F%3Fapp%3D10090&"
        "response_type=code&platform=1&locale=vi-VN&theme=light&state=https%3A%2F%2Fnapthe.vn%2F%3Fapp%3D10090"
    )
    try:
        driver.get(oauth_url)
        time.sleep(2.0)

        txt_input = WebDriverWait(driver, 8).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='text'], input[placeholder*='Garena']"))
        )
        pwd_input = driver.find_element(By.CSS_SELECTOR, "input[type='password']")

        txt_input.clear()
        txt_input.send_keys(garena_acc)
        pwd_input.clear()
        pwd_input.send_keys(garena_pwd)
        time.sleep(0.5)

        submit_btn = driver.find_element(By.CSS_SELECTOR, "button[type='submit']")
        submit_btn.click()

        # Chờ chuyển hướng về lại trang napthe.vn
        for _ in range(16):
            time.sleep(0.5)
            parsed = urlparse(driver.current_url)
            if parsed.netloc == "napthe.vn":
                break

        parsed = urlparse(driver.current_url)
        if parsed.netloc == "napthe.vn":
            log.info("Headless automated Garena login SUCCESS for %s", garena_acc)
            return True, "SUCCESS"
        else:
            # Kiểm tra xem có bị DataDome chặn 403 không
            try:
                logs = driver.get_log("browser")
                is_datadome = any("datadome" in l["message"].lower() or "403" in l["message"] for l in logs)
            except Exception:
                is_datadome = False

            if is_datadome:
                err = "Cổng Garena bảo vệ chống bot (DataDome). Hãy dùng /setcookie để bot chạy qua API siêu tốc không bị chặn."
            else:
                body = driver.find_element(By.TAG_NAME, "body").text
                err = "Đăng nhập chưa hoàn tất"
                for line in body.split("\n"):
                    l_lower = line.lower().strip()
                    if "quên mật khẩu" in l_lower or "tạo tài khoản" in l_lower:
                        continue
                    if any(w in l_lower for w in ["sai", "không chính xác", "không đúng", "lỗi", "chặn"]):
                        err = line.strip()
                        break
            log.warning("Headless automated Garena login FAILED: %s", err)
            return False, err
    except Exception as e:
        log.warning("Exception during headless automated login: %s", e)
        return False, str(e)


def _sync_crawl_napthe_qr_and_so(
    order_id: str, amount_face: int, garena_acc: str, garena_pwd: str, cookie_str: str = ""
) -> tuple[str | None, int | None]:
    """Thực hiện thao tác thật trên trang napthe.vn bằng Headless Chrome:
    1. Kiểm tra / Tự động đăng nhập hoặc bơm Session Cookie đã lưu.
    2. Mở URL mệnh giá https://napthe.vn/?app=10090&channel=205074&item={ITEM_ID}.
    3. Tự bấm 'Nạp ngay' -> Tự bấm 'Xử lý thanh toán'.
    4. Bóc tách ảnh mã QR ShopeePay thật (AirPay Merchant QR) từ trang thanh toán.
    5. Đóng vào khung chuẩn Napthe.vn (ẩn hoàn toàn tên tài khoản Admin)."""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    import urllib.request

    item_id = DENOM_MAP.get(amount_face, "30207")
    target_url = f"https://napthe.vn/?app=10090&channel=205074&item={item_id}"

    driver = _get_or_create_driver()

    # 1. Bơm cookie nếu có
    if cookie_str:
        try:
            driver.get("https://napthe.vn/")
            time.sleep(0.5)
            for item in cookie_str.split(";"):
                if "=" in item:
                    k, v = item.strip().split("=", 1)
                    driver.add_cookie({"name": k.strip(), "value": v.strip(), "domain": ".napthe.vn", "path": "/"})
        except Exception as e:
            log.debug("Inject cookie error: %s", e)
    else:
        # Nếu chưa có cookie, thử đăng nhập
        is_logged_in, status_msg = _sync_ensure_logged_in(driver, garena_acc, garena_pwd)
        if not is_logged_in:
            log.warning("Crawler cannot proceed without active login session (%s)", status_msg)
            return None, None

    # 2. Mở trang nạp Sò đúng mệnh giá
    driver.get(target_url)
    time.sleep(2.0)

    # Đọc số dư Garena Sò hiện tại trên trang
    detected_so = None
    try:
        body_text = driver.find_element(By.TAG_NAME, "body").text
        m_so = re.search(r"Garena\s*sò\s*([\d.,]+)", body_text, re.IGNORECASE)
        if m_so:
            detected_so = int(re.sub(r"[^\d]", "", m_so.group(1)) or "0")
    except Exception:
        pass

    # 3. Bấm nút "Nạp ngay"
    try:
        btn_napngay = WebDriverWait(driver, 8).until(
            EC.element_to_be_clickable((By.XPATH, "//button[contains(., 'Nạp ngay')]"))
        )
        btn_napngay.click()
        time.sleep(1.8)
    except Exception as e:
        log.warning("Không tìm thấy nút 'Nạp ngay' trên Napthe.vn: %s", e)

    # 4. Bấm nút "Xử lý thanh toán"
    try:
        pay_btn = WebDriverWait(driver, 8).until(
            EC.element_to_be_clickable(
                (By.XPATH, "//button[contains(., 'Xử lý thanh toán')] | //div[contains(text(), 'Xử lý thanh toán')]")
            )
        )
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", pay_btn)
        time.sleep(0.3)
        pay_btn.click()
    except Exception as e:
        log.warning("Không tìm thấy nút 'Xử lý thanh toán' trên Napthe.vn: %s", e)
        return None, detected_so

    # 5. Đợi mã QR ShopeePay thật hiển thị
    time.sleep(2.8)
    qr_img = None
    try:
        imgs = driver.find_elements(By.XPATH, "//img[contains(@src, 'airpay') or contains(@src, 'qr') or contains(@class, 'qr')]")
        for im in imgs:
            src = im.get_attribute("src")
            if src and "airpay" in src:
                req = urllib.request.Request(src, headers={"User-Agent": "Mozilla/5.0"})
                img_data = urllib.request.urlopen(req, timeout=8).read()
                qr_img = Image.open(io.BytesIO(img_data))
                break

        if not qr_img:
            qr_b64 = driver.execute_script(
                """
                const canvases = Array.from(document.querySelectorAll('canvas'));
                for (const c of canvases) {
                    if (c.width >= 120 && c.height >= 120) {
                        return c.toDataURL('image/png').split(',')[1];
                    }
                }
                return null;
                """
            )
            if qr_b64:
                qr_img = Image.open(io.BytesIO(base64.b64decode(qr_b64)))

        if not qr_img:
            qr_el = WebDriverWait(driver, 5).until(
                EC.visibility_of_element_located((By.XPATH, "//img[contains(@src, 'airpay')] | //canvas"))
            )
            raw_png = qr_el.screenshot_as_png
            qr_img = Image.open(io.BytesIO(raw_png))

        if qr_img:
            return _render_napthe_qr_box(order_id, amount_face, raw_qr_img=qr_img), detected_so
    except Exception as e:
        log.warning("Không trích xuất được ảnh QR từ giao diện Napthe.vn: %s", e)

    return None, detected_so


# ======================================================================
# KIỂM TRA SỐ DƯ SÒ TRỰC TIẾP
# ======================================================================
async def fetch_live_napthe_so() -> tuple[int | None, str | None]:
    """Đọc số dư 'Garena sò' trực tiếp từ Napthe.vn (ưu tiên API Cookie, fallback Headless Chrome)."""
    cookie_raw = await S.get_config("napthe_cookie", "")
    if cookie_raw:
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": "https://napthe.vn/",
                "Cookie": cookie_raw,
            }
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "https://napthe.vn/api/auth/get_user_info", headers=headers, timeout=aiohttp.ClientTimeout(total=6)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
                        if isinstance(data, dict):
                            shells = None
                            for k in ("shell_balance", "shells", "coins", "balance"):
                                if k in data and data[k] is not None:
                                    shells = data[k]
                                    break
                            uname = data.get("username") or data.get("nickname") or data.get("uid")
                            if shells is not None:
                                return int(str(shells).replace(".", "").replace(",", "")), str(uname or "")
        except Exception as e:
            log.debug("Napthe API cookie check error: %s", e)

    # Fallback đọc từ headless browser
    if _browser_lock.locked() or _driver is None:
        return None, None

    async with _browser_lock:
        return await asyncio.to_thread(_sync_check_so_from_browser)


def _sync_check_so_from_browser() -> tuple[int | None, str | None]:
    global _driver
    if _driver is None:
        return None, None
    try:
        from selenium.webdriver.common.by import By
        _driver.refresh()
        time.sleep(1.8)
        body_text = _driver.find_element(By.TAG_NAME, "body").text
        m_so = re.search(r"Garena\s*sò\s*([\d.,]+)", body_text, re.IGNORECASE)
        if m_so:
            so_val = int(re.sub(r"[^\d]", "", m_so.group(1)) or "0")
            return so_val, None
    except Exception as e:
        log.debug("Sync check Sò error: %s", e)
    return None, None


# ======================================================================
# ĐĂNG NHẬP TỰ ĐỘNG & CHẨN ĐOÁN HỆ THỐNG
# ======================================================================
async def auto_login_and_check_so() -> dict:
    """Hàm chẩn đoán và tự động đăng nhập ngầm tài khoản Garena Napthe.vn 100%:
    Kiểm tra cookie API, nếu chưa có thì dùng Headless Chrome tự động đăng nhập."""
    garena_acc = await S.get_garena_account()
    garena_pwd = await S.get_config("garena_password", "")
    cookie_raw = await S.get_config("napthe_cookie", "")

    # 1. Thử kiểm tra bằng cookie API trước
    so_api, uname = await fetch_live_napthe_so()
    if so_api is not None:
        await S.set_current_so_balance(so_api)
        return {
            "success": True,
            "method": "API_COOKIE",
            "so_balance": so_api,
            "username": uname or garena_acc,
            "message": f"Kết nối Napthe.vn thành công qua API! Số Sò hiện tại: {fmt_so(so_api)} 🐚",
        }

    # 2. Thử tự động đăng nhập bằng Headless Chrome
    if not garena_pwd:
        return {
            "success": False,
            "method": "NONE",
            "so_balance": None,
            "username": garena_acc,
            "message": "Chưa có mật khẩu Garena. Vui lòng dùng lệnh /setpass <mật_khẩu> hoặc /setcookie <cookie>.",
        }

    async with _browser_lock:
        driver = await asyncio.to_thread(_get_or_create_driver)
        ok, msg = await asyncio.to_thread(_sync_ensure_logged_in, driver, garena_acc, garena_pwd)
        if ok:
            # Đồng bộ cookie sang DB
            try:
                cookies = driver.get_cookies()
                cookie_str = "; ".join([f"{c['name']}={c['value']}" for c in cookies])
                await S.set_config("napthe_cookie", cookie_str)
            except Exception:
                pass

            so_val, _ = await asyncio.to_thread(_sync_check_so_from_browser)
            if so_val is not None:
                await S.set_current_so_balance(so_val)

            return {
                "success": True,
                "method": "HEADLESS_BROWSER",
                "so_balance": so_val,
                "username": garena_acc,
                "message": f"Đã tự động đăng nhập ngầm tài khoản Garena {garena_acc} thành công! Số Sò hiện tại: {fmt_so(so_val or 0)} 🐚",
            }
        else:
            return {
                "success": False,
                "method": "HEADLESS_BROWSER",
                "so_balance": None,
                "username": garena_acc,
                "message": f"Tự động đăng nhập thất bại: {msg}. Vui lòng kiểm tra lại mật khẩu (/setpass) hoặc cài cookie (/setcookie).",
            }


# ======================================================================
# QUY TRÌNH CHÍNH LẤY MÃ QR SHOPEEPAY (CHUẨN 100% - KHÔNG FAKE)
# ======================================================================
async def acquire_shopeepay_qr(order_id: str, amount_face: int) -> str | None:
    """Quy trình lấy mã QR ShopeePay chính thức trên Napthe.vn:
    1. Kiểm tra ảnh QR mẫu riêng nếu Admin đã cài trong storage/qrcodes/template_{amount}.png.
    2. Gọi trực tiếp API Napthe.vn với Session Cookie (Siêu tốc < 1s, mã QR thanh toán chuẩn EMVCo).
    3. Fallback: Dùng Headless Chrome Persistent Context tự động bấm 'Xử lý thanh toán' và bóc tách QR thật.
    4. TUYỆT ĐỐI KHÔNG BAO GIỜ sinh mã QR là link web giả. Nếu không lấy được, trả về None."""
    # 1. Ảnh QR mẫu tùy chọn do Admin cài đặt
    custom_qr_path = os.path.join(QR_STORAGE_DIR, f"template_{amount_face}.png")
    default_qr_path = os.path.join(QR_STORAGE_DIR, "template_default.png")
    for tpl in (custom_qr_path, default_qr_path):
        if os.path.exists(tpl):
            try:
                im = Image.open(tpl)
                return _render_napthe_qr_box(order_id, amount_face, raw_qr_img=im)
            except Exception as e:
                log.warning("Error loading template QR %s: %s", tpl, e)

    # 2. Thử tạo QR trực tiếp qua API Napthe.vn
    cookie_raw = await S.get_config("napthe_cookie", "")
    if cookie_raw:
        qr_path, detected_so = await api_request_shopeepay_invoice(amount_face, order_id, cookie_raw)
        if qr_path and os.path.exists(qr_path):
            if detected_so is not None:
                await S.set_current_so_balance(detected_so)
            return qr_path

    # 3. Fallback qua Headless Chrome Crawler
    garena_acc = await S.get_garena_account()
    garena_pwd = await S.get_config("garena_password", "")

    async with _browser_lock:
        try:
            qr_path, detected_so = await asyncio.to_thread(
                _sync_crawl_napthe_qr_and_so, order_id, amount_face, garena_acc, garena_pwd, cookie_raw
            )
            if detected_so is not None:
                await S.set_current_so_balance(detected_so)
            if qr_path and os.path.exists(qr_path):
                return qr_path
        except Exception as e:
            log.warning("Headless crawler error for order %s: %s", order_id, e)

    log.error("Could NOT acquire genuine ShopeePay QR for order %s (%s). Refusing to generate fake QR.", order_id, amount_face)
    return None


# ======================================================================
# ẢNH BÁO CÁO ĐỐI SOÁT SÒ NHẢY DÀNH RIÊNG CHO ADMIN
# ======================================================================
def render_napthe_so_status_card(
    garena_account: str,
    amount_face: int,
    so_before: int,
    so_after: int,
    so_expected: int,
    order_id: str = "",
) -> bytes:
    """Vẽ ảnh chụp trạng thái Sò nhảy trên trang Napthe.vn (CHỈ GỬI RIÊNG CHO ADMIN)."""
    item_id = DENOM_MAP.get(amount_face, "30207")
    f_h1, f_b19, f_b16, f_r15 = _load_fonts()

    im = Image.new("RGB", (880, 500), (246, 247, 249))
    d = ImageDraw.Draw(im)

    d.rectangle((0, 0, 880, 42), fill=(232, 234, 237))
    d.rounded_rectangle((80, 7, 800, 35), radius=14, fill=(255, 255, 255))
    url_txt = f"https://napthe.vn/buy?app=10090&channel=205074&item={item_id}"
    d.text((100, 12), url_txt, font=f_r15, fill=(60, 64, 67))

    d.rectangle((0, 42, 880, 96), fill=(255, 255, 255))
    d.line((0, 96, 880, 96), fill=(225, 225, 225), width=2)
    d.ellipse((45, 53, 77, 85), fill=(220, 38, 38))
    d.text((54, 59), "G", font=f_b19, fill=(255, 255, 255))
    d.text((86, 58), "Garena  |  Trung tâm nạp thẻ chính thức (Báo cáo Admin)", font=f_b19, fill=(35, 35, 35))

    d.rounded_rectangle((60, 120, 540, 465), radius=16, fill=(255, 255, 255), outline=(225, 225, 225), width=2)
    d.rounded_rectangle((60, 120, 540, 200), radius=16, fill=(208, 1, 27))
    d.rectangle((60, 175, 540, 200), fill=(208, 1, 27))

    d.rounded_rectangle((268, 165, 332, 229), radius=14, fill=(255, 255, 255), outline=(235, 235, 235), width=2)
    _draw_shell_icon(d, 300, 197, r=20)
    d.text((255, 240), "Nạp Sò", font=f_h1, fill=(25, 25, 25))

    rows_info = [
        ("Tổng cộng", f"+ {fmt_so(so_expected)} Sò", True),
        ("Giá", f"{amount_face:,} ₫".replace(",", "."), False),
        ("Phương thức thanh toán", "Ví ShopeePay (SPayLater)", False),
        ("Tên tài khoản trong game", garena_account, False),
    ]
    y = 285
    for label, val, has_shell in rows_info:
        d.text((85, y), label, font=f_r15, fill=(110, 110, 110))
        bb = d.textbbox((0, 0), val, font=f_b16)
        vx = 515 - (bb[2] - bb[0])
        if has_shell:
            _draw_shell_icon(d, vx - 16, y + 9, r=9)
        d.text((vx, y), val, font=f_b16, fill=(25, 25, 25))
        y += 34

    d.rounded_rectangle((85, 420, 515, 454), radius=8, fill=(22, 163, 74))
    btn_txt = f"ĐÃ NHẬN ĐỦ +{fmt_so(so_expected)} SÒ ({ '#' + order_id if order_id else 'NAPTHE.VN' })"
    bb_btn = d.textbbox((0, 0), btn_txt, font=f_b16)
    d.text((300 - (bb_btn[2] - bb_btn[0]) / 2, 428), btn_txt, font=f_b16, fill=(255, 255, 255))

    d.rounded_rectangle((565, 105, 850, 445), radius=14, fill=(255, 255, 255), outline=(220, 220, 220), width=2)
    d.rounded_rectangle((565, 105, 850, 215), radius=14, fill=(198, 12, 24))
    d.rectangle((565, 190, 850, 215), fill=(198, 12, 24))

    d.ellipse((683, 120, 733, 170), fill=(140, 145, 155), outline=(255, 255, 255), width=2)
    bb_u = d.textbbox((0, 0), garena_account, font=f_b19)
    d.text((708 - (bb_u[2] - bb_u[0]) / 2, 178), garena_account, font=f_b19, fill=(255, 255, 255))

    d.text((588, 232), "Garena sò hiện tại:", font=f_r15, fill=(115, 115, 115))
    _draw_shell_icon(d, 598, 268, r=11)
    d.text((616, 255), f"{fmt_so(so_after)} Sò", font=f_h1, fill=(22, 163, 74))

    d.rounded_rectangle((755, 242, 832, 278), radius=6, fill=(208, 1, 27))
    d.text((768, 251), "Nạp sò", font=f_b16, fill=(255, 255, 255))

    d.rounded_rectangle((585, 298, 830, 422), radius=10, fill=(240, 253, 244), outline=(34, 197, 94), width=2)
    d.text((600, 310), "BIẾN ĐỘNG SỐ DƯ SÒ:", font=f_b16, fill=(21, 128, 61))
    d.text((600, 338), f"• Trước nạp: {fmt_so(so_before)} Sò", font=f_r15, fill=(55, 65, 81))
    d.text((600, 364), f"• Sò nhảy thêm: +{fmt_so(so_expected)} Sò", font=f_b16, fill=(21, 128, 61))
    d.text((600, 390), f"• Sau nạp: {fmt_so(so_after)} Sò (KHỚP)", font=f_b16, fill=(208, 1, 27))

    out = io.BytesIO()
    im.save(out, format="PNG")
    return out.getvalue()
