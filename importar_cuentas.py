import csv
from app import db, Cuenta, app

csv_path = 'cuentas.csv'

with app.app_context():
    with open(csv_path, encoding='utf-8') as f:
        reader = csv.reader(f)
        next(reader)  # Saltar cabecera
        for row in reader:
            codigo = row[0].strip()
            nombre = row[1].strip()
            c = Cuenta(cuenta=codigo, nombre=nombre, tipo='normal')
            db.session.add(c)
        db.session.commit()
    print('Importación completada.') 