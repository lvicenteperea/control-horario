"""Importador Excel -> MariaDB. Python 3.11+. No escribe en el Excel."""
from __future__ import annotations

import argparse
import configparser
import getpass
import hashlib
import json
import os
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from pathlib import Path

UNIT = Decimal('0.000001')
MONTHS = set('enero febrero marzo abril mayo junio julio agosto septiembre octubre noviembre diciembre'.split())
BRACKETS = re.compile(r'\[([^\]]+)\]')


def normalized(text):
    """Ignora mayúsculas y espacios; conserva acentos y puntuación."""
    return ' '.join(unicodedata.normalize('NFKC', str(text)).casefold().split())


def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def description_key(text):
    return digest(normalized(text))


@dataclass(frozen=True)
class Record:
    person: str
    row: int
    day: date
    inherited: bool
    hours: Decimal
    description: str


@dataclass(frozen=True)
class Task:
    scope: str
    name: str

    @property
    def key(self):
        return digest(json.dumps([normalized(self.scope), normalized(self.name)], ensure_ascii=False))


def parse_day(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        for fmt in ('%d/%m/%Y', '%Y-%m-%d', '%d-%m-%Y'):
            try:
                return datetime.strptime(value.strip(), fmt).date()
            except ValueError:
                pass
    raise ValueError(f'Fecha no reconocida: {value!r}')


def parse_hours(value):
    if isinstance(value, bool) or value is None or not str(value).strip():
        raise ValueError('Horas vacías o no numéricas')
    try:
        amount = Decimal(str(value).strip().replace(',', '.'))
    except InvalidOperation as exc:
        raise ValueError(f'Horas no numéricas: {value!r}') from exc
    if not amount.is_finite() or amount < 0 or amount >= Decimal('1000000'):
        raise ValueError(f'Horas fuera de rango: {value!r}')
    if amount != amount.quantize(UNIT):
        raise ValueError('Las horas admiten como máximo seis decimales')
    return amount.quantize(UNIT)


def parse_rows(person, rows):
    records, errors = [], []
    previous = None
    headers_seen = False
    for number, values in enumerate(rows, 1):
        row = list(values) + [None] * 3
        raw_date, raw_hours, raw_description = row[:3]
        if not any(v is not None and str(v).strip() for v in row):
            continue
        label = normalized(raw_date) if raw_date is not None else ''
        if label == 'fecha' and normalized(raw_hours) == 'horas':
            headers_seen = True
            continue
        if label in MONTHS:
            # Totales mensuales originales: no son partes de trabajo.
            continue
        if not headers_seen:
            errors.append(f'{person}!fila {number}: falta cabecera Fecha/Horas')
            continue
        try:
            inherited = raw_date is None or not str(raw_date).strip()
            day = previous if inherited else parse_day(raw_date)
            if day is None:
                raise ValueError('No hay fecha anterior que heredar')
            hours = parse_hours(raw_hours)
            if raw_description is not None and not isinstance(raw_description, str):
                raise ValueError('La descripción debe ser texto')
            description = raw_description or ''
            if len(person) > 190:
                raise ValueError('Nombre de persona demasiado largo')
            records.append(Record(person, number, day, inherited, hours, description))
            previous = day
        except ValueError as exc:
            errors.append(f'{person}!fila {number}: {exc}')
    return records, errors


def read_excel(path):
    from openpyxl import load_workbook
    # Se leen fórmulas, no su caché: una fórmula sin calcular se rechaza
    # en lugar de convertirla silenciosamente en una fecha o texto vacío.
    book = load_workbook(path, read_only=True, data_only=False)
    records, errors = [], []
    try:
        for sheet in book.worksheets:
            found, issues = parse_rows(sheet.title, sheet.iter_rows(values_only=True))
            records.extend(found)
            errors.extend(issues)
    finally:
        book.close()
    if errors:
        raise ValueError('No se importó nada. Corregir estas filas:\n' + '\n'.join(errors))
    if not records:
        raise ValueError('El archivo no contiene registros de horas')
    return records


def validate_tasks(values):
    if not isinstance(values, list) or not values:
        raise ValueError('Cada regla necesita una lista no vacía de tareas')
    tasks = []
    for value in values:
        if not isinstance(value, dict):
            raise ValueError('Cada tarea debe ser un objeto con scope y name')
        scope, name = value.get('scope', ''), value.get('name', '')
        if not isinstance(scope, str) or not isinstance(name, str):
            raise ValueError('scope y name deben ser texto')
        scope, name = scope.strip(), name.strip()
        if not scope or not name or len(scope) > 190 or len(name) > 500:
            raise ValueError('Ámbito/nombre vacío o demasiado largo')
        tasks.append(Task(scope, name))
    return unique_tasks(tasks)


def unique_tasks(tasks):
    return list({task.key: task for task in tasks}.values())


def read_rules(path):
    payload = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if not isinstance(payload, list):
        raise ValueError('El fichero de reglas debe contener una lista')
    result = {}
    for rule in payload:
        if not isinstance(rule, dict):
            raise ValueError('Cada regla debe ser un objeto')
        text = rule.get('description')
        if not isinstance(text, str) or not text.strip():
            raise ValueError('Cada regla necesita description no vacía')
        key = description_key(text)
        if key in result:
            raise ValueError('Hay reglas repetidas para la misma descripción')
        result[key] = (text, validate_tasks(rule.get('tasks')))
    return result


def classify(text, rules=None):
    if not text.strip():
        return [Task('SIN CLASIFICAR', '[SIN TAREA]')], 'sin_descripcion', 'sin_descripcion'
    if rules and description_key(text) in rules:
        return rules[description_key(text)][1], 'regla', 'revisado'
    matches = list(BRACKETS.finditer(text))
    if not matches:
        return [Task('SIN CLASIFICAR', '[PENDIENTE DE CLASIFICAR]')], 'texto_libre', 'pendiente'
    tasks = []
    # Un prefijo sin código puede ser otra actividad. No se descarta.
    if text[:matches[0].start()].strip(' \t\r\n,;/-'):
        tasks.append(Task('SIN CLASIFICAR', '[PENDIENTE DE CLASIFICAR]'))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        name = text[match.end():end].strip(' \t\r\n,;/-')
        # Se elimina solamente el nexo justo anterior al siguiente bloque.
        if index + 1 < len(matches):
            name = re.sub(r'\s+y\s*$', '', name, flags=re.I).rstrip(' ,;/-')
        scope = ' '.join(match[1].split()).upper()
        if not scope or not name or len(scope) > 190 or len(name) > 500:
            tasks.append(Task('SIN CLASIFICAR', '[PENDIENTE DE CLASIFICAR]'))
        else:
            tasks.append(Task(scope, name))
    # Las extracciones son propuestas, incluso con un solo bloque: puede
    # contener varias actividades y requerir una regla revisada.
    return unique_tasks(tasks), 'extraccion', 'pendiente'


def split_hours(hours, count):
    if count < 1:
        raise ValueError('El reparto requiere alguna tarea')
    share = (hours / count).quantize(UNIT, rounding=ROUND_DOWN)
    result = [share] * count
    result[-1] += hours - sum(result)
    return result


def preview(records, rules=None):
    summary = defaultdict(lambda: {'registros': 0, 'horas': Decimal(0), 'fechas_heredadas': 0})
    states = Counter()
    for record in records:
        item = summary[record.person]
        item['registros'] += 1
        item['horas'] += record.hours
        item['fechas_heredadas'] += int(record.inherited)
        tasks, _, status = classify(record.description, rules)
        assert sum(split_hours(record.hours, len(tasks))) == record.hours
        states[status] += 1
    print(json.dumps({'personas': summary, 'estados_registros': states,
                      'registros': len(records), 'horas': sum(r.hours for r in records)},
                     ensure_ascii=False, indent=2, default=str))


def connect(config_path):
    import pymysql
    config = configparser.ConfigParser(interpolation=None)
    if not config.read(config_path, encoding='utf-8-sig'):
        raise ValueError(f'No existe {config_path}; copiar config.example.ini')
    cfg = config['mariadb']
    password = os.environ.get('CONTROL_HORARIO_DB_PASSWORD')
    if password is None:
        password = getpass.getpass('Contraseña de MariaDB: ')
    return pymysql.connect(host=cfg.get('host', 'localhost'), port=cfg.getint('port', 13306),
                           user=cfg.get('user', 'root'), password=password,
                           database=cfg.get('database', 'pruebas'), charset='utf8mb4',
                           autocommit=False, connect_timeout=10)


def init_db(connection):
    sql = (Path(__file__).parent / 'sql' / 'schema.sql').read_text(encoding='utf-8')
    sql = '\n'.join(line for line in sql.splitlines() if not line.lstrip().startswith('--'))
    with connection.cursor() as cursor:
        for statement in sql.split(';'):
            if statement.strip():
                cursor.execute(statement)
    connection.commit()
    print('Objetos ch_ preparados en la base configurada.')


def save_rules(connection, rules):
    with connection.cursor() as cursor:
        for key, (text, tasks) in rules.items():
            payload = json.dumps([{'scope': t.scope, 'name': t.name} for t in tasks], ensure_ascii=False)
            cursor.execute('INSERT INTO ch_rules (description_hash,original_description,tasks_json) '
                           'VALUES (%s,%s,%s) ON DUPLICATE KEY UPDATE '
                           'original_description=VALUES(original_description),tasks_json=VALUES(tasks_json)',
                           (key, text, payload))
    connection.commit()
    print(f'Reglas guardadas: {len(rules)}. Reimportar el Excel para aplicarlas.')


def database_rules(cursor):
    cursor.execute('SELECT description_hash,original_description,tasks_json FROM ch_rules')
    return {key: (text, validate_tasks(json.loads(payload))) for key, text, payload in cursor.fetchall()}


def import_records(connection, path, source, records):
    if not source.strip() or len(source) > 190 or len(path.name) > 255:
        raise ValueError('Nombre de origen/archivo vacío o demasiado largo')
    connection.begin()
    try:
        with connection.cursor() as cursor:
            cursor.execute('INSERT INTO ch_sources (name) VALUES (%s) '
                           'ON DUPLICATE KEY UPDATE name=VALUES(name)', (source,))
            # Serializa importaciones simultáneas del mismo origen.
            cursor.execute('SELECT id FROM ch_sources WHERE name=%s FOR UPDATE', (source,))
            source_id = cursor.fetchone()[0]
            rules = database_rules(cursor)
            cursor.execute('INSERT INTO ch_imports (source_id,filename,file_sha256,record_count,total_hours) '
                           'VALUES (%s,%s,%s,%s,%s)',
                           (source_id, path.name, hashlib.sha256(path.read_bytes()).hexdigest(),
                            len(records), sum(r.hours for r in records)))
            import_id = cursor.lastrowid
            task_ids = {}
            for record in records:
                cursor.execute('INSERT INTO ch_records (import_id,person,sheet_name,source_row,work_date,'
                               'date_inherited,original_hours,original_description,description_hash) '
                               'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                               (import_id, record.person, record.person, record.row, record.day,
                                record.inherited, record.hours, record.description,
                                description_key(record.description)))
                record_id = cursor.lastrowid
                tasks, method, status = classify(record.description, rules)
                shares = split_hours(record.hours, len(tasks))
                if sum(shares) != record.hours:
                    raise ValueError('El reparto no coincide con las horas originales')
                for task, share in zip(tasks, shares):
                    if task.key not in task_ids:
                        cursor.execute('INSERT INTO ch_tasks (task_key,scope,name) VALUES (%s,%s,%s) '
                                       'ON DUPLICATE KEY UPDATE task_key=VALUES(task_key)',
                                       (task.key, task.scope, task.name))
                        cursor.execute('SELECT id FROM ch_tasks WHERE task_key=%s', (task.key,))
                        task_ids[task.key] = cursor.fetchone()[0]
                    cursor.execute('INSERT INTO ch_allocations (record_id,task_id,hours,method,review_status) '
                                   'VALUES (%s,%s,%s,%s,%s)',
                                   (record_id, task_ids[task.key], share, method, status))
            cursor.execute('SELECT COALESCE(SUM(a.hours),0) FROM ch_allocations a '
                           'JOIN ch_records r ON r.id=a.record_id WHERE r.import_id=%s', (import_id,))
            if cursor.fetchone()[0] != sum(r.hours for r in records):
                raise ValueError('No cuadra el total almacenado: se revierte la importación')
            cursor.execute('UPDATE ch_sources SET active_import_id=%s WHERE id=%s', (import_id, source_id))
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    print(f'Importación {import_id} activada: {len(records)} registros, '
          f'{sum(r.hours for r in records):f} horas. Historial conservado.')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='config.local.ini')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('check-db', help='Comprobar conexión sin escribir')
    sub.add_parser('init-db', help='Crear tablas y vista ch_ en la base existente')
    for command in ('preview', 'import'):
        child = sub.add_parser(command)
        child.add_argument('excel', type=Path)
        if command == 'import':
            child.add_argument('--source', default='horas-core')
        else:
            child.add_argument('--rules', type=Path, help='Reglas locales opcionales para previsualizar')
    child = sub.add_parser('rules', help='Guardar reglas revisadas en MariaDB')
    child.add_argument('json_file', type=Path)
    args = parser.parse_args(argv)
    connection = None
    try:
        records = read_excel(args.excel) if args.command in ('preview', 'import') else None
        if args.command == 'preview':
            preview(records, read_rules(args.rules) if args.rules else None)
            return 0
        rules = read_rules(args.json_file) if args.command == 'rules' else None
        connection = connect(args.config)
        if args.command == 'check-db':
            with connection.cursor() as cursor:
                cursor.execute('SELECT DATABASE(), VERSION()')
                database, version = cursor.fetchone()
            print(f'Conexión correcta. Base: {database}. Versión: {version}')
        elif args.command == 'init-db':
            init_db(connection)
        elif args.command == 'rules':
            save_rules(connection, rules)
        else:
            import_records(connection, args.excel, args.source, records)
        return 0
    except (ValueError, OSError, KeyError, ImportError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 1
    except Exception as exc:
        # No imprimir argumentos de conexión ni contraseñas.
        print(f'Error de ejecución ({type(exc).__name__}): revisar archivo, configuración y permisos.', file=sys.stderr)
        return 1
    finally:
        if connection is not None:
            connection.close()


if __name__ == '__main__':
    raise SystemExit(main())
