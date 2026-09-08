"""Metabase change monitor. Secrets are never included in snapshots or alerts."""
import argparse
import csv
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import signal
import smtplib
import sqlite3
import ssl
import threading
import time
from email.message import EmailMessage
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, build_opener, HTTPRedirectHandler

LOG = logging.getLogger('monitor')
USER_FIELDS = ('id', 'email', 'first_name', 'last_name', 'is_active', 'is_superuser', 'date_joined')
KEY_FIELDS = ('id', 'name', 'group_id', 'creator_id', 'created_at', 'updated_at')
MAX_RESPONSE_BYTES = 1024 * 1024
# libpq defaults to sslmode=prefer, which silently accepts plaintext and verifies nothing.
SECURE_SSLMODES = ('require', 'verify-ca', 'verify-full')


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('HTTP redirects are disabled; configure the canonical URL')


def get_json(url, key=None):
    headers = {'Accept': 'application/json', 'User-Agent': 'metabase-health-check'}
    if key:
        headers['X-API-Key'] = key
    with build_opener(NoRedirect).open(Request(url, headers=headers), timeout=30) as response:
        payload = response.read(MAX_RESPONSE_BYTES + 1)
    if len(payload) > MAX_RESPONSE_BYTES:
        raise ValueError('Response exceeds the maximum size')
    return json.loads(payload)


def project(rows, fields):
    result = {}
    for row in rows:
        if not isinstance(row, dict) or row.get('id') is None:
            raise ValueError('Invalid entity response')
        ident = str(row['id'])
        if ident in result:
            raise ValueError('Duplicate entity ID; refusing incomplete snapshot')
        result[ident] = {field: row[field] for field in fields if field in row}
    return result


def flag(name):
    return os.environ.get(name, 'false').lower() == 'true'


