@echo off
echo ========================================
echo   Bot de Telegram - Control XPO
echo ========================================
echo.
echo Verificando configuracion...

if not exist config.py (
    echo ERROR: No se encuentra config.py
    echo Por favor, crea el archivo config.py con tus tokens
    pause
    exit
)

echo.
echo Iniciando bot de Telegram...
echo Presiona Ctrl+C para detener
echo.
python telegram_bot.py

pause
