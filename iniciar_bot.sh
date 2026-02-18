#!/bin/bash

echo "========================================"
echo "  Bot de Telegram - Control XPO"
echo "========================================"
echo ""

# Verificar que config.py existe
if [ ! -f "config.py" ]; then
    echo "ERROR: No se encuentra config.py"
    echo "Por favor, crea el archivo config.py con tus tokens"
    exit 1
fi

# Verificar que los tokens estén configurados
if grep -q "TU_TOKEN_AQUI" config.py || grep -q "TU_OPENAI_API_KEY_AQUI" config.py; then
    echo "⚠️  ADVERTENCIA: Parece que no has configurado los tokens en config.py"
    echo "Por favor, edita config.py y añade tus tokens antes de continuar"
    read -p "¿Deseas continuar de todos modos? (s/n): " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Ss]$ ]]; then
        exit 1
    fi
fi

echo ""
echo "Iniciando bot de Telegram..."
echo "Presiona Ctrl+C para detener"
echo ""

python3 telegram_bot.py
