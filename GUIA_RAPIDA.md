# 🚀 Guía Rápida - Bot de Telegram Control XPO

## ⚡ Inicio Rápido (5 minutos)

### Paso 1: Crear el Bot en Telegram

1. Abre Telegram
2. Busca **@BotFather**
3. Envía: `/newbot`
4. Sigue las instrucciones:
   - Nombre del bot: `Control XPO Bot` (o el que prefieras)
   - Username: `control_xpo_bot` (debe terminar en `bot`)
5. **Copia el TOKEN** que te da BotFather (ejemplo: `1234567890:ABCdefGHIjklMNOpqrsTUVwxyz`)

### Paso 2: Obtener Token de OpenAI

1. Ve a: https://platform.openai.com/api-keys
2. Inicia sesión o crea cuenta
3. Click en **"Create new secret key"**
4. **Copia el token** (ejemplo: `sk-proj-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`)
   - ⚠️ Solo se muestra una vez, guárdalo bien

### Paso 3: Configurar Tokens

Abre `config.py` y reemplaza:

```python
TELEGRAM_BOT_TOKEN = 'PEGA_AQUI_EL_TOKEN_DE_BOTFATHER'
OPENAI_API_KEY = 'PEGA_AQUI_EL_TOKEN_DE_OPENAI'
```

### Paso 4: Instalar Dependencias

```bash
pip install -r requirements.txt
```

### Paso 5: Iniciar Todo

**Terminal 1** - Aplicación Flask:
```bash
python app.py
```

**Terminal 2** - Bot de Telegram:
```bash
python telegram_bot.py
```

O usa los scripts:
- Windows: `iniciar_bot.bat`
- Linux/Mac: `chmod +x iniciar_bot.sh && ./iniciar_bot.sh`

### Paso 6: Probar

1. Abre Telegram
2. Busca tu bot (ej: `@control_xpo_bot`)
3. Envía `/start`
4. ¡Listo! 🎉

---

## 📋 Checklist de Configuración

- [ ] Token de Telegram obtenido de @BotFather
- [ ] Token de OpenAI obtenido de platform.openai.com
- [ ] Tokens añadidos en `config.py`
- [ ] Dependencias instaladas (`pip install -r requirements.txt`)
- [ ] Flask corriendo (`python app.py`)
- [ ] Bot corriendo (`python telegram_bot.py`)

---

## 🔍 Verificar que Funciona

1. El bot responde a `/start` ✅
2. Aparece el menú con botones ✅
3. Puedes seleccionar "Alta Viaje" ✅
4. Puedes elegir origen (Algeciras/Valladolid) ✅
5. Puedes enviar una foto ✅
6. El bot procesa la imagen y registra el viaje ✅

---

## ❌ Problemas Comunes

### "No se ha configurado el token del bot"
→ Edita `config.py` y añade `TELEGRAM_BOT_TOKEN`

### "OpenAI API Key no configurada"
→ Edita `config.py` y añade `OPENAI_API_KEY`

### "Error de conexión con el servidor"
→ Asegúrate de que `app.py` esté corriendo en otra terminal

### El bot no responde
→ Verifica que `telegram_bot.py` esté corriendo y sin errores

---

## 📱 Flujo de Uso

```
Usuario → /start
    ↓
Menú: [Alta Viaje] [Ticket Gasoil]
    ↓
Usuario → Clic en "Alta Viaje"
    ↓
Seleccionar: [Algeciras] [Valladolid]
    ↓
Usuario → Envía foto del camión
    ↓
Bot → Procesa con OpenAI Vision API
    ↓
Bot → Extrae: fecha, hora, matrículas
    ↓
Bot → Envía a Flask API
    ↓
Flask → Guarda en base de datos
    ↓
Bot → Confirma al usuario ✅
```

---

## 🔐 Seguridad

- ✅ `config.py` está en `.gitignore` (no se subirá a Git)
- ✅ Los tokens no se exponen en el código
- ✅ Usa variables de entorno en producción

---

## 💡 Tips

1. **Desarrollo Local**: El bot usa **polling**, perfecto para desarrollo
2. **Producción**: Para producción, considera usar webhook con ngrok o un servidor público
3. **Costos OpenAI**: Revisa los precios en https://openai.com/pricing
4. **Logs**: Revisa la terminal del bot para ver qué está pasando

---

## 📞 Ayuda

Si algo no funciona:
1. Revisa los logs en las terminales
2. Verifica que los tokens sean correctos
3. Asegúrate de tener créditos en OpenAI
4. Consulta `INSTRUCCIONES_BOT_TELEGRAM.md` para más detalles
