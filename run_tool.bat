@echo off
chcp 65001 >nul
title "FB Auto Poster - Lead Radar"
color 0A

if not exist "venv\Scripts\activate.bat" (
    color 0C
    echo [LOI] Chua tim thay moi truong ao venv!
    echo Vui long chay file "setup_windows.bat" truoc de cai dat tool.
    echo.
    pause
    exit /b 1
)

echo ====================================================================
echo    [*] DANG KHOI DONG FB AUTO POSTER VA LEAD RADAR...
echo    * Web UI:        http://127.0.0.1:8000
echo    * Trinh duyet:   Se tu dong mo tab web sau 2 giay...
echo    * Tat tool:      Dong cua so nay hoac bam [Ctrl + C]
echo ====================================================================
echo.

call venv\Scripts\activate.bat
python launcher.py

if %errorlevel% neq 0 (
    color 0C
    echo.
    echo [CANH BAO] Tool da dung lai do co loi phat sinh o tren.
    pause
)
