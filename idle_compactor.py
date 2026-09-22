#!/usr/bin/env python3
"""Local, conservative idle compaction for compatible Codex desktop sessions.

No API key, cloud service, extra model prompts, or transcript edits. Python 3.9+.
Compatibility is probed from the installed app and validated against live state.
"""
from __future__ import annotations
import argparse
import contextlib
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import plistlib
import socket
import sqlite3
import stat
import struct
import subprocess
import sys
import time
import uuid
import compatibility

VERSION = '0.2.1'
FRAME_LIMIT = 32 * 1024 * 1024
TAIL_LIMIT = 16 * 1024 * 1024
DEFAULTS = dict(enabled=False, idle_seconds=1500, latest_start_seconds=1740,
                min_context_tokens=50000, max_context_tokens=180000,
                max_per_day=50, max_per_month=None, cooldown_seconds=86400,
                poll_seconds=30, exclude_threads=[], allow_threads=[],
                metered_automation_approved=False)
LABEL = 'local.codex-idle-compactor'

class GuardError(RuntimeError):
    pass

def timestamp(value):
    if isinstance(value, (float, int)):
        return float(value) / (1000 if value > 100000000000 else 1)
    if not value:
        return 0.0
    try:
        return dt.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError):
        return 0.0

def private_dir(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or path.stat().st_uid != os.getuid():
        raise GuardError('State directory must be owned by you and not a symlink')
    path.chmod(0o700)

def atomic_json(path, value):
    private_dir(path.parent)
    tmp = path.with_name(path.name + '.tmp-' + uuid.uuid4().hex)
    with tmp.open('x') as f:
        os.chmod(tmp, 0o600)
        json.dump(value, f, indent=2)
        f.write('\n')
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)

def config_read(root):
    path = root / 'config.json'
    if not path.exists():
        atomic_json(path, DEFAULTS)
    c = dict(DEFAULTS)
    c.update(json.loads(path.read_text()))
    for name in ('idle_seconds', 'latest_start_seconds', 'min_context_tokens',
                 'max_context_tokens', 'max_per_day',
                 'cooldown_seconds', 'poll_seconds'):
        if type(c[name]) is not int or c[name] <= 0:
            raise GuardError('Invalid positive integer setting: ' + name)
    if not 60 <= c['idle_seconds'] < c['latest_start_seconds'] < 1800:
        raise GuardError('Idle window must end before 30 minutes')
    if c['poll_seconds'] < 10 or c['min_context_tokens'] > c['max_context_tokens']:
        raise GuardError('Invalid polling interval or context range')
    if c['max_per_day'] > 50:
        raise GuardError('Daily attempt ceiling is 50')
    if c['max_per_month'] is not None and (type(c['max_per_month']) is not int or c['max_per_month'] <= 0):
        raise GuardError('Monthly cap must be a positive integer or null for no cap')
    if c['cooldown_seconds'] < 3600:
        raise GuardError('Minimum per-task cooldown is one hour')
    for name in ('enabled', 'metered_automation_approved'):
        if type(c[name]) is not bool:
            raise GuardError('Invalid boolean setting: ' + name)
    for name in ('exclude_threads', 'allow_threads'):
        if not isinstance(c[name], list) or any(not isinstance(x, str) for x in c[name]):
            raise GuardError('Invalid thread list: ' + name)
    return c

@contextlib.contextmanager
def exclusive(root):
    private_dir(root)
    with (root / 'run.lock').open('a') as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise GuardError('Another compactor process owns the lock')
        yield

def verify_app(app):
    try:
        return compatibility.read_protocol(app)['info']
    except compatibility.CompatibilityError as e:
        raise GuardError(str(e))


