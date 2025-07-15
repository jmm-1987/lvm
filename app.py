from flask import Flask, render_template, request, redirect, url_for, flash, send_file
from flask_sqlalchemy import SQLAlchemy
import os
from datetime import datetime, date
import paramiko
from apscheduler.schedulers.background import BackgroundScheduler
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = 'pon_aqui_una_clave_secreta_larga_y_unica'

# Configuración de la base de datos SQLite
db_path = os.path.join(os.path.dirname(__file__), 'app.db')
app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{db_path}'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

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
    
    cuenta = db.relationship('Cuenta', foreign_keys=[cuenta_id])
    contrapartida = db.relationship('Cuenta', foreign_keys=[contrapartida_id]) # Relación con la cuenta de contrapartida

Movimiento.conceptos = db.relationship('MovimientoConcepto', backref='movimiento', cascade='all, delete-orphan')

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
        lvm_password = os.environ.get('LVM_PASSWORD')
        #lvm_password = "1"
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

# Proteger todas las rutas excepto login y static
@app.before_request
def require_login():
    if request.endpoint not in ('login', 'static') and not current_user.is_authenticated:
        return redirect(url_for('login'))

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
        cuenta = request.form['cuenta']
        nombre = request.form['nombre']
        tipo = request.form['tipo']
        anotaciones = request.form.get('anotaciones', '')
        cuenta_asociada_id = request.form.get('cuenta_asociada_id') if tipo == 'contrapartida' else None
        nueva = Cuenta(cuenta=cuenta, nombre=nombre, tipo=tipo, anotaciones=anotaciones, cuenta_asociada_id=cuenta_asociada_id)
        db.session.add(nueva)
        db.session.commit()
        return redirect(url_for('listar_cuentas'))
    return render_template('cuenta_form.html', cuentas_normales=cuentas_normales)

@app.route('/cuentas/editar/<int:id>', methods=['GET', 'POST'])
def editar_cuenta(id):
    cuenta = Cuenta.query.get_or_404(id)
    cuentas_normales = Cuenta.query.filter_by(tipo='normal').all()
    if request.method == 'POST':
        cuenta.cuenta = request.form['cuenta']
        cuenta.nombre = request.form['nombre']
        cuenta.tipo = request.form['tipo']
        cuenta.anotaciones = request.form.get('anotaciones', '')
        cuenta.cuenta_asociada_id = request.form.get('cuenta_asociada_id') if cuenta.tipo == 'contrapartida' else None
        db.session.commit()
        return redirect(url_for('listar_cuentas'))
    return render_template('cuenta_form.html', cuenta=cuenta, cuentas_normales=cuentas_normales)

@app.route('/cuentas/borrar/<int:id>', methods=['POST'])
def borrar_cuenta(id):
    cuenta = Cuenta.query.get_or_404(id)
    # Comprobar si la cuenta está asociada a algún concepto de movimiento
    conceptos = MovimientoConcepto.query.filter((MovimientoConcepto.cuenta_id == id) | (MovimientoConcepto.contrapartida_id == id)).first()
    if conceptos:
        flash('No se puede borrar la cuenta porque está asociada a movimientos.', 'error')
        return redirect(url_for('listar_cuentas'))
    db.session.delete(cuenta)
    db.session.commit()
    flash('Cuenta borrada correctamente.', 'success')
    return redirect(url_for('listar_cuentas'))

# Vistas para movimientos
@app.route('/movimientos')
def listar_movimientos():
    movimientos = Movimiento.query.all()
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
    return render_template('movimientos.html', movimientos=movimientos, conceptos_por_mov=conceptos_por_mov, contrapartida_por_mov=contrapartida_por_mov)

@app.route('/movimientos/nuevo', methods=['GET', 'POST'])
def nuevo_movimiento():
    cuentas_normales = Cuenta.query.filter_by(tipo='normal').all()
    cuentas_contrapartida = Cuenta.query.filter_by(tipo='contrapartida').all()
    if request.method == 'POST':
        datos = request.form
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
        db.session.commit()
        return redirect(url_for('listar_movimientos'))
    return render_template('movimiento_form.html', cuentas_normales=cuentas_normales, cuentas_contrapartida=cuentas_contrapartida)

@app.route('/movimientos/editar/<int:id>', methods=['GET', 'POST'])
def editar_movimiento(id):
    movimiento = Movimiento.query.get_or_404(id)
    cuentas_normales = Cuenta.query.filter_by(tipo='normal').all()
    cuentas_contrapartida = Cuenta.query.filter_by(tipo='contrapartida').all()
    if request.method == 'POST':
        datos = request.form
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
        db.session.commit()
        return redirect(url_for('listar_movimientos'))
    conceptos = MovimientoConcepto.query.filter_by(movimiento_id=movimiento.id).all()
    return render_template('movimiento_form.html', movimiento=movimiento, cuentas_normales=cuentas_normales, cuentas_contrapartida=cuentas_contrapartida, conceptos=conceptos)

