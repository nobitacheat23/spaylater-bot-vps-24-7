"""Watchdog 24/7: Tự động giám sát và khởi động lại Bot SPayLater ngay lập tức nếu bị dừng hoặc rớt mạng."""
import os
import subprocess
import sys
import time
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(BASE_DIR, "bot_24_7.log")


def log(msg: str):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n"
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass
    print(line, end="", flush=True)


def main():
    os.chdir(BASE_DIR)
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    log("=== KHỞI ĐỘNG HỆ THỐNG WATCHDOG SPAYLATER BOT 24/7 ===")

    while True:
        try:
            log("Starting main.py...")
            proc = subprocess.Popen(
                [sys.executable, "-u", "main.py"],
                cwd=BASE_DIR,
                env=env,
            )
            code = proc.wait()
            log(f"main.py exited with code {code}. Tự động khởi động lại sau 3 giây...")
        except KeyboardInterrupt:
            log("Watchdog stopped by user.")
            break
        except Exception as e:
            log(f"Watchdog error: {e}. Thử lại sau 5 giây...")
        time.sleep(3)


if __name__ == "__main__":
    main()
