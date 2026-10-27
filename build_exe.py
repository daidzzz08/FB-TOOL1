import sys
import os
import subprocess

# Ép stdout/stderr dùng UTF-8, tránh lỗi cp1252 trên Windows console
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def build():
    print("[BUILD] [*] Bat dau qua trinh dong goi file .exe...")
    
    # Cài đặt PyInstaller
    subprocess.run([sys.executable, "-m", "pip", "install", "pyinstaller"], check=True)

    sep = ";" if sys.platform.startswith("win") else ":"

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--onedir",
        "--name=FB_Auto_Poster",
        "--collect-all=curl_cffi",
        f"--add-data=static{sep}static",
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

    print(f"[BUILD] [RUN] Lenh: {' '.join(cmd)}")
    subprocess.run(cmd, check=True)
    print("\n[BUILD] [OK] Dong goi thanh cong! Thu muc output: dist/FB_Auto_Poster")


if __name__ == "__main__":
    build()
