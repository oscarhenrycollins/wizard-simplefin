"""SimpleFIN: the bank connection for a Wizard box.

The owner links their banks at the SimpleFIN Bridge (their own account, $15/year paid to SimpleFIN), creates a
setup token there, and pastes it here. The app claims it once for a private access URL, keeps a local copy of
accounts, balances and transactions, syncs a few times a day (SimpleFIN refreshes banks about once a day), and
offers them read-only to other apps on this box through MCP (contract wizard.bank-connection/1).

Stdlib only. The access URL lives in DATA_DIR (0600 file, 0700 folder) and is never returned by any API or tool.
"""
import base64
import datetime as dt
import json
import os
import re
import sqlite3
import ssl
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get('DATA_DIR', ROOT / '.data'))
APP_ID = 'wizard-simplefin'
VERSION = '0.1.6'
PREFIX = '/' + APP_ID
PORT = os.environ.get('PORT', '8808')
SYNC_HOURS = float(os.environ.get('SIMPLEFIN_SYNC_HOURS', '6'))
WINDOW_DAYS = 44  # SimpleFIN caps a request at 90 days and recommends at most 45
HISTORY_WINDOWS = int(os.environ.get('SIMPLEFIN_HISTORY_WINDOWS', '8'))  # x 45 days on first sync (about a year)
DAILY_LIMIT = 16  # SimpleFIN expects <= 24 requests a day and disables tokens that keep exceeding it
TIMEOUT = 120     # the Bridge can take a minute to answer while it refreshes banks
STATIC = {'.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml'}
FORWARDED = ('tailscale-user-login', 'x-forwarded-for', 'forwarded', 'x-real-ip', 'cf-connecting-ip')
LOCK = threading.RLock()
SYNC_LOCK = threading.Lock()
SYNCING = threading.Event()