class Desktop:
    """Length-prefixed local IPC, following the same thread owner as the app UI."""
    def __init__(self, home, app, timeout=12):
        try:
            self.protocol = compatibility.read_protocol(app)['versions']
        except compatibility.CompatibilityError as e:
            raise GuardError(str(e))
        paths = [home / 'ipc/ipc.sock', Path('/tmp/codex-ipc') / ('ipc-'+str(os.getuid())+'.sock')]
        path = next((p for p in paths if p.exists()), paths[0])
        for p in (path.parent, path):
            st = p.lstat()
            if st.st_uid != os.getuid() or st.st_mode & 0o022 or stat.S_ISLNK(st.st_mode):
                raise GuardError('Unsafe IPC ownership or permissions')
        if not stat.S_ISSOCK(path.stat().st_mode):
            raise GuardError('IPC path is not a socket')
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.deadline = time.monotonic() + timeout
        self.timeout = timeout
        self.client = 'pending'
        self.owner = None
        self.thread_id = None
        self.snapshots = {}
        self.sock.connect(str(path))
        try:
            r = self.request('initialize', {'clientType': 'idle-compactor'}, version=0)
            self.client = r['result']['clientId']
        except BaseException:
            self.sock.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        if self.thread_id and self.owner:
            with contextlib.suppress(OSError):
                self.broadcast('thread-stream-following-changed',
                               dict(conversationId=self.thread_id, hostId='local', following=False))
        self.sock.close()

    def send(self, obj):
        body = json.dumps(obj, separators=(',', ':')).encode()
        if len(body) > FRAME_LIMIT:
            raise GuardError('Oversize outgoing IPC frame')
        self.sock.sendall(struct.pack('<I', len(body)) + body)

    def receive(self):
        def read(size):
            out = bytearray()
            while len(out) < size:
                remaining = self.deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('IPC deadline exceeded')
                self.sock.settimeout(remaining)
                part = self.sock.recv(size - len(out))
                if not part:
                    raise GuardError('Desktop IPC disconnected')
                out.extend(part)
            return out
        size = struct.unpack('<I', read(4))[0]
        if not 0 < size <= FRAME_LIMIT:
            raise GuardError('Oversize or empty incoming IPC frame')
        obj = json.loads(read(size))
        if obj.get('type') == 'client-discovery-request':
            self.send(dict(type='client-discovery-response', requestId=obj['requestId'],
                           response={'canHandle': False}))
        if obj.get('method') == 'thread-stream-state-changed':
            if obj.get('version') != self.protocol['thread-stream-state-changed']:
                raise GuardError('Unsupported thread snapshot protocol')
            p = obj.get('params', {})
            change = p.get('change', {})
            if (change.get('type') == 'snapshot' and p.get('hostId') == 'local'
                    and obj.get('sourceClientId') == self.owner):
                self.snapshots[p['conversationId']] = change['conversationState']
        return obj

    def request(self, method, params, version=None, owner=None):
        self.deadline = time.monotonic() + self.timeout
        version = self.protocol.get(method, 0) if version is None else version
        rid = str(uuid.uuid4())
        msg = dict(type='request', requestId=rid, sourceClientId=self.client,
                   method=method, params=params, version=version, timeoutMs=10000)
        if owner:
            msg['targetClientId'] = owner
        self.send(msg)
        while True:
            obj = self.receive()
            if obj.get('type') == 'response' and obj.get('requestId') == rid:
                if obj.get('resultType') != 'success':
                    # Do not echo errors that might contain conversation content.
                    raise GuardError('Desktop request rejected: ' + method)
                if owner and obj.get('handledByClientId') != owner:
                    raise GuardError('Desktop owner changed during request')
                return obj

    def broadcast(self, method, params):
        self.send(dict(type='broadcast', method=method, params=params, version=self.protocol.get(method, 0),
                       sourceClientId=self.client, targetClientIds=[self.owner]))

    def snapshot(self, thread_id):
        if self.thread_id and self.thread_id != thread_id:
            raise GuardError('Use one desktop connection per candidate')
        r = self.request('thread-owner-discovery', dict(hostId='local', conversationId=thread_id))
        new_owner = r.get('handledByClientId')
        if self.owner and self.owner != new_owner:
            raise GuardError('Thread ownership changed')
        self.owner, self.thread_id = new_owner, thread_id
        self.snapshots.pop(thread_id, None)
        self.deadline = time.monotonic() + self.timeout
        self.broadcast('thread-stream-following-changed',
                       dict(conversationId=thread_id, hostId='local', following=True))
        while thread_id not in self.snapshots:
            self.receive()
        return self.snapshots.pop(thread_id)

    def compact(self):
        r = self.request('thread-follower-compact-thread',
                         dict(conversationId=self.thread_id), owner=self.owner)
        if r.get('result', {}).get('ok') is not True:
            raise GuardError('Unexpected compaction acknowledgement')
        # An acknowledgement is not proof that compaction finished.


