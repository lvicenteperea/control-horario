import json
import threading
import unittest
from datetime import date
from decimal import Decimal
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

from dashboard import make_handler, serialize_rows


class DashboardDataTests(unittest.TestCase):
    def test_decimal_hours_and_identifiers_keep_precision(self):
        data = serialize_rows(['hours', 'work_date', 'record_id', 'task_id', 'import_id'],
                              [(Decimal('1.666668'), date(2026, 11, 3), 9007199254740993, 4, 1)])
        self.assertEqual(data, [{'units': 1666668, 'work_date': '2026-11-03',
                                'record_id': '9007199254740993', 'task_id': '4', 'import_id': '1'}])

    def test_invalid_amounts_are_not_presented_as_valid_hours(self):
        for amount in ('-1', '0.0000001', 'NaN', 'Infinity', '9007199254.740992'):
            with self.subTest(amount=amount), self.assertRaises(ValueError):
                serialize_rows(['hours'], [(amount,)])


class DashboardHttpTests(unittest.TestCase):
    def setUp(self):
        self.fail = False

        def reader():
            if self.fail:
                raise RuntimeError('internal-secret-must-not-be-returned')
            return [{'units': 2500000, 'person': 'Persona de prueba'}]

        handler = make_handler(reader)
        handler.log_message = lambda *args: None
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, path, headers=None, method='GET'):
        port = self.server.server_address[1]
        connection = HTTPConnection('127.0.0.1', port, timeout=3)
        try:
            connection.request(method, path, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_dashboard_and_data_routes(self):
        status, headers, html = self.request('/')
        self.assertEqual(status, 200)
        self.assertIn(b'Evoluci', html)
        self.assertIn(b'/api/hours', html)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        status, _, content = self.request('/api/hours')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(content)['rows'][0]['units'], 2500000)
        self.assertEqual(self.request('/config.local.ini')[0], 404)
        self.assertEqual(self.request('/api/hours', method='POST')[0], 501)

    def test_unexpected_hosts_and_origins_cannot_read_data(self):
        self.assertEqual(self.request('/api/hours', {'Host': 'example.com'})[0], 403)
        self.assertEqual(self.request('/api/hours', {'Origin': 'https://example.com'})[0], 403)

    def test_database_errors_are_clear_without_internal_details(self):
        self.fail = True
        status, _, content = self.request('/api/hours')
        self.assertEqual(status, 503)
        self.assertIn('error', json.loads(content))
        self.assertNotIn(b'internal-secret', content)


if __name__ == '__main__':
    unittest.main()