@app.route('/movimientos/borrar/<int:id>', methods=['POST'])
def borrar_movimiento(id):
    movimiento = Movimiento.query.get_or_404(id)
    db.session.delete(movimiento)
    db.session.commit()
    flash('Movimiento borrado correctamente.', 'success')
    return redirect(url_for('listar_movimientos'))

@app.route('/resultado_explotacion', methods=['GET', 'POST'])
def resultado_explotacion():
    resultado = None
    detalle = []
    diferencia = None
    # Calcular fechas por defecto
    hoy = date.today()
    mes = hoy.month
    if mes <= 3:
        inicio_trimestre = date(hoy.year, 1, 1)
    elif mes <= 6:
        inicio_trimestre = date(hoy.year, 4, 1)
    elif mes <= 9:
        inicio_trimestre = date(hoy.year, 7, 1)
    else:
        inicio_trimestre = date(hoy.year, 10, 1)
    fecha_inicio = inicio_trimestre.strftime('%Y-%m-%d')
    fecha_fin = hoy.strftime('%Y-%m-%d')
    resultado_explotacion = None
    if request.method == 'POST':
        fecha_inicio = request.form['fecha_inicio']
        fecha_fin = request.form['fecha_fin']
        # Buscar conceptos en ese rango de fechas
        conceptos = db.session.query(MovimientoConcepto, Movimiento, Cuenta)\
            .join(Movimiento)\
            .join(Cuenta, MovimientoConcepto.cuenta_id == Cuenta.id)\
            .filter(
                Movimiento.fecha_factura >= fecha_inicio,
                Movimiento.fecha_factura <= fecha_fin
            ).all()
        # Detalle de cuentas específicas (6 y 7)
        prefijos = ('623','626','621','622','625','628','629','310','640','649','662','7')
        detalle = [
            {
                'cuenta': c.Cuenta.cuenta,
                'nombre': c.Cuenta.nombre,
                'importe': c.MovimientoConcepto.importe
            }
            for c in conceptos if str(c.Cuenta.cuenta).startswith(prefijos)
        ]
        suma_detalle = sum(d['importe'] for d in detalle)
        # Suma de cuentas que empiezan por 7 (solo para mostrar arriba, si quieres puedes dejarlo)
        suma_7 = sum(d['importe'] for d in detalle if str(d['cuenta']).startswith('7'))
        # Suma de la cuenta 70500000001
        suma_705 = sum(d['importe'] for d in detalle if str(d['cuenta']).strip() == '70500000001')
        # Suma del resto de cuentas (excluyendo la 70500000001)
        suma_resto = sum(d['importe'] for d in detalle if str(d['cuenta']).strip() != '70500000001')
        resultado = suma_7
        diferencia = resultado - suma_detalle
        resultado_explotacion = suma_705 - suma_resto
    return render_template('resultado_explotacion.html', resultado=resultado, detalle=detalle, diferencia=diferencia, fecha_inicio=fecha_inicio, fecha_fin=fecha_fin, resultado_explotacion=resultado_explotacion)

@app.route('/iva', methods=['GET', 'POST'])
def resultado_iva():
    resultado = None
    iva_repercutido = 0
    iva_soportado = 0
    # Calcular fechas por defecto
    hoy = date.today()
    mes = hoy.month
    if mes <= 3:
        inicio_trimestre = date(hoy.year, 1, 1)
    elif mes <= 6:
        inicio_trimestre = date(hoy.year, 4, 1)
    elif mes <= 9:
        inicio_trimestre = date(hoy.year, 7, 1)
    else:
        inicio_trimestre = date(hoy.year, 10, 1)
    fecha_inicio = inicio_trimestre.strftime('%Y-%m-%d')
    fecha_fin = hoy.strftime('%Y-%m-%d')
    desglose_checked = False
    desglose_contrapartidas = []
    total_repercutido = 0
    total_soportado = 0
    if request.method == 'POST':
        fecha_inicio = request.form['fecha_inicio']
        fecha_fin = request.form['fecha_fin']
        desglose_checked = 'desglose' in request.form
        conceptos = db.session.query(MovimientoConcepto, Movimiento, Cuenta)\
            .join(Movimiento)\
            .join(Cuenta, MovimientoConcepto.cuenta_id == Cuenta.id)\
            .filter(
                Movimiento.fecha_factura >= fecha_inicio,
                Movimiento.fecha_factura <= fecha_fin,
                Cuenta.cuenta.in_(['47700000001', '47200000001'])
            ).all()
        for c in conceptos:
            if str(c.Cuenta.cuenta).strip() == '47700000001':
                iva_repercutido += c.MovimientoConcepto.importe
            elif str(c.Cuenta.cuenta).strip() == '47200000001':
                iva_soportado += c.MovimientoConcepto.importe
        resultado = iva_repercutido - iva_soportado
        # Desglose por contrapartida
        if desglose_checked:
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
                        'iva_repercutido': 0,
                        'iva_soportado': 0
                    }
                if str(c.Cuenta.cuenta).strip() == '47700000001':
                    desglose_dict[key]['iva_repercutido'] += c.MovimientoConcepto.importe
                elif str(c.Cuenta.cuenta).strip() == '47200000001':
                    desglose_dict[key]['iva_soportado'] += c.MovimientoConcepto.importe
            # Filtrar solo las filas con algún valor distinto de 0
            desglose_contrapartidas = [v for v in desglose_dict.values() if v['iva_repercutido'] != 0 or v['iva_soportado'] != 0]
            # Calcular los totales
            total_repercutido = sum(v['iva_repercutido'] for v in desglose_contrapartidas)
            total_soportado = sum(v['iva_soportado'] for v in desglose_contrapartidas)
    return render_template('iva.html', resultado=resultado, iva_repercutido=iva_repercutido, iva_soportado=iva_soportado, fecha_inicio=fecha_inicio, fecha_fin=fecha_fin, desglose_checked=desglose_checked, desglose_contrapartidas=desglose_contrapartidas, total_repercutido=total_repercutido, total_soportado=total_soportado)