def read_tail(path):
    with path.open('rb') as f:
        length = f.seek(0, 2)
        start = max(0, length - TAIL_LIMIT)
        f.seek(start)
        if start:
            f.readline()
        data = f.read(TAIL_LIMIT)
    rows = []
    for line in data.splitlines(keepends=True):
        if not line.endswith(b'\n'):
            continue  # An in-progress write is not a complete record.
        try:
            rows.append(json.loads(line))
        except (ValueError, UnicodeDecodeError):
            raise GuardError('Malformed rollout record; refusing compaction')
    return rows


def activity(path):
    """Only scalar timing/counter metadata escapes this function."""
    result = dict(last_activity=0.0, completed=0.0, started=0.0,
                  compacted=0.0, turn_id=None, context_tokens=0,
                  input_tokens=0, cached_tokens=0, output_tokens=0,
                  usage_timestamp=0.0, user_timestamp=0.0)
    modern_usage = None
    legacy_usage = None
    for row in read_tail(path):
        t = timestamp(row.get('timestamp'))
        typ, p = row.get('type'), row.get('payload', {})
        if not isinstance(p, dict):
            continue
        result['last_activity'] = max(result['last_activity'], t)
        if typ == 'response_item' and p.get('role') == 'user':
            result['user_timestamp'] = max(result['user_timestamp'], t)
        if typ == 'compacted':
            result['compacted'] = max(result['compacted'], t)
        if typ == 'token_usage_record':
            modern_usage = (t, p.get('usage', {}))
        if typ == 'event_msg':
            if p.get('type') == 'task_started':
                result['started'] = t
            if p.get('type') == 'task_complete':
                result['completed'] = t
                result['turn_id'] = p.get('turn_id')
            if p.get('type') == 'token_count' and p.get('info'):
                legacy_usage = (t, p['info'].get('last_token_usage', {}))
    # Prefer modern usage rather than adding both record formats.
    usage = modern_usage or legacy_usage
    if usage:
        t, u = usage
        result.update(usage_timestamp=t, input_tokens=u.get('input_tokens', 0),
                      cached_tokens=u.get('cached_input_tokens', 0),
                      output_tokens=u.get('output_tokens', 0))
        result['context_tokens'] = u.get('input_tokens', 0) + u.get('output_tokens', 0)
    result['fingerprint'] = ':'.join(str(result[k]) for k in
                                  ('turn_id', 'last_activity', 'context_tokens', 'compacted'))
    return result


def candidates(home, now, thread_id=None):
    # Select schema by columns, not a fixed state_N filename. Prefer the highest
    # supported schema and report incompatible state rather than claiming coverage.
    paths = sorted(home.glob('state_*.sqlite'),
                   key=lambda p:int(p.stem.split('_')[-1]) if p.stem.split('_')[-1].isdigit() else -1,
                   reverse=True)
    rows = None
    for path in paths:
        try:
            with contextlib.closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=2)) as db:
                db.row_factory=sqlite3.Row
                columns={r[1] for r in db.execute('PRAGMA table_info(threads)')}
                if not {'id','rollout_path','source','archived','updated_at'} <= columns:continue
                query='SELECT id,rollout_path,source,archived'+(',model' if 'model' in columns else ',NULL AS model')+' FROM threads WHERE archived=0'
                values=[]
                if thread_id:
                    query+=' AND id=?';values.append(thread_id)
                else:
                    query+=' AND updated_at>=?';values.append(int(now-3600))
                # Subagents are excluded irrespective of the top-level source name.
                query+=" AND source NOT LIKE '%subagent%' ORDER BY updated_at DESC LIMIT 500"
                rows=db.execute(query,values).fetchall()
                break
        except sqlite3.DatabaseError:
            continue
    if rows is None:
        raise GuardError('No compatible local session database; update pending')
    for row in rows:
        path=Path(row['rollout_path']).resolve()
        if (home/'sessions').resolve() not in path.parents:continue
        yield dict(id=row['id'],path=path,model=row['model'])


def eligibility(a, c, now):
    if not a['turn_id'] or not a['completed'] or not a['usage_timestamp']:
        return 'no-complete-turn-or-usage'
    if a['started'] > a['completed'] or a['user_timestamp'] > a['completed']:
        return 'running-or-new-input'
    if a['compacted'] >= a['completed']:
        return 'already-compacted'
    idle = now - a['last_activity']
    if idle < c['idle_seconds']:
        return 'not-idle-long-enough'
    if idle >= c['latest_start_seconds']:
        return 'missed-idle-window'
    if not c['min_context_tokens'] <= a['context_tokens'] <= c['max_context_tokens']:
        return 'context-outside-pilot-range'
    return None


