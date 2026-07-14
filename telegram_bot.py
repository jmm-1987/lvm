"""
Bot de Telegram para Control XPO
Usa polling para desarrollo local (no requiere webhook público)
"""
import logging
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from datetime import datetime
import config
import os
import sys
import json
import re
import base64
from openai import OpenAI

# Configurar logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Estados del usuario para el flujo de conversación
user_states = {}

def extraer_datos_imagen_openai(image_url):
    """
    Extrae datos de la imagen usando OpenAI Vision API
    """
    if not config.OPENAI_API_KEY:
        logger.error("OpenAI API Key no configurada")
        return None
    
    try:
        # Descargar la imagen desde Telegram
        logger.info(f"Descargando imagen desde: {image_url}")
        response = requests.get(image_url, timeout=30)
        response.raise_for_status()
        image_data = response.content
        
        # Crear cliente de OpenAI
        client = OpenAI(api_key=config.OPENAI_API_KEY)
        
        # Convertir imagen a base64
        image_base64 = base64.b64encode(image_data).decode('utf-8')
        
        logger.info("Enviando imagen a OpenAI Vision API...")
        
        # Llamar a OpenAI Vision API
        response = client.chat.completions.create(
            model="gpt-4o-mini",  # Modelo más económico, también puedes usar "gpt-4-vision-preview"
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": """Analiza cuidadosamente la zona inferior trasera del remolque.

Paso 1: Identifica la matrícula blanca situada en la parte inferior derecha. Esta pertenece a la cabeza tractora. Formato: 4 números y 3 letras (ejemplo: 1234 ABC).

Paso 2: Identifica la matrícula roja situada en la parte inferior izquierda. Esta pertenece al remolque. Formato: R 1234 ABC.

Paso 3: Lee los caracteres exactamente como aparecen.

Devuelve SOLO:

{
    "matricula_cabeza": "...",
    "matricula_remolque": "..."
}"""
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{image_base64}"
                            }
                        }
                    ]
                }
            ],
            max_tokens=300,
            temperature=0.1  # Menor temperatura para respuestas más precisas
        )
        
        # Extraer JSON de la respuesta
        content = response.choices[0].message.content.strip()
        logger.info(f"Respuesta de OpenAI: {content}")
        
        # Intentar parsear JSON
        # Primero intentar parsear directamente
        try:
            datos = json.loads(content)
            return datos
        except json.JSONDecodeError:
            # Si falla, buscar JSON en la respuesta
            json_match = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', content, re.DOTALL)
            if json_match:
                datos = json.loads(json_match.group())
                return datos
            else:
                logger.error(f"No se pudo extraer JSON de la respuesta: {content}")
                return None
            
    except Exception as e:
        logger.error(f"Error al procesar imagen con OpenAI: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        return None

def agregar_boton_cancelar(keyboard):
    """Agrega el botón Cancelar siempre en la última fila del teclado"""
    # Si el último elemento ya es Cancelar, no agregarlo de nuevo
    if keyboard and len(keyboard) > 0:
        ultima_fila = keyboard[-1]
        if len(ultima_fila) == 1 and ultima_fila[0].callback_data == "cancelar":
            return keyboard
    
    # Agregar botón Cancelar en la última fila
    keyboard.append([InlineKeyboardButton("❌ Cancelar", callback_data="cancelar")])
    return keyboard

def crear_menu_principal():
    """Crea el menú principal con botones"""
    keyboard = [
        [
            InlineKeyboardButton("🚛 Alta Viaje", callback_data="alta_viaje"),
            InlineKeyboardButton("⛽ Ticket Gasoil", callback_data="ticket_gasoil")
        ]
    ]
    keyboard = agregar_boton_cancelar(keyboard)
    return InlineKeyboardMarkup(keyboard)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /start - Muestra el menú principal"""
    reply_markup = crear_menu_principal()
    
    await update.message.reply_text(
        "🤖 *Bot de Control XPO*\n\n"
        "Selecciona una opción:",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Maneja los botones del menú"""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    
    if query.data == "alta_viaje":
        # Preguntar por el origen
        keyboard = [
            [
                InlineKeyboardButton("📍 Algeciras", callback_data="origen_algeciras"),
                InlineKeyboardButton("📍 Valladolid", callback_data="origen_valladolid")
            ]
        ]
        keyboard = agregar_boton_cancelar(keyboard)
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        user_states[user_id] = {'step': 'seleccionando_origen'}
        
        await query.edit_message_text(
            "🚛 *Alta de Viaje*\n\n"
            "Selecciona el origen del viaje:",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    
    elif query.data == "ticket_gasoil":
        keyboard = []
        keyboard = agregar_boton_cancelar(keyboard)
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            "⛽ *Ticket de Gasoil*\n\n"
            "Esta funcionalidad está pendiente de desarrollo.",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    
    elif query.data.startswith("origen_"):
        origen = query.data.replace("origen_", "").capitalize()
        user_states[user_id] = {
            'step': 'esperando_foto',
            'origen': origen
        }
        
        keyboard = []
        keyboard = agregar_boton_cancelar(keyboard)
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            f"📍 Origen seleccionado: *{origen}*\n\n"
            "📸 Ahora envía una foto de la parte trasera del camión.\n\n"
            "La foto debe mostrar:\n"
            "• Matrícula de la cabeza tractora\n"
            "• Matrícula del remolque\n\n"
            "ℹ️ La fecha y hora se registrarán automáticamente al momento de enviar la foto.",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    
    elif query.data == "cancelar":
        # Limpiar estado del usuario
        if user_id in user_states:
            del user_states[user_id]
        
        # Mostrar menú principal
        reply_markup = crear_menu_principal()
        await query.edit_message_text(
            "🤖 *Bot de Control XPO*\n\n"
            "Selecciona una opción:",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Maneja las fotos enviadas por el usuario"""
    user_id = update.message.from_user.id
    
    if user_id not in user_states or user_states[user_id].get('step') != 'esperando_foto':
        await update.message.reply_text(
            "❌ Por favor, primero selecciona 'Alta Viaje' y elige el origen."
        )
        return
    
    try:
        # Obtener la foto de mayor resolución
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        file_url = file.file_path
        
        await update.message.reply_text("⏳ Procesando imagen con IA...")
        
        # Extraer datos usando OpenAI
        datos_extraidos = extraer_datos_imagen_openai(file_url)
        
        if not datos_extraidos:
            await update.message.reply_text(
                "❌ Error al procesar la imagen. Por favor, intenta de nuevo con una foto más clara."
            )
            # Limpiar estado del usuario
            if user_id in user_states:
                del user_states[user_id]
            return
    except Exception as e:
        logger.error(f"Error al obtener o procesar la foto: {str(e)}")
        await update.message.reply_text(
            "❌ Error al procesar la foto. Por favor, intenta de nuevo."
        )
        # Limpiar estado del usuario
        if user_id in user_states:
            del user_states[user_id]
        return
    
    # Preparar datos para enviar a la API
    origen = user_states[user_id].get('origen')
    
    # Extraer y validar matrículas
    matricula_cabeza = datos_extraidos.get('matricula_cabeza', '').strip()
    matricula_remolque = datos_extraidos.get('matricula_remolque', '').strip()
    
    # Validar que las matrículas no sean "no_detectado" o estén vacías
    if not matricula_cabeza or matricula_cabeza.lower() == 'no_detectado':
        await update.message.reply_text(
            "❌ No se pudo detectar la matrícula de la cabeza tractora en la imagen.\n\n"
            "Por favor, intenta de nuevo con una foto más clara donde se vea bien la matrícula."
        )
        # Limpiar estado del usuario
        if user_id in user_states:
            del user_states[user_id]
        return
    
    if not matricula_remolque or matricula_remolque.lower() == 'no_detectado':
        await update.message.reply_text(
            "❌ No se pudo detectar la matrícula del remolque en la imagen.\n\n"
            "Por favor, intenta de nuevo con una foto más clara donde se vea bien la matrícula."
        )
        # Limpiar estado del usuario
        if user_id in user_states:
            del user_states[user_id]
        return
    
    # Usar fecha y hora actuales (momento en que se envía la foto)
    fecha_actual = datetime.now().strftime('%d/%m/%Y')
    hora_actual = datetime.now().strftime('%H:%M')
    
    # Enviar datos a la API Flask
    api_url = f"{config.FLASK_APP_URL}/api/telegram/webhook"
    
    payload = {
        'origen': origen,
        'imagen_url': file_url,
        'fecha': fecha_actual,
        'hora': hora_actual,
        'matricula_cabeza': matricula_cabeza,
        'matricula_remolque': matricula_remolque,
        'telegram_user_id': user_id,
        'telegram_username': update.message.from_user.username
    }
    
    try:
        response = requests.post(api_url, json=payload, timeout=10)
        
        # Verificar si la respuesta es un redirect (302) o HTML en lugar de JSON
        content_type = response.headers.get('Content-Type', '')
        if 'text/html' in content_type or response.status_code == 302:
            logger.error(f"El endpoint devolvió HTML/Redirect en lugar de JSON. Status: {response.status_code}")
            await update.message.reply_text(
                "❌ Error: El servidor no está respondiendo correctamente. "
                "Verifica que la aplicación Flask esté corriendo y que el endpoint esté configurado."
            )
            return
        
        if response.status_code == 200:
            try:
                resultado = response.json()
                if resultado.get('success'):
                    # Usar texto plano para evitar problemas con caracteres especiales en Markdown
                    mensaje = (
                        f"✅ Viaje registrado correctamente\n\n"
                        f"📅 Fecha: {fecha_actual}\n"
                        f"🕐 Hora: {hora_actual}\n"
                        f"📍 Origen: {origen}\n"
                        f"🚛 Matrícula cabeza: {matricula_cabeza}\n"
                        f"🚚 Matrícula remolque: {matricula_remolque}"
                    )
                    await update.message.reply_text(mensaje)
                else:
                    error_msg = resultado.get('error', 'Error desconocido')
                    await update.message.reply_text(
                        f"❌ Error al registrar el viaje: {error_msg}"
                    )
            except json.JSONDecodeError as e:
                logger.error(f"Error al parsear JSON de la respuesta: {str(e)}. Respuesta: {response.text[:200]}")
                await update.message.reply_text(
                    "❌ Error: El servidor no devolvió una respuesta válida."
                )
        else:
            logger.error(f"Error HTTP {response.status_code}: {response.text[:200]}")
            await update.message.reply_text(
                f"❌ Error de conexión con el servidor (código {response.status_code})"
            )
    
    except requests.exceptions.RequestException as e:
        logger.error(f"Error al enviar datos a la API: {str(e)}")
        await update.message.reply_text(
            "❌ Error de conexión. Asegúrate de que la aplicación Flask esté corriendo."
        )
    except Exception as e:
        logger.error(f"Error inesperado al procesar foto: {str(e)}")
        await update.message.reply_text(
            "❌ Ocurrió un error inesperado. Por favor, intenta de nuevo."
        )
    finally:
        # Limpiar estado del usuario siempre, incluso si hay errores
        if user_id in user_states:
            del user_states[user_id]

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Maneja mensajes de texto"""
    await update.message.reply_text(
        "Por favor, usa los botones del menú o envía /start para comenzar."
    )

def main():
    """Función principal para iniciar el bot"""
    # Verificar que el token esté configurado
    if not config.TELEGRAM_BOT_TOKEN:
        print("❌ ERROR: No se ha configurado el token del bot de Telegram")
        print("Por favor, configura TELEGRAM_BOT_TOKEN en el archivo .env")
        sys.exit(1)
    
    # Crear aplicación
    application = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()
    
    # Añadir handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    
    # Agregar manejador de errores para Conflict
    async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Maneja errores del bot"""
        error = context.error
        if isinstance(error, Exception):
            if "Conflict" in str(error) or "terminated by other getUpdates request" in str(error):
                logger.warning("⚠️  Conflicto detectado: Otra instancia del bot está ejecutándose. "
                              "Asegúrate de que solo una instancia esté corriendo.")
                # No hacer nada, solo loguear el warning
            else:
                logger.error(f"Error no manejado: {error}", exc_info=error)
    
    application.add_error_handler(error_handler)
    
    # Iniciar bot con polling
    print("🤖 Bot iniciado. Usando polling (no requiere webhook público)")
    print(f"📱 Busca tu bot en Telegram y envía /start")
    print("⏹️  Presiona Ctrl+C para detener el bot")
    
    try:
        application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
    except Exception as e:
        if "Conflict" in str(e):
            logger.warning("⚠️  No se pudo iniciar el bot: Ya hay otra instancia ejecutándose.")
            print("⚠️  ADVERTENCIA: Parece que ya hay otra instancia del bot ejecutándose.")
            print("   Cierra cualquier otra instancia antes de iniciar esta.")
        else:
            logger.error(f"Error al iniciar el bot: {e}")
            raise

if __name__ == '__main__':
    main()
