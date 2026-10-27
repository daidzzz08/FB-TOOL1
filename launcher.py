import os
import sys

# Ép stdout/stderr dùng UTF-8, tránh lỗi cp1252 trên Windows console
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import uvicorn
import threading
import signal
from main import app


def print_banner():
    banner = r"""
  ==================================================================
      🚀 FB AUTO POSTER & LEAD RADAR - PURE COOKIE ENGINE
  ==================================================================
   * Web UI:         http://127.0.0.1:8000
   * Tự động:        Đang tự động mở trình duyệt web...
   * Hướng dẫn tắt:  Bấm tổ hợp phím [Ctrl + C] hoặc ĐÓNG CỬA SỔ này
  ==================================================================
    """
    print(banner)


def main():
    print_banner()
    try:
        # Chạy Uvicorn với chế độ log gọn gàng
        config = uvicorn.Config(
            app=app,
            host="127.0.0.1",
            port=8000,
            log_level="info",
            access_log=False  # Giảm bớt log polling để màn hình console sạch sẽ
        )
        server = uvicorn.Server(config)
        server.run()
    except (KeyboardInterrupt, SystemExit):
        print("\n[SYSTEM] 🛑 Đang tắt tool an toàn và giải phóng bộ nhớ...")
        sys.exit(0)


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    main()
