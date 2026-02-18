# 📱 Cómo Dar de Alta el Bot de Telegram - Paso a Paso

## 🎯 Objetivo
Crear un bot de Telegram que pueda recibir comandos y procesar imágenes usando OpenAI.

---

## 📋 Paso 1: Crear el Bot en Telegram

### 1.1 Abrir BotFather
1. Abre la aplicación **Telegram** en tu móvil o escritorio
2. En la barra de búsqueda, busca: **`@BotFather`**
3. Haz clic en el resultado (debe tener el icono de verificación azul ✓)

### 1.2 Crear el Bot
1. En el chat con BotFather, envía el comando: **`/newbot`**
2. BotFather te preguntará: **"Alright, a new bot. How are we going to call it? Please choose a name for your bot."**
3. Responde con el nombre que quieras, por ejemplo: **`Control XPO Bot`**

### 1.3 Elegir Username
1. BotFather te pedirá: **"Good. Now let's choose a username for your bot. It must end in `bot`. Like this, for example: TetrisBot or tetris_bot."**
2. Elige un username único que termine en `bot`, por ejemplo: **`control_xpo_bot`**
   - ⚠️ Si el username ya existe, BotFather te pedirá otro

### 1.4 Obtener el Token
1. Si todo va bien, BotFather te mostrará un mensaje como:
   ```
   Done! Congratulations on your new bot. You will find it at t.me/control_xpo_bot. 
   You can now add a description, about section and profile picture for your bot, see /help for a list of commands.
   
   Use this token to access the HTTP API:
   1234567890:ABCdefGHIjklMNOpqrsTUVwxyz
   
   Keep your token secure and store it safely, it can be used by anyone to control your bot.
   ```

2. **¡COPIA ESE TOKEN!** Se ve así: `1234567890:ABCdefGHIjklMNOpqrsTUVwxyz`
   - ⚠️ Este token es secreto, no lo compartas

---

## 🔑 Paso 2: Obtener Token de OpenAI

### 2.1 Crear Cuenta (si no tienes)
1. Ve a: **https://platform.openai.com/**
2. Haz clic en **"Sign up"** o **"Log in"**
3. Completa el registro

### 2.2 Obtener API Key
1. Una vez dentro, ve a: **https://platform.openai.com/api-keys**
2. Haz clic en el botón **"+ Create new secret key"**
3. Dale un nombre (opcional): **"Bot Control XPO"**
4. Haz clic en **"Create secret key"**
5. **¡COPIA EL TOKEN INMEDIATAMENTE!** Se ve así: `sk-proj-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`
   - ⚠️ Solo se muestra UNA VEZ, guárdalo bien
   - ⚠️ Si lo pierdes, tendrás que crear otro

### 2.3 Verificar Créditos
1. Ve a: **https://platform.openai.com/account/billing**
2. Asegúrate de tener créditos disponibles
3. Si no tienes, añade un método de pago

---

## ⚙️ Paso 3: Configurar los Tokens en el Proyecto

### 3.1 Editar config.py
1. Abre el archivo **`config.py`** en tu editor
2. Busca estas líneas:
   ```python
   TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', 'TU_TOKEN_AQUI')
   OPENAI_API_KEY = os.getenv('OPENAI_API_KEY', 'TU_OPENAI_API_KEY_AQUI')
   ```

3. Reemplaza los valores:
   ```python
   TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '1234567890:ABCdefGHIjklMNOpqrsTUVwxyz')
   OPENAI_API_KEY = os.getenv('OPENAI_API_KEY', 'sk-proj-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx')
   ```
   - Reemplaza con tus tokens reales

### 3.2 Alternativa: Variables de Entorno (Más Seguro)

**Windows (PowerShell):**
```powershell
$env:TELEGRAM_BOT_TOKEN="tu_token_aqui"
$env:OPENAI_API_KEY="tu_openai_key_aqui"
```

