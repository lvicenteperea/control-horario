import contextlib
import io
import os
import tempfile
import unittest
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

from cargar_nuevos import append_records, select_new_records
from control_horario import Record, import_records
import test_importer


def record(month, row=2, hours='2', text='Trabajo', person='Persona', year=2026):
    return Record(person, row, date(year, month, 5), False, Decimal(hours), text)


class IncrementalSelectionTests(unittest.TestCase):
    def test_new_month_keeps_original_records(self):
        old = [record(1)]
        selected, skipped = select_new_records([record(2)], old)
        self.assertEqual(selected, [record(2)])
        self.assertEqual(skipped, 0)
        self.assertEqual(old, [record(1)])

    def test_full_workbook_only_adds_new_months(self):
        selected, skipped = select_new_records([record(1), record(2, row=5)], [record(1)])
        self.assertEqual(selected, [record(2, row=5)])
        self.assertEqual(skipped, 1)

    def test_reimport_ignores_row_order_and_date_format(self):
        first = record(2, row=2)
        second = record(2, row=3, text='Otro trabajo')
        selected, skipped = select_new_records(
            [replace(second, row=50, inherited=True), replace(first, row=51)], [first, second])
        self.assertEqual(selected, [])
        self.assertEqual(skipped, 2)

    def test_same_month_other_person_is_new(self):
        other = record(1, person='Otra persona')
        self.assertEqual(select_new_records([other], [record(1)])[0], [other])

    def test_year_is_part_of_month_identity(self):
        next_year = record(1, year=2027)
        self.assertEqual(select_new_records([next_year], [record(1)])[0], [next_year])

    def test_duplicate_occurrences_preserved(self):
        both = [record(2), record(2, row=3)]
        self.assertEqual(select_new_records(both, [])[0], both)
        self.assertEqual(select_new_records(both, both), ([], 2))
        with self.assertRaises(ValueError):
            select_new_records(both, both[:1])

    def test_modified_or_partial_existing_month_blocks_entire_file(self):
        old = [record(1), record(1, row=3, text='Otra tarea')]
        for incoming in ([record(1, hours='3'), record(2)], [record(1), record(2)]):
            with self.subTest(incoming=incoming), self.assertRaisesRegex(ValueError, '2026-01'):
                select_new_records(incoming, old)

    def test_person_case_and_whitespace_do_not_duplicate_month(self):
        self.assertEqual(select_new_records([record(1, person=' PERSONA ')], [record(1)]), ([], 1))


@unittest.skipUnless(os.environ.get('CH_TEST_DB') == '1', 'Requiere MariaDB de pruebas aislado')
class IncrementalDatabaseTests(unittest.TestCase):
    setUp = test_importer.DatabaseTests.setUp
    tearDown = test_importer.DatabaseTests.tearDown

    def total(self, source):
        with self.connection.cursor() as cursor:
            cursor.execute('SELECT COALESCE(SUM(hours),0) FROM ch_v_hours WHERE source=%s', (source,))
            return cursor.fetchone()[0]

    def test_append_retry_conflict_dry_run_and_full_replacement(self):
        source = 'incremental-' + os.urandom(8).hex()
        january = [record(1)]
        february = [record(2, hours='5', text='[CORE] A [CORE] B [CORE] C')]
        march = [record(3, row=8, hours='3')]
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            path = Path(directory) / 'input.xlsx'
            path.write_bytes(b'fixture: registros ya validados')
            import_records(self.connection, path, source, january)
            with self.connection.cursor() as cursor:
                cursor.execute('SELECT record_id FROM ch_v_hours WHERE source=%s', (source,))
                old_id = cursor.fetchone()[0]
            self.assertIsNotNone(append_records(self.connection, path, source, february))
            self.assertEqual(self.total(source), Decimal(7))
            with self.connection.cursor() as cursor:
                cursor.execute('SELECT record_id FROM ch_v_hours WHERE source=%s AND month=%s', (source, '2026-01'))
                self.assertEqual(cursor.fetchone()[0], old_id)
            self.assertIsNone(append_records(self.connection, path, source, february))
            self.assertEqual(self.total(source), Decimal(7))
            append_records(self.connection, path, source, january + february + march)
            self.assertEqual(self.total(source), Decimal(10))
            # Un mes con cambios impide añadir otros meses del mismo fichero.
            with self.assertRaises(ValueError):
                append_records(self.connection, path, source, [record(2, hours='6'), record(4)])
            self.assertEqual(self.total(source), Decimal(10))
            append_records(self.connection, path, source, [record(4)], dry_run=True)
            self.assertEqual(self.total(source), Decimal(10))
            # La carga completa continúa reemplazando todo el conjunto activo.
            import_records(self.connection, path, source, january)
            self.assertEqual(self.total(source), Decimal(2))
            append_records(self.connection, path, source, february)
            self.assertEqual(self.total(source), Decimal(7))

    def test_requires_initial_load_and_rolls_back_on_insert_failure(self):
        source = 'rollback-' + os.urandom(8).hex()
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            path = Path(directory) / 'input.xlsx'
            path.write_bytes(b'fixture')
            with self.assertRaisesRegex(ValueError, 'inicial'):
                append_records(self.connection, path, source, [record(2)])
            import_records(self.connection, path, source, [record(1)])
            with self.assertRaises(Exception):
                append_records(self.connection, path, source, [record(2), record(3, row=3, hours='-1')])
            self.assertEqual(self.total(source), Decimal(2))
            with self.connection.cursor() as cursor:
                cursor.execute('SELECT COUNT(*) FROM ch_imports i JOIN ch_sources s ON s.id=i.source_id WHERE s.name=%s', (source,))
                self.assertEqual(cursor.fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()
