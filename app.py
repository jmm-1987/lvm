import os
import sqlite3
import threading
import shutil
import json
from pathlib import Path

from flask import Flask, render_template, request, redirect, url_for, flash, session, send_file, Response, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from sqlalchemy.exc import IntegrityError
from sqlalchemy import inspect, text
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta
import csv
import io
import zipfile
import tempfile
import paramiko
import re
import calendar
import PyPDF2
from openpyxl import Workbook
from openpyxl import load_workbook
from reportlab.lib.pagesizes import A4, letter, landscape
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib import colors

# Cargar configuración desde .env (vía config)
import config as app_config

app = Flask(__name__)
app.secret_key = app_config.SECRET_KEY

# Configuración de la base de datos desde .env
_database_url = app_config.DATABASE_URL
if _database_url.startswith("sqlite:///"):
    # Ruta relativa: resolver respecto al directorio del proyecto
    _db_name = _database_url.replace("sqlite:///", "").strip() or "app.db"
    _db_path = Path(__file__).resolve().parent / _db_name
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{_db_path}"
else:
    app.config["SQLALCHEMY_DATABASE_URI"] = _database_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

# Añadir datetime al contexto global de Jinja2
@app.context_processor
def inject_datetime():
    return dict(datetime=datetime)

# Modelo de Cuenta
class Cuenta(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    cuenta = db.Column(db.String(50), nullable=False)  # Ahora almacena el número completo
    nombre = db.Column(db.String(100), nullable=False)
    tipo = db.Column(db.String(20), nullable=False, default='normal')  # 'normal' o 'contrapartida'
    anotaciones = db.Column(db.String(255), nullable=True)
    cuenta_asociada_id = db.Column(db.Integer, db.ForeignKey('cuenta.id'), nullable=True)
    cuenta_asociada = db.relationship('Cuenta', remote_side=[id])

# Modelo de Movimiento
class Movimiento(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    tipo = db.Column(db.String(20), nullable=False)
    fecha_trabajo = db.Column(db.String(20), nullable=False)
    fecha_factura = db.Column(db.String(20), nullable=False)
    num_factura = db.Column(db.String(50), nullable=False)
    base_imponible = db.Column(db.Float, nullable=False)
    total = db.Column(db.Float, nullable=False)

# Modelo de MovimientoConcepto (relación muchos a muchos entre Movimiento y Cuenta)
class MovimientoConcepto(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    movimiento_id = db.Column(db.Integer, db.ForeignKey('movimiento.id'), nullable=False)
    cuenta_id = db.Column(db.Integer, db.ForeignKey('cuenta.id'), nullable=False)
    importe = db.Column(db.Float, nullable=False)
    concepto = db.Column(db.String(100), nullable=True)
    contrapartida_id = db.Column(db.Integer, db.ForeignKey('cuenta.id'), nullable=True) # Nuevo campo para la contrapartida
    
    # Campos para control de declaración IVA
    declarado = db.Column(db.Boolean, nullable=False, default=False) # Marca si ha sido declarado
    trimestre_declaracion = db.Column(db.String(20), nullable=True) # Trimestre en que fue declarado (ej: "2025Q3")
    
    cuenta = db.relationship('Cuenta', foreign_keys=[cuenta_id])
    contrapartida = db.relationship('Cuenta', foreign_keys=[contrapartida_id]) # Relación con la cuenta de contrapartida

Movimiento.conceptos = db.relationship('MovimientoConcepto', backref='movimiento', cascade='all, delete-orphan')

# Modelo de Viaje XPO
class ViajeXPO(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    fecha = db.Column(db.String(20), nullable=False)  # Formato DD/MM/YYYY
    hora = db.Column(db.String(10), nullable=True)  # Formato HH:MM
    origen = db.Column(db.String(50), nullable=False)  # Algeciras o Valladolid
    matricula_cabeza = db.Column(db.String(20), nullable=False)  # Matrícula de la cabeza tractora
    matricula_remolque = db.Column(db.String(20), nullable=False)  # Matrícula del remolque
    manifiesto = db.Column(db.String(20), nullable=True)  # Número de manifiesto (8 dígitos)
    facturado = db.Column(db.String(10), nullable=True, default='no')  # 'si' o 'no'
    fecha_creacion = db.Column(db.String(20), nullable=False, default=lambda: datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    origen_telegram = db.Column(db.Boolean, nullable=False, default=False)  # Indica si viene del bot de Telegram

# Modelo de Camión (análisis de explotación)
class Camion(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    matricula = db.Column(db.String(20), nullable=False, unique=True)
    alias = db.Column(db.String(100), nullable=True)
    activo = db.Column(db.Boolean, nullable=False, default=True)

    def etiqueta(self):
        if self.alias:
            return f"{self.matricula} ({self.alias})"
        return self.matricula

# Precio oficial del gasoil por mes (referencia para desviación)
class PrecioGasoilOficial(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    anio = db.Column(db.Integer, nullable=False)
    mes = db.Column(db.Integer, nullable=False)
    precio = db.Column(db.Float, nullable=False)
    __table_args__ = (db.UniqueConstraint('anio', 'mes', name='uq_precio_gasoil_oficial_mes'),)

# Registro mensual de ingresos por camión (km, facturación e incremento)
class RegistroIngreso(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    camion_id = db.Column(db.Integer, db.ForeignKey('camion.id'), nullable=False)
    anio = db.Column(db.Integer, nullable=False)
    mes = db.Column(db.Integer, nullable=False)
    km_realizados = db.Column(db.Float, nullable=False, default=0)
    ingreso_ruta = db.Column(db.Float, nullable=False, default=0)
    ingreso_chofer_adicional = db.Column(db.Float, nullable=False, default=0)
    ingreso_extra = db.Column(db.Float, nullable=False, default=0)
    ingreso_autopista = db.Column(db.Float, nullable=False, default=0)
    incremento_combustible = db.Column(db.Float, nullable=False, default=0)
    observaciones = db.Column(db.String(255), nullable=True)
    __table_args__ = (db.UniqueConstraint('camion_id', 'anio', 'mes', name='uq_registro_ingreso_camion_mes'),)

# Catálogo de rutas con km por viaje
class Ruta(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(120), nullable=False, unique=True)
    km = db.Column(db.Float, nullable=False, default=0)
    observaciones = db.Column(db.String(255), nullable=True)
    activa = db.Column(db.Boolean, nullable=False, default=True)

    def etiqueta(self):
        return f"{self.nombre} ({self.km:g} km)"

# Tramos de un ingreso: ruta × número de viajes
class RegistroIngresoTramo(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    ingreso_id = db.Column(db.Integer, db.ForeignKey('registro_ingreso.id'), nullable=False)
    ruta_id = db.Column(db.Integer, db.ForeignKey('ruta.id'), nullable=False)
    num_viajes = db.Column(db.Float, nullable=False, default=0)

    def km_tramo(self):
        km_ruta = self.ruta.km if self.ruta else 0
        return (km_ruta or 0) * (self.num_viajes or 0)

# Varios registros de gasoil por camión, gasolinera y tipo
class RegistroGasoil(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    camion_id = db.Column(db.Integer, db.ForeignKey('camion.id'), nullable=False)
    anio = db.Column(db.Integer, nullable=False)
    mes = db.Column(db.Integer, nullable=False)
    marca_gasolinera = db.Column(db.String(80), nullable=False, default='')
    tipo_gasoil = db.Column(db.String(80), nullable=False, default='')
    litros = db.Column(db.Float, nullable=False, default=0)
    gasto_con_iva = db.Column(db.Float, nullable=False, default=0)
    gasto_sin_iva = db.Column(db.Float, nullable=False, default=0)
    bonificacion = db.Column(db.Float, nullable=False, default=0)
    gasto_neto = db.Column(db.Float, nullable=False, default=0)
    gasto_addblue = db.Column(db.Float, nullable=False, default=0)
    iva_porcentaje = db.Column(db.Float, nullable=False, default=21)
    observaciones = db.Column(db.String(255), nullable=True)

# Tabla antigua (un registro mixto); se mantiene solo para migrar datos ya guardados
class RegistroAnalisis(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    camion_id = db.Column(db.Integer, db.ForeignKey('camion.id'), nullable=False)
    anio = db.Column(db.Integer, nullable=False)
    mes = db.Column(db.Integer, nullable=False)
    km_realizados = db.Column(db.Float, nullable=False, default=0)
    ingreso_ruta = db.Column(db.Float, nullable=False, default=0)
    ingreso_chofer_adicional = db.Column(db.Float, nullable=False, default=0)
    ingreso_extra = db.Column(db.Float, nullable=False, default=0)
    ingreso_autopista = db.Column(db.Float, nullable=False, default=0)
    gasto_gasoil_con_iva = db.Column(db.Float, nullable=False, default=0)
    gasto_gasoil_sin_iva = db.Column(db.Float, nullable=False, default=0)
    bonificacion_gasoil = db.Column(db.Float, nullable=False, default=0)
    gasto_gasoil = db.Column(db.Float, nullable=False, default=0)
    litros_gasoil = db.Column(db.Float, nullable=False, default=0)
    gasto_addblue = db.Column(db.Float, nullable=False, default=0)
    incremento_combustible = db.Column(db.Float, nullable=False, default=0)
    observaciones = db.Column(db.String(255), nullable=True)

Camion.ingresos = db.relationship('RegistroIngreso', backref='camion', cascade='all, delete-orphan')
Camion.repostajes = db.relationship('RegistroGasoil', backref='camion', cascade='all, delete-orphan')
RegistroIngreso.tramos = db.relationship('RegistroIngresoTramo', backref='ingreso', cascade='all, delete-orphan')
Ruta.tramos = db.relationship('RegistroIngresoTramo', backref='ruta')

# Bonus calidad y suplemento HVO: un importe al mes, se reparte por km
class RegistroReparto(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    anio = db.Column(db.Integer, nullable=False)
    mes = db.Column(db.Integer, nullable=False)
    tipo = db.Column(db.String(40), nullable=False)
    importe = db.Column(db.Float, nullable=False, default=0)
    observaciones = db.Column(db.String(255), nullable=True)
    __table_args__ = (db.UniqueConstraint('anio', 'mes', 'tipo', name='uq_reparto_anio_mes_tipo'),)

MESES_NOMBRE = {
    1: 'Enero', 2: 'Febrero', 3: 'Marzo', 4: 'Abril', 5: 'Mayo', 6: 'Junio',
    7: 'Julio', 8: 'Agosto', 9: 'Septiembre', 10: 'Octubre', 11: 'Noviembre', 12: 'Diciembre'
}

IVA_GASOIL = 0.21
TIPOS_IVA = [21, 10, 4, 0]
MARCAS_GASOLINERA = ['SOLRED', 'VALCARCE', 'GUILLEN', 'DST', 'NIVES', 'VYS', 'Cepsa', 'BP', 'Galp', 'Shell', 'Petronor', 'Ballenoil', 'Plenoil', 'Disa', 'Meroil', 'Otras']
TIPOS_GASOIL = ['Gasóleo A', 'HVO', 'Gasóleo A Premium', 'Gasóleo B', 'Gasóleo C']
TIPOS_REPARTO = [
    ('bonus_calidad', 'Bonus calidad'),
    ('suplemento_hvo', 'Suplemento HVO'),
]
_esquema_analisis_ok = False

# Eliminar el modelo Usuario y la tabla de usuarios
# Definir un usuario en memoria para Flask-Login
class UsuarioFalso(UserMixin):
    def __init__(self, id):
        self.id = id
        self.username = 'lvm'

# Configuración de Flask-Login
login_manager = LoginManager()
login_manager.login_view = 'login'
login_manager.init_app(app)

@login_manager.user_loader
def load_user(user_id):
    if user_id == 'lvm':
        return UsuarioFalso('lvm')
    return None

# Ruta de login
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        lvm_password = app_config.LVM_PASSWORD
        if username == 'lvm' and lvm_password and password == lvm_password:
            user = UsuarioFalso('lvm')
            login_user(user)
            return redirect(url_for('index'))
        else:
            flash('Usuario o contraseña incorrectos', 'error')
    return render_template('login.html')

# Ruta de logout
@app.route('/logout', methods=['GET', 'POST'])
@login_required
def logout():
    subir_db_a_ftp()  # Subir la base de datos antes de cerrar sesión
    logout_user()
    return redirect(url_for('login'))

# Proteger todas las rutas excepto login, static y API de Telegram
@app.before_request
def require_login():
    # Excluir rutas públicas: login, static files y API de Telegram
    excluded_endpoints = ('login', 'static', 'telegram_webhook')
    if request.endpoint not in excluded_endpoints and not current_user.is_authenticated:
        return redirect(url_for('login'))
    if request.endpoint and 'analisis' in request.endpoint:
        asegurar_esquema_analisis()

@app.route('/')
def index():
    return render_template('index.html')

# Vistas para cuentas
@app.route('/cuentas')
def listar_cuentas():
    cuentas_normales = Cuenta.query.filter_by(tipo='normal').all()
    cuentas_contrapartida = Cuenta.query.filter_by(tipo='contrapartida').all()
    return render_template('cuentas.html', cuentas_normales=cuentas_normales, cuentas_contrapartida=cuentas_contrapartida)

@app.route('/cuentas/nueva', methods=['GET', 'POST'])
def nueva_cuenta():
    cuentas_normales = Cuenta.query.filter_by(tipo='normal').all()
    if request.method == 'POST':
        try:
            cuenta = request.form['cuenta']
            nombre = request.form['nombre']
            tipo = request.form['tipo']
            anotaciones = request.form.get('anotaciones', '')
            cuenta_asociada_id = request.form.get('cuenta_asociada_id') if tipo == 'contrapartida' else None
            nueva = Cuenta(cuenta=cuenta, nombre=nombre, tipo=tipo, anotaciones=anotaciones, cuenta_asociada_id=cuenta_asociada_id)
            db.session.add(nueva)
            commit_seguro("crear cuenta")
            flash('Cuenta creada correctamente.', 'success')
            return redirect(url_for('listar_cuentas'))
        except Exception as e:
            db.session.rollback()
            flash(f'Error al crear la cuenta: {str(e)}', 'error')
            return render_template('cuenta_form.html', cuentas_normales=cuentas_normales)
    return render_template('cuenta_form.html', cuentas_normales=cuentas_normales)

@app.route('/cuentas/editar/<int:id>', methods=['GET', 'POST'])
def editar_cuenta(id):
    cuenta = Cuenta.query.get_or_404(id)
    cuentas_normales = Cuenta.query.filter_by(tipo='normal').all()
    if request.method == 'POST':
        try:
            cuenta.cuenta = request.form['cuenta']
            cuenta.nombre = request.form['nombre']
            cuenta.tipo = request.form['tipo']
            cuenta.anotaciones = request.form.get('anotaciones', '')
            cuenta.cuenta_asociada_id = request.form.get('cuenta_asociada_id') if cuenta.tipo == 'contrapartida' else None
            commit_seguro("editar cuenta")
            flash('Cuenta actualizada correctamente.', 'success')
            return redirect(url_for('listar_cuentas'))
        except Exception as e:
            db.session.rollback()
            flash(f'Error al actualizar la cuenta: {str(e)}', 'error')
            return render_template('cuenta_form.html', cuenta=cuenta, cuentas_normales=cuentas_normales)
    return render_template('cuenta_form.html', cuenta=cuenta, cuentas_normales=cuentas_normales)

@app.route('/cuentas/borrar/<int:id>', methods=['POST'])
def borrar_cuenta(id):
    try:
        cuenta = Cuenta.query.get_or_404(id)
        # Comprobar si la cuenta está asociada a algún concepto de movimiento
        conceptos = MovimientoConcepto.query.filter((MovimientoConcepto.cuenta_id == id) | (MovimientoConcepto.contrapartida_id == id)).first()
        if conceptos:
            flash('No se puede borrar la cuenta porque está asociada a movimientos.', 'error')
            return redirect(url_for('listar_cuentas'))
        db.session.delete(cuenta)
        commit_seguro("borrar cuenta")
        flash('Cuenta borrada correctamente.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error al borrar la cuenta: {str(e)}', 'error')
    return redirect(url_for('listar_cuentas'))

# Vistas para movimientos
@app.route('/movimientos', methods=['GET', 'POST'])
def listar_movimientos():
    # Obtener parámetros de filtro
    tipo_filtro = request.form.get('tipo_filtro', 'rango') if request.method == 'POST' else 'rango'
    año = request.form.get('año', str(datetime.today().year)) if request.method == 'POST' else str(datetime.today().year)
    trimestre = request.form.get('trimestre', '') if request.method == 'POST' else ''
    mes = request.form.get('mes', '') if request.method == 'POST' else ''
    mes_desde = request.form.get('mes_desde', '') if request.method == 'POST' else ''
    año_desde = request.form.get('año_desde', str(datetime.today().year)) if request.method == 'POST' else str(datetime.today().year)
    mes_hasta = request.form.get('mes_hasta', '') if request.method == 'POST' else ''
    año_hasta = request.form.get('año_hasta', str(datetime.today().year)) if request.method == 'POST' else str(datetime.today().year)
    
    # Calcular fechas según el tipo de filtro
    fecha_desde_default = ''
    fecha_hasta_default = ''
    
    if tipo_filtro == 'trimestre' and trimestre:
        año_int = int(año)
        if trimestre == 'Q1':
            fecha_desde_default = f"{año_int}-01-01"
            fecha_hasta_default = f"{año_int}-03-31"
        elif trimestre == 'Q2':
            fecha_desde_default = f"{año_int}-04-01"
            fecha_hasta_default = f"{año_int}-06-30"
        elif trimestre == 'Q3':
            fecha_desde_default = f"{año_int}-07-01"
            fecha_hasta_default = f"{año_int}-09-30"
        elif trimestre == 'Q4':
            fecha_desde_default = f"{año_int}-10-01"
            fecha_hasta_default = f"{año_int}-12-31"
    elif tipo_filtro == 'mes' and mes:
        año_int = int(año)
        mes_int = int(mes)
        fecha_desde_default = f"{año_int}-{mes_int:02d}-01"
        # Calcular último día del mes
        if mes_int in [1, 3, 5, 7, 8, 10, 12]:
            ultimo_dia = 31
        elif mes_int in [4, 6, 9, 11]:
            ultimo_dia = 30
        else:  # febrero
            ultimo_dia = 29 if año_int % 4 == 0 and (año_int % 100 != 0 or año_int % 400 == 0) else 28
        fecha_hasta_default = f"{año_int}-{mes_int:02d}-{ultimo_dia:02d}"
    elif tipo_filtro == 'rango' and mes_desde and año_desde and mes_hasta and año_hasta:
        año_desde_int = int(año_desde)
        mes_desde_int = int(mes_desde)
        año_hasta_int = int(año_hasta)
        mes_hasta_int = int(mes_hasta)
        
        # Fecha desde: primer día del mes desde
        fecha_desde_default = f"{año_desde_int}-{mes_desde_int:02d}-01"
        
        # Fecha hasta: último día del mes hasta
        if mes_hasta_int in [1, 3, 5, 7, 8, 10, 12]:
            ultimo_dia = 31
        elif mes_hasta_int in [4, 6, 9, 11]:
            ultimo_dia = 30
        else:  # febrero
            ultimo_dia = 29 if año_hasta_int % 4 == 0 and (año_hasta_int % 100 != 0 or año_hasta_int % 400 == 0) else 28
        fecha_hasta_default = f"{año_hasta_int}-{mes_hasta_int:02d}-{ultimo_dia:02d}"
    else:
        # Por defecto, mostrar los últimos 6 meses
        hoy = datetime.today()
        # Calcular fecha de hace 6 meses
        if hoy.month > 6:
            fecha_desde_default = f"{hoy.year}-{hoy.month - 6:02d}-01"
            mes_desde = str(hoy.month - 6)
            año_desde = str(hoy.year)
        else:
            año_anterior = hoy.year - 1
            mes_anterior = hoy.month + 6
            fecha_desde_default = f"{año_anterior}-{mes_anterior:02d}-01"
            mes_desde = str(mes_anterior)
            año_desde = str(año_anterior)
        
        fecha_hasta_default = hoy.strftime('%Y-%m-%d')
        mes_hasta = str(hoy.month)
        año_hasta = str(hoy.year)
    
    # Filtrar movimientos por fecha si se especificó un filtro
    if fecha_desde_default and fecha_hasta_default:
        # Convertir fechas para comparación
        fecha_inicio_dt, fecha_fin_dt = convertir_fechas_para_filtro(fecha_desde_default, fecha_hasta_default)
        
        # Obtener todos los movimientos y filtrar por fecha
        todos_movimientos = db.session.query(Movimiento).all()
        movimientos_filtrados = []
        
        for mov in todos_movimientos:
            fecha_movimiento = parsear_fecha_robusto(mov.fecha_factura)
            if fecha_movimiento and fecha_inicio_dt <= fecha_movimiento <= fecha_fin_dt:
                movimientos_filtrados.append(mov)
        
        # Ordenar por fecha de factura (de más reciente a más antigua)
        movimientos = sorted(movimientos_filtrados, 
                           key=lambda x: parsear_fecha_robusto(x.fecha_factura) or datetime.min, 
                           reverse=True)
    else:
        # Sin filtro de fecha, mostrar todos ordenados por fecha
        movimientos = db.session.query(Movimiento).order_by(
            db.func.strftime('%Y-%m-%d', 
                db.func.substr(Movimiento.fecha_factura, 7, 4) + '-' + 
                db.func.substr(Movimiento.fecha_factura, 4, 2) + '-' + 
                db.func.substr(Movimiento.fecha_factura, 1, 2)
            ).desc()
        ).all()
    
    # Preparar datos de conceptos y contrapartidas
    conceptos_por_mov = {}
    contrapartida_por_mov = {}
    for mov in movimientos:
        conceptos = MovimientoConcepto.query.filter_by(movimiento_id=mov.id).all()
        conceptos_por_mov[mov.id] = conceptos
        # Tomar la contrapartida de la primera línea (todas deben ser iguales)
        if conceptos:
            contrapartida_id = getattr(conceptos[0], 'contrapartida_id', None)
            contrapartida = Cuenta.query.get(contrapartida_id) if contrapartida_id else None
        else:
            contrapartida = None
        contrapartida_por_mov[mov.id] = contrapartida
    
    # Calcular total de movimientos en el sistema
    total_movimientos_sistema = db.session.query(Movimiento).count()
    
    return render_template('movimientos.html', 
                         movimientos=movimientos, 
                         conceptos_por_mov=conceptos_por_mov, 
                         contrapartida_por_mov=contrapartida_por_mov, 
                         fecha_desde_default=fecha_desde_default, 
                         fecha_hasta_default=fecha_hasta_default,
                         tipo_filtro=tipo_filtro,
                         año=año,
                         trimestre=trimestre,
                         mes=mes,
                         mes_desde=mes_desde,
                         año_desde=año_desde,
                         mes_hasta=mes_hasta,
                         año_hasta=año_hasta,
                         total_movimientos_sistema=total_movimientos_sistema)

@app.route('/movimientos/nuevo', methods=['GET', 'POST'])
def nuevo_movimiento():
    cuentas_normales = Cuenta.query.filter_by(tipo='normal').all()
    cuentas_contrapartida = Cuenta.query.filter_by(tipo='contrapartida').all()
    if request.method == 'POST':
        datos = request.form
        # Validar que el número de factura no esté duplicado
        num_factura = datos['num_factura']
        existe = Movimiento.query.filter_by(num_factura=num_factura).first()
        if existe:
            flash('Ya existe un movimiento con ese número de factura.', 'error')
            return render_template('movimiento_form.html', cuentas_normales=cuentas_normales, cuentas_contrapartida=cuentas_contrapartida, movimiento=None, conceptos=None)
        # Validar que todas las contrapartidas sean iguales
        contrapartidas = set()
        idx = 0
        while f'contrapartida_{idx}' in datos:
            contrapartidas.add(datos[f'contrapartida_{idx}'])
            idx += 1
        if len(contrapartidas) > 1:
            flash('Todas las líneas deben tener la misma cuenta de contrapartida.', 'error')
            return render_template('movimiento_form.html', cuentas_normales=cuentas_normales, cuentas_contrapartida=cuentas_contrapartida, movimiento=None, conceptos=None)
        # Guardar el movimiento
        nuevo = Movimiento(
            tipo=datos['tipo'],
            fecha_trabajo=datos['fecha_trabajo'],
            fecha_factura=datos['fecha_factura'],
            num_factura=datos['num_factura'],
            base_imponible=float(datos['base_imponible']) if 'base_imponible' in datos else 0,
            total=float(datos['total'])
        )
        db.session.add(nuevo)
        db.session.flush()
        idx = 0
        while f'cuenta_{idx}' in datos:
            cuenta_id = int(datos[f'cuenta_{idx}'])
            contrapartida_id = int(datos[f'contrapartida_{idx}'])
            importe = float(datos[f'importe_{idx}'])
            concepto = datos.get(f'concepto_{idx}', '')
            concepto_obj = MovimientoConcepto(movimiento_id=nuevo.id, cuenta_id=cuenta_id, importe=importe, concepto=concepto)
            # Guardar la contrapartida como campo adicional si lo necesitas
            concepto_obj.contrapartida_id = contrapartida_id
            db.session.add(concepto_obj)
            idx += 1
        try:
            commit_seguro("crear movimiento")
            flash('Movimiento creado correctamente.', 'success')
            return redirect(url_for('listar_movimientos'))
        except Exception as e:
            db.session.rollback()
            flash(f'Error al guardar el movimiento: {str(e)}', 'error')
            return render_template('movimiento_form.html', cuentas_normales=cuentas_normales, cuentas_contrapartida=cuentas_contrapartida, movimiento=None, conceptos=None)
    return render_template('movimiento_form.html', cuentas_normales=cuentas_normales, cuentas_contrapartida=cuentas_contrapartida)

@app.route('/movimientos/editar/<int:id>', methods=['GET', 'POST'])
def editar_movimiento(id):
    movimiento = Movimiento.query.get_or_404(id)
    cuentas_normales = Cuenta.query.filter_by(tipo='normal').all()
    cuentas_contrapartida = Cuenta.query.filter_by(tipo='contrapartida').all()
    if request.method == 'POST':
        datos = request.form
        # Validar que el número de factura no esté duplicado (excepto el propio movimiento)
        num_factura = datos['num_factura']
        existe = Movimiento.query.filter(Movimiento.num_factura == num_factura, Movimiento.id != movimiento.id).first()
        if existe:
            flash('Ya existe un movimiento con ese número de factura.', 'error')
            conceptos = MovimientoConcepto.query.filter_by(movimiento_id=movimiento.id).all()
            return render_template('movimiento_form.html', movimiento=movimiento, cuentas_normales=cuentas_normales, cuentas_contrapartida=cuentas_contrapartida, conceptos=conceptos)
        # Validar que todas las contrapartidas sean iguales
        contrapartidas = set()
        idx = 0
        while f'contrapartida_{idx}' in datos:
            contrapartidas.add(datos[f'contrapartida_{idx}'])
            idx += 1
        if len(contrapartidas) > 1:
            flash('Todas las líneas deben tener la misma cuenta de contrapartida.', 'error')
            conceptos = MovimientoConcepto.query.filter_by(movimiento_id=movimiento.id).all()
            return render_template('movimiento_form.html', movimiento=movimiento, cuentas_normales=cuentas_normales, cuentas_contrapartida=cuentas_contrapartida, conceptos=conceptos)
        movimiento.tipo = datos['tipo']
        movimiento.fecha_trabajo = datos['fecha_trabajo']
        movimiento.fecha_factura = datos['fecha_factura']
        movimiento.num_factura = datos['num_factura']
        movimiento.base_imponible = float(datos['base_imponible']) if 'base_imponible' in datos else 0
        movimiento.total = float(datos['total'])
        MovimientoConcepto.query.filter_by(movimiento_id=movimiento.id).delete()
        idx = 0
        while f'cuenta_{idx}' in datos:
            cuenta_id = int(datos[f'cuenta_{idx}'])
            contrapartida_id = int(datos[f'contrapartida_{idx}'])
            importe = float(datos[f'importe_{idx}'])
            concepto = datos.get(f'concepto_{idx}', '')
            concepto_obj = MovimientoConcepto(movimiento_id=movimiento.id, cuenta_id=cuenta_id, importe=importe, concepto=concepto)
            concepto_obj.contrapartida_id = contrapartida_id
            db.session.add(concepto_obj)
            idx += 1
        try:
            commit_seguro("editar movimiento")
            flash('Movimiento actualizado correctamente.', 'success')
            return redirect(url_for('listar_movimientos'))
        except Exception as e:
            db.session.rollback()
            flash(f'Error al actualizar el movimiento: {str(e)}', 'error')
            conceptos = MovimientoConcepto.query.filter_by(movimiento_id=movimiento.id).all()
            return render_template('movimiento_form.html', movimiento=movimiento, cuentas_normales=cuentas_normales, cuentas_contrapartida=cuentas_contrapartida, conceptos=conceptos)
    conceptos = MovimientoConcepto.query.filter_by(movimiento_id=movimiento.id).all()
    return render_template('movimiento_form.html', movimiento=movimiento, cuentas_normales=cuentas_normales, cuentas_contrapartida=cuentas_contrapartida, conceptos=conceptos)

@app.route('/movimientos/borrar/<int:id>', methods=['POST'])
def borrar_movimiento(id):
    try:
        movimiento = Movimiento.query.get_or_404(id)
        db.session.delete(movimiento)
        commit_seguro("borrar movimiento")
        flash('Movimiento borrado correctamente.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error al borrar el movimiento: {str(e)}', 'error')
    
    return redirect(url_for('listar_movimientos'))

@app.route('/movimientos/duplicar/<int:id>', methods=['GET'])
@login_required
def duplicar_movimiento(id):
    movimiento = Movimiento.query.get_or_404(id)
    conceptos = MovimientoConcepto.query.filter_by(movimiento_id=movimiento.id).all()
    cuentas_normales = Cuenta.query.filter_by(tipo='normal').all()
    cuentas_contrapartida = Cuenta.query.filter_by(tipo='contrapartida').all()
    # Preparamos un objeto similar al de edición pero con fechas y factura vacías
    movimiento_clon = Movimiento(
        tipo=movimiento.tipo,
        fecha_trabajo='',
        fecha_factura='',
        num_factura='',
        base_imponible=movimiento.base_imponible,
        total=movimiento.total
    )
    # Pasamos los conceptos para que se muestren en el formulario
    return render_template('movimiento_form.html', movimiento=movimiento_clon, conceptos=conceptos, cuentas_normales=cuentas_normales, cuentas_contrapartida=cuentas_contrapartida)

@app.route('/movimientos/ver/<int:id>', methods=['GET'])
@login_required
def ver_movimiento(id):
    movimiento = Movimiento.query.get_or_404(id)
    conceptos = MovimientoConcepto.query.filter_by(movimiento_id=movimiento.id).all()
    cuentas_normales = Cuenta.query.filter_by(tipo='normal').all()
    cuentas_contrapartida = Cuenta.query.filter_by(tipo='contrapartida').all()
    return render_template('movimiento_form.html', movimiento=movimiento, conceptos=conceptos, cuentas_normales=cuentas_normales, cuentas_contrapartida=cuentas_contrapartida, solo_lectura=True)

@app.route('/debug_movimiento/<int:id>')
@login_required
def debug_movimiento(id):
    movimiento = Movimiento.query.get_or_404(id)
    conceptos = MovimientoConcepto.query.filter_by(movimiento_id=movimiento.id).all()
    
    debug_info = f"""
    <h2>Debug Movimiento ID: {id}</h2>
    <p><strong>Número de factura:</strong> {movimiento.num_factura}</p>
    <p><strong>Total del movimiento:</strong> {movimiento.total} €</p>
    <p><strong>Número de conceptos:</strong> {len(conceptos)}</p>
    <h3>Conceptos:</h3>
    <ul>
    """
    
    suma_conceptos = 0
    for c in conceptos:
        debug_info += f"<li>Cuenta: {c.cuenta.cuenta} - {c.cuenta.nombre} | Importe: {c.importe} € | Contrapartida: {c.contrapartida.cuenta if c.contrapartida else 'Sin contrapartida'}</li>"
        suma_conceptos += c.importe
    
    debug_info += f"""
    </ul>
    <p><strong>Suma de conceptos:</strong> {suma_conceptos} €</p>
    <p><strong>Diferencia:</strong> {movimiento.total - suma_conceptos} €</p>
    """
    
    return debug_info

@app.route('/buscar_movimiento/<factura>')
@login_required
def buscar_movimiento(factura):
    movimientos = Movimiento.query.filter_by(num_factura=factura).all()
    
    debug_info = f"""
    <h2>Búsqueda de movimientos con factura: {factura}</h2>
    <p><strong>Número de movimientos encontrados:</strong> {len(movimientos)}</p>
    """
    
    for mov in movimientos:
        conceptos = MovimientoConcepto.query.filter_by(movimiento_id=mov.id).all()
        suma_conceptos = sum(c.importe for c in conceptos)
        debug_info += f"""
        <h3>Movimiento ID: {mov.id}</h3>
        <p><strong>Total:</strong> {mov.total} €</p>
        <p><strong>Suma conceptos:</strong> {suma_conceptos} €</p>
        <p><strong>Diferencia:</strong> {mov.total - suma_conceptos} €</p>
        <p><a href="/debug_movimiento/{mov.id}">Ver detalles completos</a></p>
        <hr>
        """
    
    return debug_info

@app.route('/seguridad_social', methods=['GET', 'POST'])
@login_required
def seguridad_social():
    """Informe 521 Seguridad Social - Suma de cuentas de seguridad social a cargo de empresa y empleado"""
    # Calcular fechas por defecto (mes actual)
    hoy = datetime.today()
    fecha_inicio = hoy.replace(day=1).strftime('%Y-%m-%d')
    fecha_fin = hoy.strftime('%Y-%m-%d')
    
    total_seguridad_social = 0
    detalle_seguridad_social = []
    
    if request.method == 'POST':
        fecha_inicio = request.form['fecha_inicio']
        fecha_fin = request.form['fecha_fin']
        
        # Convertir fechas para filtro
        fecha_inicio_dt, fecha_fin_dt = convertir_fechas_para_filtro(fecha_inicio, fecha_fin)
        
        # Buscar conceptos de seguridad social en el rango de fechas
        Contrapartida = db.aliased(Cuenta)
        conceptos = db.session.query(MovimientoConcepto, Movimiento, Cuenta, Contrapartida)\
            .join(Movimiento)\
            .join(Cuenta, MovimientoConcepto.cuenta_id == Cuenta.id)\
            .outerjoin(Contrapartida, MovimientoConcepto.contrapartida_id == Contrapartida.id)\
            .filter(
                db.or_(
                    Cuenta.cuenta.like('521%'),  # Cuentas que empiecen por 521
                    Cuenta.nombre.ilike('%seguridad social%'),
                    Cuenta.nombre.ilike('%seguridad_social%'),
                    Cuenta.nombre.ilike('%ss%')
                )
            )\
            .filter(
                db.and_(
                    Movimiento.fecha_factura >= fecha_inicio,
                    Movimiento.fecha_factura <= fecha_fin
                )
            )\
            .all()
        
        # Agrupar por cuenta y sumar importes
        cuentas_agrupadas = {}
        for c in conceptos:
            cuenta_key = f"{c.Cuenta.cuenta} - {c.Cuenta.nombre}"
            if cuenta_key not in cuentas_agrupadas:
                cuentas_agrupadas[cuenta_key] = {
                    'cuenta': c.Cuenta.cuenta,
                    'nombre': c.Cuenta.nombre,
                    'total': 0,
                    'movimientos': []
                }
            cuentas_agrupadas[cuenta_key]['total'] += c.MovimientoConcepto.importe
            # Obtener información de la contrapartida
            contrapartida_info = ""
            if len(c) > 3 and c[3]:  # c[3] es el alias Contrapartida
                contrapartida_info = f"{c[3].cuenta} - {c[3].nombre}"
            elif c.MovimientoConcepto.concepto:
                contrapartida_info = c.MovimientoConcepto.concepto
            
            cuentas_agrupadas[cuenta_key]['movimientos'].append({
                'fecha': c.Movimiento.fecha_factura,
                'importe': c.MovimientoConcepto.importe,
                'concepto': contrapartida_info
            })
        
        # Convertir a lista ordenada
        detalle_seguridad_social = []
        for cuenta_key, datos in cuentas_agrupadas.items():
            detalle_seguridad_social.append(datos)
            total_seguridad_social += datos['total']
        
        # Ordenar por número de cuenta
        detalle_seguridad_social.sort(key=lambda x: x['cuenta'])
    
    return render_template('seguridad_social.html',
                         fecha_inicio=fecha_inicio,
                         fecha_fin=fecha_fin,
                         total_seguridad_social=total_seguridad_social,
                         detalle_seguridad_social=detalle_seguridad_social)

@app.route('/resultado_explotacion', methods=['GET', 'POST'])
def resultado_explotacion():
    resultado = None
    detalle = []
    diferencia = None

    def calcular_rango_periodo(tipo_periodo, periodo_anio, periodo_mes, periodo_trimestre):
        if tipo_periodo == 'anio':
            inicio = datetime(periodo_anio, 1, 1)
            fin = datetime(periodo_anio, 12, 31)
        elif tipo_periodo == 'trimestre':
            trimestre_a_inicio = {'Q1': 1, 'Q2': 4, 'Q3': 7, 'Q4': 10}
            inicio_mes = trimestre_a_inicio.get(periodo_trimestre, 1)
            fin_mes = inicio_mes + 2
            inicio = datetime(periodo_anio, inicio_mes, 1)
            fin = datetime(periodo_anio, fin_mes, calendar.monthrange(periodo_anio, fin_mes)[1])
        else:
            inicio = datetime(periodo_anio, periodo_mes, 1)
            fin = datetime(periodo_anio, periodo_mes, calendar.monthrange(periodo_anio, periodo_mes)[1])
        return inicio.strftime('%Y-%m-%d'), fin.strftime('%Y-%m-%d')

    # Valores por defecto
    hoy = datetime.today()
    tipo_periodo = 'mes'
    periodo_anio = hoy.year
    periodo_mes = hoy.month
    periodo_trimestre = f"Q{((hoy.month - 1) // 3) + 1}"
    campo_fecha = 'fecha_factura'
    fecha_inicio, fecha_fin = calcular_rango_periodo(tipo_periodo, periodo_anio, periodo_mes, periodo_trimestre)

    meses_disponibles = [
        (1, 'Enero'), (2, 'Febrero'), (3, 'Marzo'), (4, 'Abril'),
        (5, 'Mayo'), (6, 'Junio'), (7, 'Julio'), (8, 'Agosto'),
        (9, 'Septiembre'), (10, 'Octubre'), (11, 'Noviembre'), (12, 'Diciembre')
    ]

    resultado_explotacion = None
    suma_resultado_neto = 0
    resultado_neto = 0
    detalle_resultado_neto = []
    suma_7 = 0
    suma_resto = 0
    grafica_meses = []
    grafica_categorias = []

    if request.method == 'POST':
        tipo_periodo = request.form.get('tipo_periodo', 'mes')
        if tipo_periodo not in ('mes', 'trimestre', 'anio'):
            tipo_periodo = 'mes'
        campo_fecha_form = request.form.get('campo_fecha', 'fecha_factura')
        campo_fecha = campo_fecha_form if campo_fecha_form in ('fecha_factura', 'fecha_trabajo') else 'fecha_factura'
        periodo_trimestre = request.form.get('periodo_trimestre', periodo_trimestre)
        try:
            periodo_anio = int(request.form.get('periodo_anio', periodo_anio))
        except (TypeError, ValueError):
            periodo_anio = hoy.year
        try:
            periodo_mes = int(request.form.get('periodo_mes', periodo_mes))
        except (TypeError, ValueError):
            periodo_mes = hoy.month

        if periodo_mes < 1 or periodo_mes > 12:
            periodo_mes = hoy.month
        if periodo_trimestre not in ('Q1', 'Q2', 'Q3', 'Q4'):
            periodo_trimestre = f"Q{((hoy.month - 1) // 3) + 1}"

        fecha_inicio, fecha_fin = calcular_rango_periodo(tipo_periodo, periodo_anio, periodo_mes, periodo_trimestre)

        # Buscar conceptos en ese rango de fechas
        # Convertir fechas de YYYY-MM-DD a objetos datetime para la comparación
        fecha_inicio_dt, fecha_fin_dt = convertir_fechas_para_filtro(fecha_inicio, fecha_fin)
        
        conceptos = db.session.query(MovimientoConcepto, Movimiento, Cuenta)\
            .join(Movimiento)\
            .join(Cuenta, MovimientoConcepto.cuenta_id == Cuenta.id)\
            .all()
        
        # Filtrar por fecha usando comparación de datetime
        conceptos_filtrados = []
        for c in conceptos:
            fecha_referencia = getattr(c.Movimiento, campo_fecha, None)
            fecha_movimiento = parsear_fecha_robusto(fecha_referencia)
            if fecha_movimiento and fecha_inicio_dt <= fecha_movimiento <= fecha_fin_dt:
                conceptos_filtrados.append(c)
        
        conceptos = conceptos_filtrados
        
        # Debug: imprimir información sobre los conceptos encontrados
        print(f"Conceptos encontrados en rango {fecha_inicio} a {fecha_fin}: {len(conceptos)}")
        for c in conceptos:
            print(f"Cuenta: {c.Cuenta.cuenta} - {c.Cuenta.nombre}, Importe: {c.MovimientoConcepto.importe}, Fecha: {getattr(c.Movimiento, campo_fecha, None)}")
        
        # Buscar específicamente las nóminas
        nominas = db.session.query(MovimientoConcepto, Movimiento, Cuenta)\
            .join(Movimiento)\
            .join(Cuenta, MovimientoConcepto.cuenta_id == Cuenta.id)\
            .filter(
                Movimiento.num_factura.like('Nómina%')
            ).all()
        
        # Filtrar nóminas por fecha usando comparación de datetime
        nominas_filtradas = []
        for n in nominas:
            fecha_referencia = getattr(n.Movimiento, campo_fecha, None)
            fecha_movimiento = parsear_fecha_robusto(fecha_referencia)
            if fecha_movimiento and fecha_inicio_dt <= fecha_movimiento <= fecha_fin_dt:
                nominas_filtradas.append(n)
        
        nominas = nominas_filtradas
        
        print(f"Nóminas encontradas: {len(nominas)}")
        for n in nominas:
            print(f"Nómina: {n.Movimiento.num_factura}, Cuenta: {n.Cuenta.cuenta} - {n.Cuenta.nombre}, Importe: {n.MovimientoConcepto.importe}")
        
        # Agrupar y sumar importes por cuenta, incluyendo detalles de transacciones
        prefijos = ('623','626','621','622','625','628','629','310','640','641','642','649','662','678','7')
        cuentas_excluidas = ('642000000002',)  # Cuentas específicas a excluir del resultado de explotación
        cuentas_resultado_neto = ('74000000002', '76900000001')  # Cuentas que se suman al resultado neto
        meses_nombres = ['Ene', 'Feb', 'Mar', 'Abr', 'May', 'Jun', 'Jul', 'Ago', 'Sep', 'Oct', 'Nov', 'Dic']
        grafica_dict = {}

        # La gráfica debe respetar exactamente el periodo elegido arriba
        meses_grafica = []
        if tipo_periodo == 'anio':
            meses_grafica = list(range(1, 13))
        elif tipo_periodo == 'trimestre':
            trimestre_a_inicio = {'Q1': 1, 'Q2': 4, 'Q3': 7, 'Q4': 10}
            mes_inicio = trimestre_a_inicio.get(periodo_trimestre, 1)
            meses_grafica = [mes_inicio, mes_inicio + 1, mes_inicio + 2]
        else:
            meses_grafica = [periodo_mes]

        for mes_grafica in meses_grafica:
            key_mes = f"{periodo_anio:04d}-{mes_grafica:02d}"
            grafica_dict[key_mes] = {
                'label': f"{meses_nombres[mes_grafica - 1]} {periodo_anio}",
                'ingresos': 0.0,
                'gastos_por_categoria': {}
            }

        detalle_dict = {}
        for c in conceptos:
            cuenta = str(c.Cuenta.cuenta)
            fecha_ref_mov = getattr(c.Movimiento, campo_fecha, None)
            fecha_movimiento = parsear_fecha_robusto(fecha_ref_mov)
            mes_key = f"{fecha_movimiento.year:04d}-{fecha_movimiento.month:02d}" if fecha_movimiento else None
            # Verificar si la cuenta empieza con alguno de los prefijos
            incluir_cuenta = False
            for prefijo in prefijos:
                if cuenta.startswith(prefijo):
                    incluir_cuenta = True
                    break
            
            # Excluir cuentas específicas aunque cumplan con los prefijos
            if cuenta in cuentas_excluidas or cuenta in cuentas_resultado_neto:
                incluir_cuenta = False
            
            if incluir_cuenta:
                if mes_key and mes_key in grafica_dict:
                    if cuenta.startswith('7'):
                        grafica_dict[mes_key]['ingresos'] += c.MovimientoConcepto.importe
                    else:
                        nombre_categoria = (c.Cuenta.nombre or '').strip() or f"Cuenta {cuenta}"
                        if nombre_categoria not in grafica_dict[mes_key]['gastos_por_categoria']:
                            grafica_dict[mes_key]['gastos_por_categoria'][nombre_categoria] = 0.0
                        grafica_dict[mes_key]['gastos_por_categoria'][nombre_categoria] += c.MovimientoConcepto.importe

                key = cuenta
                if key not in detalle_dict:
                    detalle_dict[key] = {
                        'cuenta': cuenta,
                        'nombre': c.Cuenta.nombre,
                        'importe': 0,
                        'transacciones': []
                    }
                detalle_dict[key]['importe'] += c.MovimientoConcepto.importe
                # Añadir detalles de la transacción
                contrapartida_info = ""
                if c.MovimientoConcepto.contrapartida:
                    contrapartida_info = f"{c.MovimientoConcepto.contrapartida.cuenta} - {c.MovimientoConcepto.contrapartida.nombre}"
                else:
                    contrapartida_info = "Sin contrapartida"
                
                transaccion = {
                    'fecha': getattr(c.Movimiento, campo_fecha, None),
                    'contrapartida': contrapartida_info,
                    'importe': c.MovimientoConcepto.importe,
                    'concepto': c.MovimientoConcepto.concepto or "Sin concepto"
                }
                detalle_dict[key]['transacciones'].append(transaccion)
        detalle = list(detalle_dict.values())
        # Ordenar por número de cuenta
        detalle = sorted(detalle, key=lambda x: x['cuenta'])
        suma_detalle = sum(d['importe'] for d in detalle)
        suma_7 = sum(d['importe'] for d in detalle if str(d['cuenta']).startswith('7'))
        suma_705 = sum(d['importe'] for d in detalle if str(d['cuenta']).strip() == '70500000001')
        suma_resto = sum(d['importe'] for d in detalle if not str(d['cuenta']).startswith('7'))
        resultado = suma_7
        diferencia = suma_resto  # Gastos fijos (todas las cuentas que no son 7)
        resultado_explotacion = suma_7 - suma_resto
        
        # Calcular importes de las cuentas del resultado neto y agregarlas al detalle
        suma_resultado_neto = 0
        detalle_resultado_neto = []
        
        for c in conceptos:
            cuenta = str(c.Cuenta.cuenta)
            if cuenta in cuentas_resultado_neto:
                suma_resultado_neto += c.MovimientoConcepto.importe
                
                # Agregar al detalle del resultado neto
                key = cuenta
                if key not in [d['cuenta'] for d in detalle_resultado_neto]:
                    detalle_resultado_neto.append({
                        'cuenta': cuenta,
                        'nombre': c.Cuenta.nombre,
                        'importe': 0,
                        'transacciones': []
                    })
                
                # Encontrar la entrada en detalle_resultado_neto y actualizar
                for d in detalle_resultado_neto:
                    if d['cuenta'] == cuenta:
                        d['importe'] += c.MovimientoConcepto.importe
                        # Añadir detalles de la transacción
                        contrapartida_info = ""
                        if c.MovimientoConcepto.contrapartida:
                            contrapartida_info = f"{c.MovimientoConcepto.contrapartida.cuenta} - {c.MovimientoConcepto.contrapartida.nombre}"
                        else:
                            contrapartida_info = "Sin contrapartida"
                        
                        transaccion = {
                            'fecha': getattr(c.Movimiento, campo_fecha, None),
                            'contrapartida': contrapartida_info,
                            'importe': c.MovimientoConcepto.importe,
                            'concepto': c.MovimientoConcepto.concepto or "Sin concepto"
                        }
                        d['transacciones'].append(transaccion)
                        break
        
        # Calcular resultado neto
        resultado_neto = resultado_explotacion + suma_resultado_neto

        grafica_meses = []
        categorias_activas = set()
        for key_mes in sorted(grafica_dict.keys()):
            gastos_abs = {}
            for categoria, importe in grafica_dict[key_mes]['gastos_por_categoria'].items():
                valor_abs = abs(importe)
                gastos_abs[categoria] = valor_abs
                if valor_abs > 0:
                    categorias_activas.add(categoria)
            grafica_meses.append({
                'label': grafica_dict[key_mes]['label'],
                'ingresos': abs(grafica_dict[key_mes]['ingresos']),
                'gastos_por_categoria': gastos_abs
            })
        grafica_categorias = sorted(categorias_activas)
    
    # Formatear fechas para mostrar (siempre)
    fecha_inicio_formateada = datetime.strptime(fecha_inicio, '%Y-%m-%d').strftime('%d/%m/%Y')
    fecha_fin_formateada = datetime.strptime(fecha_fin, '%Y-%m-%d').strftime('%d/%m/%Y')
    
    return render_template(
        'resultado_explotacion.html',
        resultado=resultado,
        detalle=detalle,
        diferencia=diferencia,
        fecha_inicio=fecha_inicio,
        fecha_fin=fecha_fin,
        fecha_inicio_formateada=fecha_inicio_formateada,
        fecha_fin_formateada=fecha_fin_formateada,
        resultado_explotacion=resultado_explotacion,
        suma_resultado_neto=suma_resultado_neto,
        resultado_neto=resultado_neto,
        detalle_resultado_neto=detalle_resultado_neto,
        tipo_periodo=tipo_periodo,
        periodo_anio=periodo_anio,
        periodo_mes=periodo_mes,
        periodo_trimestre=periodo_trimestre,
        meses_disponibles=meses_disponibles,
        campo_fecha=campo_fecha,
        total_ingresos=suma_7,
        total_gastos=suma_resto,
        grafica_meses=grafica_meses,
        grafica_categorias=grafica_categorias
    )

@app.route('/iva', methods=['GET', 'POST'])
def resultado_iva():
    resultado = None
    iva_repercutido = 0
    iva_soportado = 0
    # Calcular fechas por defecto
    hoy = datetime.today()
    mes = hoy.month
    if mes <= 3:
        inicio_trimestre = datetime(hoy.year, 1, 1)
    elif mes <= 6:
        inicio_trimestre = datetime(hoy.year, 4, 1)
    elif mes <= 9:
        inicio_trimestre = datetime(hoy.year, 7, 1)
    else:
        inicio_trimestre = datetime(hoy.year, 10, 1)
    fecha_inicio = inicio_trimestre.strftime('%Y-%m-%d')
    fecha_fin = hoy.strftime('%Y-%m-%d')
    desglose_checked = False
    filtro_declarado = 'todos'  # 'todos', 'declarados', 'no_declarados'
    trimestre_filtro = ''
    desglose_contrapartidas = []
    total_repercutido = 0
    total_soportado = 0
    if request.method == 'POST':
        fecha_inicio = request.form['fecha_inicio']
        fecha_fin = request.form['fecha_fin']
        desglose_checked = 'desglose' in request.form
        filtro_declarado = request.form.get('filtro_declarado', 'todos')
        trimestre_filtro = request.form.get('trimestre_filtro', '')
        # Convertir fechas de YYYY-MM-DD a objetos datetime para la comparación
        fecha_inicio_dt, fecha_fin_dt = convertir_fechas_para_filtro(fecha_inicio, fecha_fin)
        
        conceptos = db.session.query(MovimientoConcepto, Movimiento, Cuenta)\
            .join(Movimiento)\
            .join(Cuenta, MovimientoConcepto.cuenta_id == Cuenta.id)\
            .filter(
                Cuenta.cuenta.in_(['47700000001', '47200000001'])
            ).all()
        
        # Filtrar por fecha y declarado usando comparación de datetime
        conceptos_filtrados = []
        for c in conceptos:
            fecha_movimiento = parsear_fecha_robusto(c.Movimiento.fecha_factura)
            if fecha_movimiento and fecha_inicio_dt <= fecha_movimiento <= fecha_fin_dt:
                # Aplicar filtro de declarado
                if filtro_declarado == 'declarados' and not c.MovimientoConcepto.declarado:
                    continue
                elif filtro_declarado == 'no_declarados' and c.MovimientoConcepto.declarado:
                    continue
                
                # Aplicar filtro de trimestre si se especifica
                if trimestre_filtro and c.MovimientoConcepto.trimestre_declaracion != trimestre_filtro:
                    continue
                
                conceptos_filtrados.append(c)
        
        conceptos = conceptos_filtrados
        for c in conceptos:
            if str(c.Cuenta.cuenta).strip() == '47700000001':
                iva_repercutido += c.MovimientoConcepto.importe
            elif str(c.Cuenta.cuenta).strip() == '47200000001':
                iva_soportado += c.MovimientoConcepto.importe
        resultado = iva_repercutido - iva_soportado
        # Desglose por movimientos individuales separados
        if desglose_checked:
            movimientos_repercutido = []
            movimientos_soportado = []
            
            for c in conceptos:
                contrapartida = c.MovimientoConcepto.contrapartida
                contrapartida_info = "Sin contrapartida"
                if contrapartida:
                    contrapartida_info = f"{contrapartida.cuenta} - {contrapartida.nombre}"
                
                movimiento = {
                    'id': c.MovimientoConcepto.id,
                    'fecha': c.Movimiento.fecha_factura,
                    'num_factura': c.Movimiento.num_factura or "Sin número",
                    'concepto': c.MovimientoConcepto.concepto or "Sin concepto",
                    'contrapartida': contrapartida_info,
                    'importe': c.MovimientoConcepto.importe,
                    'declarado': c.MovimientoConcepto.declarado,
                    'trimestre_declaracion': c.MovimientoConcepto.trimestre_declaracion
                }
                
                if str(c.Cuenta.cuenta).strip() == '47700000001':
                    movimientos_repercutido.append(movimiento)
                elif str(c.Cuenta.cuenta).strip() == '47200000001':
                    movimientos_soportado.append(movimiento)
            
            # Ordenar por fecha (más reciente primero)
            movimientos_repercutido.sort(key=lambda x: x['fecha'], reverse=True)
            movimientos_soportado.sort(key=lambda x: x['fecha'], reverse=True)
            
            # Reutilizamos las variables para compatibilidad con el template
            desglose_contrapartidas = {
                'repercutido': movimientos_repercutido,
                'soportado': movimientos_soportado
            }
            # Calcular los totales
            total_repercutido = sum(m['importe'] for m in movimientos_repercutido)
            total_soportado = sum(m['importe'] for m in movimientos_soportado)
    return render_template('iva.html', resultado=resultado, iva_repercutido=iva_repercutido, iva_soportado=iva_soportado, fecha_inicio=fecha_inicio, fecha_fin=fecha_fin, desglose_checked=desglose_checked, desglose_contrapartidas=desglose_contrapartidas, total_repercutido=total_repercutido, total_soportado=total_soportado, filtro_declarado=filtro_declarado, trimestre_filtro=trimestre_filtro)

@app.route('/marcar_declarado', methods=['POST'])
@login_required
def marcar_declarado():
    """Marcar movimientos de IVA como declarados"""
    try:
        data = request.get_json()
        concepto_ids = data.get('concepto_ids', [])
        trimestre = data.get('trimestre', '')
        
        if not concepto_ids or not trimestre:
            return jsonify({'success': False, 'message': 'Faltan datos requeridos'}), 400
        
        # Verificar que el trimestre tenga formato correcto
        if not trimestre or len(trimestre) < 6:
            return jsonify({'success': False, 'message': 'Formato de trimestre inválido (ej: 2025Q3)'}), 400
        
        # Actualizar los movimientos
        actualizados = 0
        for concepto_id in concepto_ids:
            concepto = MovimientoConcepto.query.get(concepto_id)
            if concepto:
                concepto.declarado = True
                concepto.trimestre_declaracion = trimestre
                actualizados += 1
        
        db.session.commit()
        return jsonify({'success': True, 'message': f'{actualizados} movimientos marcados como declarados en {trimestre}'})
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': f'Error: {str(e)}'}), 500

@app.route('/obtener_movimiento_completo/<int:movimiento_id>')
@login_required
def obtener_movimiento_completo(movimiento_id):
    try:
        # Buscar el movimiento completo
        movimiento = Movimiento.query.get(movimiento_id)
        if not movimiento:
            return jsonify({'success': False, 'message': 'Movimiento no encontrado'})
        
        # Obtener todos los conceptos del movimiento
        conceptos = db.session.query(MovimientoConcepto, Cuenta)\
            .join(Cuenta, MovimientoConcepto.cuenta_id == Cuenta.id)\
            .filter(MovimientoConcepto.movimiento_id == movimiento_id)\
            .all()
        
        # Estructurar los datos del movimiento (igual que en movimiento_form.html)
        movimiento_data = {
            'id': movimiento.id,
            'tipo': getattr(movimiento, 'tipo', 'Gasto'),
            'fecha_trabajo': str(movimiento.fecha_trabajo) if hasattr(movimiento, 'fecha_trabajo') and movimiento.fecha_trabajo else None,
            'fecha_factura': str(movimiento.fecha_factura) if movimiento.fecha_factura else None,
            'num_factura': movimiento.num_factura,
            'conceptos': []
        }
        
        # Agregar cada concepto del movimiento
        for concepto, cuenta in conceptos:
            contrapartida_info = None
            if concepto.contrapartida:
                contrapartida_info = {
                    'id': concepto.contrapartida.id,
                    'cuenta': concepto.contrapartida.cuenta,
                    'nombre': concepto.contrapartida.nombre
                }
            
            concepto_data = {
                'id': concepto.id,
                'cuenta': {
                    'id': cuenta.id,
                    'cuenta': cuenta.cuenta,
                    'nombre': cuenta.nombre
                },
                'importe': concepto.importe,
                'concepto': concepto.concepto,
                'contrapartida': contrapartida_info,
                'declarado': concepto.declarado,
                'trimestre_declaracion': concepto.trimestre_declaracion
            }
            movimiento_data['conceptos'].append(concepto_data)
        
        return jsonify({'success': True, 'movimiento': movimiento_data})
        
    except Exception as e:
        return jsonify({'success': False, 'message': f'Error al obtener movimiento: {str(e)}'})

@app.route('/cancelar_declarado', methods=['POST'])
@login_required
def cancelar_declarado():
    """Cancelar declaración de movimientos de IVA"""
    try:
        data = request.get_json()
        concepto_ids = data.get('concepto_ids', [])
        
        if not concepto_ids:
            return jsonify({'success': False, 'message': 'No se proporcionaron IDs de movimientos'}), 400
        
        # Actualizar los movimientos
        actualizados = 0
        for concepto_id in concepto_ids:
            concepto = MovimientoConcepto.query.get(concepto_id)
            if concepto and concepto.declarado:
                concepto.declarado = False
                concepto.trimestre_declaracion = None
                actualizados += 1
        
        db.session.commit()
        return jsonify({'success': True, 'message': f'{actualizados} movimientos cancelados como declarados'})
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': f'Error: {str(e)}'}), 500

@app.route('/proponer_declarados_desde_excel', methods=['POST'])
@login_required
def proponer_declarados_desde_excel():
    """Compara un Excel de libro IVA y devuelve propuestas por fecha+importe IVA."""
    try:
        archivo = request.files.get('archivo_iva')
        movimientos_raw = request.form.get('movimientos', '[]')
        tipo_propuesta = (request.form.get('tipo_propuesta') or '').strip().lower()

        if not archivo:
            return jsonify({'success': False, 'message': 'No se ha subido ningún archivo'}), 400

        nombre_archivo = (archivo.filename or '').lower()
        if not nombre_archivo.endswith('.xlsx'):
            return jsonify({'success': False, 'message': 'Formato no válido. Sube un archivo .xlsx'}), 400

        try:
            movimientos = json.loads(movimientos_raw)
        except Exception:
            return jsonify({'success': False, 'message': 'No se pudieron leer los movimientos enviados'}), 400

        if not isinstance(movimientos, list) or not movimientos:
            return jsonify({'success': False, 'message': 'No hay movimientos para comparar'}), 400

        def normalizar_importe(valor):
            if valor is None:
                return None
            if isinstance(valor, (int, float)):
                return round(abs(float(valor)), 2)
            valor_str = str(valor).strip().replace('€', '').replace(' ', '')
            valor_str = valor_str.replace('.', '').replace(',', '.')
            try:
                return round(abs(float(valor_str)), 2)
            except ValueError:
                return None

        # Índice de movimientos mostrados en pantalla por (fecha, importe).
        index_movimientos = {}
        movimientos_norm = []
        for m in movimientos:
            tipo = (m.get('tipo') or '').strip().lower()
            if tipo_propuesta in ('soportado', 'repercutido') and tipo != tipo_propuesta:
                continue
            mid = m.get('id')
            fecha_dt = parsear_fecha_robusto(str(m.get('fecha', '')).strip())
            importe_norm = normalizar_importe(m.get('importe'))
            if not mid or not fecha_dt or importe_norm is None:
                continue
            fecha_iso = fecha_dt.date().isoformat()
            clave = (fecha_iso, importe_norm)
            index_movimientos.setdefault(clave, []).append(int(mid))
            movimientos_norm.append({
                'id': int(mid),
                'fecha': fecha_iso,
                'importe': importe_norm
            })

        if not index_movimientos:
            return jsonify({'success': False, 'message': 'No hay movimientos válidos para comparar'}), 400

        libro = load_workbook(filename=io.BytesIO(archivo.read()), data_only=True)
        hoja = libro[libro.sheetnames[0]]

        def _norm_header(txt):
            if not txt:
                return ''
            t = str(txt).strip().lower()
            for a, b in (('á', 'a'), ('é', 'e'), ('í', 'i'), ('ó', 'o'), ('ú', 'u')):
                t = t.replace(a, b)
            return ' '.join(t.split())

        col_fecha = None
        col_cuota_iva = None
        claves_excel = set()
        log_detalle_lineas = []
        max_log_detalle = 300

        for num_fila, fila in enumerate(hoja.iter_rows(values_only=True), start=1):
            if not fila:
                continue

            # Detectar cabeceras si existen en la fila
            if col_fecha is None or col_cuota_iva is None:
                for idx, celda in enumerate(fila):
                    if celda is None:
                        continue
                    texto = _norm_header(celda)
                    if col_fecha is None and texto == 'fecha':
                        col_fecha = idx
                    if col_cuota_iva is None and 'cuota' in texto and 'iva' in texto:
                        col_cuota_iva = idx

            # Fallback para formato habitual del libro de registro exportado
            idx_fecha = col_fecha if col_fecha is not None else 7
            idx_cuota = col_cuota_iva if col_cuota_iva is not None else 48
            if len(fila) <= max(idx_fecha, idx_cuota):
                continue

            fecha_val = fila[idx_fecha]
            cuota_val = fila[idx_cuota]
            if fecha_val in (None, '') or cuota_val in (None, ''):
                continue

            if isinstance(fecha_val, datetime):
                fecha_dt = fecha_val
            else:
                fecha_dt = parsear_fecha_robusto(str(fecha_val).strip())

            cuota_norm = normalizar_importe(cuota_val)
            if not fecha_dt or cuota_norm is None:
                continue

            fecha_iso = fecha_dt.date().isoformat()
            claves_excel.add((fecha_iso, cuota_norm))

            if len(log_detalle_lineas) < max_log_detalle:
                log_detalle_lineas.append(
                    f"Fila {num_fila}: fecha_raw={fecha_val!r}, cuota_raw={cuota_val!r}, "
                    f"fecha_norm={fecha_iso}, cuota_norm={cuota_norm:.2f}"
                )

        propuestos = set()
        exactas = 0
        fallback_fecha_importe = 0
        fallback_solo_importe = 0
        for clave in claves_excel:
            ids = index_movimientos.get(clave, [])
            for mid in ids:
                if mid not in propuestos:
                    exactas += 1
                propuestos.add(mid)

        def _fecha_mas_dias(fecha_iso, dias):
            try:
                d = datetime.strptime(fecha_iso, '%Y-%m-%d').date()
                return (d + timedelta(days=dias)).isoformat()
            except ValueError:
                return None

        # Fallback 1: tolerancia de importe y ±1 día (redondeos / huso).
        if not propuestos:
            for fecha_excel, imp_excel in claves_excel:
                fechas_probar = {fecha_excel}
                for delta in (-1, 1):
                    alt = _fecha_mas_dias(fecha_excel, delta)
                    if alt:
                        fechas_probar.add(alt)
                for mov in movimientos_norm:
                    if mov['fecha'] not in fechas_probar:
                        continue
                    if abs(mov['importe'] - imp_excel) <= 0.05:
                        if mov['id'] not in propuestos:
                            fallback_fecha_importe += 1
                        propuestos.add(mov['id'])

        # Fallback 2: si sigue a 0, cruzar solo por importe (con tolerancia) en importes "raros"
        # para evitar dejar vacío cuando haya descuadre sistemático de fechas.
        if not propuestos:
            importes_excel = sorted({imp for _, imp in claves_excel})
            for mov in movimientos_norm:
                for imp_excel in importes_excel:
                    if abs(mov['importe'] - imp_excel) <= 0.01:
                        if mov['id'] not in propuestos:
                            fallback_solo_importe += 1
                        propuestos.add(mov['id'])
                        break

        log_lineas = [
            f"Tipo de propuesta: {tipo_propuesta or 'todos'}",
            f"Movimientos recibidos: {len(movimientos)}",
            f"Movimientos válidos comparados: {len(movimientos_norm)}",
            f"Claves válidas extraídas del Excel: {len(claves_excel)}",
            f"Coincidencias exactas (fecha+importe): {exactas}",
            f"Coincidencias fallback fecha±1 + importe: {fallback_fecha_importe}",
            f"Coincidencias fallback solo importe: {fallback_solo_importe}",
            f"Total propuestas marcadas: {len(propuestos)}",
        ]
        if len(log_detalle_lineas) >= max_log_detalle:
            log_lineas.append(f"Detalle por fila truncado a {max_log_detalle} líneas.")

        return jsonify({
            'success': True,
            'message': (
                f"Se han encontrado {len(propuestos)} propuestas por fecha e importe "
                f"(tipo: {tipo_propuesta or 'todos'}, movimientos comparados: {len(movimientos_norm)}, claves Excel: {len(claves_excel)})"
            ),
            'propuestos': sorted(list(propuestos)),
            'coincidencias_excel': len(claves_excel),
            'log': log_lineas,
            'log_detalle': log_detalle_lineas
        })

    except Exception as e:
        return jsonify({'success': False, 'message': f'Error al procesar el fichero: {str(e)}'}), 500

@app.route('/retencion_alquileres', methods=['GET', 'POST'])
def retencion_alquileres():
    # Fechas por defecto: inicio de trimestre y hoy
    hoy = datetime.today()
    mes = hoy.month
    if mes <= 3:
        inicio_trimestre = datetime(hoy.year, 1, 1)
    elif mes <= 6:
        inicio_trimestre = datetime(hoy.year, 4, 1)
    elif mes <= 9:
        inicio_trimestre = datetime(hoy.year, 7, 1)
    else:
        inicio_trimestre = datetime(hoy.year, 10, 1)
    fecha_inicio = inicio_trimestre.strftime('%Y-%m-%d')
    fecha_fin = hoy.strftime('%Y-%m-%d')
    resultado = None
    desglose_contrapartidas = []
    total_importe = 0
    if request.method == 'POST':
        fecha_inicio = request.form['fecha_inicio']
        fecha_fin = request.form['fecha_fin']
    
    # Convertir fechas de YYYY-MM-DD a objetos datetime para la comparación
    fecha_inicio_dt, fecha_fin_dt = convertir_fechas_para_filtro(fecha_inicio, fecha_fin)
    
    conceptos = db.session.query(MovimientoConcepto, Movimiento, Cuenta)\
        .join(Movimiento)\
        .join(Cuenta, MovimientoConcepto.cuenta_id == Cuenta.id)\
        .filter(
            Cuenta.cuenta == '47510000003'
        ).all()
    
    # Filtrar por fecha usando comparación de datetime
    conceptos_filtrados = []
    for c in conceptos:
        fecha_movimiento = parsear_fecha_robusto(c.Movimiento.fecha_factura)
        if fecha_movimiento and fecha_inicio_dt <= fecha_movimiento <= fecha_fin_dt:
            conceptos_filtrados.append(c)
    
    conceptos = conceptos_filtrados
    resultado = sum(c.MovimientoConcepto.importe for c in conceptos)
    # Desglose por contrapartida
    desglose_dict = {}
    for c in conceptos:
        contrapartida = c.MovimientoConcepto.contrapartida
        if not contrapartida:
            continue
        key = contrapartida.cuenta
        if key not in desglose_dict:
            desglose_dict[key] = {
                'contrapartida': contrapartida.cuenta,
                'nombre': contrapartida.nombre,
                'importe': 0
            }
        desglose_dict[key]['importe'] += c.MovimientoConcepto.importe
    desglose_contrapartidas = [v for v in desglose_dict.values() if v['importe'] != 0]
    total_importe = sum(v['importe'] for v in desglose_contrapartidas)
    return render_template('retencion_alquileres.html', resultado=resultado, fecha_inicio=fecha_inicio, fecha_fin=fecha_fin, desglose_contrapartidas=desglose_contrapartidas, total_importe=total_importe)

@app.route('/retencion_empleados', methods=['GET', 'POST'])
def retencion_empleados():
    # Fechas por defecto: inicio de trimestre y hoy
    hoy = datetime.today()
    mes = hoy.month
    if mes <= 3:
        inicio_trimestre = datetime(hoy.year, 1, 1)
    elif mes <= 6:
        inicio_trimestre = datetime(hoy.year, 4, 1)
    elif mes <= 9:
        inicio_trimestre = datetime(hoy.year, 7, 1)
    else:
        inicio_trimestre = datetime(hoy.year, 10, 1)
    fecha_inicio = inicio_trimestre.strftime('%Y-%m-%d')
    fecha_fin = hoy.strftime('%Y-%m-%d')
    resultado = None
    desglose_contrapartidas = []
    total_importe = 0
    if request.method == 'POST':
        fecha_inicio = request.form['fecha_inicio']
        fecha_fin = request.form['fecha_fin']
    
    # Convertir fechas de YYYY-MM-DD a objetos datetime para la comparación
    fecha_inicio_dt, fecha_fin_dt = convertir_fechas_para_filtro(fecha_inicio, fecha_fin)
    
    conceptos = db.session.query(MovimientoConcepto, Movimiento, Cuenta)\
        .join(Movimiento)\
        .join(Cuenta, MovimientoConcepto.cuenta_id == Cuenta.id)\
        .filter(
            Cuenta.cuenta == '47510000001'
        ).all()
    
    # Filtrar por fecha usando comparación de datetime
    conceptos_filtrados = []
    for c in conceptos:
        fecha_movimiento = parsear_fecha_robusto(c.Movimiento.fecha_factura)
        if fecha_movimiento and fecha_inicio_dt <= fecha_movimiento <= fecha_fin_dt:
            conceptos_filtrados.append(c)
    
    conceptos = conceptos_filtrados
    resultado = sum(c.MovimientoConcepto.importe for c in conceptos)
    # Desglose por contrapartida
    desglose_dict = {}
    for c in conceptos:
        contrapartida = c.MovimientoConcepto.contrapartida
        if not contrapartida:
            continue
        key = contrapartida.cuenta
        if key not in desglose_dict:
            desglose_dict[key] = {
                'contrapartida': contrapartida.cuenta,
                'nombre': contrapartida.nombre,
                'importe': 0
            }
        desglose_dict[key]['importe'] += c.MovimientoConcepto.importe
    desglose_contrapartidas = [v for v in desglose_dict.values() if v['importe'] != 0]
    total_importe = sum(v['importe'] for v in desglose_contrapartidas)
    return render_template('retencion_empleados.html', resultado=resultado, fecha_inicio=fecha_inicio, fecha_fin=fecha_fin, desglose_contrapartidas=desglose_contrapartidas, total_importe=total_importe)

@app.route('/347', methods=['GET', 'POST'])
def informe_347():
    # Fechas por defecto: inicio de trimestre y hoy
    hoy = datetime.today()
    mes = hoy.month
    if mes <= 3:
        inicio_trimestre = datetime(hoy.year, 1, 1)
    elif mes <= 6:
        inicio_trimestre = datetime(hoy.year, 4, 1)
    elif mes <= 9:
        inicio_trimestre = datetime(hoy.year, 7, 1)
    else:
        inicio_trimestre = datetime(hoy.year, 10, 1)
    fecha_inicio = inicio_trimestre.strftime('%Y-%m-%d')
    fecha_fin = hoy.strftime('%Y-%m-%d')
    resumen_contrapartidas = []
    resumen_contrapartidas_3000 = []
    resumen_contrapartidas_menos_3000 = []
    total_importe = 0
    total_importe_3000 = 0
    total_importe_menos_3000 = 0
    if request.method == 'POST':
        fecha_inicio = request.form['fecha_inicio']
        fecha_fin = request.form['fecha_fin']
    
    # Convertir fechas de YYYY-MM-DD a objetos datetime para la comparación
    fecha_inicio_dt, fecha_fin_dt = convertir_fechas_para_filtro(fecha_inicio, fecha_fin)
    
    # Agrupar por contrapartida y sumar importes
    conceptos = db.session.query(MovimientoConcepto, Movimiento, Cuenta)\
        .join(Movimiento)\
        .join(Cuenta, MovimientoConcepto.contrapartida_id == Cuenta.id)\
        .all()
    
    # Filtrar por fecha usando comparación de datetime
    conceptos_filtrados = []
    for c in conceptos:
        fecha_movimiento = parsear_fecha_robusto(c.Movimiento.fecha_factura)
        if fecha_movimiento and fecha_inicio_dt <= fecha_movimiento <= fecha_fin_dt:
            conceptos_filtrados.append(c)
    
    conceptos = conceptos_filtrados
    resumen_dict = {}
    for c in conceptos:
        contrapartida = c.Cuenta
        if not contrapartida:
            continue
        key = contrapartida.cuenta
        if key not in resumen_dict:
            resumen_dict[key] = {
                'contrapartida': contrapartida.cuenta,
                'nombre': contrapartida.nombre,
                'importe': 0
            }
        resumen_dict[key]['importe'] += c.MovimientoConcepto.importe
    resumen_contrapartidas = [v for v in resumen_dict.values() if v['importe'] != 0]
    # Ordenar alfabéticamente por nombre
    resumen_contrapartidas = sorted(resumen_contrapartidas, key=lambda x: x['nombre'])
    # Separar por umbral de 3000 euros
    for contrapartida in resumen_contrapartidas:
        if contrapartida['importe'] >= 3000:
            resumen_contrapartidas_3000.append(contrapartida)
            total_importe_3000 += contrapartida['importe']
        else:
            resumen_contrapartidas_menos_3000.append(contrapartida)
            total_importe_menos_3000 += contrapartida['importe']
    total_importe = sum(v['importe'] for v in resumen_contrapartidas)
    return render_template('347.html', fecha_inicio=fecha_inicio, fecha_fin=fecha_fin, 
                         resumen_contrapartidas=resumen_contrapartidas, 
                         resumen_contrapartidas_3000=resumen_contrapartidas_3000,
                         resumen_contrapartidas_menos_3000=resumen_contrapartidas_menos_3000,
                         total_importe=total_importe,
                         total_importe_3000=total_importe_3000,
                         total_importe_menos_3000=total_importe_menos_3000)

@app.route('/descargar_db')
@login_required
def descargar_db():
    """Descargar la base de datos SQLite actual al equipo del usuario."""
    uri = app.config.get('SQLALCHEMY_DATABASE_URI', '')
    if not uri.startswith('sqlite:///'):
        flash('La descarga directa solo está disponible para base de datos SQLite.', 'error')
        return redirect(url_for('index'))

    db_path = Path(uri.replace('sqlite:///', '').strip())
    if not db_path.is_absolute():
        db_path = Path(__file__).resolve().parent / db_path

    if not db_path.exists():
        flash(f'No se encontró la base de datos en: {db_path}', 'error')
        return redirect(url_for('index'))

    fecha_hora = datetime.now().strftime('%Y%m%d_%H%M%S')
    nombre_archivo = f'lvm_backup_{fecha_hora}.db'
    return send_file(str(db_path), as_attachment=True, download_name=nombre_archivo)

# Función para subir la base de datos por SFTP

def subir_db_a_ftp():
    sftp_host = os.environ.get('FTP_HOST')
    sftp_user = os.environ.get('FTP_USER')
    sftp_pass = os.environ.get('FTP_PASS')
    sftp_dir = os.environ.get('FTP_DIR', '/')
    db_path = os.path.join(os.path.dirname(__file__), 'app.db')
    fecha = datetime.now().strftime('%Y%m%d')
    nombre_archivo = f'lvm{fecha}.db'
    if not sftp_host or not sftp_user or not sftp_pass:
        print('Faltan variables de entorno para la conexión SFTP.')
        return
    try:
        transport = paramiko.Transport((sftp_host, 22))
        transport.connect(username=sftp_user, password=sftp_pass)
        sftp = paramiko.SFTPClient.from_transport(transport)
        # Intentar cambiar al directorio, si falla lo crea
        try:
            sftp.chdir(sftp_dir)
        except IOError:
            # Crear el directorio (soporta rutas anidadas)
            dirs = sftp_dir.strip('/').split('/')
            path = ''
            for d in dirs:
                path += '/' + d
                try:
                    sftp.chdir(path)
                except IOError:
                    sftp.mkdir(path)
                    sftp.chdir(path)
        # Listar archivos de backup existentes
        archivos = sftp.listdir()
        backups = sorted([f for f in archivos if f.startswith('lvm') and f.endswith('.db')])
        # Si hay más de 2, borrar los más antiguos (dejar solo los 2 más recientes)
        if len(backups) > 2:
            for f in backups[:-2]:
                try:
                    sftp.remove(f)
                    print(f'Backup antiguo eliminado: {f}')
                except Exception as e:
                    print(f'No se pudo eliminar {f}: {e}')
        # Subir el nuevo backup
        sftp.put(db_path, nombre_archivo)
        sftp.close()
        transport.close()
        print(f'Backup de la base de datos subido por SFTP como {nombre_archivo}.')
    except Exception as e:
        print(f'Error al subir el backup por SFTP: {e}')

def exportar_csv_a_ftp():
    sftp_host = os.environ.get('FTP_HOST')
    sftp_user = os.environ.get('FTP_USER')
    sftp_pass = os.environ.get('FTP_PASS')
    sftp_dir = os.environ.get('FTP_DIR', '/')
    fecha = datetime.now().strftime('%Y%m%d')
    nombre_archivo = f'lvm_csv_{fecha}.zip'
    
    if not sftp_host or not sftp_user or not sftp_pass:
        print('Faltan variables de entorno para la conexión SFTP.')
        return
    
    try:
        # Crear archivo ZIP con todos los CSV
        import zipfile
        zip_path = os.path.join(os.path.dirname(__file__), nombre_archivo)
        
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            # Exportar cada tabla a CSV
            tablas = [Cuenta, Movimiento, MovimientoConcepto]
            nombres_tablas = ['cuentas', 'movimientos', 'movimientos_conceptos']
            
            for tabla, nombre in zip(tablas, nombres_tablas):
                csv_buffer = io.StringIO()
                writer = csv.writer(csv_buffer)
                
                # Obtener datos de la tabla
                registros = tabla.query.all()
                
                if registros:
                    # Escribir encabezados
                    columnas = [column.name for column in tabla.__table__.columns]
                    writer.writerow(columnas)
                    
                    # Escribir datos
                    for registro in registros:
                        fila = []
                        for columna in columnas:
                            valor = getattr(registro, columna)
                            fila.append(str(valor) if valor is not None else '')
                        writer.writerow(fila)
                
                # Añadir CSV al ZIP
                zipf.writestr(f'{nombre}.csv', csv_buffer.getvalue())
        
        # Subir ZIP al FTP
        transport = paramiko.Transport((sftp_host, 22))
        transport.connect(username=sftp_user, password=sftp_pass)
        sftp = paramiko.SFTPClient.from_transport(transport)
        
        try:
            sftp.chdir(sftp_dir)
        except IOError:
            dirs = sftp_dir.strip('/').split('/')
            path = ''
            for d in dirs:
                path += '/' + d
                try:
                    sftp.chdir(path)
                except IOError:
                    sftp.mkdir(path)
                    sftp.chdir(path)
        
        # Listar archivos de backup CSV existentes
        archivos = sftp.listdir()
        backups_csv = sorted([f for f in archivos if f.startswith('lvm_csv_') and f.endswith('.zip')])
        # Si hay más de 2, borrar los más antiguos
        if len(backups_csv) > 2:
            for f in backups_csv[:-2]:
                try:
                    sftp.remove(f)
                    print(f'Backup CSV antiguo eliminado: {f}')
                except Exception as e:
                    print(f'No se pudo eliminar {f}: {e}')
        
        # Subir el nuevo backup CSV
        sftp.put(zip_path, nombre_archivo)
        sftp.close()
        transport.close()
        
        # Eliminar archivo ZIP local
        os.remove(zip_path)
        
        print(f'Backup CSV subido por SFTP como {nombre_archivo}.')
        
    except Exception as e:
        print(f'Error al exportar CSV por SFTP: {e}')

@app.route('/exportar_csv')
@login_required
def exportar_csv():
    try:
        # Crear archivo ZIP con todos los CSV en directorio temporal
        import zipfile
        import tempfile
        fecha = datetime.now().strftime('%Y%m%d')
        nombre_archivo = f'lvm_csv_{fecha}.zip'
        
        # Crear directorio temporal
        with tempfile.TemporaryDirectory() as temp_dir:
            zip_path = os.path.join(temp_dir, nombre_archivo)
            
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                # Exportar cada tabla a CSV
                tablas = [Cuenta, Movimiento, MovimientoConcepto]
                nombres_tablas = ['cuentas', 'movimientos', 'movimientos_conceptos']
                
                for tabla, nombre in zip(tablas, nombres_tablas):
                    csv_buffer = io.StringIO()
                    writer = csv.writer(csv_buffer)
                    
                    # Obtener datos de la tabla
                    registros = tabla.query.all()
                    
                    if registros:
                        # Escribir encabezados
                        columnas = [column.name for column in tabla.__table__.columns]
                        writer.writerow(columnas)
                        
                        # Escribir datos
                        for registro in registros:
                            fila = []
                            for columna in columnas:
                                valor = getattr(registro, columna)
                                fila.append(str(valor) if valor is not None else '')
                            writer.writerow(fila)
                    
                    # Añadir CSV al ZIP
                    zipf.writestr(f'{nombre}.csv', csv_buffer.getvalue())
                
            # Subir al FTP usando el archivo temporal
            exportar_csv_a_ftp_from_path(zip_path, nombre_archivo)
            
            # Devolver archivo para descarga (se eliminará automáticamente al salir del contexto)
            return send_file(zip_path, as_attachment=True, download_name=nombre_archivo)
        
    except Exception as e:
        flash(f'Error al exportar CSV: {e}', 'error')
        return redirect(url_for('index'))

def exportar_csv_a_ftp_from_path(zip_path, nombre_archivo):
    sftp_host = os.environ.get('FTP_HOST')
    sftp_user = os.environ.get('FTP_USER')
    sftp_pass = os.environ.get('FTP_PASS')
    sftp_dir = os.environ.get('FTP_DIR', '/')
    
    if not sftp_host or not sftp_user or not sftp_pass:
        print('Faltan variables de entorno para la conexión SFTP.')
        return
    
    try:
        # Subir ZIP al FTP
        transport = paramiko.Transport((sftp_host, 22))
        transport.connect(username=sftp_user, password=sftp_pass)
        sftp = paramiko.SFTPClient.from_transport(transport)
        
        try:
            sftp.chdir(sftp_dir)
        except IOError:
            dirs = sftp_dir.strip('/').split('/')
            path = ''
            for d in dirs:
                path += '/' + d
                try:
                    sftp.chdir(path)
                except IOError:
                    sftp.mkdir(path)
                    sftp.chdir(path)
        
        # Listar archivos de backup CSV existentes
        archivos = sftp.listdir()
        backups_csv = sorted([f for f in archivos if f.startswith('lvm_csv_') and f.endswith('.zip')])
        # Si hay más de 2, borrar los más antiguos
        if len(backups_csv) > 2:
            for f in backups_csv[:-2]:
                try:
                    sftp.remove(f)
                    print(f'Backup CSV antiguo eliminado: {f}')
                except Exception as e:
                    print(f'No se pudo eliminar {f}: {e}')
        
        # Subir el nuevo backup CSV
        sftp.put(zip_path, nombre_archivo)
        sftp.close()
        transport.close()
        
        print(f'Backup CSV subido por SFTP como {nombre_archivo}.')
        
    except Exception as e:
        print(f'Error al exportar CSV por SFTP: {e}')

@app.route('/importar_csv', methods=['GET', 'POST'])
@login_required
def importar_csv():
    if request.method == 'POST':
        if 'archivo' not in request.files:
            flash('No se seleccionó ningún archivo.', 'error')
            return redirect(url_for('importar_csv'))
        
        archivo = request.files['archivo']
        if archivo.filename == '':
            flash('No se seleccionó ningún archivo.', 'error')
            return redirect(url_for('importar_csv'))
        
        if not archivo.filename.endswith('.zip'):
            flash('El archivo debe ser un ZIP.', 'error')
            return redirect(url_for('importar_csv'))
        
        try:
            import zipfile
            import tempfile
            
            # Crear directorio temporal
            with tempfile.TemporaryDirectory() as temp_dir:
                # Guardar archivo ZIP
                zip_path = os.path.join(temp_dir, archivo.filename)
                archivo.save(zip_path)
                
                # Extraer y procesar CSV
                with zipfile.ZipFile(zip_path, 'r') as zipf:
                    # Mapeo de nombres de archivo a modelos
                    mapeo_tablas = {
                        'cuentas.csv': Cuenta,
                        'movimientos.csv': Movimiento,
                        'movimientos_conceptos.csv': MovimientoConcepto
                    }
                    
                    for nombre_archivo in zipf.namelist():
                        if nombre_archivo in mapeo_tablas:
                            modelo = mapeo_tablas[nombre_archivo]
                            
                            # Leer CSV
                            with zipf.open(nombre_archivo, 'r') as csv_file:
                                csv_reader = csv.reader(io.TextIOWrapper(csv_file, encoding='utf-8'))
                                encabezados = next(csv_reader)  # Saltar encabezados
                                
                                # Limpiar tabla existente
                                modelo.query.delete()
                                
                                # Insertar nuevos datos
                                for fila in csv_reader:
                                    if len(fila) == len(encabezados):
                                        registro = {}
                                        for i, columna in enumerate(encabezados):
                                            valor = fila[i]
                                            if valor == '':
                                                valor = None
                                            elif columna in ['id', 'cuenta_id', 'movimiento_id', 'contrapartida_id', 'cuenta_asociada_id']:
                                                try:
                                                    valor = int(valor) if valor else None
                                                except ValueError:
                                                    valor = None
                                            elif columna in ['base_imponible', 'total', 'importe']:
                                                try:
                                                    valor = float(valor) if valor else 0.0
                                                except ValueError:
                                                    valor = 0.0
                                            
                                            registro[columna] = valor
                                        
                                        nuevo_registro = modelo(**registro)
                                        db.session.add(nuevo_registro)
                
                db.session.commit()
                flash('Datos importados correctamente.', 'success')
                
        except Exception as e:
            flash(f'Error al importar CSV: {e}', 'error')
            db.session.rollback()
        
        return redirect(url_for('index'))
    
    return render_template('importar_csv.html')

@app.route('/extraer_nominas_pdf', methods=['GET', 'POST'])
def extraer_nominas_pdf():
    if request.method == 'GET':
        return render_template('extraer_nominas.html')
    
    if 'pdf_file' not in request.files:
        flash('No se seleccionó ningún archivo', 'error')
        return redirect(request.url)
    
    file = request.files['pdf_file']
    if file.filename == '':
        flash('No se seleccionó ningún archivo', 'error')
        return redirect(request.url)
    
    if file and file.filename.endswith('.pdf'):
        try:
            # Guardar el archivo subido
            pdf_path = os.path.join(app.root_path, file.filename)
            file.save(pdf_path)
            print(f"PDF guardado en: {pdf_path}")
            
            # Extraer texto del PDF
            texto_completo = ""
            with open(pdf_path, 'rb') as pdf_file:
                pdf_reader = PyPDF2.PdfReader(pdf_file)
                for page_num in range(len(pdf_reader.pages)):
                    page = pdf_reader.pages[page_num]
                    texto_completo += page.extract_text() + "\n"
            
            print(f"Texto extraído: {len(texto_completo)} caracteres")
            
            # Extraer datos específicos
            datos_nomina = extraer_datos_nomina(texto_completo)
            
            if not datos_nomina:
                flash('No se pudieron extraer datos del PDF', 'error')
                return redirect(request.url)
            
            # Crear Excel con los datos
            wb = Workbook()
            ws = wb.active
            ws.title = "Datos Nóminas"
            
            # Encabezados
            headers = ['Nombre Trabajador', 'Total Devengado', 'Total Aportaciones', 'IRPF', 'Líquido a Percibir', 'Total SS Empresa', 'Archivo PDF']
            for col, header in enumerate(headers, 1):
                ws.cell(row=1, column=col, value=header)
            
            # Datos
            row = 2
            ws.cell(row=row, column=1, value=datos_nomina.get('nombre_trabajador', ''))
            ws.cell(row=row, column=2, value=datos_nomina.get('total_devengado', ''))
            ws.cell(row=row, column=3, value=datos_nomina.get('total_aportaciones', ''))
            ws.cell(row=row, column=4, value=datos_nomina.get('irpf', ''))
            ws.cell(row=row, column=5, value=datos_nomina.get('liquido_percibir', ''))
            ws.cell(row=row, column=6, value=datos_nomina.get('total_ss_empresa', ''))
            ws.cell(row=row, column=7, value=file.filename)
            
            # Guardar Excel
            excel_path_temp = os.path.join(app.root_path, f'nomina_datos_{datetime.now().strftime("%Y%m%d")}.xlsx')
            wb.save(excel_path_temp)
            wb.close()
            
            # Copiar a ubicación final
            excel_path_final = os.path.join(app.root_path, f'nomina_datos_{datetime.now().strftime("%Y%m%d")}.xlsx')
            shutil.copy2(excel_path_temp, excel_path_final)
            
            print(f"Excel guardado en: {excel_path_final}")
            
            # Limpiar archivo temporal
            os.remove(pdf_path)
            os.remove(excel_path_temp)
            
            flash('Datos extraídos correctamente', 'success')
            
            # Enviar archivo al usuario
            response = send_file(
                excel_path_final,
                mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                as_attachment=True,
                download_name=f'nomina_datos_{datetime.now().strftime("%Y%m%d")}.xlsx'
            )
            
            @response.call_on_close
            def cleanup():
                try:
                    os.remove(excel_path_final)
                except:
                    pass
            
            return response
            
        except Exception as e:
            flash(f'Error al extraer datos de nóminas: {str(e)}', 'error')
            return redirect(request.url)
    else:
        flash('Por favor selecciona un archivo PDF válido', 'error')
        return redirect(request.url)

def extraer_datos_nomina(texto):
    """Extrae datos específicos del texto de la nómina"""
    datos = {}
    
    # Dividir el texto en líneas para análisis más preciso
    lineas = texto.split('\n')
    
    # Buscar nombres de trabajadores
    for i, linea in enumerate(lineas):
        if 'LOGISTICA VENANCIO MATEOS SL' in linea:
            # Buscar en las siguientes líneas el nombre
            for j in range(i+1, min(i+5, len(lineas))):
                nombre_match = re.search(r'([A-ZÁÉÍÓÚÑ][a-záéíóúñ]+\s+[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+(?:\s+[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+)*)', lineas[j])
                if nombre_match:
                    datos['nombre_trabajador'] = nombre_match.group(1).strip()
                    print(f"Encontrado nombre_trabajador: {datos['nombre_trabajador']}")
                    break
            break
    
    # Buscar total devengado - está en la línea que contiene "A. TOTAL DEVENGADO"
    for linea in lineas:
        if 'A. TOTAL DEVENGADO' in linea:
            # Buscar números en esa línea
            numeros = re.findall(r'([0-9]{1,3}(?:[.,][0-9]{3})*(?:[.,][0-9]{2})?)', linea)
            if numeros:
                datos['total_devengado'] = numeros[-1].replace(',', '.')  # Tomar el último número
                print(f"Encontrado total_devengado: {datos['total_devengado']}")
            break
    
    # Buscar total aportaciones - está en la línea que contiene "1-.TOTAL APORTACIONES"
    for linea in lineas:
        if '1-.TOTAL APORTACIONES' in linea:
            numeros = re.findall(r'([0-9]{1,3}(?:[.,][0-9]{3})*(?:[.,][0-9]{2})?)', linea)
            if numeros:
                datos['total_aportaciones'] = numeros[-1].replace(',', '.')
                print(f"Encontrado total_aportaciones: {datos['total_aportaciones']}")
            break
    
    # Buscar IRPF - está en la línea que contiene "2-. I.R.P.F"
    for linea in lineas:
        if '2-. I.R.P.F' in linea:
            numeros = re.findall(r'([0-9]{1,3}(?:[.,][0-9]{3})*(?:[.,][0-9]{2})?)', linea)
            if numeros:
                datos['irpf'] = numeros[-1].replace(',', '.')
                print(f"Encontrado irpf: {datos['irpf']}")
            break
    
    # Buscar líquido a percibir - está en la línea que contiene "LIQUIDO TOTAL A PERCIBIR"
    for linea in lineas:
        if 'LIQUIDO TOTAL A PERCIBIR' in linea:
            numeros = re.findall(r'([0-9]{1,3}(?:[.,][0-9]{3})*(?:[.,][0-9]{2})?)', linea)
            if numeros:
                datos['liquido_percibir'] = numeros[-1].replace(',', '.')
                print(f"Encontrado liquido_percibir: {datos['liquido_percibir']}")
            break
    
    # Buscar total SS empresa - está en la línea que contiene "Total SS Empresa"
    for linea in lineas:
        if 'Total SS Empresa' in linea:
            numeros = re.findall(r'([0-9]{1,3}(?:[.,][0-9]{3})*(?:[.,][0-9]{2})?)', linea)
            if numeros:
                datos['total_ss_empresa'] = numeros[-1].replace(',', '.')
                print(f"Encontrado total_ss_empresa: {datos['total_ss_empresa']}")
            break
    
    # Si no encuentra los datos con los patrones específicos, buscar de forma más general
    if 'total_devengado' not in datos:
        # Buscar números grandes que podrían ser el total devengado
        devengados_general = re.findall(r'([0-9]{1,3}(?:[.,][0-9]{3})*(?:[.,][0-9]{2})?)\s*\.{10,}', texto)
        if devengados_general:
            datos['total_devengado'] = devengados_general[0].replace(',', '.')
            print(f"Encontrado total_devengado (general): {datos['total_devengado']}")
    
    if 'total_aportaciones' not in datos:
        # Buscar números después de "TOTAL APORTACIONES"
        aportaciones_general = re.findall(r'TOTAL\s*APORTACIONES[^\d]*([0-9]{1,3}(?:[.,][0-9]{3})*(?:[.,][0-9]{2})?)', texto)
        if aportaciones_general:
            datos['total_aportaciones'] = aportaciones_general[0].replace(',', '.')
            print(f"Encontrado total_aportaciones (general): {datos['total_aportaciones']}")
    
    if 'irpf' not in datos:
        # Buscar números después de "I.R.P.F"
        irpf_general = re.findall(r'I\.R\.P\.F[^\d]*([0-9]{1,3}(?:[.,][0-9]{3})*(?:[.,][0-9]{2})?)', texto)
        if irpf_general:
            datos['irpf'] = irpf_general[0].replace(',', '.')
            print(f"Encontrado irpf (general): {datos['irpf']}")
    
    if 'liquido_percibir' not in datos:
        # Buscar números después de "A PERCIBIR"
        liquidos_general = re.findall(r'A\s*PERCIBIR[^\d]*([0-9]{1,3}(?:[.,][0-9]{3})*(?:[.,][0-9]{2})?)', texto)
        if liquidos_general:
            datos['liquido_percibir'] = liquidos_general[0].replace(',', '.')
            print(f"Encontrado liquido_percibir (general): {datos['liquido_percibir']}")
    
    if 'total_ss_empresa' not in datos:
        # Buscar números después de "SS Empresa"
        ss_general = re.findall(r'SS\s*Empresa[^\d]*([0-9]{1,3}(?:[.,][0-9]{3})*(?:[.,][0-9]{2})?)', texto)
        if ss_general:
            datos['total_ss_empresa'] = ss_general[0].replace(',', '.')
            print(f"Encontrado total_ss_empresa (general): {datos['total_ss_empresa']}")
    
    return datos

@app.route('/test_pdf_extraction')
def test_pdf_extraction():
    """Función de prueba para extraer texto del PDF"""
    pdf_path = os.path.join(app.root_path, 'LOGISTICA VENANCIO JULIO.pdf')
    
    if not os.path.exists(pdf_path):
        return f"<h3>Error: No se encontró el archivo PDF en {pdf_path}</h3>"
    
    try:
        texto_completo = ""
        with open(pdf_path, 'rb') as pdf_file:
            pdf_reader = PyPDF2.PdfReader(pdf_file)
            for page_num in range(len(pdf_reader.pages)):
                page = pdf_reader.pages[page_num]
                texto_pagina = page.extract_text()
                texto_completo += f"--- PÁGINA {page_num + 1} ---\n{texto_pagina}\n\n"
        
        return f"""
        <h3>Texto extraído del PDF</h3>
        <p><strong>Total de caracteres extraídos:</strong> {len(texto_completo)}</p>
        <p><strong>Archivo:</strong> LOGISTICA VENANCIO JULIO.pdf</p>
        <p><strong>Número de páginas:</strong> {len(pdf_reader.pages)}</p>
        <hr>
        <pre style="max-height: 500px; overflow-y: scroll; border: 1px solid #ccc; padding: 10px; background-color: #f9f9f9;">{texto_completo}</pre>
        """
        
    except Exception as e:
        return f"<h3>Error al extraer texto del PDF:</h3><p>{str(e)}</p>"

@app.route('/guardar_cambios', methods=['POST'])
@login_required
def guardar_cambios():
    subir_db_a_ftp()
    flash('Cambios guardados y base de datos subida correctamente.', 'success')
    return redirect(request.referrer or url_for('index'))

@app.template_filter('tipomov')
def tipomov(value):
    if value is None:
        return ''
    if value.lower() == 'gasto':
        return 'Gasto'
    elif value.lower() == 'ingreso':
        return 'Ingreso'
    return value.capitalize()

@app.template_filter('datetimeformat')
def datetimeformat(value, format='%d/%m/%Y'):
    try:
        from datetime import datetime
        return datetime.strptime(value, '%Y-%m-%d').strftime(format)
    except Exception:
        return value

@app.template_filter('dateinput')
def dateinput(value):
    """Convierte fecha de dd/mm/yyyy a yyyy-mm-dd para input type='date'"""
    try:
        from datetime import datetime
        if value:
            # Si ya está en formato yyyy-mm-dd, devolverlo tal como está
            if '-' in value and len(value.split('-')[0]) == 4:
                return value
            # Si está en formato dd/mm/yyyy, convertirlo
            return datetime.strptime(value, '%d/%m/%Y').strftime('%Y-%m-%d')
    except Exception:
        pass
    return value

@app.template_filter('limpiar_empleado')
def limpiar_empleado_filter(nombre):
    """Filtro de Jinja2 para limpiar nombres de empleados"""
    return limpiar_nombre_empleado(nombre)

@app.template_filter('mes_anio')
def mes_anio_filter(fecha):
    """Convierte fecha a formato mm-aaaa"""
    try:
        from datetime import datetime
        if not fecha:
            return ''
        # Si está en formato YYYY-MM-DD
        if '-' in fecha and len(fecha.split('-')[0]) == 4:
            fecha_obj = datetime.strptime(fecha, '%Y-%m-%d')
            return fecha_obj.strftime('%m-%Y')
        # Si está en formato dd/mm/yyyy
        elif '/' in fecha:
            fecha_obj = datetime.strptime(fecha, '%d/%m/%Y')
            return fecha_obj.strftime('%m-%Y')
    except Exception:
        pass
    return fecha

def convertir_fechas_para_filtro(fecha_inicio, fecha_fin):
    """
    Convierte fechas de YYYY-MM-DD a objetos datetime para usar en filtros de base de datos
    """
    fecha_inicio_dt = datetime.strptime(fecha_inicio, '%Y-%m-%d')
    fecha_fin_dt = datetime.strptime(fecha_fin, '%Y-%m-%d')
    return fecha_inicio_dt, fecha_fin_dt

def ejecutar_con_reintentos(funcion_db, max_reintentos=3, delay=0.1):
    """
    Ejecuta una función de base de datos con reintentos en caso de bloqueo
    """
    import time
    import random
    
    for intento in range(max_reintentos):
        try:
            return funcion_db()
        except Exception as e:
            if "database is locked" in str(e) and intento < max_reintentos - 1:
                # Esperar un tiempo aleatorio antes del siguiente intento
                tiempo_espera = delay * (2 ** intento) + random.uniform(0, 0.1)
                time.sleep(tiempo_espera)
                continue
            else:
                raise e
    
    return None

def commit_con_reintentos():
    """
    Hace commit de la sesión con reintentos en caso de bloqueo
    """
    def _commit():
        db.session.commit()
        return True
    
    try:
        ejecutar_con_reintentos(_commit)
        return True
    except Exception as e:
        db.session.rollback()
        raise e

def commit_seguro(operacion_descripcion="operación"):
    """
    Función helper para hacer commits seguros con manejo completo de errores.
    Asegura que siempre se haga rollback en caso de error.
    
    Args:
        operacion_descripcion: Descripción de la operación para mensajes de error
    
    Returns:
        True si el commit fue exitoso
    
    Raises:
        Exception: Si el commit falla después de los reintentos
    """
    try:
        commit_con_reintentos()
        return True
    except Exception as e:
        db.session.rollback()
        app.logger.error(f'Error en commit de {operacion_descripcion}: {str(e)}')
        raise e

def parsear_float_form(valor, por_defecto=0.0):
    """Convierte un valor de formulario a float, aceptando coma o punto decimal."""
    if valor is None:
        return por_defecto
    texto = str(valor).strip().replace(' ', '').replace('\xa0', '')
    if texto == '':
        return por_defecto
    if ',' in texto and '.' in texto:
        texto = texto.replace('.', '').replace(',', '.')
    elif ',' in texto:
        texto = texto.replace(',', '.')
    try:
        return float(texto)
    except (TypeError, ValueError):
        return por_defecto

def calcular_gasoil_desglose(con_iva, bonificacion=0.0, iva_porcentaje=None):
    """A partir del gasoil con IVA, obtiene sin IVA y el neto tras bonificación."""
    con_iva = con_iva or 0.0
    bonificacion = bonificacion or 0.0
    if iva_porcentaje is None:
        iva = IVA_GASOIL
    else:
        iva = (iva_porcentaje or 0) / 100.0
        if iva < 0:
            iva = IVA_GASOIL
    sin_iva = round(con_iva / (1 + iva), 2) if (1 + iva) else con_iva
    neto = round(sin_iva - bonificacion, 2)
    return sin_iva, neto

def asegurar_esquema_analisis():
    """Crea tablas nuevas y migra registros mixtos antiguos si los hay."""
    global _esquema_analisis_ok
    if _esquema_analisis_ok:
        return
    db.create_all()
    try:
        _asegurar_columnas_analisis()
        _migrar_registros_mixtos_analisis()
        _esquema_analisis_ok = True
    except Exception as e:
        app.logger.error(f'Error al actualizar esquema de análisis: {e}')

def _asegurar_columnas_analisis():
    inspector = inspect(db.engine)
    if 'registro_gasoil' not in inspector.get_table_names():
        return
    columnas = {c['name'] for c in inspector.get_columns('registro_gasoil')}
    if 'iva_porcentaje' not in columnas:
        db.session.execute(text('ALTER TABLE registro_gasoil ADD COLUMN iva_porcentaje FLOAT DEFAULT 21'))
        db.session.commit()

def _migrar_registros_mixtos_analisis():
    inspector = inspect(db.engine)
    if 'registro_analisis' not in inspector.get_table_names():
        return
    if RegistroIngreso.query.count() or RegistroGasoil.query.count():
        return
    antiguos = RegistroAnalisis.query.all()
    for old in antiguos:
        tiene_ingreso = any([
            old.km_realizados, old.ingreso_ruta, old.ingreso_chofer_adicional,
            old.ingreso_extra, old.ingreso_autopista, old.incremento_combustible,
        ])
        if tiene_ingreso:
            db.session.add(RegistroIngreso(
                camion_id=old.camion_id,
                anio=old.anio,
                mes=old.mes,
                km_realizados=old.km_realizados or 0,
                ingreso_ruta=old.ingreso_ruta or 0,
                ingreso_chofer_adicional=old.ingreso_chofer_adicional or 0,
                ingreso_extra=old.ingreso_extra or 0,
                ingreso_autopista=old.ingreso_autopista or 0,
                incremento_combustible=old.incremento_combustible or 0,
                observaciones=old.observaciones,
            ))
        tiene_gasoil = any([
            old.litros_gasoil, old.gasto_gasoil, old.gasto_gasoil_con_iva,
            old.gasto_addblue, old.bonificacion_gasoil,
        ])
        if tiene_gasoil:
            db.session.add(RegistroGasoil(
                camion_id=old.camion_id,
                anio=old.anio,
                mes=old.mes,
                marca_gasolinera='Sin especificar',
                tipo_gasoil='Gasóleo A',
                litros=old.litros_gasoil or 0,
                gasto_con_iva=old.gasto_gasoil_con_iva or 0,
                gasto_sin_iva=old.gasto_gasoil_sin_iva or 0,
                bonificacion=old.bonificacion_gasoil or 0,
                gasto_neto=old.gasto_gasoil or 0,
                gasto_addblue=old.gasto_addblue or 0,
                observaciones=old.observaciones,
            ))
    if antiguos:
        db.session.commit()

def valores_distintos(modelo, campo, base):
    extra = [row[0] for row in db.session.query(campo).distinct().all() if row[0]]
    return sorted(set(base) | set(extra), key=lambda x: x.lower())

def _camiones_para_formulario(registro=None):
    camiones = Camion.query.filter_by(activo=True).order_by(Camion.matricula).all()
    if registro and registro.camion and registro.camion not in camiones:
        camiones = [registro.camion] + camiones
    return camiones

def _rutas_para_formulario(registro=None):
    rutas = Ruta.query.filter_by(activa=True).order_by(Ruta.nombre).all()
    if registro:
        usadas = [t.ruta for t in registro.tramos if t.ruta]
        for ruta in usadas:
            if ruta not in rutas:
                rutas.append(ruta)
        rutas.sort(key=lambda r: (r.nombre or '').lower())
    return rutas

def parsear_entero_form(valor, por_defecto=0):
    """Convierte un valor de formulario a entero."""
    try:
        return int(valor)
    except (TypeError, ValueError):
        return por_defecto

def metricas_registro_analisis(registro, precio_oficial=None):
    """Calcula incidencia de gasoil, precio medio echa y €/km de un registro mensual."""
    ingresos = (
        (registro.ingreso_ruta or 0)
        + (registro.ingreso_chofer_adicional or 0)
        + (registro.ingreso_extra or 0)
        + (registro.ingreso_autopista or 0)
    )
    incremento = registro.incremento_combustible or 0
    bonus_calidad = getattr(registro, 'bonus_calidad', 0) or 0
    suplemento_hvo = getattr(registro, 'suplemento_hvo', 0) or 0
    ingresos_totales = ingresos + incremento + bonus_calidad + suplemento_hvo
    gastos = (registro.gasto_gasoil or 0) + (registro.gasto_addblue or 0)
    km = registro.km_realizados or 0
    litros = registro.litros_gasoil or 0
    gasto_gasoil = registro.gasto_gasoil or 0
    con_iva = getattr(registro, 'gasto_gasoil_con_iva', 0) or 0
    sin_iva = getattr(registro, 'gasto_gasoil_sin_iva', 0) or 0
    bonificacion = getattr(registro, 'bonificacion_gasoil', 0) or 0

    precio_echado = (gasto_gasoil / litros) if litros > 0 else None
    precio_echado_con_iva = (con_iva / litros) if litros > 0 and con_iva else None
    incidencia = (gasto_gasoil / ingresos_totales * 100) if ingresos_totales > 0 else None
    cobrado_km = (ingresos_totales / km) if km > 0 else None
    gasoil_km = (gasto_gasoil / km) if km > 0 else None
    base_desviacion = precio_echado_con_iva if precio_echado_con_iva is not None else precio_echado
    desviacion = None
    desviacion_pct = None
    if base_desviacion is not None and precio_oficial:
        desviacion = base_desviacion - precio_oficial
        desviacion_pct = (desviacion / precio_oficial * 100) if precio_oficial else None

    return {
        'ingresos': ingresos,
        'incremento': incremento,
        'bonus_calidad': bonus_calidad,
        'suplemento_hvo': suplemento_hvo,
        'ingresos_totales': ingresos_totales,
        'gastos': gastos,
        'km': km,
        'litros': litros,
        'gasto_gasoil': gasto_gasoil,
        'gasto_gasoil_con_iva': con_iva,
        'gasto_gasoil_sin_iva': sin_iva,
        'bonificacion_gasoil': bonificacion,
        'precio_echado': precio_echado,
        'precio_echado_con_iva': precio_echado_con_iva,
        'precio_oficial': precio_oficial,
        'incidencia': incidencia,
        'cobrado_km': cobrado_km,
        'gasoil_km': gasoil_km,
        'desviacion': desviacion,
        'desviacion_pct': desviacion_pct,
        'margen': ingresos_totales - gastos,
    }

def agregar_totales_metricas(totales, metricas):
    totales['km'] += metricas['km']
    totales['litros'] += metricas['litros']
    totales['gasto_gasoil'] += metricas['gasto_gasoil']
    totales['gasto_gasoil_con_iva'] += metricas.get('gasto_gasoil_con_iva') or 0
    totales['gasto_gasoil_sin_iva'] += metricas.get('gasto_gasoil_sin_iva') or 0
    totales['bonificacion_gasoil'] += metricas.get('bonificacion_gasoil') or 0
    totales['ingresos'] += metricas['ingresos']
    totales['incremento'] += metricas['incremento']
    totales['bonus_calidad'] = (totales.get('bonus_calidad') or 0) + (metricas.get('bonus_calidad') or 0)
    totales['suplemento_hvo'] = (totales.get('suplemento_hvo') or 0) + (metricas.get('suplemento_hvo') or 0)
    totales['ingresos_totales'] += metricas['ingresos_totales']
    totales['gastos'] += metricas['gastos']
    if metricas.get('precio_oficial') and metricas['litros'] > 0:
        totales['litros_con_oficial'] += metricas['litros']
        totales['coste_oficial'] += metricas['litros'] * metricas['precio_oficial']

def metricas_desde_totales(totales, precio_oficial=None):
    if precio_oficial is None and totales.get('litros_con_oficial'):
        precio_oficial = totales['coste_oficial'] / totales['litros_con_oficial']
    fake = type('T', (), {
        'ingreso_ruta': totales['ingresos'],
        'ingreso_chofer_adicional': 0,
        'ingreso_extra': 0,
        'ingreso_autopista': 0,
        'incremento_combustible': totales['incremento'],
        'bonus_calidad': totales.get('bonus_calidad') or 0,
        'suplemento_hvo': totales.get('suplemento_hvo') or 0,
        'gasto_gasoil': totales['gasto_gasoil'],
        'gasto_gasoil_con_iva': totales.get('gasto_gasoil_con_iva') or 0,
        'gasto_gasoil_sin_iva': totales.get('gasto_gasoil_sin_iva') or 0,
        'bonificacion_gasoil': totales.get('bonificacion_gasoil') or 0,
        'gasto_addblue': totales['gastos'] - totales['gasto_gasoil'],
        'km_realizados': totales['km'],
        'litros_gasoil': totales['litros'],
    })()
    return metricas_registro_analisis(fake, precio_oficial)

TOTALES_VACIOS = {
    'km': 0.0, 'litros': 0.0, 'gasto_gasoil': 0.0, 'ingresos': 0.0,
    'incremento': 0.0, 'ingresos_totales': 0.0, 'gastos': 0.0,
    'bonus_calidad': 0.0, 'suplemento_hvo': 0.0,
    'litros_con_oficial': 0.0, 'coste_oficial': 0.0,
    'gasto_gasoil_con_iva': 0.0, 'gasto_gasoil_sin_iva': 0.0, 'bonificacion_gasoil': 0.0,
}

def limpiar_nombre_empleado(nombre):
    """Limpia el nombre del empleado quitando prefijos y DNI"""
    if not nombre:
        return nombre
    
    # Quitar prefijos comunes
    nombre_limpio = nombre.replace('EMP ', '').replace('emp ', '').replace('Emp ', '')
    
    # Quitar DNI (patrón: números seguidos de letra o solo números)
    import re
    # Patrón para DNI español (8 números + letra o solo números)
    dni_pattern = r'\b\d{8}[A-Z]?\b|\b\d{9}\b'
    nombre_limpio = re.sub(dni_pattern, '', nombre_limpio)
    
    # Limpiar espacios extra
    nombre_limpio = ' '.join(nombre_limpio.split())
    
    return nombre_limpio.strip()

def parsear_fecha_robusto(fecha_str):
    """
    Intenta parsear una fecha en diferentes formatos comunes
    """
    if not fecha_str:
        return None
    
    # Lista de formatos a intentar
    formatos = [
        '%d/%m/%Y',    # 01/01/2024
        '%d/%m/%y',    # 01/01/24
        '%d-%m-%Y',    # 01-01-2024
        '%d-%m-%y',    # 01-01-24
        '%Y-%m-%d',    # 2024-01-01
        '%Y/%m/%d',    # 2024/01/01
        '%d/%m/%Y',    # 1/1/2024 (con ceros a la izquierda)
        '%d/%m/%y',    # 1/1/24 (con ceros a la izquierda)
    ]
    
    for formato in formatos:
        try:
            return datetime.strptime(fecha_str, formato)
        except ValueError:
            continue
    
    # Si ninguno funciona, intentar limpiar la fecha
    fecha_limpia = fecha_str.strip()
    if fecha_limpia != fecha_str:
        return parsear_fecha_robusto(fecha_limpia)
    
    return None

@app.route('/configurar_general_nominas', methods=['GET', 'POST'])
@login_required
def configurar_general_nominas():
    if request.method == 'POST':
        # Convertir fechas de dd/mm/yyyy a yyyy-mm-dd antes de guardar
        fecha_trabajo = request.form['fecha_trabajo']
        fecha_factura = request.form['fecha_factura']
        
        # Convertir formato si viene en dd/mm/yyyy
        if '/' in fecha_trabajo and len(fecha_trabajo.split('/')) == 3:
            partes = fecha_trabajo.split('/')
            if len(partes[0]) == 2 and len(partes[1]) == 2 and len(partes[2]) == 4:
                fecha_trabajo = f"{partes[2]}-{partes[1]}-{partes[0]}"
        
        if '/' in fecha_factura and len(fecha_factura.split('/')) == 3:
            partes = fecha_factura.split('/')
            if len(partes[0]) == 2 and len(partes[1]) == 2 and len(partes[2]) == 4:
                fecha_factura = f"{partes[2]}-{partes[1]}-{partes[0]}"
        
        # Guardar configuración general
        session['nomina_general_config'] = {
            'fecha_trabajo': fecha_trabajo,
            'fecha_factura': fecha_factura,
            'num_factura': request.form['num_factura']
        }
        flash('Configuración general de nóminas guardada correctamente.', 'success')
        return redirect(url_for('ver_todas_nominas'))
    
    # Cargar configuración existente o valores por defecto
    config = session.get('nomina_general_config', {
        'fecha_trabajo': datetime.now().strftime('%Y-%m-%d'),
        'fecha_factura': datetime.now().strftime('%Y-%m-%d'),
        'num_factura': f'Nómina {datetime.now().strftime("%B %Y")}'
    })
    
    # Convertir fechas a formato dd/mm/yyyy para mostrar en la interfaz
    if '-' in config['fecha_trabajo'] and len(config['fecha_trabajo'].split('-')) == 3:
        partes = config['fecha_trabajo'].split('-')
        if len(partes[0]) == 4 and len(partes[1]) == 2 and len(partes[2]) == 2:
            config['fecha_trabajo'] = f"{partes[2]}/{partes[1]}/{partes[0]}"
    
    if '-' in config['fecha_factura'] and len(config['fecha_factura'].split('-')) == 3:
        partes = config['fecha_factura'].split('-')
        if len(partes[0]) == 4 and len(partes[1]) == 2 and len(partes[2]) == 2:
            config['fecha_factura'] = f"{partes[2]}/{partes[1]}/{partes[0]}"
    
    return render_template('configurar_general_nominas.html', config=config)

@app.route('/generar_todas_nominas', methods=['POST'])
@login_required
def generar_todas_nominas():
    # Obtener configuración general
    general_config = session.get('nomina_general_config')
    if not general_config:
        flash('Debe configurar los parámetros generales primero.', 'error')
        return redirect(url_for('configurar_general_nominas'))
    
    # Obtener todos los empleados
    empleados = Cuenta.query.filter(
        Cuenta.tipo == 'contrapartida',
        Cuenta.nombre.like('EMP%')
    ).all()
    
    if not empleados:
        flash('No se encontraron empleados en la base de datos.', 'error')
        return redirect(url_for('configurar_general_nominas'))
    
    # Obtener cuentas necesarias
    cuentas = {
        'sueldos': Cuenta.query.filter_by(cuenta='640000000001').first(),
        'retencion': Cuenta.query.filter_by(cuenta='47510000001').first(),
        'ss_trabajador': Cuenta.query.filter_by(cuenta='642000000002').first(),
        'ss_empresa': Cuenta.query.filter_by(cuenta='642000000001').first(),
        'dietas': Cuenta.query.filter_by(cuenta='649000000002').first()
    }
    
    # Crear cuentas faltantes automáticamente
    cuentas_por_crear = {
        'sueldos': ('640000000001', 'SUELDOS Y SALARIOS'),
        'dietas': ('649000000002', 'DIETAS TRABAJADORES')
    }
    
    for nombre, (numero, nombre_cuenta) in cuentas_por_crear.items():
        if not cuentas[nombre]:
            nueva_cuenta = Cuenta(
                cuenta=numero,
                nombre=nombre_cuenta,
                tipo='normal'
            )
            db.session.add(nueva_cuenta)
            db.session.flush()
            cuentas[nombre] = nueva_cuenta
            print(f"Cuenta creada: {numero} - {nombre_cuenta}")
    
    # Commit para guardar las cuentas creadas
    db.session.commit()
    
    # Verificar que todas las cuentas existen
    cuentas_faltantes = [k for k, v in cuentas.items() if not v]
    if cuentas_faltantes:
        flash(f'Faltan las siguientes cuentas: {", ".join(cuentas_faltantes)}', 'error')
        return redirect(url_for('configurar_general_nominas'))
    

    
    movimientos_creados = 0
    
    for empleado in empleados:
        # Buscar la última nómina del empleado buscando movimientos donde él sea la contrapartida
        ultima_nomina = db.session.query(Movimiento).join(MovimientoConcepto).filter(
            MovimientoConcepto.contrapartida_id == empleado.id,
            Movimiento.tipo == 'Gasto',
            Movimiento.num_factura.like('Nómina%')
        ).order_by(Movimiento.fecha_trabajo.desc()).first()
        
        # Valores por defecto: 0 si no hay nómina previa, valores de la última nómina si existe
        valores_default = {
            'liquido_percibir': 0.00,
            'retencion_irpf': 0.00,
            'ss_trabajador': 0.00,
            'ss_empresa': 0.00,
            'dietas': 0.00
        }
        
        # Si existe una nómina anterior, usar esos valores
        if ultima_nomina:
            conceptos = MovimientoConcepto.query.filter_by(movimiento_id=ultima_nomina.id).all()
            
            for concepto in conceptos:
                if concepto.cuenta.cuenta == '640000000001':  # Sueldos
                    valores_default['liquido_percibir'] = concepto.importe
                elif concepto.cuenta.cuenta == '47510000001':  # Retención
                    valores_default['retencion_irpf'] = concepto.importe
                elif concepto.cuenta.cuenta == '642000000002':  # SS Trabajador
                    valores_default['ss_trabajador'] = concepto.importe
                elif concepto.cuenta.cuenta == '642000000001':  # SS Empresa
                    valores_default['ss_empresa'] = concepto.importe
                elif concepto.cuenta.cuenta == '649000000002':  # Dietas
                    valores_default['dietas'] = concepto.importe
        
        # Obtener configuración específica del empleado
        config_key = f'nomina_config_{empleado.id}'
        empleado_config = session.get(config_key, valores_default)
        
        # Calcular base imponible y total
        base_imponible = empleado_config['liquido_percibir'] + empleado_config['ss_trabajador'] + empleado_config['retencion_irpf']
        total = empleado_config['liquido_percibir'] + empleado_config['ss_empresa'] + empleado_config['dietas']
        
        # Crear movimiento principal
        # Añadir el nombre del empleado al número de factura para evitar confusión
        num_factura_empleado = f"{general_config['num_factura']} - {limpiar_nombre_empleado(empleado.nombre)}"
        movimiento = Movimiento(
            tipo='Gasto',
            fecha_trabajo=general_config['fecha_trabajo'],
            fecha_factura=general_config['fecha_factura'],
            num_factura=num_factura_empleado,
            base_imponible=base_imponible,
            total=total
        )
        db.session.add(movimiento)
        db.session.flush()  # Para obtener el ID del movimiento
        
        # Crear conceptos del movimiento
        conceptos = [
            (cuentas['sueldos'], empleado_config['liquido_percibir']),
            (cuentas['retencion'], empleado_config['retencion_irpf']),
            (cuentas['ss_trabajador'], empleado_config['ss_trabajador']),
            (cuentas['ss_empresa'], empleado_config['ss_empresa']),
            (cuentas['dietas'], empleado_config['dietas'])
        ]
        
        for cuenta, importe in conceptos:
            concepto = MovimientoConcepto(
                movimiento_id=movimiento.id,
                cuenta_id=cuenta.id,
                contrapartida_id=empleado.id,
                importe=importe,
                concepto=''
            )
            db.session.add(concepto)
        
        movimientos_creados += 1
        
        # Limpiar configuración específica del empleado
        session.pop(config_key, None)
    
    try:
        commit_seguro(f"generar {movimientos_creados} nóminas")
        flash(f'Se han creado {movimientos_creados} movimientos de nómina correctamente.', 'success')
        # Limpiar configuración general
        session.pop('nomina_general_config', None)
    except Exception as e:
        db.session.rollback()
        flash(f'Error al crear los movimientos de nómina: {str(e)}. No se ha guardado ningún movimiento.', 'error')
    
    return redirect(url_for('listar_movimientos'))

@app.route('/ver_todas_nominas')
@login_required
def ver_todas_nominas():
    # Obtener configuración general
    general_config = session.get('nomina_general_config', {
        'fecha_trabajo': datetime.now().strftime('%Y-%m-%d'),
        'fecha_factura': datetime.now().strftime('%Y-%m-%d'),
        'num_factura': f'Nómina {datetime.now().strftime("%B %Y")}'
    })
    
    # Convertir fechas a formato dd/mm/yyyy para mostrar en la interfaz
    config_display = general_config.copy()
    if '-' in config_display['fecha_trabajo'] and len(config_display['fecha_trabajo'].split('-')) == 3:
        partes = config_display['fecha_trabajo'].split('-')
        if len(partes[0]) == 4 and len(partes[1]) == 2 and len(partes[2]) == 2:
            config_display['fecha_trabajo'] = f"{partes[2]}/{partes[1]}/{partes[0]}"
    
    if '-' in config_display['fecha_factura'] and len(config_display['fecha_factura'].split('-')) == 3:
        partes = config_display['fecha_factura'].split('-')
        if len(partes[0]) == 4 and len(partes[1]) == 2 and len(partes[2]) == 2:
            config_display['fecha_factura'] = f"{partes[2]}/{partes[1]}/{partes[0]}"
    
    # Obtener todos los empleados
    empleados = Cuenta.query.filter(
        Cuenta.tipo == 'contrapartida',
        Cuenta.nombre.like('EMP%')
    ).order_by(Cuenta.nombre).all()
    
    # Obtener configuraciones de cada empleado
    empleados_config = []
    for empleado in empleados:
        # Buscar la última nómina del empleado buscando movimientos donde él sea la contrapartida
        ultima_nomina = db.session.query(Movimiento).join(MovimientoConcepto).filter(
            MovimientoConcepto.contrapartida_id == empleado.id,
            Movimiento.tipo == 'Gasto',
            Movimiento.num_factura.like('Nómina%')
        ).order_by(Movimiento.fecha_trabajo.desc()).first()
        
        # Valores por defecto: 0 si no hay nómina previa, valores de la última nómina si existe
        valores_default = {
            'liquido_percibir': 0.00,
            'retencion_irpf': 0.00,
            'ss_trabajador': 0.00,
            'ss_empresa': 0.00,
            'dietas': 0.00
        }
        
        # Si existe una nómina anterior, usar esos valores
        if ultima_nomina:
            conceptos = MovimientoConcepto.query.filter_by(movimiento_id=ultima_nomina.id).all()
            print(f"DEBUG - Empleado: {empleado.nombre} - Última nómina: {ultima_nomina.num_factura}")
            
            for concepto in conceptos:
                print(f"DEBUG - Concepto: Cuenta {concepto.cuenta.cuenta} - {concepto.cuenta.nombre}, Importe: {concepto.importe}")
                if concepto.cuenta.cuenta == '640000000001':  # Sueldos
                    valores_default['liquido_percibir'] = concepto.importe
                    print(f"DEBUG - Asignado liquido_percibir: {concepto.importe}")
                elif concepto.cuenta.cuenta == '47510000001':  # Retención
                    valores_default['retencion_irpf'] = concepto.importe
                    print(f"DEBUG - Asignado retencion_irpf: {concepto.importe}")
                elif concepto.cuenta.cuenta == '642000000002':  # SS Trabajador
                    valores_default['ss_trabajador'] = concepto.importe
                    print(f"DEBUG - Asignado ss_trabajador: {concepto.importe}")
                elif concepto.cuenta.cuenta == '642000000001':  # SS Empresa
                    valores_default['ss_empresa'] = concepto.importe
                    print(f"DEBUG - Asignado ss_empresa: {concepto.importe}")
                elif concepto.cuenta.cuenta == '649000000002':  # Dietas
                    valores_default['dietas'] = concepto.importe
                    print(f"DEBUG - Asignado dietas: {concepto.importe}")
                else:
                    print(f"DEBUG - Cuenta NO reconocida: {concepto.cuenta.cuenta}")
            
            print(f"DEBUG - Valores finales para {empleado.nombre}: {valores_default}")
        else:
            print(f"DEBUG - Empleado: {empleado.nombre} - No se encontró ninguna nómina anterior")
        
        # Obtener configuración actual o usar valores de la última nómina
        config_key = f'nomina_config_{empleado.id}'
        empleado_config = session.get(config_key, valores_default)
        
        # Calcular total
        total = empleado_config['liquido_percibir'] + empleado_config['ss_empresa'] + empleado_config['dietas'] + empleado_config['retencion_irpf'] + empleado_config['ss_trabajador']
        
        empleados_config.append({
            'empleado': empleado,
            'config': empleado_config,
            'total': total,
            'tiene_ultima_nomina': ultima_nomina is not None
        })
    
    return render_template('ver_todas_nominas.html', empleados_config=empleados_config, general_config=config_display)

@app.route('/guardar_config_empleado/<int:empleado_id>', methods=['POST'])
@login_required
def guardar_config_empleado(empleado_id):
    empleado = Cuenta.query.get_or_404(empleado_id)
    
    # Guardar configuración específica del empleado
    config_key = f'nomina_config_{empleado_id}'
    session[config_key] = {
        'liquido_percibir': float(request.form['liquido_percibir']),
        'retencion_irpf': float(request.form['retencion_irpf']),
        'ss_trabajador': float(request.form['ss_trabajador']),
        'ss_empresa': float(request.form['ss_empresa']),
        'dietas': float(request.form['dietas'])
    }
    
    flash(f'Configuración de {limpiar_nombre_empleado(empleado.nombre)} guardada correctamente.', 'success')
    return redirect(url_for('ver_todas_nominas'))

# ==================== CONTROL XPO ====================


# Análisis y estadísticas
@app.route('/cuentas_resumen', methods=['GET', 'POST'])
@login_required
def cuentas_resumen():
    """Página de resumen de todas las cuentas con importes"""
    # Calcular fechas por defecto - año completo
    hoy = datetime.today()
    fecha_inicio = datetime(hoy.year, 1, 1).strftime('%Y-%m-%d')
    fecha_fin = hoy.strftime('%Y-%m-%d')
    tipo_cuenta = 'contrapartida'  # Por defecto mostrar solo contrapartidas
    
    resumen_cuentas = []
    
    if request.method == 'POST':
        fecha_inicio = request.form['fecha_inicio']
        fecha_fin = request.form['fecha_fin']
        tipo_cuenta = request.form.get('tipo_cuenta', 'todas')
    
    # Convertir fechas para filtro
    fecha_inicio_dt, fecha_fin_dt = convertir_fechas_para_filtro(fecha_inicio, fecha_fin)
    
    # Obtener todos los conceptos en el rango de fechas
    # Incluir tanto las cuentas principales como las contrapartidas
    from sqlalchemy.orm import aliased
    
    CuentaPrincipal = aliased(Cuenta)
    CuentaContrapartida = aliased(Cuenta)
    
    conceptos = db.session.query(MovimientoConcepto, Movimiento, CuentaPrincipal, CuentaContrapartida)\
        .join(Movimiento)\
        .join(CuentaPrincipal, MovimientoConcepto.cuenta_id == CuentaPrincipal.id)\
        .outerjoin(CuentaContrapartida, MovimientoConcepto.contrapartida_id == CuentaContrapartida.id)\
        .all()
    
    # Filtrar por fecha
    conceptos_filtrados = []
    for c in conceptos:
        fecha_movimiento = parsear_fecha_robusto(c.Movimiento.fecha_factura)
        if fecha_movimiento and fecha_inicio_dt <= fecha_movimiento <= fecha_fin_dt:
            conceptos_filtrados.append(c)
    
    # Agrupar por cuenta (tanto principales como contrapartidas)
    cuentas_dict = {}
    
    for c in conceptos_filtrados:
        # Procesar cuenta principal
        cuenta_principal = c[2]  # CuentaPrincipal
        cuenta_contrapartida = c[3]  # CuentaContrapartida (puede ser None)
        
        # Procesar cuenta principal solo si no estamos filtrando por contrapartida
        if cuenta_principal and tipo_cuenta != 'contrapartida':
            cuenta_id = cuenta_principal.id
            cuenta_numero = cuenta_principal.cuenta
            cuenta_nombre = cuenta_principal.nombre
            cuenta_tipo = cuenta_principal.tipo
            
            # Filtrar por tipo de cuenta si se especifica
            if tipo_cuenta != 'todas':
                if tipo_cuenta == 'normal' and cuenta_tipo != 'normal':
                    continue
                elif tipo_cuenta == 'contrapartida' and cuenta_tipo != 'contrapartida':
                    continue
            
            if cuenta_id not in cuentas_dict:
                cuentas_dict[cuenta_id] = {
                    'cuenta': cuenta_numero,
                    'nombre': cuenta_nombre,
                    'tipo': cuenta_tipo,
                    'importe_total': 0,
                    'num_movimientos': 0,
                    'movimientos': []
                }
            
            cuentas_dict[cuenta_id]['importe_total'] += c[0].importe
            cuentas_dict[cuenta_id]['num_movimientos'] += 1
            
            # Agregar detalle del movimiento
            movimiento_info = {
                'fecha': c[1].fecha_factura,
                'num_factura': c[1].num_factura,
                'tipo': c[1].tipo,
                'importe': c[0].importe,
                'concepto': c[0].concepto or '',
                'contrapartida': f"{cuenta_contrapartida.cuenta} - {cuenta_contrapartida.nombre}" if cuenta_contrapartida else "Sin contrapartida"
            }
            cuentas_dict[cuenta_id]['movimientos'].append(movimiento_info)
        
        # Procesar cuenta contrapartida solo si estamos filtrando por contrapartida o todas
        if cuenta_contrapartida and tipo_cuenta in ['contrapartida', 'todas']:
            cuenta_id = cuenta_contrapartida.id
            cuenta_numero = cuenta_contrapartida.cuenta
            cuenta_nombre = cuenta_contrapartida.nombre
            cuenta_tipo = cuenta_contrapartida.tipo
            
            # Filtrar por tipo de cuenta si se especifica
            if tipo_cuenta != 'todas':
                if tipo_cuenta == 'normal' and cuenta_tipo != 'normal':
                    continue
                elif tipo_cuenta == 'contrapartida' and cuenta_tipo != 'contrapartida':
                    continue
            
            if cuenta_id not in cuentas_dict:
                cuentas_dict[cuenta_id] = {
                    'cuenta': cuenta_numero,
                    'nombre': cuenta_nombre,
                    'tipo': cuenta_tipo,
                    'importe_total': 0,
                    'num_movimientos': 0,
                    'movimientos': []
                }
            
            cuentas_dict[cuenta_id]['importe_total'] += c[0].importe
            cuentas_dict[cuenta_id]['num_movimientos'] += 1
            
            # Agregar detalle del movimiento
            movimiento_info = {
                'fecha': c[1].fecha_factura,
                'num_factura': c[1].num_factura,
                'tipo': c[1].tipo,
                'importe': c[0].importe,
                'concepto': c[0].concepto or '',
                'contrapartida': f"{cuenta_principal.cuenta} - {cuenta_principal.nombre}" if cuenta_principal else "Sin contrapartida"
            }
            cuentas_dict[cuenta_id]['movimientos'].append(movimiento_info)
    
    # Convertir a lista y ordenar por número de cuenta
    resumen_cuentas = list(cuentas_dict.values())
    resumen_cuentas.sort(key=lambda x: x['cuenta'])
    
    # Ordenar movimientos por fecha dentro de cada cuenta
    for cuenta in resumen_cuentas:
        cuenta['movimientos'].sort(key=lambda x: x['fecha'], reverse=True)
    
    return render_template('cuentas_resumen.html', 
                         resumen_cuentas=resumen_cuentas,
                         fecha_inicio=fecha_inicio,
                         fecha_fin=fecha_fin,
                         tipo_cuenta=tipo_cuenta)

@app.route('/control_xpo')
@login_required
def control_xpo():
    """Listar todos los viajes XPO"""
    viajes = ViajeXPO.query.order_by(ViajeXPO.fecha.desc(), ViajeXPO.hora.desc()).all()
    total_viajes = ViajeXPO.query.count()
    return render_template('control_xpo.html', viajes=viajes, total_viajes=total_viajes)

@app.route('/control_xpo/nuevo', methods=['GET', 'POST'])
@login_required
def nuevo_viaje():
    """Crear nuevo viaje"""
    if request.method == 'POST':
        fecha = request.form['fecha']
        hora = request.form.get('hora', '').strip() or None
        origen = request.form['origen']
        matricula_cabeza = request.form['matricula_cabeza'].upper().strip()
        matricula_remolque = request.form['matricula_remolque'].upper().strip()
        manifiesto = request.form.get('manifiesto', '').strip() or None
        facturado = request.form.get('facturado', 'no')
        
        # Convertir fecha de YYYY-MM-DD (formato input date) a DD/MM/YYYY para almacenar
        if '-' in fecha and len(fecha.split('-')) == 3:
            partes = fecha.split('-')
            if len(partes[0]) == 4:  # Formato YYYY-MM-DD
                fecha = f"{partes[2]}/{partes[1]}/{partes[0]}"  # Convertir a DD/MM/YYYY
        
        nuevo_viaje = ViajeXPO(
            fecha=fecha,
            hora=hora,
            origen=origen,
            matricula_cabeza=matricula_cabeza,
            matricula_remolque=matricula_remolque,
            manifiesto=manifiesto,
            facturado=facturado,
            origen_telegram=False
        )
        
        try:
            db.session.add(nuevo_viaje)
            commit_seguro("crear viaje")
            flash('Viaje registrado correctamente.', 'success')
            return redirect(url_for('control_xpo'))
        except Exception as e:
            db.session.rollback()
            flash(f'Error al registrar el viaje: {str(e)}', 'error')
            return render_template('viaje_form.html')
    
    return render_template('viaje_form.html')

@app.route('/control_xpo/editar/<int:id>', methods=['GET', 'POST'])
@login_required
def editar_viaje(id):
    """Editar viaje existente"""
    viaje = ViajeXPO.query.get_or_404(id)
    
    if request.method == 'POST':
        fecha = request.form['fecha']
        hora = request.form.get('hora', '').strip() or None
        origen = request.form['origen']
        matricula_cabeza = request.form['matricula_cabeza'].upper().strip()
        matricula_remolque = request.form['matricula_remolque'].upper().strip()
        manifiesto = request.form.get('manifiesto', '').strip() or None
        facturado = request.form.get('facturado', 'no')
        
        # Convertir fecha de YYYY-MM-DD (formato input date) a DD/MM/YYYY para almacenar
        if '-' in fecha and len(fecha.split('-')) == 3:
            partes = fecha.split('-')
            if len(partes[0]) == 4:  # Formato YYYY-MM-DD
                fecha = f"{partes[2]}/{partes[1]}/{partes[0]}"  # Convertir a DD/MM/YYYY
        
        try:
            viaje.fecha = fecha
            viaje.hora = hora
            viaje.origen = origen
            viaje.matricula_cabeza = matricula_cabeza
            viaje.matricula_remolque = matricula_remolque
            viaje.manifiesto = manifiesto
            viaje.facturado = facturado
            
            commit_seguro("editar viaje")
            flash('Viaje actualizado correctamente.', 'success')
            return redirect(url_for('control_xpo'))
        except Exception as e:
            db.session.rollback()
            flash(f'Error al actualizar el viaje: {str(e)}', 'error')
            return render_template('viaje_form.html', viaje=viaje)
    
    return render_template('viaje_form.html', viaje=viaje)

@app.route('/control_xpo/borrar/<int:id>', methods=['POST'])
@login_required
def borrar_viaje(id):
    """Borrar viaje"""
    try:
        viaje = ViajeXPO.query.get_or_404(id)
        db.session.delete(viaje)
        commit_seguro("borrar viaje")
        flash('Viaje borrado correctamente.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error al borrar el viaje: {str(e)}', 'error')
    return redirect(url_for('control_xpo'))

@app.route('/admin/importar_bd', methods=['GET', 'POST'])
@login_required
def importar_bd():
    """Importar base de datos completa (sustituye la actual; se hace copia de seguridad)."""
    if request.method == 'GET':
        return render_template('importar_bd.html')

    archivo = request.files.get('archivo_db')
    if not archivo or archivo.filename == '':
        flash('No se ha seleccionado ningún archivo.', 'error')
        return redirect(url_for('importar_bd'))

    if not archivo.filename.lower().endswith('.db'):
        flash('El archivo debe tener extensión .db', 'error')
        return redirect(url_for('importar_bd'))

    uri = app.config['SQLALCHEMY_DATABASE_URI']
    if not uri.startswith('sqlite:///'):
        flash('La importación solo está soportada para base de datos SQLite.', 'error')
        return redirect(url_for('importar_bd'))

    db_path = Path(uri.replace('sqlite:///', '').strip())
    if not db_path.is_absolute():
        db_path = Path(__file__).resolve().parent / db_path

    try:
        # Cerrar conexiones antes de tocar el archivo
        db.session.remove()
        db.engine.dispose()

        # Copia de seguridad de la BD actual
        if db_path.exists():
            backup_path = db_path.parent / f"{db_path.stem}_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}{db_path.suffix}"
            shutil.copy2(db_path, backup_path)

        # Guardar el archivo subido como nueva BD
        archivo.save(str(db_path))

        flash('Base de datos importada correctamente. Se creó una copia de seguridad de la anterior.', 'success')
    except Exception as e:
        app.logger.exception('Error al importar BD')
        flash(f'Error al importar la base de datos: {e}', 'error')

    return redirect(url_for('importar_bd'))

@app.route('/api/telegram/webhook', methods=['POST'])
def telegram_webhook():
    """Endpoint para recibir datos del bot de Telegram"""
    try:
        data = request.get_json()
        
        # Extraer datos del mensaje
        origen = data.get('origen')  # Algeciras o Valladolid
        
        # Los datos ya vienen extraídos del bot (que usa OpenAI)
        fecha_extraida = data.get('fecha', datetime.now().strftime('%d/%m/%Y'))
        hora_extraida = data.get('hora', datetime.now().strftime('%H:%M'))
        matricula_cabeza_extraida = data.get('matricula_cabeza', '').upper().strip()
        matricula_remolque_extraida = data.get('matricula_remolque', '').upper().strip()
        
        # Validar que tenemos los datos mínimos
        if not matricula_cabeza_extraida or not matricula_remolque_extraida:
            return jsonify({
                'success': False, 
                'error': 'No se pudieron extraer las matrículas de la imagen'
            }), 400
        
        # Crear nuevo viaje
        nuevo_viaje = ViajeXPO(
            fecha=fecha_extraida,
            hora=hora_extraida,
            origen=origen,
            matricula_cabeza=matricula_cabeza_extraida,
            matricula_remolque=matricula_remolque_extraida,
            manifiesto=None,
            facturado='no',
            origen_telegram=True
        )
        
        db.session.add(nuevo_viaje)
        commit_seguro("crear viaje desde Telegram")
        
        return jsonify({
            'success': True, 
            'message': 'Viaje registrado correctamente',
            'viaje_id': nuevo_viaje.id
        }), 200
        
    except Exception as e:
        db.session.rollback()
        app.logger.error(f'Error en webhook de Telegram: {str(e)}')
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/473_pagos_cuenta', methods=['GET', 'POST'])
def pagos_cuenta_473():
    """473 – Pagos a cuenta que minorará el Impuesto sobre Sociedades - Cuenta 47300000001"""
    resultado = None
    detalle = []
    total_importe = 0
    # Calcular fechas por defecto
    hoy = datetime.today()
    mes = hoy.month
    if mes <= 3:
        inicio_trimestre = datetime(hoy.year, 1, 1)
    elif mes <= 6:
        inicio_trimestre = datetime(hoy.year, 4, 1)
    elif mes <= 9:
        inicio_trimestre = datetime(hoy.year, 7, 1)
    else:
        inicio_trimestre = datetime(hoy.year, 10, 1)
    fecha_inicio = inicio_trimestre.strftime('%Y-%m-%d')
    fecha_fin = hoy.strftime('%Y-%m-%d')
    
    if request.method == 'POST':
        fecha_inicio = request.form['fecha_inicio']
        fecha_fin = request.form['fecha_fin']
        # Convertir fechas de YYYY-MM-DD a objetos datetime para la comparación
        fecha_inicio_dt, fecha_fin_dt = convertir_fechas_para_filtro(fecha_inicio, fecha_fin)
        
        # Buscar conceptos de la cuenta 47300000001 en ese rango de fechas
        # Usar LIKE para evitar problemas con espacios
        conceptos = db.session.query(MovimientoConcepto, Movimiento, Cuenta)\
            .join(Movimiento)\
            .join(Cuenta, MovimientoConcepto.cuenta_id == Cuenta.id)\
            .filter(Cuenta.cuenta.like('47300000001%'))\
            .all()
        
        # Filtrar por fecha usando comparación de datetime
        conceptos_filtrados = []
        for c in conceptos:
            fecha_movimiento = parsear_fecha_robusto(c.Movimiento.fecha_factura)
            if fecha_movimiento and fecha_inicio_dt <= fecha_movimiento <= fecha_fin_dt:
                conceptos_filtrados.append(c)
        
        conceptos = conceptos_filtrados
        
        # Debug: imprimir información sobre los conceptos encontrados
        print(f"473 Pagos a cuenta - Conceptos encontrados: {len(conceptos)}")
        
        # Agrupar y sumar importes por cuenta, incluyendo detalles de transacciones
        detalle_dict = {}
        for c in conceptos:
            cuenta = str(c.Cuenta.cuenta)
            key = cuenta
            if key not in detalle_dict:
                detalle_dict[key] = {
                    'cuenta': cuenta,
                    'nombre': c.Cuenta.nombre,
                    'importe': 0,
                    'transacciones': []
                }
            detalle_dict[key]['importe'] += c.MovimientoConcepto.importe
            # Añadir detalles de la transacción
            contrapartida_info = ""
            if c.MovimientoConcepto.contrapartida:
                contrapartida_info = f"{c.MovimientoConcepto.contrapartida.cuenta} - {c.MovimientoConcepto.contrapartida.nombre}"
            else:
                contrapartida_info = "Sin contrapartida"
            
            transaccion = {
                'fecha': c.Movimiento.fecha_factura,
                'contrapartida': contrapartida_info,
                'importe': c.MovimientoConcepto.importe,
                'concepto': c.MovimientoConcepto.concepto or "Sin concepto",
                'num_factura': c.Movimiento.num_factura or "Sin número"
            }
            detalle_dict[key]['transacciones'].append(transaccion)
        
        detalle = list(detalle_dict.values())
        # Ordenar por número de cuenta
        detalle = sorted(detalle, key=lambda x: x['cuenta'])
        total_importe = sum(d['importe'] for d in detalle)
        resultado = total_importe
    
    return render_template('473_pagos_cuenta.html', resultado=resultado, detalle=detalle, total_importe=total_importe, fecha_inicio=fecha_inicio, fecha_fin=fecha_fin)

def _acumular_gasoil_en(totales, g):
    totales['litros'] += g.litros or 0
    totales['gasto_gasoil'] += g.gasto_neto or 0
    totales['gasto_gasoil_con_iva'] += g.gasto_con_iva or 0
    totales['gasto_gasoil_sin_iva'] += g.gasto_sin_iva or 0
    totales['bonificacion_gasoil'] += g.bonificacion or 0
    totales['gastos'] += (g.gasto_neto or 0) + (g.gasto_addblue or 0)

def _acumular_ingreso_en(totales, ing):
    ingresos = (
        (ing.ingreso_ruta or 0)
        + (ing.ingreso_chofer_adicional or 0)
        + (ing.ingreso_extra or 0)
        + (ing.ingreso_autopista or 0)
    )
    totales['km'] += ing.km_realizados or 0
    totales['ingresos'] += ingresos
    totales['incremento'] += ing.incremento_combustible or 0
    totales['ingresos_totales'] += ingresos + (ing.incremento_combustible or 0)

def _km_flota_por_mes(anio):
    """Km de cada camión y total de flota por mes, para repartir bonus/HVO."""
    por_mes = {}
    for ing in RegistroIngreso.query.filter_by(anio=anio).all():
        datos = por_mes.setdefault(ing.mes, {'total': 0.0, 'por_camion': {}})
        km = ing.km_realizados or 0
        datos['por_camion'][ing.camion_id] = datos['por_camion'].get(ing.camion_id, 0) + km
        datos['total'] += km
    return por_mes

def _aplicar_repartos(por_clave, anio, camion_id=0, camiones_por_id=None, mes=0):
    """Reparte bonus calidad y suplemento HVO del mes a razón de los km de cada camión."""
    consulta = RegistroReparto.query.filter_by(anio=anio)
    if mes:
        consulta = consulta.filter_by(mes=mes)
    repartos = consulta.all()
    if not repartos:
        return
    km_flota = _km_flota_por_mes(anio)
    camiones_por_id = camiones_por_id or {}
    for reparto in repartos:
        datos_mes = km_flota.get(reparto.mes) or {'total': 0, 'por_camion': {}}
        total_km = datos_mes['total'] or 0
        if total_km <= 0 or not (reparto.importe or 0):
            continue
        campo = 'bonus_calidad' if reparto.tipo == 'bonus_calidad' else 'suplemento_hvo'
        items = sorted(
            ((cid, km) for cid, km in datos_mes['por_camion'].items() if (km or 0) > 0),
            key=lambda x: x[0],
        )
        asignado = 0.0
        for i, (cid, km) in enumerate(items):
            if km <= 0:
                continue
            if i == len(items) - 1:
                share = round((reparto.importe or 0) - asignado, 2)
            else:
                share = round((reparto.importe or 0) * km / total_km, 2)
                asignado += share
            if camion_id and cid != camion_id:
                continue
            key = (cid, reparto.mes)
            if key not in por_clave:
                por_clave[key] = {
                    'camion': camiones_por_id.get(cid),
                    'mes': reparto.mes,
                    'totales': dict(TOTALES_VACIOS),
                    'ingreso': None,
                }
            por_clave[key]['totales'][campo] = (por_clave[key]['totales'].get(campo) or 0) + share

def _listar_repercusiones(por_clave, anio, mes=0):
    """Parte de bonus y HVO que corresponde a cada camión, mes a mes, dentro del filtro."""
    km_flota = _km_flota_por_mes(anio)
    consulta = RegistroReparto.query.filter_by(anio=anio)
    if mes:
        consulta = consulta.filter_by(mes=mes)
    importes = {}
    for reparto in consulta.all():
        slot = importes.setdefault(reparto.mes, {'bonus_calidad': 0.0, 'suplemento_hvo': 0.0})
        if reparto.tipo in slot:
            slot[reparto.tipo] = reparto.importe or 0

    lineas_por_mes = {}
    for (cid, mes_val), bucket in por_clave.items():
        if mes and mes_val != mes:
            continue
        bonus = bucket['totales'].get('bonus_calidad') or 0
        hvo = bucket['totales'].get('suplemento_hvo') or 0
        if not bonus and not hvo:
            continue
        datos = km_flota.get(mes_val) or {}
        km_total = datos.get('total') or 0
        km = (datos.get('por_camion') or {}).get(cid, 0) or 0
        lineas_por_mes.setdefault(mes_val, []).append({
            'camion': bucket.get('camion'),
            'km': km,
            'pct': (km / km_total * 100) if km_total else 0,
            'bonus_calidad': bonus,
            'suplemento_hvo': hvo,
            'total': bonus + hvo,
        })

    grupos = []
    for mes_val in sorted(set(importes) | set(lineas_por_mes)):
        imp = importes.get(mes_val) or {'bonus_calidad': 0.0, 'suplemento_hvo': 0.0}
        if not (imp['bonus_calidad'] or imp['suplemento_hvo'] or lineas_por_mes.get(mes_val)):
            continue
        lineas = sorted(
            lineas_por_mes.get(mes_val, []),
            key=lambda x: x['camion'].matricula if x['camion'] else '',
        )
        datos = km_flota.get(mes_val) or {}
        grupos.append({
            'mes': mes_val,
            'mes_nombre': MESES_NOMBRE.get(mes_val, mes_val),
            'bonus_mes': imp['bonus_calidad'],
            'hvo_mes': imp['suplemento_hvo'],
            'km_total': datos.get('total') or 0,
            'lineas': lineas,
            'bonus_repercutido': sum(linea['bonus_calidad'] for linea in lineas),
            'hvo_repercutido': sum(linea['suplemento_hvo'] for linea in lineas),
        })
    return grupos

# ==================== ANÁLISIS DE CAMIONES / GASOIL ====================

def _filtros_analisis():
    hoy = datetime.today()
    anio = parsear_entero_form(request.args.get('anio', hoy.year), hoy.year)
    mes = parsear_entero_form(request.args.get('mes', 0), 0)
    camion_id = parsear_entero_form(request.args.get('camion_id', 0), 0)
    return anio, mes, camion_id

def _resumen_analisis(anio, mes=0, camion_id=0):
    """Cruza ingresos y gasoil por camión y mes."""
    camiones = Camion.query.order_by(Camion.matricula).all()
    camiones_por_id = {c.id: c for c in camiones}

    q_ing = RegistroIngreso.query.filter_by(anio=anio)
    q_gas = RegistroGasoil.query.filter_by(anio=anio)
    if mes:
        q_ing = q_ing.filter_by(mes=mes)
        q_gas = q_gas.filter_by(mes=mes)
    if camion_id:
        q_ing = q_ing.filter_by(camion_id=camion_id)
        q_gas = q_gas.filter_by(camion_id=camion_id)
    ingresos = q_ing.order_by(RegistroIngreso.mes, RegistroIngreso.camion_id).all()
    repostajes = q_gas.order_by(RegistroGasoil.mes, RegistroGasoil.camion_id, RegistroGasoil.marca_gasolinera).all()

    precios_oficiales = {
        p.mes: p.precio
        for p in PrecioGasoilOficial.query.filter_by(anio=anio).all()
    }

    por_clave = {}
    def clave_bucket(camion_id_val, mes_val):
        key = (camion_id_val, mes_val)
        if key not in por_clave:
            por_clave[key] = {
                'camion': camiones_por_id.get(camion_id_val),
                'mes': mes_val,
                'totales': dict(TOTALES_VACIOS),
                'ingreso': None,
            }
        return por_clave[key]

    for ing in ingresos:
        bucket = clave_bucket(ing.camion_id, ing.mes)
        bucket['camion'] = ing.camion
        bucket['ingreso'] = ing
        _acumular_ingreso_en(bucket['totales'], ing)

    for g in repostajes:
        bucket = clave_bucket(g.camion_id, g.mes)
        bucket['camion'] = bucket['camion'] or g.camion
        _acumular_gasoil_en(bucket['totales'], g)
        if precios_oficiales.get(g.mes) and (g.litros or 0) > 0:
            bucket['totales']['litros_con_oficial'] += g.litros or 0
            bucket['totales']['coste_oficial'] += (g.litros or 0) * precios_oficiales[g.mes]

    _aplicar_repartos(por_clave, anio, camion_id, camiones_por_id, mes)
    repercusiones = _listar_repercusiones(por_clave, anio, mes)

    filas = []
    totales = dict(TOTALES_VACIOS)
    por_camion = {}
    por_mes_raw = {m: dict(TOTALES_VACIOS) for m in range(1, 13)}
    for (cid, mes_val), bucket in sorted(por_clave.items(), key=lambda x: (x[0][1], x[1]['camion'].matricula if x[1]['camion'] else '')):
        precio_oficial = precios_oficiales.get(mes_val)
        metricas = metricas_desde_totales(bucket['totales'], precio_oficial)
        filas.append({
            'camion': bucket['camion'],
            'ingreso': bucket['ingreso'],
            'mes': mes_val,
            'mes_nombre': MESES_NOMBRE.get(mes_val, mes_val),
            **metricas,
        })
        agregar_totales_metricas(totales, metricas)
        for k, v in bucket['totales'].items():
            por_mes_raw[mes_val][k] += v or 0
        if bucket['camion']:
            resumen = por_camion.setdefault(cid, {
                'camion': bucket['camion'],
                'totales': dict(TOTALES_VACIOS),
            })
            agregar_totales_metricas(resumen['totales'], metricas)

    precio_oficial_filtro = precios_oficiales.get(mes) if mes else None
    totales_metricas = metricas_desde_totales(totales, precio_oficial_filtro)
    resumen_camiones = []
    for datos in por_camion.values():
        resumen_camiones.append({
            'camion': datos['camion'],
            **metricas_desde_totales(datos['totales'], precio_oficial_filtro),
        })
    resumen_camiones.sort(key=lambda x: x['camion'].matricula)
    resumen_meses = [
        {
            'mes': m,
            'mes_nombre': MESES_NOMBRE[m],
            **metricas_desde_totales(por_mes_raw[m], precios_oficiales.get(m)),
        }
        for m in range(1, 13)
    ]

    años_disponibles = sorted({
        row[0] for row in (
            list(db.session.query(RegistroIngreso.anio).distinct().all())
            + list(db.session.query(RegistroGasoil.anio).distinct().all())
            + list(db.session.query(RegistroReparto.anio).distinct().all())
        ) if row[0]
    } | {anio, datetime.today().year})

    filas_gasoil = []
    for g in repostajes:
        litros = g.litros or 0
        filas_gasoil.append({
            'registro': g,
            'mes_nombre': MESES_NOMBRE.get(g.mes, g.mes),
            'precio_neto': (g.gasto_neto / litros) if litros else None,
            'precio_con_iva': (g.gasto_con_iva / litros) if litros and g.gasto_con_iva else None,
        })

    return {
        'filas': filas,
        'filas_ingresos': ingresos,
        'filas_gasoil': filas_gasoil,
        'resumen_camiones': resumen_camiones,
        'resumen_meses': resumen_meses,
        'totales': totales_metricas,
        'camiones': camiones,
        'años_disponibles': años_disponibles,
        'precios_oficiales': precios_oficiales,
        'repartos': RegistroReparto.query.filter_by(anio=anio).order_by(RegistroReparto.mes, RegistroReparto.tipo).all() if not mes else RegistroReparto.query.filter_by(anio=anio, mes=mes).order_by(RegistroReparto.tipo).all(),
        'repercusiones': repercusiones,
    }

def _punto_grafica(nombre, metricas):
    ingresos = metricas.get('ingresos_totales') or 0
    gasoil = metricas.get('gasto_gasoil') or 0
    return {
        'nombre': nombre,
        'km': round(metricas.get('km') or 0, 0),
        'ingresos': round(ingresos, 2),
        'gasoil': round(gasoil, 2),
        'diferencia': round(ingresos - gasoil, 2),
        'incidencia': None if metricas.get('incidencia') is None else round(metricas['incidencia'], 1),
        'precio_l': None if metricas.get('precio_echado') is None else round(metricas['precio_echado'], 3),
        'cobrado_km': None if metricas.get('cobrado_km') is None else round(metricas['cobrado_km'], 3),
        'gasto_km': None if metricas.get('gasoil_km') is None else round(metricas['gasoil_km'], 3),
        'litros': round(metricas.get('litros') or 0, 1),
        'bonus': round(metricas.get('bonus_calidad') or 0, 2),
        'hvo': round(metricas.get('suplemento_hvo') or 0, 2),
    }

@app.route('/analisis')
@login_required
def analisis():
    """Dashboard: cruza ingresos y varios repostajes por camión y mes."""
    anio, mes, camion_id = _filtros_analisis()
    datos = _resumen_analisis(anio, mes, camion_id)
    return render_template(
        'analisis.html',
        filas=datos['filas'],
        filas_ingresos=datos['filas_ingresos'],
        filas_gasoil=datos['filas_gasoil'],
        resumen_camiones=datos['resumen_camiones'],
        totales=datos['totales'],
        camiones=datos['camiones'],
        anio=anio,
        mes=mes,
        camion_id=camion_id,
        meses=MESES_NOMBRE,
        años_disponibles=datos['años_disponibles'],
        precios_oficiales=datos['precios_oficiales'],
        repartos=datos['repartos'],
        repercusiones=datos['repercusiones'],
    )

@app.route('/analisis/grafica')
@login_required
def grafica_analisis():
    """Vista gráfica del mes: km, ingresos, gasoil, repercusión y €/km."""
    anio, mes, camion_id = _filtros_analisis()
    if 'mes' not in request.args:
        datos_anio = _resumen_analisis(anio, 0, camion_id)
        meses_con_datos = [fila['mes'] for fila in datos_anio['filas']]
        mes = max(meses_con_datos) if meses_con_datos else datetime.today().month
    datos = _resumen_analisis(anio, mes, camion_id)
    datos_anio = _resumen_analisis(anio, 0, camion_id)
    camiones_serie = [
        _punto_grafica(fila['camion'].etiqueta() if fila['camion'] else 'Camión', fila)
        for fila in datos['resumen_camiones']
    ]
    meses_serie = [
        _punto_grafica(fila['mes_nombre'], fila)
        for fila in datos_anio['resumen_meses']
        if (fila.get('km') or 0) or (fila.get('ingresos_totales') or 0) or (fila.get('gasto_gasoil') or 0)
    ]
    return render_template(
        'analisis_grafica.html',
        totales=_punto_grafica('Total', datos['totales']),
        camiones_serie=camiones_serie,
        meses_serie=meses_serie,
        camiones=datos['camiones'],
        anio=anio,
        mes=mes,
        camion_id=camion_id,
        meses=MESES_NOMBRE,
        años_disponibles=datos['años_disponibles'],
        titulo_periodo=MESES_NOMBRE.get(mes, 'Año') + f' {anio}' if mes else f'Año {anio}',
    )

@app.route('/analisis/registro/nuevo')
@login_required
def nuevo_registro_analisis():
    return redirect(url_for('nuevo_ingreso_analisis', **request.args))

@app.route('/analisis/ingresos/nuevo', methods=['GET', 'POST'])
@login_required
def nuevo_ingreso_analisis():
    return _guardar_ingreso_analisis()

@app.route('/analisis/ingresos/editar/<int:id>', methods=['GET', 'POST'])
@login_required
def editar_ingreso_analisis(id):
    return _guardar_ingreso_analisis(RegistroIngreso.query.get_or_404(id))

def _guardar_ingreso_analisis(registro=None):
    camiones = _camiones_para_formulario(registro)
    if not camiones and request.method == 'GET':
        flash('Primero debes dar de alta al menos un camión.', 'error')
        return redirect(url_for('listar_camiones_analisis'))
    hoy = datetime.today()
    if request.method == 'POST':
        camion_id = parsear_entero_form(request.form.get('camion_id'))
        anio = parsear_entero_form(request.form.get('anio'), hoy.year)
        mes = parsear_entero_form(request.form.get('mes'), hoy.month)
        camion = Camion.query.get(camion_id)
        if not camion or mes < 1 or mes > 12:
            flash('Selecciona un camión y un mes válidos.', 'error')
        else:
            existente = RegistroIngreso.query.filter_by(camion_id=camion_id, anio=anio, mes=mes).first()
            if existente and (registro is None or existente.id != registro.id):
                flash('Ya hay ingresos de ese camión y mes. Se ha abierto para editarlos.', 'error')
                return redirect(url_for('editar_ingreso_analisis', id=existente.id))
            if registro is None:
                registro = RegistroIngreso()
                db.session.add(registro)
            registro.camion_id = camion_id
            registro.anio = anio
            registro.mes = mes
            registro.ingreso_ruta = parsear_float_form(request.form.get('ingreso_ruta'))
            registro.ingreso_chofer_adicional = parsear_float_form(request.form.get('ingreso_chofer_adicional'))
            registro.ingreso_extra = parsear_float_form(request.form.get('ingreso_extra'))
            registro.ingreso_autopista = parsear_float_form(request.form.get('ingreso_autopista'))
            registro.incremento_combustible = parsear_float_form(request.form.get('incremento_combustible'))
            registro.observaciones = (request.form.get('observaciones') or '').strip() or None
            db.session.flush()
            _guardar_tramos_ingreso(registro)
            km_form = (request.form.get('km_realizados') or '').strip()
            if km_form:
                registro.km_realizados = parsear_float_form(km_form)
            elif registro.tramos:
                registro.km_realizados = sum(t.km_tramo() for t in registro.tramos)
            else:
                registro.km_realizados = 0
            try:
                commit_seguro("guardar ingresos de análisis")
                flash('Ingresos guardados correctamente.', 'success')
                return redirect(url_for('analisis', anio=anio, mes=mes, camion_id=camion_id))
            except IntegrityError:
                db.session.rollback()
                flash('Ya existe un registro de ingresos para ese camión y mes.', 'error')
            except Exception as e:
                db.session.rollback()
                flash(f'Error al guardar los ingresos: {str(e)}', 'error')
    return render_template(
        'analisis_ingreso_form.html',
        registro=registro,
        camiones=camiones,
        meses=MESES_NOMBRE,
        anio_actual=hoy.year,
        anio_pref=parsear_entero_form(request.args.get('anio'), hoy.year),
        mes_pref=parsear_entero_form(request.args.get('mes'), hoy.month),
        camion_pref=parsear_entero_form(request.args.get('camion_id'), 0),
        rutas=_rutas_para_formulario(registro),
        tramos=list(registro.tramos) if registro else [],
    )

@app.route('/analisis/ingresos/borrar/<int:id>', methods=['POST'])
@login_required
def borrar_ingreso_analisis(id):
    registro = RegistroIngreso.query.get_or_404(id)
    anio = registro.anio
    try:
        db.session.delete(registro)
        commit_seguro("borrar ingresos de análisis")
        flash('Ingresos borrados correctamente.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error al borrar los ingresos: {str(e)}', 'error')
    return redirect(url_for('analisis', anio=anio))

def _guardar_tramos_ingreso(registro):
    for tramo in list(registro.tramos):
        db.session.delete(tramo)
    db.session.flush()
    ruta_ids = request.form.getlist('tramo_ruta_id')
    viajes_list = request.form.getlist('tramo_viajes')
    for ruta_id_str, viajes_str in zip(ruta_ids, viajes_list):
        ruta_id = parsear_entero_form(ruta_id_str)
        num_viajes = parsear_float_form(viajes_str)
        if not ruta_id or num_viajes <= 0:
            continue
        ruta = Ruta.query.get(ruta_id)
        if not ruta:
            continue
        db.session.add(RegistroIngresoTramo(
            ingreso=registro,
            ruta_id=ruta.id,
            num_viajes=num_viajes,
        ))
    db.session.flush()

@app.route('/analisis/rutas', methods=['GET', 'POST'])
@login_required
def listar_rutas_analisis():
    if request.method == 'POST':
        nombre = (request.form.get('nombre') or '').strip()
        km = parsear_float_form(request.form.get('km'))
        observaciones = (request.form.get('observaciones') or '').strip() or None
        if not nombre:
            flash('El nombre de la ruta es obligatorio.', 'error')
        elif km <= 0:
            flash('Los km de la ruta deben ser mayores que 0.', 'error')
        elif Ruta.query.filter_by(nombre=nombre).first():
            flash('Ya existe una ruta con ese nombre.', 'error')
        else:
            db.session.add(Ruta(nombre=nombre, km=km, observaciones=observaciones, activa=True))
            try:
                commit_seguro("crear ruta")
                flash('Ruta añadida correctamente.', 'success')
                return redirect(url_for('listar_rutas_analisis'))
            except IntegrityError:
                db.session.rollback()
                flash('Ya existe una ruta con ese nombre.', 'error')
            except Exception as e:
                db.session.rollback()
                flash(f'Error al crear la ruta: {str(e)}', 'error')
    rutas = Ruta.query.order_by(Ruta.nombre).all()
    return render_template('analisis_rutas.html', rutas=rutas)

@app.route('/analisis/rutas/editar/<int:id>', methods=['POST'])
@login_required
def editar_ruta_analisis(id):
    ruta = Ruta.query.get_or_404(id)
    nombre = (request.form.get('nombre') or '').strip()
    km = parsear_float_form(request.form.get('km'))
    observaciones = (request.form.get('observaciones') or '').strip() or None
    activa = request.form.get('activa') == 'on'
    if not nombre or km <= 0:
        flash('Nombre y km de la ruta son obligatorios.', 'error')
        return redirect(url_for('listar_rutas_analisis'))
    duplicado = Ruta.query.filter(Ruta.nombre == nombre, Ruta.id != ruta.id).first()
    if duplicado:
        flash('Ya existe otra ruta con ese nombre.', 'error')
        return redirect(url_for('listar_rutas_analisis'))
    ruta.nombre = nombre
    ruta.km = km
    ruta.observaciones = observaciones
    ruta.activa = activa
    for tramo in ruta.tramos:
        if tramo.ingreso:
            tramo.ingreso.km_realizados = sum(t.km_tramo() for t in tramo.ingreso.tramos)
    try:
        commit_seguro("editar ruta")
        flash('Ruta actualizada correctamente.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error al actualizar la ruta: {str(e)}', 'error')
    return redirect(url_for('listar_rutas_analisis'))

@app.route('/analisis/rutas/borrar/<int:id>', methods=['POST'])
@login_required
def borrar_ruta_analisis(id):
    ruta = Ruta.query.get_or_404(id)
    if ruta.tramos:
        flash('No se puede borrar la ruta porque está usada en ingresos. Desactívala si ya no se usa.', 'error')
        return redirect(url_for('listar_rutas_analisis'))
    try:
        db.session.delete(ruta)
        commit_seguro("borrar ruta")
        flash('Ruta borrada correctamente.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error al borrar la ruta: {str(e)}', 'error')
    return redirect(url_for('listar_rutas_analisis'))

@app.route('/analisis/gasoil/nuevo', methods=['GET', 'POST'])
@login_required
def nuevo_gasoil_analisis():
    return _guardar_gasoil_analisis()

@app.route('/analisis/gasoil/editar/<int:id>', methods=['GET', 'POST'])
@login_required
def editar_gasoil_analisis(id):
    return _guardar_gasoil_analisis(RegistroGasoil.query.get_or_404(id))

def _guardar_gasoil_analisis(registro=None):
    camiones = _camiones_para_formulario(registro)
    if not camiones and request.method == 'GET':
        flash('Primero debes dar de alta al menos un camión.', 'error')
        return redirect(url_for('listar_camiones_analisis'))
    hoy = datetime.today()
    marcas = valores_distintos(RegistroGasoil, RegistroGasoil.marca_gasolinera, MARCAS_GASOLINERA)
    tipos = valores_distintos(RegistroGasoil, RegistroGasoil.tipo_gasoil, TIPOS_GASOIL)
    if request.method == 'POST':
        camion_id = parsear_entero_form(request.form.get('camion_id'))
        anio = parsear_entero_form(request.form.get('anio'), hoy.year)
        mes = parsear_entero_form(request.form.get('mes'), hoy.month)
        camion = Camion.query.get(camion_id)
        if not camion or mes < 1 or mes > 12:
            flash('Selecciona un camión y un mes válidos.', 'error')
        else:
            if registro is None:
                registro = RegistroGasoil()
                db.session.add(registro)
            con_iva = parsear_float_form(request.form.get('gasto_con_iva'))
            bonificacion = parsear_float_form(request.form.get('bonificacion'))
            iva_porcentaje = parsear_float_form(request.form.get('iva_porcentaje'), 21)
            if iva_porcentaje not in TIPOS_IVA:
                iva_porcentaje = 21
            sin_iva, neto = calcular_gasoil_desglose(con_iva, bonificacion, iva_porcentaje)
            registro.camion_id = camion_id
            registro.anio = anio
            registro.mes = mes
            registro.marca_gasolinera = (request.form.get('marca_gasolinera') or '').strip() or 'Sin especificar'
            registro.tipo_gasoil = (request.form.get('tipo_gasoil') or '').strip() or 'Gasóleo A'
            registro.litros = parsear_float_form(request.form.get('litros'))
            registro.gasto_con_iva = con_iva
            registro.gasto_sin_iva = sin_iva
            registro.bonificacion = bonificacion
            registro.gasto_neto = neto
            registro.gasto_addblue = parsear_float_form(request.form.get('gasto_addblue'))
            registro.iva_porcentaje = iva_porcentaje
            registro.observaciones = (request.form.get('observaciones') or '').strip() or None
            try:
                commit_seguro("guardar gasoil de análisis")
                flash('Registro de gasoil guardado. Puedes añadir otro de otra gasolinera o tipo.', 'success')
                return redirect(url_for('analisis', anio=anio, mes=mes, camion_id=camion_id))
            except Exception as e:
                db.session.rollback()
                flash(f'Error al guardar el gasoil: {str(e)}', 'error')
    return render_template(
        'analisis_gasoil_form.html',
        registro=registro,
        camiones=camiones,
        meses=MESES_NOMBRE,
        marcas=marcas,
        tipos=tipos,
        tipos_iva=TIPOS_IVA,
        anio_actual=hoy.year,
        iva_gasoil=IVA_GASOIL,
        anio_pref=parsear_entero_form(request.args.get('anio'), hoy.year),
        mes_pref=parsear_entero_form(request.args.get('mes'), hoy.month),
        camion_pref=parsear_entero_form(request.args.get('camion_id'), 0),
    )

@app.route('/analisis/gasoil/borrar/<int:id>', methods=['POST'])
@login_required
def borrar_gasoil_analisis(id):
    registro = RegistroGasoil.query.get_or_404(id)
    anio = registro.anio
    try:
        db.session.delete(registro)
        commit_seguro("borrar gasoil de análisis")
        flash('Registro de gasoil borrado correctamente.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error al borrar el gasoil: {str(e)}', 'error')
    return redirect(url_for('analisis', anio=anio))

@app.route('/analisis/camiones', methods=['GET', 'POST'])
@login_required
def listar_camiones_analisis():
    if request.method == 'POST':
        matricula = (request.form.get('matricula') or '').upper().strip()
        alias = (request.form.get('alias') or '').strip() or None
        if not matricula:
            flash('La matrícula es obligatoria.', 'error')
        elif Camion.query.filter_by(matricula=matricula).first():
            flash('Ya existe un camión con esa matrícula.', 'error')
        else:
            db.session.add(Camion(matricula=matricula, alias=alias, activo=True))
            try:
                commit_seguro("crear camión")
                flash('Camión añadido correctamente.', 'success')
                return redirect(url_for('listar_camiones_analisis'))
            except IntegrityError:
                db.session.rollback()
                flash('Ya existe un camión con esa matrícula.', 'error')
            except Exception as e:
                db.session.rollback()
                flash(f'Error al crear el camión: {str(e)}', 'error')

    camiones = Camion.query.order_by(Camion.matricula).all()
    return render_template('analisis_camiones.html', camiones=camiones)

@app.route('/analisis/camiones/editar/<int:id>', methods=['POST'])
@login_required
def editar_camion_analisis(id):
    camion = Camion.query.get_or_404(id)
    matricula = (request.form.get('matricula') or '').upper().strip()
    alias = (request.form.get('alias') or '').strip() or None
    activo = request.form.get('activo') == 'on'
    if not matricula:
        flash('La matrícula es obligatoria.', 'error')
        return redirect(url_for('listar_camiones_analisis'))
    duplicado = Camion.query.filter(Camion.matricula == matricula, Camion.id != camion.id).first()
    if duplicado:
        flash('Ya existe otro camión con esa matrícula.', 'error')
        return redirect(url_for('listar_camiones_analisis'))
    camion.matricula = matricula
    camion.alias = alias
    camion.activo = activo
    try:
        commit_seguro("editar camión")
        flash('Camión actualizado correctamente.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error al actualizar el camión: {str(e)}', 'error')
    return redirect(url_for('listar_camiones_analisis'))

@app.route('/analisis/camiones/borrar/<int:id>', methods=['POST'])
@login_required
def borrar_camion_analisis(id):
    camion = Camion.query.get_or_404(id)
    if camion.ingresos or camion.repostajes:
        flash('No se puede borrar el camión porque tiene registros de análisis. Desactívalo si ya no se usa.', 'error')
        return redirect(url_for('listar_camiones_analisis'))
    try:
        db.session.delete(camion)
        commit_seguro("borrar camión")
        flash('Camión borrado correctamente.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error al borrar el camión: {str(e)}', 'error')
    return redirect(url_for('listar_camiones_analisis'))

@app.route('/analisis/camiones/importar_xpo', methods=['POST'])
@login_required
def importar_camiones_xpo():
    matriculas = db.session.query(ViajeXPO.matricula_cabeza).distinct().all()
    creados = 0
    for (matricula,) in matriculas:
        if not matricula:
            continue
        matricula = matricula.upper().strip()
        if not Camion.query.filter_by(matricula=matricula).first():
            db.session.add(Camion(matricula=matricula, activo=True))
            creados += 1
    try:
        if creados:
            commit_seguro("importar camiones desde XPO")
        flash(f'Se importaron {creados} camiones desde Control XPO.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error al importar camiones: {str(e)}', 'error')
    return redirect(url_for('listar_camiones_analisis'))

@app.route('/analisis/precios', methods=['GET', 'POST'])
@login_required
def precios_oficiales_gasoil():
    hoy = datetime.today()
    anio = parsear_entero_form(request.values.get('anio', hoy.year), hoy.year)

    if request.method == 'POST':
        try:
            for mes in range(1, 13):
                texto = (request.form.get(f'precio_{mes}') or '').strip()
                existente = PrecioGasoilOficial.query.filter_by(anio=anio, mes=mes).first()
                if texto == '':
                    if existente:
                        db.session.delete(existente)
                    continue
                precio = parsear_float_form(texto, None)
                if precio is None or precio <= 0:
                    continue
                if existente:
                    existente.precio = precio
                else:
                    db.session.add(PrecioGasoilOficial(anio=anio, mes=mes, precio=precio))
            commit_seguro("guardar precios oficiales de gasoil")
            flash('Precios oficiales guardados correctamente.', 'success')
            return redirect(url_for('precios_oficiales_gasoil', anio=anio))
        except Exception as e:
            db.session.rollback()
            flash(f'Error al guardar los precios: {str(e)}', 'error')

    existentes = {
        p.mes: p.precio
        for p in PrecioGasoilOficial.query.filter_by(anio=anio).all()
    }
    meses_precios = [
        {'mes': mes, 'nombre': MESES_NOMBRE[mes], 'precio': existentes.get(mes)}
        for mes in range(1, 13)
    ]
    años_disponibles = sorted({
        row[0] for row in db.session.query(PrecioGasoilOficial.anio).distinct().all() if row[0]
    } | {anio, hoy.year, hoy.year - 1, hoy.year + 1})
    return render_template(
        'analisis_precios.html',
        anio=anio,
        meses_precios=meses_precios,
        años_disponibles=años_disponibles,
        meses=MESES_NOMBRE,
    )

@app.route('/analisis/repartos', methods=['GET', 'POST'])
@login_required
def repartos_analisis():
    """Bonus calidad y suplemento HVO mensuales, repartidos por km de cada camión."""
    hoy = datetime.today()
    anio = parsear_entero_form(request.values.get('anio', hoy.year), hoy.year)
    mes_prev = parsear_entero_form(request.values.get('mes', hoy.month), hoy.month)
    if mes_prev < 1 or mes_prev > 12:
        mes_prev = hoy.month

    if request.method == 'POST':
        try:
            for mes in range(1, 13):
                for tipo, _nombre in TIPOS_REPARTO:
                    texto = (request.form.get(f'{tipo}_{mes}') or '').strip()
                    existente = RegistroReparto.query.filter_by(anio=anio, mes=mes, tipo=tipo).first()
                    if texto == '':
                        if existente:
                            db.session.delete(existente)
                        continue
                    importe = parsear_float_form(texto)
                    if existente:
                        existente.importe = importe
                    else:
                        db.session.add(RegistroReparto(anio=anio, mes=mes, tipo=tipo, importe=importe))
            commit_seguro("guardar bonus calidad y suplemento HVO")
            flash('Repartos guardados. Se aplican a cada camión según sus km del mes.', 'success')
            return redirect(url_for('repartos_analisis', anio=anio, mes=mes_prev))
        except Exception as e:
            db.session.rollback()
            flash(f'Error al guardar los repartos: {str(e)}', 'error')

    existentes = {}
    for r in RegistroReparto.query.filter_by(anio=anio).all():
        existentes[(r.mes, r.tipo)] = r.importe

    km_flota = _km_flota_por_mes(anio)
    camiones = {c.id: c for c in Camion.query.order_by(Camion.matricula).all()}
    km_json = {}
    for mes, datos in km_flota.items():
        km_json[str(mes)] = {
            'total': datos['total'],
            'camiones': [
                {
                    'id': cid,
                    'nombre': camiones[cid].etiqueta() if cid in camiones else str(cid),
                    'km': km,
                }
                for cid, km in sorted(datos['por_camion'].items(), key=lambda x: (camiones[x[0]].matricula if x[0] in camiones else ''))
                if km > 0
            ],
        }

    meses_reparto = []
    for mes in range(1, 13):
        meses_reparto.append({
            'mes': mes,
            'nombre': MESES_NOMBRE[mes],
            'bonus_calidad': existentes.get((mes, 'bonus_calidad')),
            'suplemento_hvo': existentes.get((mes, 'suplemento_hvo')),
            'km_total': (km_flota.get(mes) or {}).get('total') or 0,
        })

    años_disponibles = sorted({
        row[0] for row in (
            list(db.session.query(RegistroIngreso.anio).distinct().all())
            + list(db.session.query(RegistroReparto.anio).distinct().all())
        ) if row[0]
    } | {anio, hoy.year, hoy.year - 1, hoy.year + 1})

    return render_template(
        'analisis_repartos.html',
        anio=anio,
        mes_prev=mes_prev,
        meses_reparto=meses_reparto,
        km_json=km_json,
        años_disponibles=años_disponibles,
        meses=MESES_NOMBRE,
    )

# Función para migrar la base de datos y agregar nuevos campos
def migrar_base_datos():
    """Migra la base de datos para agregar campos de declaración IVA"""
    try:
        # Crear las tablas si no existen (esto agregará los nuevos campos)
        with app.app_context():
            db.create_all()
            print("✅ Migración completada: Campos de declaración IVA agregados")
            return True
    except Exception as e:
        print(f"❌ Error en la migración: {str(e)}")
        return False

def iniciar_bot_telegram():
    """Inicia el bot de Telegram en un hilo separado"""
    try:
        # Importar aquí para evitar errores si telegram_bot no está disponible
        from telegram_bot import main as bot_main
        import config
        
        # Verificar que el token esté configurado
        if not config.TELEGRAM_BOT_TOKEN:
            print("⚠️  Bot de Telegram no iniciado: TELEGRAM_BOT_TOKEN no configurado en .env")
            print("   La aplicación Flask funcionará normalmente sin el bot.")
            return
        
        # Iniciar el bot en un hilo separado
        bot_thread = threading.Thread(target=bot_main, daemon=True)
        bot_thread.start()
        print("✅ Bot de Telegram iniciado en segundo plano")
        
    except ImportError as e:
        print(f"⚠️  No se pudo importar el módulo del bot de Telegram: {e}")
        print("   La aplicación Flask funcionará normalmente sin el bot.")
    except Exception as e:
        print(f"⚠️  Error al iniciar el bot de Telegram: {e}")
        print("   La aplicación Flask funcionará normalmente sin el bot.")

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        asegurar_esquema_analisis()
    
    # Iniciar el bot de Telegram antes de iniciar Flask
    iniciar_bot_telegram()
    
    # Iniciar Flask
    print("🌐 Iniciando aplicación Flask...")
    app.run(debug=True) 