class Config:
    def __init__(self):
        self.url = os.environ.get('METABASE_URL', '').rstrip('/')
        parsed = urlparse(self.url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.query or parsed.fragment:
            raise ValueError('METABASE_URL must be an HTTP(S) instance URL without credentials/query/fragment')
        if parsed.scheme == 'http' and not flag('ALLOW_INSECURE_URL'):
            raise ValueError('METABASE_URL must use https, which the API key travels over; set ALLOW_INSECURE_URL=true to send it in cleartext')
        self.key = os.environ.get('METABASE_API_KEY', '')
        self.db = os.environ.get('METABASE_DATABASE_URL', '')
        self.source = os.environ.get('ENTITY_SOURCE', 'api')
        if self.source not in ('api', 'database'):
            raise ValueError('ENTITY_SOURCE must be api or database')
        if self.source == 'api' and not self.key:
            raise ValueError('METABASE_API_KEY is required in API mode')
        if self.source == 'database':
            if not self.db.startswith(('postgresql://', 'postgres://')):
                raise ValueError('Database mode requires a PostgreSQL METABASE_DATABASE_URL')
            sslmode = parse_qs(urlparse(self.db).query).get('sslmode', [''])[-1]
            if sslmode not in SECURE_SSLMODES and not flag('ALLOW_INSECURE_DB'):
                raise ValueError('METABASE_DATABASE_URL needs sslmode=verify-full (or require/verify-ca); set ALLOW_INSECURE_DB=true to accept an unverified connection')
        self.interval = int(os.environ.get('POLL_INTERVAL_SECONDS', '300'))
        if self.interval < 30:
            raise ValueError('POLL_INTERVAL_SECONDS must be at least 30')
        self.state = Path(os.environ.get('STATE_PATH', '/data/state.sqlite3'))
        self.smtp_host = os.environ.get('SMTP_HOST', '')
        self.smtp_mode = os.environ.get('SMTP_SECURITY', 'starttls')
        if self.smtp_mode not in ('starttls', 'ssl', 'none'):
            raise ValueError('SMTP_SECURITY must be starttls, ssl, or none')
        if self.smtp_mode == 'none' and os.environ.get('SMTP_USERNAME') and not flag('ALLOW_INSECURE_SMTP'):
            raise ValueError('SMTP_SECURITY=none sends SMTP_PASSWORD in cleartext; use starttls or ssl, or set ALLOW_INSECURE_SMTP=true')
        self.smtp_port = int(os.environ.get('SMTP_PORT', '465' if self.smtp_mode == 'ssl' else '587'))
        self.sender = os.environ.get('SMTP_FROM', '')
        self.recipients = list(dict.fromkeys(x.strip() for x in next(csv.reader([os.environ.get('NOTIFY_EMAILS', '')])) if x.strip()))
        for address in [self.sender, *self.recipients]:
            if not re.fullmatch(r'[^\s@,<>]+@[^\s@,<>]+', address):
                raise ValueError('SMTP_FROM and NOTIFY_EMAILS must contain plain email addresses')
        if not self.smtp_host or not self.recipients:
            raise ValueError('SMTP_HOST, SMTP_FROM, and NOTIFY_EMAILS are required')
        self.releases = os.environ.get('CHECK_RELEASES', 'true').lower() == 'true'
        self.status_interval = int(os.environ.get('STATUS_INTERVAL_SECONDS', '604800'))
        if self.status_interval < 0:
            raise ValueError('STATUS_INTERVAL_SECONDS must be zero or positive')


class Sources:
    def __init__(self, config):
        self.c = config

    def api_rows(self, path):
        rows = []
        for _ in range(1000):
            separator = '&' if '?' in path else '?'
            payload = get_json(f'{self.c.url}{path}{separator}limit=100&offset={len(rows)}', self.c.key)
            if isinstance(payload, list):
                if rows:
                    raise ValueError('Unexpected pagination shape')
                return payload
            if not isinstance(payload, dict) or not isinstance(payload.get('data'), list):
                raise ValueError('Unexpected list response')
            page = payload['data']
            rows.extend(page)
            if 'total' in payload:
                if len(rows) == int(payload['total']):
                    return rows
                if not page or len(rows) > int(payload['total']):
                    raise ValueError('Incomplete pagination')
            elif len(page) < 100:
                return rows
        raise ValueError('Pagination limit exceeded')

    def entities(self, kind):
        fields = USER_FIELDS if kind == 'users' else KEY_FIELDS
        if self.c.source == 'api':
            path = '/api/user/?status=all' if kind == 'users' else '/api/api-key/'
            return project(self.api_rows(path), fields)
        import psycopg
        from psycopg.rows import dict_row
        table = 'core_user' if kind == 'users' else 'api_key'
        # Fixed allowlist: never read password hashes, key hashes, salts, or tokens.
        with psycopg.connect(self.c.db, connect_timeout=10, row_factory=dict_row) as conn:
            conn.execute('SET TRANSACTION READ ONLY')
            conn.execute("SET LOCAL statement_timeout = '15s'")
            rows = conn.execute(f'SELECT {", ".join(fields)} FROM {table}').fetchall()
        return json.loads(json.dumps(project(rows, fields), default=str))

    def version(self):
        result = get_json(self.c.url + '/api/session/properties', self.c.key)
        tag = result.get('version', {}).get('tag')
        if not isinstance(tag, str) or not tag:
            raise ValueError('Installed version missing from session properties')
        return {'installed': {'tag': tag}}

    def release(self):
        result = get_json('https://api.github.com/repos/metabase/metabase/releases/latest')
        if not result.get('tag_name') or result.get('prerelease') or result.get('draft'):
            raise ValueError('Invalid stable release response')
        return {'stable': {'tag': result['tag_name'], 'url': result['html_url']}}


def compare_versions(installed, latest):
    """Compare stable Metabase releases, normalizing the OSS/Enterprise prefix."""
    def parse(tag):
        match = re.fullmatch(r'v?([01])\.(\d+)\.(\d+)(?:\.(\d+))?', tag)
        if not match:
            return None
        return tuple(int(x or 0) for x in match.groups()[1:])
    current, published = parse(installed), parse(latest)
    if current is None or published is None:
        return 'unknown (non-stable or unrecognized version)'
    if current < published:
        return 'update available'
    return 'up to date' if current == published else 'ahead of published latest'


def changes(old, new):
    events = []
    for ident, value in new.items():
        if ident not in old:
            events.append({'event': 'created', 'id': ident, 'after': value})
        elif old[ident] != value:
            fields = sorted(k for k in old[ident].keys() | value.keys() if old[ident].get(k) != value.get(k))
            events.append({'event': 'updated', 'id': ident, 'fields': fields, 'before': old[ident], 'after': value})
    for ident in old.keys() - new.keys():
        events.append({'event': 'removed', 'id': ident, 'before': old[ident]})
    return events


class Store:
    def __init__(self, path, identity):
        path.parent.mkdir(parents=True, exist_ok=True)
        # Snapshots and queued mail hold account metadata; SQLite copies this mode to its journal.
        path.touch(exist_ok=True)
        path.chmod(0o600)
        self.conn = sqlite3.connect(path)
        self.conn.executescript('''
            CREATE TABLE IF NOT EXISTS snapshots (name TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS outbox (id INTEGER PRIMARY KEY, body TEXT NOT NULL, recipients TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS metadata (name TEXT PRIMARY KEY, value TEXT NOT NULL);
        ''')
        row = self.conn.execute("SELECT value FROM metadata WHERE name='identity'").fetchone()
        if row and row[0] != identity:
            raise ValueError('State belongs to another instance or source; use a new STATE_PATH')
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO metadata VALUES ('identity', ?)", (identity,))

    def observe(self, name, snapshot, recipients):
        encoded = json.dumps(snapshot, sort_keys=True)
        with self.conn:
            previous = self.conn.execute('SELECT value FROM snapshots WHERE name=?', (name,)).fetchone()
            events = changes(json.loads(previous[0]), snapshot) if previous else []
            if events:
                body = json.dumps({'monitor': name, 'observed_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'changes': events}, indent=2)
                self.conn.execute('INSERT INTO outbox(body, recipients) VALUES (?, ?)', (body, json.dumps(recipients)))
            self.conn.execute('INSERT OR REPLACE INTO snapshots VALUES (?, ?)', (name, encoded))
        return len(events)

    def status(self, report, recipients, interval, now=None):
        """Queue immediately on first run, then on a durable interval."""
        if not interval:
            return False
        now = time.time() if now is None else now
        with self.conn:
            row = self.conn.execute("SELECT value FROM metadata WHERE name='last_status'").fetchone()
            if row and now - float(row[0]) < interval:
                return False
            body = json.dumps({'monitor': 'status', 'observed_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now)), **report}, indent=2)
            self.conn.execute('INSERT INTO outbox(body, recipients) VALUES (?, ?)', (body, json.dumps(recipients)))
            self.conn.execute("INSERT OR REPLACE INTO metadata VALUES ('last_status', ?)", (str(now),))
        return True


def send(config, body, recipient):
    message = EmailMessage()
    status = json.loads(body).get('monitor') == 'status'
    message['Subject'] = '[Metabase monitor] Status report' if status else '[Metabase monitor] Changes detected'
    message['From'] = config.sender
    message['To'] = recipient
    message.set_content(f'Instance: {config.url}\n\n{body}')
    context = ssl.create_default_context()
    client = smtplib.SMTP_SSL(config.smtp_host, config.smtp_port, timeout=30, context=context) if config.smtp_mode == 'ssl' else smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=30)
    with client:
        if config.smtp_mode == 'starttls':
            client.starttls(context=context)
        username = os.environ.get('SMTP_USERNAME', '')
        if username:
            client.login(username, os.environ.get('SMTP_PASSWORD', ''))
        client.send_message(message)


def deliver(store, config, sender=send):
    ok = True
    for ident, body, encoded in store.conn.execute('SELECT id, body, recipients FROM outbox ORDER BY id').fetchall():
        remaining = json.loads(encoded)
        for recipient in list(remaining):
            try:
                sender(config, body, recipient)
            except Exception as exc:
                LOG.error('Email delivery failed (%s); queued for retry', type(exc).__name__)
                ok = False
                continue
            remaining.remove(recipient)
            with store.conn:
                if remaining:
                    store.conn.execute('UPDATE outbox SET recipients=? WHERE id=?', (json.dumps(remaining), ident))
                else:
                    store.conn.execute('DELETE FROM outbox WHERE id=?', (ident,))
    return ok


def cycle(store, sources, config):
    checks = {'users': lambda: sources.entities('users'), 'api_keys': lambda: sources.entities('api_keys'), 'version': sources.version}
    if config.releases:
        checks['releases'] = sources.release
    ok = True
    results, snapshots = {}, {}
    for name, check in checks.items():
        try:
            snapshots[name] = check()
            count = store.observe(name, snapshots[name], config.recipients)
            results[name] = 'ok'
            LOG.info('%s: %d changes', name, count)
        except Exception as exc:
            # Exception messages can contain credentials, URLs, or response bodies.
            LOG.error('%s check failed (%s); baseline preserved', name, type(exc).__name__)
            results[name] = 'failed: ' + type(exc).__name__
            ok = False
    version = {'installed': snapshots.get('version', {}).get('installed', {}).get('tag'),
               'latest': snapshots.get('releases', {}).get('stable', {}).get('tag')}
    if version['installed'] and version['latest']:
        version['comparison'] = compare_versions(version['installed'], version['latest'])
    else:
        version['comparison'] = 'unavailable' if config.releases else 'release checking disabled'
    report = {'checks': results, 'version': version,
              'counts': {k: len(snapshots[k]) for k in ('users', 'api_keys') if k in snapshots},
              'pending_messages': store.conn.execute('SELECT count(*) FROM outbox').fetchone()[0]}
    # Only current successful readings are compared; never label stale data current.
    if version['installed'] and version['latest']:
        store.observe('update_status', {'version': version}, config.recipients)
    store.status(report, config.recipients, getattr(config, 'status_interval', 604800))
    delivered = deliver(store, config)
    if ok and delivered:
        heartbeat = config.state.parent / 'healthy'
        heartbeat.touch()
    return ok and delivered


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once', action='store_true', help='Poll once and deliver queued changes')
    parser.add_argument('--healthcheck', action='store_true')
    args = parser.parse_args()
    if args.healthcheck:
        path = Path(os.environ.get('STATE_PATH', '/data/state.sqlite3')).parent / 'healthy'
        max_age = max(180, 3 * int(os.environ.get('POLL_INTERVAL_SECONDS', '300')))
        return 0 if path.exists() and time.time() - path.stat().st_mtime < max_age else 1
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    try:
        config = Config()
        identity = hashlib.sha256((config.url + '|' + config.source).encode()).hexdigest()
        store = Store(config.state, identity)
    except Exception as exc:
        LOG.error('Startup failed (%s); check configuration and state permissions', type(exc).__name__)
        return 1
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    sources = Sources(config)
    while not stop.is_set():
        ok = cycle(store, sources, config)
        if args.once:
            return 0 if ok else 1
        stop.wait(config.interval)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
