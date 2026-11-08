import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from control_horario import (
    Record, Task, classify, description_key, import_records, init_db,
    parse_hours, parse_rows, read_rules, save_rules, split_hours,
)


class ParserTests(unittest.TestCase):
    def test_date_inheritance_and_monthly_totals(self):
        rows = [('Fecha', 'Horas', 'Descripcicón'),
                (date(2026, 1, 5), 3, 'Trabajo'),
                (None, 2, None), ('Enero', 5, None),
                ('Fecha', 'Horas', 'Descripcicón'),
                ('06/02/2026', '1,5', '[CORE] - Pruebas')]
        records, errors = parse_rows('Persona', rows)
        self.assertFalse(errors)
        self.assertEqual(len(records), 3)
        self.assertEqual(records[1].day, date(2026, 1, 5))
        self.assertTrue(records[1].inherited)
        self.assertEqual(sum(r.hours for r in records), Decimal('6.5'))

    def test_identical_entries_are_preserved(self):
        row = (date(2026, 1, 5), 2, 'Trabajo')
        records, errors = parse_rows('Persona', [('Fecha', 'Horas'), row, row])
        self.assertFalse(errors)
        self.assertEqual(len(records), 2)

    def test_invalid_rows_not_silently_skipped(self):
        rows = [('Fecha', 'Horas'), (None, 2), ('ayer', 3),
                (date(2026, 1, 1), -1), (date(2026, 1, 1), '=SUM(B2:B3)')]
        records, errors = parse_rows('Persona', rows)
        self.assertFalse(records)
        self.assertEqual(len(errors), 4)

    def test_decimal_validation(self):
        self.assertEqual(parse_hours('0'), Decimal(0))
        self.assertEqual(parse_hours('3,2'), Decimal('3.2'))
        for value in (True, 'NaN', 'Infinity', '-1', '1000000', '0.0000001', None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_hours(value)


class ClassificationTests(unittest.TestCase):
    def test_equal_split_conserves_fractional_total(self):
        shares = split_hours(Decimal('5.000000'), 3)
        self.assertEqual(shares, [Decimal('1.666666'), Decimal('1.666666'), Decimal('1.666668')])
        self.assertEqual(sum(shares), Decimal(5))

    def test_blocks_same_scope_are_different_tasks(self):
        tasks, method, status = classify('[CORE] - Reservas y [CORE] - Rotaciones')
        self.assertEqual([t.name for t in tasks], ['Reservas', 'Rotaciones'])
        self.assertEqual(status, 'pendiente')

    def test_newlines_and_prefix_are_not_lost(self):
        tasks, _, _ = classify('Pase a prod y [DATA] - Pruebas\n[DATA] - Contadores')
        self.assertEqual(len(tasks), 3)
        self.assertEqual(tasks[0].name, '[PENDIENTE DE CLASIFICAR]')

    def test_blank_and_unknown_are_distinct(self):
        self.assertEqual(classify(' ')[0][0].name, '[SIN TAREA]')
        self.assertEqual(classify('Trabajo sin código')[0][0].name, '[PENDIENTE DE CLASIFICAR]')

    def test_reviewed_rule_overrides_extraction(self):
        text = '[CORE] - Pruebas y documentación'
        rules = {description_key(text): (text, [Task('CORE', 'Pruebas'), Task('CORE', 'Documentación')])}
        tasks, method, status = classify(text.upper(), rules)
        self.assertEqual(len(tasks), 2)
        self.assertEqual((method, status), ('regla', 'revisado'))

    def test_rules_file_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'rules.json'
            path.write_text(json.dumps([{'description': 'a', 'tasks': []}]))
            with self.assertRaises(ValueError):
                read_rules(path)


@unittest.skipUnless(os.environ.get('CH_TEST_DB') == '1', 'Requiere MariaDB de pruebas aislado')
class DatabaseTests(unittest.TestCase):
    def setUp(self):
        import pymysql
        self.connection = pymysql.connect(host='127.0.0.1',
            port=int(os.environ.get('CH_TEST_PORT', '3306')),
            user='root', password=os.environ['CH_TEST_PASSWORD'],
            database='control_horario_test', autocommit=False, charset='utf8mb4')
        with contextlib.redirect_stdout(io.StringIO()):
            init_db(self.connection)

    def tearDown(self):
        self.connection.close()

    def test_snapshots_rules_and_rollback(self):
        source = 'test-' + os.urandom(8).hex()
        records = [Record('Persona', 2, date(2026, 1, 5), False, Decimal(5), '[CORE] A [CORE] B [CORE] C'),
                   Record('Persona', 3, date(2026, 1, 5), True, Decimal(2), 'Texto libre')]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'input.xlsx'
            path.write_bytes(b'fixture: import_records recibe datos ya validados')
            with contextlib.redirect_stdout(io.StringIO()):
                import_records(self.connection, path, source, records)
                import_records(self.connection, path, source, records)
            with self.connection.cursor() as cursor:
                cursor.execute('SELECT SUM(hours) FROM ch_v_hours WHERE source=%s', (source,))
                self.assertEqual(cursor.fetchone()[0], Decimal(7))
                cursor.execute('SELECT COUNT(*) FROM ch_imports i JOIN ch_sources s ON s.id=i.source_id WHERE s.name=%s', (source,))
                self.assertEqual(cursor.fetchone()[0], 2)
            rule = {'Texto libre': [Task('CORE', 'Tarea revisada')]}
            with contextlib.redirect_stdout(io.StringIO()):
                save_rules(self.connection, {description_key(k): (k, v) for k, v in rule.items()})
                import_records(self.connection, path, source, records)
            with self.connection.cursor() as cursor:
                cursor.execute('SELECT task,review_status FROM ch_v_hours WHERE source=%s AND hours=2', (source,))
                self.assertEqual(cursor.fetchone(), ('Tarea revisada', 'revisado'))
                cursor.execute('SELECT active_import_id FROM ch_sources WHERE name=%s', (source,))
                active_before = cursor.fetchone()[0]
            invalid = [Record('Persona', 4, date(2026, 1, 1), False, Decimal(-1), '')]
            with self.assertRaises(Exception):
                import_records(self.connection, path, source, invalid)
            with self.connection.cursor() as cursor:
                cursor.execute('SELECT active_import_id FROM ch_sources WHERE name=%s', (source,))
                self.assertEqual(cursor.fetchone()[0], active_before)


if __name__ == '__main__':
    unittest.main()
