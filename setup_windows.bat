@echo off
chcp 65001 >nul
title [CAI DAT] FB Auto Poster & Lead Radar Pro
color 0A

echo ==================================================================
echo   🚀 DANG KHOI TAO MOI TRUONG PYTHON CHO FB AUTO POSTER
echo ==================================================================
echo.

:: 1. Kiem tra Python tren may
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [LOI] Khong tim thay Python tren may cua ban!
    echo.
    echo Huong dan:
    echo 1. Tai Python 3.10 tro len tai: https://www.python.org/downloads/
    echo 2. Khi cai dat, NHO TICH VAO O: [x] "Add python.exe to PATH"
    echo.
    pause
    exit /b 1
)

for /f "tokens=*" %%v in ('python --version 2^>^&1') do set PY_VER=%%v
echo [*] Tim thay: %PY_VER%
echo.

:: 2. Tao virtualenv neu chua co
if not exist "venv\Scripts\python.exe" (
    echo [*] Dang khoi tao moi truong ao venv...
    python -m venv venv
    if %errorlevel% neq 0 (
        echo [LOI] Khong the tao moi truong ao venv.
        pause
        exit /b 1
    )
    echo [*] Da tao venv thanh cong!
) else (
    echo [*] Moi truong ao venv da ton tai san.
)
echo.

:: 3. Nang cap pip va cai dat thu vien
echo [*] Dang cap nhat pip va cai dat cac thu vien can thiet...
venv\Scripts\python.exe -m pip install --upgrade pip >nul 2>&1
venv\Scripts\python.exe -m pip install -r requirements.txt

if %errorlevel% neq 0 (
    echo.
    echo [LOI] Co loi xay ra trong qua trinh cai dat thu vien!
    pause
    exit /b 1
)

echo.
echo ==================================================================
echo   ✅ CAI DAT HOAN TAT 100%!
echo ==================================================================
echo   Gio day ban chi can chay file [run_tool.bat] de bat dau su dung!
echo ==================================================================
echo.
pause
