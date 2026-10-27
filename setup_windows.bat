@echo off
chcp 65001 >nul
title "Cài Đặt FB Auto Poster - Lead Radar"
color 0B

echo ====================================================================
echo    [*] BAT DAU CAI DAT MOI TRUONG CHO TOOL FB AUTO POSTER
echo ====================================================================
echo.

:: 1. Kiem tra Python (Thu ca 'python' va 'py')
set "PY_CMD="
python --version >nul 2>&1
if %errorlevel% equ 0 (
    set "PY_CMD=python"
) else (
    py --version >nul 2>&1
    if %errorlevel% equ 0 (
        set "PY_CMD=py"
    )
)

if "%PY_CMD%"=="" (
    color 0C
    echo [LOI] Khong tim thay Python tren may tinh cua ban!
    echo.
    echo Huong dan khac phuc:
    echo 1. Tai Python 3.10 tro len tai: https://www.python.org/downloads/
    echo 2. Khi cai dat, BAT BUOC TICH CHON: [x] "Add python.exe to PATH"
    echo 3. Neu vua cai xong, hay DONG cua so nay va MO LAI de nhan dien PATH.
    echo.
    pause
    exit /b 1
)

echo [1/3] Da tim thay trinh khoi chay Python: %PY_CMD%
%PY_CMD% --version

:: 2. Khoi tao moi truong ao venv
echo.
echo [2/3] Dang khoi tao moi truong ao (venv)...
if not exist "venv\Scripts\activate.bat" (
    %PY_CMD% -m venv venv
    echo Da tao thanh cong thu muc venv.
) else (
    echo Moi truong venv da ton tai.
)

:: 3. Cai dat thu vien
echo.
echo [3/3] Dang cai dat cac thu vien can thiet tu requirements.txt...
call venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt

if %errorlevel% neq 0 (
    color 0C
    echo.
    echo [LOI] Cai dat thu vien that bai! Vui long kiem tra lai ket noi Internet.
    pause
    exit /b 1
)

echo.
color 0A
echo ====================================================================
echo    [OK] CAI DAT HOAN TAT THANH CONG!
echo    Bay gio ban chi can click dup vao file "run_tool.bat" de su dung.
echo ====================================================================
echo.
pause
