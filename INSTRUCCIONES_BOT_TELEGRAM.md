# 🤖 Instrucciones para Configurar el Bot de Telegram

## 📋 Requisitos Previos

1. Python 3.8 o superior
2. Cuenta de Telegram
3. Cuenta de OpenAI (para la API de Vision)

---

## 🔧 Paso 1: Crear el Bot en Telegram

1. Abre Telegram y busca **@BotFather**
2. Envía el comando `/newbot`
3. Sigue las instrucciones:
   - Elige un nombre para tu bot (ej: "Control XPO Bot")
   - Elige un username (debe terminar en `bot`, ej: `control_xpo_bot`)
4. **BotFather te dará un TOKEN** - **¡GUÁRDALO!** Se verá así:
   ```
   1234567890:ABCdefGHIjklMNOpqrsTUVwxyz
   ```

---

## 🔑 Paso 2: Obtener Token de OpenAI

1. Ve a https://platform.openai.com/
2. Inicia sesión o crea una cuenta
3. Ve a **API Keys** (https://platform.openai.com/api-keys)
4. Haz clic en **"Create new secret key"**
5. **Copia el token** - Se verá así:
   ```
   sk-proj-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
   ```
   ⚠️ **IMPORTANTE**: Solo se muestra una vez. Guárdalo bien.

---

## ⚙️ Paso 3: Configurar los Tokens

1. Abre el archivo `config.py`
2. Reemplaza los valores:

```python
# Token del Bot de Telegram (de @BotFather)
TELEGRAM_BOT_TOKEN = 'TU_TOKEN_AQUI'  # ← Pega aquí el token de BotFather

# Token de OpenAI API
OPENAI_API_KEY = 'TU_OPENAI_API_KEY_AQUI'  # ← Pega aquí el token de OpenAI
```

**O mejor aún**, usa variables de entorno (más seguro):

### En Windows (PowerShell):
```powershell
$env:TELEGRAM_BOT_TOKEN="tu_token_aqui"
$env:OPENAI_API_KEY="tu_openai_key_aqui"
```

### En Linux/Mac:
```bash
export TELEGRAM_BOT_TOKEN="tu_token_aqui"
export OPENAI_API_KEY="tu_openai_key_aqui"
```

---

## 📦 Paso 4: Instalar Dependencias

```bash
pip install -r requirements.txt
```

O instala manualmente:
```bash
pip install python-telegram-bot>=20.0 openai>=1.0.0 requests>=2.31.0
```

---

## 🚀 Paso 5: Iniciar la Aplicación Flask

En una terminal, ejecuta:

```bash
python app.py
```

La aplicación debería iniciarse en `http://localhost:5000`

---

## 🤖 Paso 6: Iniciar el Bot de Telegram

En **otra terminal** (deja Flask corriendo), ejecuta:

```bash
python telegram_bot.py
```

Deberías ver:
```
🤖 Bot iniciado. Usando polling (no requiere webhook público)
📱 Busca tu bot en Telegram y envía /start
⏹️  Presiona Ctrl+C para detener el bot
```

---

## 📱 Paso 7: Probar el Bot

1. Abre Telegram
2. Busca tu bot por su username (ej: `@control_xpo_bot`)
3. Envía `/start`
4. Deberías ver un menú con opciones:
   - 🚛 Alta Viaje
   - ⛽ Ticket Gasoil

---

## 🔄 Flujo de Uso del Bot

### Para registrar un viaje:

1. Usuario envía `/start`
2. Selecciona "🚛 Alta Viaje"
3. Elige origen: **Algeciras** o **Valladolid**
4. Envía una **foto de la parte trasera del camión**
5. El bot procesa la imagen con OpenAI Vision API
6. Extrae automáticamente:
   - Fecha
   - Hora
   - Matrícula cabeza tractora
   - Matrícula remolque
7. Envía los datos a la aplicación Flask
8. El viaje se registra en la base de datos

---

## 🛠️ Solución de Problemas

### Error: "No se ha configurado el token del bot"
- Verifica que `config.py` tenga el token correcto
- O que las variables de entorno estén configuradas

### Error: "Error al procesar imagen con OpenAI"
- Verifica que el token de OpenAI sea correcto
- Asegúrate de tener créditos en tu cuenta de OpenAI
- Verifica que la imagen sea clara y muestre las matrículas

### Error: "Error de conexión con el servidor"
- Asegúrate de que `app.py` esté corriendo
- Verifica que `FLASK_APP_URL` en `config.py` sea `http://localhost:5000`

### El bot no responde
- Verifica que `telegram_bot.py` esté corriendo
- Revisa los logs en la terminal
- Asegúrate de que el token del bot sea correcto

---

## 📝 Notas Importantes

1. **Polling vs Webhook**: Este bot usa **polling**, que es perfecto para desarrollo local. No necesitas un servidor público ni ngrok.

2. **Seguridad**: 
   - **NO subas `config.py` con tokens reales a Git**
   - Usa variables de entorno en producción
   - Añade `config.py` al `.gitignore`

3. **OpenAI Costs**: El uso de OpenAI Vision API tiene costos. Revisa los precios en https://openai.com/pricing

4. **Base de Datos**: Los viajes se guardan en la misma base de datos SQLite (`app.db`)

---

## 🎯 Próximos Pasos

- [ ] Implementar "Ticket Gasoil"
- [ ] Añadir validaciones adicionales
- [ ] Mejorar el procesamiento de imágenes
- [ ] Añadir notificaciones al usuario cuando se complete el registro

---

## 📞 Soporte

Si tienes problemas, revisa:
- Los logs en las terminales donde corren Flask y el bot
- La documentación de python-telegram-bot: https://python-telegram-bot.org/
- La documentación de OpenAI: https://platform.openai.com/docs