def live_guard(state, a, c, thread_id):
    if state.get('id') != thread_id or state.get('hostId') != 'local':
        raise GuardError('Unexpected task or host')
    runtime = state.get('threadRuntimeStatus', {}).get('type')
    if runtime is None:
        turns = state.get('turns', [])
        latest = turns[-1] if turns and isinstance(turns[-1], dict) else {}
        if latest.get('status') != 'completed' or latest.get('turnId') != a['turn_id']:
            raise GuardError('Older task state cannot establish idleness')
    elif runtime != 'idle':
        raise GuardError('Live task is not idle')
    if state.get('resumeState') != 'resumed':
        raise GuardError('Task is not loaded in the desktop app')
    if state.get('requests') or state.get('unconfirmedTurnSubmissions'):
        raise GuardError('Task has pending requests or submissions')
    if state.get('sideConversation') or state.get('agentNickname') or state.get('ephemeral'):
        raise GuardError('Subagents and ephemeral tasks are excluded')
    last = state.get('latestTokenUsageInfo', {}).get('last', {})
    count = last.get('inputTokens', 0) + last.get('outputTokens', 0)
    if count != a['context_tokens']:
        raise GuardError('Desktop and saved token counts differ')
    if not c['min_context_tokens'] <= count <= c['max_context_tokens']:
        raise GuardError('Live context outside configured range')
    return count

class Ledger:
    def __init__(self, root):
        private_dir(root)
        path = root / 'ledger.sqlite'
        self.db = sqlite3.connect(path)
        path.chmod(0o600)
        self.db.execute('''CREATE TABLE IF NOT EXISTS attempts (
            id INTEGER PRIMARY KEY, thread_id TEXT NOT NULL, turn_id TEXT NOT NULL,
            started REAL NOT NULL, status TEXT NOT NULL, context_tokens INTEGER NOT NULL,
            before_compacted REAL NOT NULL, finished REAL,
            UNIQUE(thread_id, turn_id))''')
        self.db.commit()

    def reason(self, thread_id, turn_id, c, now):
        if self.db.execute("SELECT 1 FROM attempts WHERE status != 'completed' LIMIT 1").fetchone():
            return 'unresolved-attempt-review-required'
        if self.db.execute('SELECT 1 FROM attempts WHERE thread_id=? AND turn_id=?',
                           (thread_id, turn_id)).fetchone():
            return 'already-attempted-this-turn'
        if self.db.execute('SELECT 1 FROM attempts WHERE thread_id=? AND started>?',
                           (thread_id, now - c['cooldown_seconds'])).fetchone():
            return 'per-task-cooldown'
        utc = dt.datetime.fromtimestamp(now, dt.timezone.utc)
        day = utc.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        month = utc.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()
        for start, cap, label in [(day, c['max_per_day'], 'daily-cap'),
                                  (month, c['max_per_month'], 'monthly-cap')]:
            if cap is None:
                continue
            count = self.db.execute('SELECT count(*) FROM attempts WHERE started>=?', (start,)).fetchone()[0]
            if count >= cap:
                return label
        return None

    def reserve(self, row, a, c, now):
        # Reservation survives errors/crashes; uncertain requests are NEVER retried.
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            reason = self.reason(row['id'], a['turn_id'], c, now)
            if reason:
                raise GuardError(reason)
            cur = self.db.execute('''INSERT INTO attempts
                (thread_id,turn_id,started,status,context_tokens,before_compacted)
                VALUES(?,?,?,'reserved',?,?)''',
                (row['id'], a['turn_id'], now, a['context_tokens'], a['compacted']))
        return cur.lastrowid

    def status(self, attempt, status):
        with self.db:
            self.db.execute('UPDATE attempts SET status=?, finished=? WHERE id=?',
                            (status, time.time(), attempt))

    def reconcile(self, home):
        results = []
        for aid, tid, before in self.db.execute("SELECT id,thread_id,before_compacted FROM attempts WHERE status != 'completed'").fetchall():
            rows = list(candidates(home, time.time(), tid))
            if rows and activity(rows[0]['path'])['compacted'] > before:
                self.status(aid, 'completed')
                results.append({'attempt': aid, 'status': 'completed'})
            else:
                results.append({'attempt': aid, 'status': 'unresolved-no-retry'})
        return results

    def summary(self):
        return [dict(status=s, count=n) for s, n in self.db.execute(
            'SELECT status,count(*) FROM attempts GROUP BY status')]

    def close(self):
        self.db.close()


