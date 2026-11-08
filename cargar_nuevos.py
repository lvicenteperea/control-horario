"""Añadir meses nuevos por persona sin modificar las horas ya cargadas."""
from __future__ import annotations

import argparse
import hashlib
import sys
from collections import Counter, defaultdict
from pathlib import Path

from control_horario import (
    Record, classify, connect, database_rules, description_key,
    normalized, read_excel, split_hours,
)


def month_key(record):
    return normalized(record.person), record.day.strftime('%Y-%m')


def content_counts(records):
    # Se comparan ocurrencias: dos partes iguales en filas distintas pueden
    # ser legítimos. La fila y el formato de la fecha no son identidad.
    return Counter((r.day, r.hours, r.description) for r in records)


def select_new_records(incoming, existing):
    old_months = defaultdict(list)
    new_months = defaultdict(list)
    for record in existing:
        old_months[month_key(record)].append(record)
    for record in incoming:
        new_months[month_key(record)].append(record)
    selected, skipped, conflicts = [], 0, []
    for key, records in new_months.items():
        if key not in old_months:
            selected.extend(records)
        elif content_counts(records) == content_counts(old_months[key]):
            skipped += len(records)
        else:
            conflicts.append(f'{records[0].person}: {key[1]}')
    if conflicts:
        raise ValueError('No se añadió nada. Estos meses ya existen con datos diferentes '
                         '(o el archivo contiene solo parte de ellos): '
                         + ', '.join(conflicts)
                         + '. Usar un proceso de modificación o una carga completa revisada.')
    return selected, skipped


def active_records(cursor, source_id, base_id):
    cursor.execute(
        'SELECT r.person,r.source_row,r.work_date,r.date_inherited,'
        'r.original_hours,r.original_description FROM ch_records r '
        'JOIN ch_imports i ON i.id=r.import_id '
        'WHERE i.source_id=%s AND (i.id=%s OR EXISTS ('
        'SELECT 1 FROM ch_incremental_imports ci '
        'WHERE ci.import_id=i.id AND ci.base_import_id=%s))',
        (source_id, base_id, base_id))
    return [Record(person, row, day, bool(inherited), hours, text)
            for person, row, day, inherited, hours, text in cursor.fetchall()]


def store_increment(cursor, path, source_id, base_id, records):
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
    # Vincular solo después de verificar. Nunca sustituir active_import_id:
    # los registros y asignaciones anteriores mantienen sus propios IDs.
    cursor.execute('INSERT INTO ch_incremental_imports (import_id,base_import_id) VALUES (%s,%s)',
                   (import_id, base_id))
    return import_id


def append_records(connection, path, source, records, dry_run=False):
    if not source.strip() or len(source) > 190 or len(path.name) > 255:
        raise ValueError('Nombre de origen/archivo vacío o demasiado largo')
    connection.begin()
    try:
        with connection.cursor() as cursor:
            # El mismo bloqueo que usa la carga completa impide dos cargas
            # simultáneas sobre una imagen antigua del origen.
            cursor.execute('SELECT id,active_import_id FROM ch_sources WHERE name=%s FOR UPDATE', (source,))
            found = cursor.fetchone()
            if found is None or found[1] is None:
                raise ValueError('El origen no tiene una carga completa inicial. '
                                 'Ejecutar primero control_horario.py import con el histórico.')
            source_id, base_id = found
            # FOR UPDATE se ejecuta antes de la primera lectura consistente
            # para ver las cargas que hayan terminado mientras esperábamos.
            existing = active_records(cursor, source_id, base_id)
            selected, skipped = select_new_records(records, existing)
            months = sorted({f'{r.person}: {r.day:%Y-%m}' for r in selected})
            print(f'Registros nuevos: {len(selected)}; ya cargados: {skipped}; '
                  f'horas nuevas: {sum(r.hours for r in selected):f}.')
            if months:
                print('Meses nuevos: ' + ', '.join(months))
            if dry_run or not selected:
                connection.rollback()
                print('Previsualización sin cambios.' if dry_run else 'Sin datos nuevos; no se creó otra importación.')
                return None
            import_id = store_increment(cursor, path, source_id, base_id, selected)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    print(f'Carga incremental {import_id} completada. Datos anteriores conservados.')
    return import_id


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('excel', type=Path)
    parser.add_argument('--source', default='horas-core')
    parser.add_argument('--config', default='config.local.ini')
    parser.add_argument('--dry-run', action='store_true', help='Comparar con MariaDB sin escribir')
    args = parser.parse_args(argv)
    connection = None
    try:
        records = read_excel(args.excel)
        connection = connect(args.config)
        append_records(connection, args.excel, args.source, records, args.dry_run)
        return 0
    except (ValueError, OSError, KeyError, ImportError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 1
    except Exception as exc:
        if getattr(exc, 'args', ()) and exc.args[0] == 1146:
            print('Error: falta preparar el esquema. Ejecutar control_horario.py init-db '
                  'con esta versión y la misma configuración.', file=sys.stderr)
        else:
            print(f'Error de ejecución ({type(exc).__name__}): revisar configuración y permisos.', file=sys.stderr)
        return 1
    finally:
        if connection is not None:
            connection.close()


if __name__ == '__main__':
    raise SystemExit(main())
