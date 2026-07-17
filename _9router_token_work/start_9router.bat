@echo off
:: ============================================
::  9Router - One Click Start (Admin)
:: ============================================

:: Tự động yêu cầu quyền Administrator
>nul 2>&1 "%SYSTEMROOT%\system32\cacls.exe" "%SYSTEMROOT%\system32\config\system"
if '%errorlevel%' NEQ '0' (
    echo Dang yeu cau quyen Administrator...
    goto UACPrompt
) else ( goto gotAdmin )

:UACPrompt
    echo Set UAC = CreateObject^("Shell.Application"^) > "%temp%\getadmin.vbs"
    echo UAC.ShellExecute "%~f0", "", "", "runas", 1 >> "%temp%\getadmin.vbs"
    "%temp%\getadmin.vbs"
    exit /B

:gotAdmin
    if exist "%temp%\getadmin.vbs" ( del "%temp%\getadmin.vbs" )
    pushd "%CD%"
    for /f "delims=" %%i in ('powershell -NoProfile -Command "(Get-Item '%~dp0').FullName"') do CD /D "%%i"

:: ============================================
title 9Router - AI Smart Router
cls
echo ============================================
echo   9Router - AI Smart Router
echo ============================================
echo.
echo   Dashboard:  http://localhost:20128
echo   API:        http://localhost:20128/v1
echo.
echo   Dang khoi dong... Vui long doi...
echo   (Nhan Ctrl+C de dung server)
echo ============================================
echo.

:: Chạy 9Router
call npm run start
pause
