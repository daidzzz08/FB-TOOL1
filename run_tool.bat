@echo off
chcp 65001 >nul
title FB Auto Poster & Lead Radar - Pure Cookie Engine
color 0B

:: Kiem tra neu chua chay setup_windows.bat
if not exist "venv\Scripts\python.exe" (
    echo ==================================================================
    echo   ❌ CHUA KHOI TAO MOI TRUONG CHO TOOL!
    echo ==================================================================
    echo   Ban chua cai dat cac thu vien can thiet.
    echo   👉 Vui long click dup chuot vao file [setup_windows.bat] truoc nhe!
    echo ==================================================================
    echo.
    pause
    exit /b 1
)

:: Khoi chay tool thong qua moi truong ao
venv\Scripts\python.exe launcher.py

:: Neu launcher gap su co dung dot ngot
if %errorlevel% neq 0 (
    echo.
    echo ==================================================================
    echo   ⚠️ Chuong trinh da ket thuc voi ma loi: %errorlevel%
    echo ==================================================================
    pause
)