**Windows (CMD):**
```cmd
set TELEGRAM_BOT_TOKEN=tu_token_aqui
set OPENAI_API_KEY=tu_openai_key_aqui
```

**Linux/Mac:**
```bash
export TELEGRAM_BOT_TOKEN="tu_token_aqui"
export OPENAI_API_KEY="tu_openai_key_aqui"
```

---

## 📦 Paso 4: Instalar Dependencias

Abre una terminal en la carpeta del proyecto y ejecuta:

```bash
pip install -r requirements.txt
```

Esto instalará:
- `python-telegram-bot` - Para interactuar con Telegram
- `openai` - Para usar la API de OpenAI
- `requests` - Para hacer peticiones HTTP

---

## 🚀 Paso 5: Iniciar la Aplicación

### 5.1 Terminal 1 - Flask (Aplicación Web)
```bash
python app.py
```

Deberías ver:
```
 * Running on http://127.0.0.1:5000
```

### 5.2 Terminal 2 - Bot de Telegram
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

## ✅ Paso 6: Probar el Bot

1. Abre Telegram
2. Busca tu bot por su username (ej: `@control_xpo_bot`)
3. Haz clic en **"Start"** o envía **`/start`**
4. Deberías ver un menú con botones:
   - 🚛 Alta Viaje
   - ⛽ Ticket Gasoil

---

## 🔍 Verificar que Todo Funciona

### Test 1: El bot responde
- ✅ Envías `/start` → El bot responde con el menú

### Test 2: Selección de opciones
- ✅ Clic en "Alta Viaje" → Aparecen opciones de origen
- ✅ Clic en "Algeciras" o "Valladolid" → El bot pide foto

### Test 3: Procesamiento de imagen
- ✅ Envías una foto → El bot procesa con OpenAI
- ✅ El bot extrae datos y los envía a Flask
- ✅ El viaje se registra en la base de datos

---

## ❌ Solución de Problemas

### Error: "No se ha configurado el token del bot"
**Solución:**
- Verifica que `config.py` tenga el token correcto
- O configura la variable de entorno `TELEGRAM_BOT_TOKEN`

### Error: "OpenAI API Key no configurada"
**Solución:**
- Verifica que `config.py` tenga la API key correcta
- O configura la variable de entorno `OPENAI_API_KEY`

### Error: "Error de conexión con el servidor"
**Solución:**
- Asegúrate de que `app.py` esté corriendo
- Verifica que `FLASK_APP_URL` en `config.py` sea `http://localhost:5000`

### El bot no responde
**Solución:**
- Verifica que `telegram_bot.py` esté corriendo
- Revisa los logs en la terminal
- Asegúrate de que el token del bot sea correcto

### Error al procesar imagen
**Solución:**
- Verifica que tengas créditos en OpenAI
- Asegúrate de que la imagen sea clara
- Revisa los logs para ver el error específico

---

## 📝 Notas Importantes

1. **Polling vs Webhook**: Este bot usa **polling**, perfecto para desarrollo local. No necesitas un servidor público ni ngrok.

2. **Seguridad**: 
   - ⚠️ **NO subas `config.py` con tokens reales a Git**
   - El archivo `.gitignore` ya está configurado para ignorarlo
   - Usa variables de entorno en producción

3. **Costos**: 
   - OpenAI Vision API tiene costos por uso
   - Revisa los precios en: https://openai.com/pricing
   - El modelo `gpt-4o-mini` es más económico que `gpt-4-vision-preview`

4. **Base de Datos**: 
   - Los viajes se guardan en `app.db` (SQLite)
   - Puedes verlos en la aplicación web en `/control_xpo`

---

## 🎉 ¡Listo!

Si llegaste hasta aquí y el bot responde a `/start`, ¡todo está funcionando correctamente!

Para más detalles, consulta:
- `GUIA_RAPIDA.md` - Guía rápida de inicio
- `INSTRUCCIONES_BOT_TELEGRAM.md` - Instrucciones detalladas