def run_compaction(row, a, c, root, home, app, ledger, automatic=False,
                   desktop_factory=Desktop, timeout=180):
    with desktop_factory(home, app) as desktop:
        state = desktop.snapshot(row['id'])
        live_guard(state, a, c, row['id'])
        fresh = activity(row['path'])
        if fresh['fingerprint'] != a['fingerprint']:
            raise GuardError('Task changed during preflight')
        # Read the kill switch again immediately before reserving and dispatching.
        current = config_read(root)
        if automatic and not (current['enabled'] and current['metered_automation_approved']):
            raise GuardError('Automatic compaction is paused')
        if row['id'] in current['exclude_threads'] or (current['allow_threads'] and row['id'] not in current['allow_threads']):
            raise GuardError('Task excluded by current configuration')
        reason = eligibility(fresh, current, time.time())
        if reason:
            raise GuardError(reason)
        attempt = ledger.reserve(row, fresh, current, time.time())
        try:
            desktop.compact()
            ledger.status(attempt, 'acknowledged')
        except Exception:
            ledger.status(attempt, 'uncertain-no-retry')
            raise
        deadline = time.monotonic() + timeout
        observed_stat = None
        while time.monotonic() < deadline:
            st = row['path'].stat()
            signature = (st.st_ino, st.st_size, st.st_mtime_ns)
            if signature == observed_stat:
                time.sleep(1)
                continue
            observed_stat = signature
            after = activity(row['path'])
            if after['compacted'] > fresh['compacted']:
                ledger.status(attempt, 'completed')
                return {'thread_id': row['id'], 'status': 'completed',
                        'context_before': fresh['context_tokens']}
            # A user resuming work ends our wait; do not interrupt their turn.
            if after['user_timestamp'] > fresh['last_activity']:
                ledger.status(attempt, 'unverified-user-resumed')
                return {'thread_id': row['id'], 'status': 'unverified-user-resumed'}
            time.sleep(1)
        ledger.status(attempt, 'unverified-timeout-no-retry')
        return {'thread_id': row['id'], 'status': 'unverified-timeout-no-retry'}


def scan(root, home, app, execute=False, thread_id=None):
    c = config_read(root)
    verify_app(app)
    if execute and not thread_id and not (c['enabled'] and c['metered_automation_approved']):
        raise GuardError('Automatic calls disabled; use plan or explicitly approve a capped pilot')
    ledger = Ledger(root)
    output = []
    try:
        for row in candidates(home, time.time(), thread_id):
            if row['id'] in c['exclude_threads'] or (c['allow_threads'] and row['id'] not in c['allow_threads']):
                continue
            try:
                a = activity(row['path'])
                reason = eligibility(a, c, time.time()) or ledger.reason(row['id'], a['turn_id'], c, time.time())
                entry = dict(thread_id=row['id'], context_tokens=a['context_tokens'],
                             idle_seconds=round(time.time() - a['last_activity']),
                             decision=reason or 'candidate-needs-live-check')
                if not reason:
                    if execute:
                        entry = run_compaction(row, a, c, root, home, app, ledger,
                                               automatic=thread_id is None)
                    else:
                        with Desktop(home, app) as desktop:
                            state = desktop.snapshot(row['id'])
                            live_guard(state, a, c, row['id'])
                        entry['decision'] = 'eligible'
                output.append(entry)
            except (GuardError, OSError, ValueError, KeyError, TypeError) as e:
                # No raw transport/JSON errors or transcript content in logs.
                output.append(dict(thread_id=row['id'], decision='skipped', error=type(e).__name__))
            if execute and output and output[-1].get('status'):
                break  # At most one request per scan.
    finally:
        ledger.close()
    return output



