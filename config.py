"""
Configuración desde variables de entorno (.env).
Las claves y tokens se leen de .env; no incluir valores reales aquí.
"""
import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    _env_path = Path(__file__).resolve().parent / ".env"
    load_dotenv(dotenv_path=_env_path)
except ImportError:
    pass  # Sin python-dotenv: usar solo variables de entorno del sistema

# Token del Bot de Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

# Token de OpenAI API
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

# URL base de la aplicación Flask
FLASK_APP_URL = (os.getenv("FLASK_APP_URL", "http://localhost:5000") or "http://localhost:5000").strip()

# Base de datos (por defecto SQLite en el directorio del proyecto)
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///app.db").strip() or "sqlite:///app.db"

# Clave secreta de Flask (obligatoria en producción)
SECRET_KEY = os.getenv("SECRET_KEY", "cambiar-en-produccion").strip() or "cambiar-en-produccion"

# Contraseña de login de la aplicación (usuario: lvm)
LVM_PASSWORD = os.getenv("LVM_PASSWORD", "").strip()
