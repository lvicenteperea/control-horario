"""Dashboard local de consulta. Ejecutar: python dashboard.py."""
from __future__ import annotations

import argparse
import configparser
import getpass
import json
import os
import sys
import webbrowser
from datetime import date, datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

QUERY = '''SELECT source, import_id, record_id, person, work_date,
 task_id, scope, task, hours, method, review_status,
 original_description, date_inherited, sheet_name, source_row
 FROM ch_v_hours ORDER BY work_date, person, record_id, task_id'''


def connection_options(config_path):
    config = configparser.ConfigParser(interpolation=None)
    if not config.read(config_path, encoding='utf-8-sig'):
        raise ValueError(f'No existe {config_path}; copiar config.example.ini')
    cfg = config['mariadb']
    password = os.environ.get('CONTROL_HORARIO_DB_PASSWORD')
    if password is None:
        password = getpass.getpass('Contraseña de MariaDB: ')
    return dict(host=cfg.get('host', 'localhost'), port=cfg.getint('port', 13306),
                user=cfg.get('user', 'root'), password=password,
                database=cfg.get('database', 'pruebas'), charset='utf8mb4',
                connect_timeout=10, read_timeout=30, write_timeout=30,
                autocommit=True)


def serialize_rows(columns, values):
    result = []
    for values_row in values:
        row = dict(zip(columns, values_row))
        # Millonésimas de hora: las sumas del navegador son enteras y
        # conservan la precisión de DECIMAL(12,6).
        units = Decimal(str(row.pop('hours'))) * 1000000
        if not units.is_finite() or units != units.to_integral_value() or units < 0:
            raise ValueError('Las horas almacenadas no tienen una precisión válida')
        row['units'] = int(units)
        if row['units'] > 9007199254740991:
            raise ValueError('Valor demasiado grande para el dashboard')
        for key, value in row.items():
            if isinstance(value, (date, datetime)):
                row[key] = value.isoformat()
            elif key in ('record_id', 'task_id', 'import_id'):
                row[key] = str(value)
        result.append(row)
    if sum(r['units'] for r in result) > 9007199254740991:
        raise ValueError('El total supera la precisión del dashboard')
    return result


class DatabaseReader:
    def __init__(self, options):
        self.options = options

    def __call__(self):
        import pymysql
        connection = pymysql.connect(**self.options)
        try:
            with connection.cursor() as cursor:
                # Consulta fija de solo lectura; los filtros se aplican en
                # el navegador y nunca se concatenan al SQL.
                cursor.execute(QUERY)
                columns = [column[0] for column in cursor.description]
                return serialize_rows(columns, cursor.fetchall())
        finally:
            connection.close()


def make_handler(reader):
    class Handler(BaseHTTPRequestHandler):
        def send_content(self, status, content, mime):
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(content)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(content)

        def do_GET(self):
            port = self.server.server_address[1]
            allowed_hosts = {f'127.0.0.1:{port}', f'localhost:{port}'}
            host = self.headers.get('Host', '')
            origin = self.headers.get('Origin')
            if host not in allowed_hosts or (origin and origin not in {f'http://{h}' for h in allowed_hosts}):
                self.send_content(403, b'Acceso local requerido', 'text/plain; charset=utf-8')
                return
            if self.path == '/':
                content = (Path(__file__).parent / 'web' / 'dashboard.html').read_bytes()
                self.send_content(200, content, 'text/html; charset=utf-8')
            elif self.path == '/api/hours':
                try:
                    payload = {'rows': reader(), 'updated_at': datetime.now().astimezone().isoformat()}
                    self.send_content(200, json.dumps(payload, ensure_ascii=False).encode('utf-8'), 'application/json; charset=utf-8')
                except Exception as exc:
                    code = exc.args[0] if exc.args and isinstance(exc.args[0], int) else None
                    message = ('Falta preparar el esquema: ejecutar control_horario.py init-db.' if code == 1146
                               else 'No se pudieron leer las horas. Revisa MariaDB, la configuración y los permisos.')
                    self.send_content(503, json.dumps({'error': message}).encode('utf-8'), 'application/json; charset=utf-8')
            else:
                self.send_content(404, b'No encontrado', 'text/plain; charset=utf-8')

    return Handler


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='config.local.ini')
    parser.add_argument('--port', type=int, default=8501)
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args(argv)
    try:
        if not 1 <= args.port <= 65535:
            raise ValueError('El puerto debe estar entre 1 y 65535')
        reader = DatabaseReader(connection_options(args.config))
        reader()  # Comprobar la conexión antes de abrir el navegador.
        with ThreadingHTTPServer(('127.0.0.1', args.port), make_handler(reader)) as server:
            url = f'http://127.0.0.1:{args.port}'
            print(f'Dashboard disponible en {url}. Ctrl+C para cerrar.')
            if not args.no_browser:
                webbrowser.open(url)
            server.serve_forever()
        return 0
    except KeyboardInterrupt:
        print('\nDashboard cerrado.')
        return 0
    except (ValueError, OSError, KeyError, ImportError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 1
    except Exception as exc:
        code = exc.args[0] if exc.args and isinstance(exc.args[0], int) else None
        if code == 1146:
            message = 'Falta el esquema. Ejecutar control_horario.py init-db.'
        elif code == 1045:
            message = 'Acceso denegado: comprobar usuario, contraseña y permisos.'
        else:
            message = 'No se pudo conectar: comprobar MariaDB y config.local.ini.'
        print(f'Error: {message}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