def measurement(home, ledger):
    """Observed usage only. Missing compaction usage stays null, never zero."""
    result = []
    for aid, tid, started, status, context in ledger.db.execute(
            'SELECT id,thread_id,started,status,context_tokens FROM attempts ORDER BY id'):
        out = dict(attempt=aid, thread_id=tid, status=status, context_before=context,
                   compaction_usage=None, first_resume_usage=None)
        rows = list(candidates(home, time.time(), tid))
        if not rows:
            result.append(out)
            continue
        records = read_tail(rows[0]['path'])
        compacted = next((timestamp(r.get('timestamp')) for r in records
                          if r.get('type') == 'compacted' and timestamp(r.get('timestamp')) >= started), None)
        if compacted is None:
            result.append(out)
            continue
        sums = dict(input_tokens=0, cached_input_tokens=0, cache_write_input_tokens=0, output_tokens=0)
        seen = set()
        count = 0
        user_resumed = False
        for r in records:
            when, p = timestamp(r.get('timestamp')), r.get('payload', {})
            if not isinstance(p, dict):
                continue
            if r.get('type') == 'response_item' and p.get('role') == 'user' and when > compacted:
                user_resumed = True
            if r.get('type') != 'token_usage_record':
                continue
            key = p.get('response_id') or (when, p.get('turn_id'))
            if key in seen:
                continue
            seen.add(key)
            u = p.get('usage', {})
            safe = {k: u.get(k, 0) for k in sums}
            safe['noncached_input_tokens'] = max(0, safe['input_tokens'] - safe['cached_input_tokens'])
            if started <= when <= compacted:
                for k in sums:
                    sums[k] += safe[k]
                count += 1
            elif when > compacted and user_resumed and out['first_resume_usage'] is None:
                out['first_resume_usage'] = safe
        if count:
            sums['noncached_input_tokens'] = max(0, sums['input_tokens'] - sums['cached_input_tokens'])
            out['compaction_usage'] = sums
        result.append(out)
    return result


def launch_agent(args, remove=False):
    if sys.platform != 'darwin':
        raise GuardError('LaunchAgent installation is macOS-only')
    path = Path.home() / 'Library/LaunchAgents' / (LABEL + '.plist')
    domain = 'gui/' + str(os.getuid())
    if remove:
        subprocess.run(['launchctl', 'bootout', domain + '/' + LABEL], capture_output=True)
        check = subprocess.run(['launchctl', 'print', domain + '/' + LABEL], capture_output=True)
        if check.returncode == 0:
            raise GuardError('Watcher is still loaded; uninstall did not complete')
        path.unlink(missing_ok=True)
        return {'installed': False, 'data_preserved': True}
    config_read(args.state)
    verify_app(args.app)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise GuardError('Already installed; uninstall before changing the LaunchAgent')
    payload = dict(Label=LABEL, ProgramArguments=[sys.executable, str(Path(__file__).resolve()),
                   '--state', str(args.state), '--codex-home', str(args.codex_home),
                   '--app', str(args.app), 'watch'], RunAtLoad=True, KeepAlive=True,
                   ThrottleInterval=30, ProcessType='Background')
    with path.open('xb') as f:
        plistlib.dump(payload, f)
    path.chmod(0o600)
    try:
        subprocess.run(['launchctl', 'bootstrap', domain, str(path)], check=True, capture_output=True)
    except subprocess.CalledProcessError:
        path.unlink(missing_ok=True)
        raise GuardError('LaunchAgent bootstrap failed')
    return {'installed': True, 'mode': 'automatic' if config_read(args.state)['enabled'] else 'observation',
            'launch_agent': str(path)}


def config_contract():
    return DEFAULTS['enabled'] is False and DEFAULTS['max_per_day'] == 50 and DEFAULTS['max_per_month'] is None

