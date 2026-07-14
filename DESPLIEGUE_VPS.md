# Despliegue LVM en VPS con Ubuntu 24.04 y Nginx

Guía para desplegar la aplicación LVM en un VPS con **Ubuntu 24.04** y **Nginx**, usando **Gunicorn** como servidor WSGI y **systemd** para los servicios.

---

## Requisitos previos

- Un VPS con Ubuntu 24.04 (acceso SSH como **root**).
- Dominio o IP pública apuntando al VPS (opcional: certificado SSL con Let's Encrypt).

En esta guía todo se ejecuta como **root**.

---

## 1. Preparar el servidor

### 1.1 Actualizar el sistema

```bash
apt update && apt upgrade -y
```

### 1.2 Instalar dependencias del sistema

```bash
apt install -y python3 python3-pip python3-venv nginx git
```

---

## 2. Crear directorio de la aplicación

```bash
mkdir -p /var/www/lvm
```

(Si prefieres otro path, por ejemplo `/opt/lvm`, úsalo y ajusta las rutas en los servicios systemd y en Nginx.)

---

## 3. Subir el código al VPS

### Opción A: Clonar desde Git

```bash
cd /var/www/lvm
git clone https://github.com/TU_USUARIO/TU_REPO.git .
# O si ya tienes el repo en el servidor:
# git pull origin main
```

### Opción B: Subir con SCP/SFTP

Desde tu máquina local:

```bash
scp -r /ruta/local/lvm/* root@TU_IP_VPS:/var/www/lvm/
```

Asegúrate de incluir: `app.py`, `telegram_bot.py`, `config.py`, `requirements.txt`, carpeta `templates/`, carpeta `deploy/`, y `.env.example` (no subas `.env` con secretos por la red; créalo en el servidor).

---

## 4. Entorno virtual y dependencias Python

```bash
cd /var/www/lvm
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
deactivate
```

---

## 5. Configurar variables de entorno

```bash
cd /var/www/lvm
cp .env.example .env
nano .env
```

Rellena al menos:

| Variable | Descripción | Ejemplo |
|----------|-------------|---------|
| `SECRET_KEY` | Clave secreta de Flask (genera una aleatoria) | `openssl rand -hex 32` |
| `LVM_PASSWORD` | Contraseña de acceso a la web (usuario: lvm) | Tu contraseña segura |
| `DATABASE_URL` | Base de datos (por defecto SQLite) | `sqlite:///app.db` |
| `FLASK_APP_URL` | URL pública de la app (para el bot) | `https://tudominio.com` o `http://TU_IP` |
| `TELEGRAM_BOT_TOKEN` | Token del bot (opcional) | Desde @BotFather |
| `OPENAI_API_KEY` | API de OpenAI (opcional, para el bot) | Tu clave |

Para generar una `SECRET_KEY`:

```bash
openssl rand -hex 32
```

Protege el archivo:

```bash
chmod 600 .env
```

---

## 6. Inicializar la base de datos

```bash
cd /var/www/lvm
source venv/bin/activate
python3 -c "
from app import app, db
with app.app_context():
    db.create_all()
    print('Base de datos creada.')
"
deactivate
```

Crea el archivo `app.db` en `/var/www/lvm` (o la ruta que tengas en `DATABASE_URL`). Si quieres migrar una BD existente, copia tu `app.db` al servidor en ese directorio.

---

## 7. Servicios systemd

### 7.1 Servicio principal (Gunicorn + Flask)

```bash
cp /var/www/lvm/deploy/lvm.service /etc/systemd/system/
nano /etc/systemd/system/lvm.service
```

Comprueba que `WorkingDirectory`, `Environment="PATH=..."`, `EnvironmentFile` y `ExecStart` coincidan con tu instalación (por defecto `/var/www/lvm`). Si usas otro directorio (por ejemplo `/opt/lvm`), cámbialo en las tres rutas.

```bash
systemctl daemon-reload
systemctl enable lvm
systemctl start lvm
systemctl status lvm
```

### 7.2 (Opcional) Bot de Telegram

Si quieres el bot de Telegram en el servidor (polling):

```bash
cp /var/www/lvm/deploy/lvm-telegram.service /etc/systemd/system/
# Ajusta WorkingDirectory y rutas si no usas /var/www/lvm
systemctl daemon-reload
systemctl enable lvm-telegram
systemctl start lvm-telegram
systemctl status lvm-telegram
```

Si no configuras el bot, la web funcionará igual; solo no tendrás el bot en el VPS.

---

## 8. Nginx

### 8.1 Copiar y editar la configuración

```bash
cp /var/www/lvm/deploy/nginx-lvm.conf /etc/nginx/sites-available/lvm
nano /etc/nginx/sites-available/lvm
```

Sustituye `TU_DOMINIO_O_IP` por tu dominio o por la IP del VPS (por ejemplo `192.168.1.10` o `app.midominio.com`).

### 8.2 Activar el sitio y comprobar

```bash
ln -s /etc/nginx/sites-available/lvm /etc/nginx/sites-enabled/
# Si Nginx tiene un site "default" que molesta, desactívalo:
# rm /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx
```

### 8.3 (Opcional) HTTPS con Let's Encrypt

```bash
apt install -y certbot python3-certbot-nginx
certbot --nginx -d tudominio.com
```

Luego edita de nuevo `/etc/nginx/sites-available/lvm`: descomenta el bloque que redirige HTTP a HTTPS y el bloque `server` con `listen 443 ssl` y las rutas de los certificados que Certbot haya configurado (suelen estar en `/etc/letsencrypt/live/TU_DOMINIO/`). Ajusta `server_name` si es necesario.

En `.env` pon `FLASK_APP_URL=https://tudominio.com` para que el bot use la URL correcta.

---

## 9. Comprobar que todo funciona

1. **Web**: Abre en el navegador `http://TU_IP` o `http://tudominio.com`. Deberías ver la pantalla de login (usuario: `lvm`, contraseña: la que pusiste en `LVM_PASSWORD`).
2. **Logs**:
   - Gunicorn: `journalctl -u lvm -f`
   - Telegram: `journalctl -u lvm-telegram -f`
   - Nginx: `tail -f /var/log/nginx/error.log`

---

## Resumen de comandos útiles

| Acción | Comando |
|--------|---------|
| Reiniciar aplicación | `systemctl restart lvm` |
| Reiniciar bot Telegram | `systemctl restart lvm-telegram` |
| Ver estado | `systemctl status lvm` |
| Ver logs en vivo | `journalctl -u lvm -f` |
| Recargar Nginx | `systemctl reload nginx` |
| Probar config Nginx | `nginx -t` |

---

## Estructura de archivos de despliegue

En el repositorio:

- `deploy/lvm.service` — Servicio systemd para Gunicorn (Flask).
- `deploy/lvm-telegram.service` — Servicio systemd para el bot de Telegram (opcional).
- `deploy/nginx-lvm.conf` — Configuración de Nginx para el sitio.

Rutas por defecto en el servidor: `/var/www/lvm` (puedes cambiarlas a `/opt/lvm` editando los `.service` y la config de Nginx).

---

## Solución de problemas

- **502 Bad Gateway**: Gunicorn no está corriendo o no escucha en `127.0.0.1:5000`. Revisa `systemctl status lvm` y `journalctl -u lvm`.
- **No puedo hacer login**: Comprueba que `LVM_PASSWORD` esté definida en `.env`.
- **Bot no responde**: Verifica `TELEGRAM_BOT_TOKEN` y `FLASK_APP_URL` en `.env`. Si el bot llama a la API en la misma máquina, `FLASK_APP_URL` puede ser `http://127.0.0.1:5000` o la URL pública si el bot está en otro equipo.
- **Permisos denegados en app.db**: Ejecutando como root no debería haber problemas; comprueba que el archivo exista y que la ruta en `DATABASE_URL` sea correcta.

Si sigues estos pasos, la aplicación quedará desplegada en tu VPS con Ubuntu 24.04 y Nginx.