class Problem(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message


# ---------------- private storage ----------------

def _private():
    DATA.mkdir(parents=True, exist_ok=True)
    os.chmod(DATA, 0o700)
    return DATA


def _write_secret(name, text):
    p = _private() / name
    tmp = p.with_name('.' + name + '.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(text)
    os.replace(tmp, p)


def _config():
    try:
        return json.loads((DATA / 'config.json').read_text())
    except (FileNotFoundError, ValueError):
        return {}


def _save_config(c):
    _write_secret('config.json', json.dumps(c))


def _access():
    try:
        return (DATA / 'access_url').read_text().strip() or None
    except FileNotFoundError:
        return None


def db():
    c = sqlite3.connect(_private() / 'bank.sqlite3', timeout=15)
    c.row_factory = sqlite3.Row
    c.executescript('''
      CREATE TABLE IF NOT EXISTS connections (id TEXT PRIMARY KEY, name TEXT, org TEXT, org_url TEXT, error TEXT);
      CREATE TABLE IF NOT EXISTS accounts (id TEXT PRIMARY KEY, conn TEXT, institution TEXT, name TEXT, last_four TEXT,
        currency TEXT, balance REAL, available REAL, balance_at TEXT);
      CREATE TABLE IF NOT EXISTS transactions (account TEXT, id TEXT, date TEXT, posted INTEGER, amount REAL,
        description TEXT, payee TEXT, memo TEXT, pending INTEGER, seen TEXT, PRIMARY KEY (account, id));
      CREATE INDEX IF NOT EXISTS tx_date ON transactions(date);
    ''')
    os.chmod(DATA / 'bank.sqlite3', 0o600)
    return c


def _now():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def _clean(s, n=300):
    """SimpleFIN asks apps to sanitize its messages before showing them: plain text, bounded."""
    return re.sub(r'[\x00-\x1f<>]+', ' ', str(s or '')).strip()[:n]


# ---------------- SimpleFIN protocol ----------------

def _http(method, url, timeout=TIMEOUT):
    u = urlsplit(url)
    if u.scheme != 'https' or not u.hostname:
        raise Problem(400, 'SimpleFIN addresses must be https.')
    headers = {'Accept': 'application/json', 'User-Agent': f'wizard-simplefin/{VERSION}'}
    if u.username:
        headers['Authorization'] = 'Basic ' + base64.b64encode(f'{unquote(u.username)}:{unquote(u.password or "")}'.encode()).decode()
    clean = u._replace(netloc=u.hostname + (f':{u.port}' if u.port else '')).geturl()
    req = urllib.request.Request(clean, method=method, data=b'' if method == 'POST' else None, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context()) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except (OSError, ssl.SSLError) as e:
        raise Problem(502, f'Could not reach SimpleFIN ({_clean(getattr(e, "reason", e), 120)}). Try again in a minute.') from None


def _count_request():
    cfg = _config()
    day_ago = time.time() - 86400
    recent = [t for t in cfg.get('requests', []) if t > day_ago]
    if len(recent) >= DAILY_LIMIT:
        raise Problem(429, 'SimpleFIN allows only a few refreshes a day and this box has used them. It will sync again on its own later.')
    recent.append(time.time())
    cfg['requests'] = recent
    _save_config(cfg)


def claim(body):
    tok = re.sub(r'\s+', '', str(body.get('token') or ''))
    if not tok:
        raise Problem(400, 'Paste the setup token from SimpleFIN.')
    try:
        url = base64.b64decode(tok + '=' * (-len(tok) % 4), validate=False).decode()
    except (ValueError, UnicodeDecodeError):
        url = ''
    if not re.match(r'^https://[^\s/]+/\S+$', url):
        raise Problem(400, "That doesn't look like a SimpleFIN setup token. Copy the whole token from SimpleFIN's Apps page.")
    if _access():
        raise Problem(409, 'Already connected. Disconnect first to use a new token.')
    code, out = _http('POST', url)
    if code == 403:
        raise Problem(403, 'SimpleFIN says this token was already used. If you did not use it, someone else may have: '
                           'disable that connection on SimpleFIN and create a new token.')
    access = out.decode(errors='replace').strip()
    if code != 200 or not access.startswith('https://'):
        raise Problem(502, f'SimpleFIN did not accept the token (answered {code}). Create a new token and try again.')
    _write_secret('access_url', access)
    cfg = _config()
    cfg.update(connected=_now(), last_sync=None, error=None, history_done=False, requests=[])
    _save_config(cfg)
    start_sync()
    return status()


def disconnect():
    with LOCK:
        (DATA / 'access_url').unlink(missing_ok=True)
        c = db()
        try:
            with c:
                for t in ('transactions', 'accounts', 'connections'):
                    c.execute(f'DELETE FROM {t}')
        finally:
            c.close()
        cfg = _config()
        for k in ('connected', 'last_sync', 'error', 'history_done', 'requests', 'last_try'):
            cfg.pop(k, None)
        _save_config(cfg)
    return status()


def _num(v):
    try:
        return round(float(v), 2) if v not in (None, '') else None
    except (TypeError, ValueError):
        return None


def _date(ts):
    try:
        return dt.datetime.fromtimestamp(int(ts), dt.timezone.utc).strftime('%Y-%m-%d') if ts else None
    except (TypeError, ValueError, OSError):
        return None


def _fetch(start, end=None):
    access = _access()
    if not access:
        raise Problem(409, 'Paste your SimpleFIN setup token first.')
    _count_request()
    q = f'version=2&pending=1&start-date={int(start)}' + (f'&end-date={int(end)}' if end else '')
    code, out = _http('GET', access.rstrip('/') + '/accounts?' + q)
    if code in (401, 403):
        raise Problem(403, 'SimpleFIN turned this connection off. Disconnect, then paste a new setup token.')
    if code == 402:
        raise Problem(402, 'Your SimpleFIN subscription needs attention. Open SimpleFIN to renew it.')
    if code != 200:
        raise Problem(502, f'SimpleFIN answered {code}. It will try again later.')
    try:
        return json.loads(out)
    except ValueError:
        raise Problem(502, 'SimpleFIN sent something unreadable. It will try again later.') from None


def _store(c, d):
    errs = d.get('errlist') or [{'msg': m} for m in (d.get('errors') or [])]
    conn_err = {}
    general = []
    for e in errs:
        msg = _clean(e.get('msg') or e.get('message') or e if isinstance(e, (dict, str)) else '')
        if re.search(r'date range', msg, re.I):  # advice about our own request size, not a bank problem
            continue
        (conn_err.setdefault(e.get('conn_id'), []) if isinstance(e, dict) and e.get('conn_id') else general).append(msg)
    for cn in d.get('connections') or []:
        c.execute('INSERT OR REPLACE INTO connections VALUES (?,?,?,?,?)', (
            cn.get('conn_id'), _clean(cn.get('name'), 80), _clean(cn.get('org_name') or cn.get('name'), 80),
            cn.get('org_url'), ' '.join(conn_err.get(cn.get('conn_id'), [])) or None))
    n = 0
    for a in d.get('accounts') or []:
        org = a.get('org') or {}
        inst = org.get('name') or next((r['org'] for r in c.execute('SELECT org FROM connections WHERE id=?', (a.get('conn_id'),))), None) or a.get('conn_name')
        if org and not a.get('conn_id'):  # protocol v1 shape: org per account
            cid = org.get('id') or org.get('domain') or inst
            c.execute('INSERT OR IGNORE INTO connections VALUES (?,?,?,?,?)', (cid, inst, inst, org.get('url'), None))
            a['conn_id'] = cid
        name = _clean(a.get('name'), 120)
        m = re.search(r'(\d{4,5})\D*$', name)
        c.execute('INSERT OR REPLACE INTO accounts VALUES (?,?,?,?,?,?,?,?,?)', (
            str(a['id']), a.get('conn_id'), _clean(inst, 80), name, m.group(1) if m else None,
            a.get('currency') if re.fullmatch(r'[A-Z]{3}', str(a.get('currency') or '')) else 'USD',
            _num(a.get('balance')), _num(a.get('available-balance')), _date(a.get('balance-date'))))
        for t in a.get('transactions') or []:
            when = t.get('transacted_at') or t.get('posted')
            c.execute('''INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?)
              ON CONFLICT(account,id) DO UPDATE SET date=excluded.date, posted=excluded.posted, amount=excluded.amount,
              description=excluded.description, payee=excluded.payee, memo=excluded.memo, pending=excluded.pending''', (
                str(a['id']), str(t['id']), _date(when), int(t.get('posted') or 0), _num(t.get('amount')),
                _clean(t.get('description'), 200), _clean(t.get('payee'), 120) or None, _clean(t.get('memo'), 200) or None,
                1 if t.get('pending') or not t.get('posted') else 0, _now()))
            n += 1
    return n, general


def sync():
    if not SYNC_LOCK.acquire(blocking=False):
        raise Problem(409, 'A sync is already running.')
    SYNCING.set()
    try:
        cfg = _config()
        now = time.time()
        # First sync: walk back in 45-day windows (SimpleFIN's recommended range). Later: the last 30 days.
        w = WINDOW_DAYS * 86400
        with LOCK:
            c = db()
            try:
                known = {r[0] for r in c.execute('SELECT id FROM accounts')}
                # Accounts whose history was never pulled in full (banks linked after the first sync).
                shallow = {r[0] for r in c.execute("SELECT account FROM transactions GROUP BY account HAVING MIN(date) >= ?",
                           (dt.date.fromtimestamp(now - 35 * 86400).isoformat(),))}
            finally:
                c.close()
        backfill = set(cfg.get('backfilled') or [])
        need = (shallow - backfill) if cfg.get('history_done') else set()
        full = not cfg.get('history_done') or bool(need)
        windows = [(now - w * (i + 1), now - w * i if i else None) for i in range(HISTORY_WINDOWS)] if full else [(now - 30 * 86400, None)]
        notes = []
        for i, (start, end) in enumerate(windows):
            d = _fetch(start, end)
            with LOCK:
                c = db()
                try:
                    with c:
                        n, general = _store(c, d)
                finally:
                    c.close()
            notes += general
            if i and n == 0:  # banks keep no older history
                break
            if _config().get('requests') and len(_config()['requests']) >= DAILY_LIMIT - 1:
                break  # leave room for tomorrow's refresh; the rest backfills next time
        cfg = _config()
        cfg.update(last_sync=_now(), error=' '.join(dict.fromkeys(notes))[:400] or None, history_done=True,
                   backfilled=sorted(backfill | need | (known if full else set())))
        _save_config(cfg)
    except Problem as e:
        cfg = _config()
        cfg['error'] = e.message
        cfg['last_try'] = _now()
        _save_config(cfg)
        raise
    finally:
        SYNCING.clear()
        SYNC_LOCK.release()
    return status()


def start_sync():
    def run():
        try:
            sync()
        except Problem:
            pass
    threading.Thread(target=run, daemon=True).start()


def _scheduler():
    while True:
        time.sleep(60)
        cfg = _config()
        last = cfg.get('last_sync') or cfg.get('last_try')
        if not _access() or SYNCING.is_set():
            continue
        age = (dt.datetime.now(dt.timezone.utc) - dt.datetime.strptime(last, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=dt.timezone.utc)).total_seconds() if last else 1e9
        if age > SYNC_HOURS * 3600:
            try:
                sync()
            except Exception:  # noqa: BLE001 - the scheduler must survive a bad sync
                pass


# ---------------- read API ----------------

def status():
    cfg = _config()
    with LOCK:
        c = db()
        try:
            conns = [dict(r) for r in c.execute('SELECT id,name,org,org_url,error FROM connections ORDER BY org')]
            accts = [dict(r) for r in c.execute('SELECT id,conn,institution,name,last_four,currency,balance,available,balance_at FROM accounts ORDER BY institution,name')]
            n = c.execute('SELECT COUNT(*) FROM transactions').fetchone()[0]
        finally:
            c.close()
    banks = [{'id': cn['id'], 'institution': cn['org'] or cn['name'], 'status': 'needs_login' if cn['error'] else 'connected',
              'error': cn['error'], 'accounts': [a for a in accts if a['conn'] == cn['id']]} for cn in conns]
    connected = bool(_access())
    return {
        'contract': 'wizard.bank-connection/1', 'provider': 'simplefin', 'name': 'SimpleFIN',
        'status': 'connected' if connected else 'not_configured', 'syncing': SYNCING.is_set(),
        'connected_since': cfg.get('connected'), 'banks': banks, 'transactions': n,
        'last_sync': cfg.get('last_sync'), 'error': cfg.get('error') if connected else None, 'sync_every_hours': SYNC_HOURS,
    }


def accounts():
    return [{'id': a['id'], 'institution': b['institution'], 'name': a['name'], 'last_four': a['last_four'], 'currency': a['currency'],
             'balance': a['balance'], 'available': a['available'], 'balance_at': a['balance_at']}
            for b in status()['banks'] for a in b['accounts']]


def transactions(args):
    q, p = ['''SELECT t.id, t.account, a.institution, a.name AS account_name, a.last_four, t.date, t.amount, t.description,
               t.payee AS counterparty, t.memo, CASE t.pending WHEN 1 THEN 'pending' ELSE 'posted' END AS status
               FROM transactions t LEFT JOIN accounts a ON a.id=t.account WHERE 1=1'''], []
    for key, op in (('from', '>='), ('to', '<=')):
        v = args.get(key)
        if v:
            if not re.match(r'^\d{4}-\d{2}-\d{2}$', str(v)):
                raise Problem(400, f'{key} must be YYYY-MM-DD.')
            q.append(f'AND t.date {op} ?'); p.append(v)
    if args.get('account'):
        v = str(args['account'])
        q.append('AND (t.account = ? OR a.last_four = ? OR a.name LIKE ?)'); p += [v, v, '%' + v[:60] + '%']
    if args.get('search'):
        s = '%' + str(args['search'])[:80] + '%'
        q.append('AND (t.description LIKE ? OR t.payee LIKE ? OR t.memo LIKE ?)'); p += [s, s, s]
    for key, op in (('min_amount', '>='), ('max_amount', '<=')):
        if args.get(key) not in (None, ''):
            q.append(f'AND t.amount {op} ?'); p.append(float(args[key]))
    if args.get('status') in ('posted', 'pending'):
        q.append('AND t.pending = ?'); p.append(1 if args['status'] == 'pending' else 0)
    limit = max(1, min(int(args.get('limit') or 100), 1000))
    q.append('ORDER BY t.date DESC, t.posted DESC LIMIT ?'); p.append(limit)
    with LOCK:
        c = db()
        try:
            rows = [dict(r) for r in c.execute(' '.join(q), p)]
        finally:
            c.close()
    return {'count': len(rows), 'transactions': rows,
            'note': 'Negative = money out of the account. Card refunds and statement credits are positive. Card balances are negative when you owe.'}


TOOLS = [
    {'name': 'bank_status', 'description': 'SimpleFIN bank connection status: linked banks, their accounts, last sync and any bank SimpleFIN says needs attention. Never returns credentials. To set up, the owner opens the SimpleFIN app (/wizard-simplefin/); never ask for the setup token in chat.',
     'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}, 'annotations': {'readOnlyHint': True}},
    {'name': 'bank_accounts', 'description': 'All linked bank and card accounts with institution, name, last digits when the name shows them, and current balance (negative on cards = owed).',
     'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}, 'annotations': {'readOnlyHint': True}},
    {'name': 'bank_transactions', 'description': 'Search synced transactions, newest first. Filters: from/to (YYYY-MM-DD), account (id, last digits or name), search (description/payee/memo), min_amount/max_amount, status (posted/pending), limit (default 100, max 1000).',
     'inputSchema': {'type': 'object', 'properties': {'from': {'type': 'string'}, 'to': {'type': 'string'}, 'account': {'type': 'string'}, 'search': {'type': 'string'},
                     'min_amount': {'type': 'number'}, 'max_amount': {'type': 'number'}, 'status': {'type': 'string', 'enum': ['posted', 'pending']}, 'limit': {'type': 'integer'}}, 'additionalProperties': False},
     'annotations': {'readOnlyHint': True}},
    {'name': 'bank_sync', 'description': 'Fetch the latest data from SimpleFIN now. SimpleFIN refreshes banks about once a day and allows only a few requests a day, so use sparingly; it also syncs on its own.',
     'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}, 'annotations': {'readOnlyHint': False, 'destructiveHint': False}},
]


def rpc(msg):
    if not isinstance(msg, dict):
        return {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32600, 'message': 'Expected a JSON-RPC object.'}}
    m, i, p = msg.get('method'), msg.get('id'), msg.get('params') or {}
    if 'id' not in msg:
        return None
    try:
        if m == 'initialize':
            r = {'protocolVersion': '2025-06-18', 'capabilities': {'tools': {}}, 'serverInfo': {'name': APP_ID, 'version': VERSION}}
        elif m == 'tools/list':
            r = {'tools': TOOLS}
        elif m == 'tools/call':
            a, n = p.get('arguments') or {}, p.get('name')
            if n == 'bank_status':
                v = status()
            elif n == 'bank_accounts':
                v = {'accounts': accounts()}
            elif n == 'bank_transactions':
                v = transactions(a)
            elif n == 'bank_sync':
                v = sync()
            else:
                return {'jsonrpc': '2.0', 'id': i, 'error': {'code': -32602, 'message': 'Unknown tool.'}}
            r = {'content': [{'type': 'text', 'text': json.dumps(v, ensure_ascii=False)}], 'structuredContent': v, 'isError': False}
        elif m == 'ping':
            r = {}
        else:
            return {'jsonrpc': '2.0', 'id': i, 'error': {'code': -32601, 'message': 'Unknown method.'}}
    except Problem as e:
        r = {'content': [{'type': 'text', 'text': e.message}], 'isError': True}
    except (ValueError, TypeError):
        r = {'content': [{'type': 'text', 'text': 'Invalid arguments.'}], 'isError': True}
    return {'jsonrpc': '2.0', 'id': i, 'result': r}


# ---------------- HTTP ----------------

def _csv(name):
    return {x.strip().lower() for x in os.environ.get(name, '').split(',') if x.strip()}


_LOOPBACK = {f'127.0.0.1:{PORT}', f'localhost:{PORT}', f'{APP_ID}:{PORT}'}
TRUSTED_HOSTS = _csv('WIZARD_TRUSTED_HOSTS') | _LOOPBACK
TRUSTED_ORIGINS = _csv('WIZARD_TRUSTED_ORIGINS') | {'http://' + h for h in _LOOPBACK}
CSP = "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; object-src 'none'; base-uri 'self'; frame-ancestors 'self'"


class Handler(BaseHTTPRequestHandler):
    def refused(self, write=False):
        if (self.headers.get('Host') or '').lower() not in TRUSTED_HOSTS:
            self.send(403, {'error': 'Unknown host.'}); return True
        origin = (self.headers.get('Origin') or '').lower()
        if origin and origin not in TRUSTED_ORIGINS:
            self.send(403, {'error': 'Cross-origin request refused.'}); return True
        if write and not origin and (self.headers.get('Sec-Fetch-Site') or '') == 'cross-site':
            self.send(403, {'error': 'Cross-origin request refused.'}); return True
        return False

    def send(self, code, body=None, kind='application/json'):
        b = body if isinstance(body, bytes) else b'' if body is None else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(b)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', CSP)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(b)

    def route(self):
        p = urlsplit(self.path).path
        return '/' if p == PREFIX else p[len(PREFIX):] if p.startswith(PREFIX + '/') else p

    def body(self):
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            raise Problem(415, 'Use application/json.')
        size = int(self.headers.get('Content-Length', '0') or 0)
        if not 0 <= size <= 65536:
            raise Problem(413, 'Request too large.')
        try:
            d = json.loads(self.rfile.read(size) or b'{}')
        except ValueError:
            raise Problem(400, 'Invalid JSON.') from None
        if not isinstance(d, dict):
            raise Problem(400, 'Expected a JSON object.')
        return d

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if self.refused():
            return
        path = self.route()
        try:
            if path in ('/healthz', '/api/health'):
                s = status()
                return self.send(200, {'ok': True, 'app': APP_ID, 'version': VERSION, 'status': s['status'], 'banks': len(s['banks'])})
            if path == '/api/status':
                return self.send(200, status())
            if path == '/api/accounts':
                return self.send(200, {'accounts': accounts()})
            if path == '/api/transactions':
                return self.send(200, transactions({k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}))
            if path == '/openapi.json':
                return self.send(200, (ROOT / 'openapi.json').read_bytes())
            name = 'index.html' if path == '/' else path[1:]
            kind = STATIC.get(Path(name).suffix)
            if kind and '/' not in name and not name.startswith('.') and (ROOT / 'web' / name).is_file():
                return self.send(200, (ROOT / 'web' / name).read_bytes(), kind)
            self.send(405 if path == '/mcp' else 404, {'error': 'Use POST /mcp.' if path == '/mcp' else 'Not found.'})
        except Problem as e:
            self.send(e.code, {'error': e.message})
        except (ValueError, TypeError):
            self.send(400, {'error': 'Invalid query.'})

    def do_POST(self):
        if self.refused(write=True):
            return
        path = self.route()
        try:
            if path == '/mcp':
                if any(self.headers.get(h) for h in FORWARDED):
                    return self.send(403, {'error': 'MCP is box-only.'})
                out = rpc(self.body())
                return self.send(202 if out is None else 200, out)
            b = self.body()
            if path == '/api/connect':
                return self.send(200, claim(b))
            if path == '/api/disconnect':
                return self.send(200, disconnect())
            if path == '/api/sync':
                if SYNCING.is_set():
                    raise Problem(409, 'Already syncing.')
                start_sync()
                time.sleep(0.2)
                return self.send(202, status())
            self.send(404, {'error': 'Not found.'})
        except Problem as e:
            self.send(e.code, {'error': e.message})

    def log_message(self, *a):
        pass


if __name__ == '__main__':
    os.umask(0o077)
    for f in _private().rglob('*'):
        try:
            os.chmod(f, 0o700 if f.is_dir() else 0o600)
        except OSError:
            pass
    _c = _config()
    if _c.get('connected') and not _c.get('last_sync'):
        _since = dt.datetime.strptime(_c['connected'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=dt.timezone.utc).timestamp()
        _c['requests'] = [t for t in _c.get('requests', []) if t >= _since]
        _c.pop('last_try', None)
        _save_config(_c)
    db().close()
    threading.Thread(target=_scheduler, daemon=True).start()
    ThreadingHTTPServer((os.environ.get('BIND', '0.0.0.0'), int(PORT)), Handler).serve_forever()