def monthly_cap(value):
    if value.lower() in ('none', 'null'):
        return None
    try:
        cap = int(value)
        if cap > 0:
            return cap
    except ValueError:
        pass
    raise argparse.ArgumentTypeError('Use a positive integer or none')

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, default=Path.home()/'Library/Application Support/Codex Idle Compactor')
    parser.add_argument('--codex-home', type=Path, default=Path(os.environ.get('CODEX_HOME', str(Path.home()/'.codex'))))
    parser.add_argument('--app', type=Path, default=Path('/Applications/ChatGPT.app'))
    subs = parser.add_subparsers(dest='command', required=True)
    for name in ('self-check', 'doctor', 'plan', 'once', 'watch', 'pause', 'status', 'install', 'uninstall', 'reconcile', 'metrics'):
        subs.add_parser(name)
    p = subs.add_parser('compact', help='One explicit, manually invoked compaction of an eligible task')
    p.add_argument('thread_id')
    p = subs.add_parser('enable', help='Approve recurring metered compactions with explicit attempt caps')
    p.add_argument('--accept-metered-compaction', action='store_true', required=True)
    p.add_argument('--max-per-day', type=int, required=True)
    p.add_argument('--max-per-month', type=monthly_cap, required=True)
    args = parser.parse_args()
    for name in ('state', 'codex_home', 'app'):
        setattr(args, name, getattr(args, name).expanduser().resolve())
    if args.command == 'self-check':
        assert config_contract()
        print(json.dumps({'version': VERSION, 'healthy': True}))
        return
    private_dir(args.state)
    if args.command in ('enable', 'pause'):
        # Settings remain mutable while the watcher holds its execution lock.
        c = config_read(args.state)
        if args.command == 'enable':
            if not 1 <= args.max_per_day <= 50:
                raise GuardError('Daily cap must be 1..50')
            c.update(enabled=True, metered_automation_approved=True,
                     max_per_day=args.max_per_day, max_per_month=args.max_per_month)
        else:
            c.update(enabled=False, metered_automation_approved=False)
        atomic_json(args.state/'config.json', c)
        print(json.dumps({'enabled': c['enabled'], 'max_per_day': c['max_per_day'],
                          'max_per_month': c['max_per_month']}))
        return
    if args.command == 'install':
        subprocess.run([sys.executable,str(Path(__file__).parent/'install.py'),'--state',str(args.state)],check=True)
        return
    if args.command == 'uninstall':
        print(json.dumps(launch_agent(args, remove=True)))
        return
    if args.command == 'doctor':
        version = verify_app(args.app)
        with Desktop(args.codex_home, args.app):
            pass
        print(json.dumps(dict(app_version=version, ipc='connected', supported_scope='local Codex-backed tasks',
                              automatic_enabled=config_read(args.state)['enabled'])))
        return
    if args.command == 'metrics':
        ledger = Ledger(args.state)
        try:
            print(json.dumps(measurement(args.codex_home, ledger), indent=2))
        finally:
            ledger.close()
        return
    if args.command == 'status':
        c = config_read(args.state)
        ledger = Ledger(args.state)
        try:
            print(json.dumps(dict(config=c, attempts=ledger.summary()), indent=2))
        finally:
            ledger.close()
        return
    with exclusive(args.state):
        if args.command == 'reconcile':
            ledger = Ledger(args.state)
            try:
                print(json.dumps(ledger.reconcile(args.codex_home)))
            finally:
                ledger.close()
            return
        if args.command == 'watch':
            last = None
            while True:
                interval = 30
                try:
                    c = config_read(args.state)
                    interval = c['poll_seconds']
                    out = scan(args.state, args.codex_home, args.app,
                               execute=c['enabled'] and c['metered_automation_approved'])
                    atomic_json(args.state/'latest-plan.json', dict(at=time.time(), decisions=out))
                    atomic_json(args.state/'worker-health.json',dict(at=time.time(),version=VERSION,status='running'))
                    # Only aggregate changes on stdout; no transcript content or titles.
                    summary = json.dumps(dict(eligible=sum(x.get('decision')=='eligible' for x in out),
                                              automatic=c['enabled'], outcomes=[x.get('status') for x in out if x.get('status')]))
                    if summary != last:
                        print(summary, flush=True)
                        last = summary
                except Exception as e:
                    error = str(e) if isinstance(e, GuardError) else type(e).__name__
                    atomic_json(args.state/'worker-health.json',dict(at=time.time(),version=VERSION,status='compatibility-blocked',error=error))
                    print(json.dumps({'watcher_error': error, 'calls_blocked': True}), flush=True)
                if (args.state/'restart-request').exists():
                    return
                time.sleep(interval)
        else:
            out = scan(args.state, args.codex_home, args.app,
                       execute=args.command in ('once', 'compact'), thread_id=getattr(args, 'thread_id', None))
            print(json.dumps(out, indent=2))

if __name__ == '__main__':
    try:
        main()
    except (GuardError, OSError, ValueError, KeyError) as e:
        print('Idle Compactor: ' + (str(e) if isinstance(e, GuardError) else type(e).__name__), file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        pass
