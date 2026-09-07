import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from monitor import compare_versions, Store, Sources, changes, cycle, deliver, project, USER_FIELDS


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'state.sqlite3'
        self.store = Store(self.path, 'instance')
        self.addCleanup(self.store.conn.close)

    def test_baseline_and_restart(self):
        self.assertEqual(self.store.observe('users', {'1': {'email': 'a'}}, ['a@b.com']), 0)
        other = Store(self.path, 'instance')
        self.addCleanup(other.conn.close)
        self.assertEqual(other.observe('users', {'1': {'email': 'b'}}, ['a@b.com']), 1)
        self.assertEqual(other.observe('users', {'1': {'email': 'b'}}, ['a@b.com']), 0)
        self.assertEqual(other.conn.execute('SELECT count(*) FROM outbox').fetchone()[0], 1)

    def test_create_update_remove(self):
        events = changes({'1': {'name': 'a'}, '2': {}}, {'1': {'name': 'b'}, '3': {}})
        self.assertEqual({e['event'] for e in events}, {'created', 'updated', 'removed'})
        self.assertEqual(next(e for e in events if e['event'] == 'updated')['fields'], ['name'])

    def test_no_secrets_or_login_noise(self):
        row = {'id': 1, 'email': 'a', 'password': 'secret', 'last_login': 'today', 'salt': 'secret'}
        self.assertEqual(project([row], USER_FIELDS), {'1': {'id': 1, 'email': 'a'}})

    def test_partial_delivery_retries_only_failed_recipient(self):
        self.store.observe('users', {}, ['a', 'b'])
        self.store.observe('users', {'1': {}}, ['a', 'b'])
        sent = []
        def sender(config, body, recipient):
            if recipient == 'b':
                raise OSError('secret')
            sent.append(recipient)
        self.assertFalse(deliver(self.store, None, sender))
        self.assertEqual(sent, ['a'])
        self.assertEqual(json.loads(self.store.conn.execute('SELECT recipients FROM outbox').fetchone()[0]), ['b'])
        self.assertTrue(deliver(self.store, None, lambda c, b, r: sent.append(r)))
        self.assertEqual(sent, ['a', 'b'])
        self.assertEqual(self.store.conn.execute('SELECT count(*) FROM outbox').fetchone()[0], 0)

    def test_failed_source_keeps_baseline_and_others_continue(self):
        self.store.observe('users', {'1': {}}, [])
        source = SimpleNamespace(entities=lambda kind: (_ for _ in ()).throw(OSError()), version=lambda: {'installed': {'tag': 'v1'}})
        config = SimpleNamespace(releases=False, recipients=[], state=self.path)
        self.assertFalse(cycle(self.store, source, config))
        self.assertEqual(json.loads(self.store.conn.execute("SELECT value FROM snapshots WHERE name='users'").fetchone()[0]), {'1': {}})
        self.assertIsNotNone(self.store.conn.execute("SELECT value FROM snapshots WHERE name='version'").fetchone())
        self.assertFalse((self.path.parent / 'healthy').exists())

    def test_pagination_and_truncation(self):
        source = Sources(SimpleNamespace(url='https://example.com', key='secret'))
        with patch('monitor.get_json', side_effect=[{'data': [{'id': 1}], 'total': 2}, {'data': [{'id': 2}], 'total': 2}]):
            self.assertEqual(len(source.api_rows('/api/user/?status=all')), 2)
        with patch('monitor.get_json', side_effect=[{'data': [{'id': 1}], 'total': 2}, {'data': [], 'total': 2}]):
            with self.assertRaises(ValueError):
                source.api_rows('/api/user/')

    def test_instance_guard(self):
        with self.assertRaises(ValueError):
            Store(self.path, 'other-instance')

    def test_version_comparison(self):
        self.assertEqual(compare_versions('v0.59.9', 'v0.59.10'), 'update available')
        self.assertEqual(compare_versions('v1.59.10', 'v0.59.10'), 'up to date')
        self.assertEqual(compare_versions('v0.59.10.1', 'v0.59.10'), 'ahead of published latest')
        self.assertTrue(compare_versions('v0.60.0-beta', 'v0.59.10').startswith('unknown'))

    def test_status_schedule_survives_restart(self):
        self.assertTrue(self.store.status({'checks': {'users': 'ok'}}, ['a'], 604800, now=100))
        other = Store(self.path, 'instance')
        self.addCleanup(other.conn.close)
        self.assertFalse(other.status({}, ['a'], 604800, now=101))
        self.assertTrue(other.status({}, ['a'], 604800, now=604900))
        self.assertFalse(other.status({}, ['a'], 0, now=9999999))
        self.assertEqual(other.conn.execute('SELECT count(*) FROM outbox').fetchone()[0], 2)

    def test_status_reports_failed_checks_without_stale_version(self):
        self.store.observe('version', {'installed': {'tag': 'v0.50.0'}}, ['a'])
        source = SimpleNamespace(entities=lambda kind: {}, version=lambda: (_ for _ in ()).throw(OSError()))
        config = SimpleNamespace(releases=False, recipients=['a'], state=self.path, status_interval=604800)
        with patch('monitor.deliver', return_value=True):
            self.assertFalse(cycle(self.store, source, config))
        body = json.loads(self.store.conn.execute('SELECT body FROM outbox').fetchone()[0])
        self.assertEqual(body['checks']['version'], 'failed: OSError')
        self.assertIsNone(body['version']['installed'])

    def test_duplicates_rejected(self):
        with self.assertRaises(ValueError):
            project([{'id': 1}, {'id': 1}], USER_FIELDS)


if __name__ == '__main__':
    unittest.main()