@app.route('/retencion_alquileres', methods=['GET', 'POST'])
def retencion_alquileres():
    # Fechas por defecto: inicio de trimestre y hoy
    hoy = date.today()
    mes = hoy.month
    if mes <= 3:
        inicio_trimestre = date(hoy.year, 1, 1)
    elif mes <= 6:
        inicio_trimestre = date(hoy.year, 4, 1)
    elif mes <= 9:
        inicio_trimestre = date(hoy.year, 7, 1)
    else:
        inicio_trimestre = date(hoy.year, 10, 1)
    fecha_inicio = inicio_trimestre.strftime('%Y-%m-%d')
    fecha_fin = hoy.strftime('%Y-%m-%d')
    resultado = None
    desglose_contrapartidas = []
    total_importe = 0
    if request.method == 'POST':
        fecha_inicio = request.form['fecha_inicio']
        fecha_fin = request.form['fecha_fin']
    conceptos = db.session.query(MovimientoConcepto, Movimiento, Cuenta)\
        .join(Movimiento)\
        .join(Cuenta, MovimientoConcepto.cuenta_id == Cuenta.id)\
        .filter(
            Movimiento.fecha_factura >= fecha_inicio,
            Movimiento.fecha_factura <= fecha_fin,
            Cuenta.cuenta == '47510000003'
        ).all()
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
    hoy = date.today()
    mes = hoy.month
    if mes <= 3:
        inicio_trimestre = date(hoy.year, 1, 1)
    elif mes <= 6:
        inicio_trimestre = date(hoy.year, 4, 1)
    elif mes <= 9:
        inicio_trimestre = date(hoy.year, 7, 1)
    else:
        inicio_trimestre = date(hoy.year, 10, 1)
    fecha_inicio = inicio_trimestre.strftime('%Y-%m-%d')
    fecha_fin = hoy.strftime('%Y-%m-%d')
    resultado = None
    desglose_contrapartidas = []
    total_importe = 0
    if request.method == 'POST':
        fecha_inicio = request.form['fecha_inicio']
        fecha_fin = request.form['fecha_fin']
    conceptos = db.session.query(MovimientoConcepto, Movimiento, Cuenta)\
        .join(Movimiento)\
        .join(Cuenta, MovimientoConcepto.cuenta_id == Cuenta.id)\
        .filter(
            Movimiento.fecha_factura >= fecha_inicio,
            Movimiento.fecha_factura <= fecha_fin,
            Cuenta.cuenta == '47510000001'
        ).all()
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
    hoy = date.today()
    mes = hoy.month
    if mes <= 3:
        inicio_trimestre = date(hoy.year, 1, 1)
    elif mes <= 6:
        inicio_trimestre = date(hoy.year, 4, 1)
    elif mes <= 9:
        inicio_trimestre = date(hoy.year, 7, 1)
    else:
        inicio_trimestre = date(hoy.year, 10, 1)
    fecha_inicio = inicio_trimestre.strftime('%Y-%m-%d')
    fecha_fin = hoy.strftime('%Y-%m-%d')
    resumen_contrapartidas = []
    total_importe = 0
    if request.method == 'POST':
        fecha_inicio = request.form['fecha_inicio']
        fecha_fin = request.form['fecha_fin']
    # Agrupar por contrapartida y sumar importes
    conceptos = db.session.query(MovimientoConcepto, Movimiento, Cuenta)\
        .join(Movimiento)\
        .join(Cuenta, MovimientoConcepto.contrapartida_id == Cuenta.id)\
        .filter(
            Movimiento.fecha_factura >= fecha_inicio,
            Movimiento.fecha_factura <= fecha_fin
        ).all()
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
    total_importe = sum(v['importe'] for v in resumen_contrapartidas)
    return render_template('347.html', fecha_inicio=fecha_inicio, fecha_fin=fecha_fin, resumen_contrapartidas=resumen_contrapartidas, total_importe=total_importe)

@app.route('/descargar_db')
def descargar_db():
    db_path = os.path.join(os.path.dirname(__file__), 'app.db')
    fecha = datetime.now().strftime('%Y%m%d')
    nombre_archivo = f'lvm{fecha}.db'
    return send_file(db_path, as_attachment=True, download_name=nombre_archivo)

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

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(debug=True) 