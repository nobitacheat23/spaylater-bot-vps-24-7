import base64
import json
import os
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

# Tự động giải mã cấu hình khi chạy trên Cloud VPS 24/7 (cloud_env.dat)
_CLOUD_DAT = os.path.join(BASE_DIR, "cloud_env.dat")
if not os.getenv("BOT_TOKEN") and os.path.exists(_CLOUD_DAT):
    try:
        _raw = base64.b64decode(open(_CLOUD_DAT, "r", encoding="ascii").read().strip())
        _key = b"SPAYLATER_CLOUD_VPS_KEY_2026"
        _dec = json.loads(bytes(b ^ _key[i % len(_key)] for i, b in enumerate(_raw)).decode("utf-8"))
        for _k, _v in _dec.items():
            if _k and _v is not None and not os.getenv(_k):
                os.environ[_k] = str(_v)
    except Exception:
        pass


def _get(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _bool(name: str, default: str = "false") -> bool:
    return _get(name, default).lower() in ("1", "true", "yes", "on")


BOT_TOKEN = _get("BOT_TOKEN")
ADMIN_IDS = [int(x) for x in _get("ADMIN_IDS").replace(" ", "").split(",") if x.isdigit()]
ADMIN_GROUP_ID = int(_get("ADMIN_GROUP_ID") or (str(ADMIN_IDS[0]) if ADMIN_IDS else "0"))
SUPPORT_USERNAME = _get("SUPPORT_USERNAME", "useralex199").lstrip("@")

DEFAULT_FEE_PERCENT = float(_get("DEFAULT_FEE_PERCENT", "12") or 12.0)
QR_EXPIRE_SECONDS = int(_get("QR_EXPIRE_SECONDS", "300") or 300)
GARENA_ACCOUNT = _get("GARENA_ACCOUNT", "nhat2301088") or "nhat2301088"
GARENA_PASSWORD = _get("GARENA_PASSWORD")

BUTTON_STYLE = _get("BUTTON_STYLE", "success") or None
CUSTOM_EMOJI_IN_TEXT = _bool("CUSTOM_EMOJI_IN_TEXT", "true")

DB_PATH = os.path.join(BASE_DIR, _get("DB_PATH", "spaylater.db"))
QR_STORAGE_DIR = os.path.join(BASE_DIR, "storage", "qrcodes")
BROWSER_SESSION_DIR = os.path.join(BASE_DIR, "storage", "browser_session")
os.makedirs(QR_STORAGE_DIR, exist_ok=True)
os.makedirs(BROWSER_SESSION_DIR, exist_ok=True)

# Các mốc mệnh giá rút SPayLater (Min tối thiểu 200.000đ -> Napthe Item ID)
# Tỷ lệ quy đổi chuẩn Napthe.vn: 500 VNĐ = 1 Sò (VD: 200k = 400 Sò, 5M = 10.000 Sò)
MIN_WITHDRAW_AMOUNT = 200_000
NAPTHE_DENOMINATIONS = [
    (200_000, "30207"),
    (500_000, "30208"),
    (1_000_000, "30209"),
    (2_000_000, "30210"),
    (5_000_000, "30211"),
]
DENOM_MAP = dict(NAPTHE_DENOMINATIONS)

# Quy đổi mệnh giá VNĐ -> Số Sò Garena tương ứng trên Napthe.vn (Chỉ hiển thị cho Admin)
SO_MAP = {
    200_000: 400,
    500_000: 1_000,
    1_000_000: 2_000,
    2_000_000: 4_000,
    5_000_000: 10_000,
}


def amount_to_so(amount_face: int) -> int:
    return SO_MAP.get(amount_face, max(1, amount_face // 500))


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS
