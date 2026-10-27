import sys
import os
import subprocess
from pathlib import Path


def build():
    print("[BUILD] 🚀 Bắt đầu quá trình tạo file .exe...")
    subprocess.run([sys.executable, "-m", "pip", "install", "pyinstaller"], check=True)

    sep = ";" if sys.platform.startswith("win") else ":"

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--onedir",                          # Dạng thư mục giúp khởi động nhanh tức thì
        "--name=FB_Auto_Poster",
        "--collect-all=curl_cffi",           # Gom DLLs và certs của curl-impersonate
        f"--add-data=static{sep}static",     # Đóng gói giao diện HTML/CSS/JS
        "--hidden-import=uvicorn.logging",
        "--hidden-import=uvicorn.loops",
        "--hidden-import=uvicorn.loops.auto",
        "--hidden-import=uvicorn.protocols",
        "--hidden-import=uvicorn.protocols.http",
        "--hidden-import=uvicorn.protocols.http.auto",
        "--hidden-import=uvicorn.lifespans",
        "--hidden-import=uvicorn.lifespans.on",
        "launcher.py"
    ]

    print(f"[BUILD] ⚙️ Lệnh thực thi: {' '.join(cmd)}")
    subprocess.run(cmd, check=True)

    dist_dir = Path("dist") / "FB_Auto_Poster"
    if dist_dir.exists():
        (dist_dir / "data").mkdir(exist_ok=True)
        (dist_dir / "uploads").mkdir(exist_ok=True)
        print(f"[BUILD] ✔️ Đã tạo thư mục data và uploads tại: {dist_dir}")

    print("\n[BUILD] ✅ Hoàn tất! File thực thi nằm trong thư mục: dist/FB_Auto_Poster")


if __name__ == "__main__":
    build()
