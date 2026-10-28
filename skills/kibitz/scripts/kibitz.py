#!/usr/bin/env python3
"""Minimal macOS companion: screen -> OpenClaw vision -> notification."""
import argparse
from collections import deque
from contextlib import contextmanager
from datetime import datetime
import fcntl
import json
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from zoneinfo import ZoneInfo
from agent_backend import (command as agent_command, parameters as agent_parameters,
                           parse_envelope as parse_agent_envelope, session_key)

SCRIPT = Path(__file__).resolve()
PROMPT = SCRIPT.parent.parent / 'references' / 'companion.md'
SESSION_PROMPT = SCRIPT.parent.parent / 'references' / 'session.md'
DEFAULT_CONTEXT = SCRIPT.parent.parent / 'references' / 'context.md'
DEFAULT_MODEL = 'deepseek/deepseek-v4-flash'
DEFAULT_WORKSPACE = Path('~/.openclaw/workspace').expanduser()


def now():
    return datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds')


def write_json(path, value):
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temp.chmod(0o600)
    temp.replace(path)


def read_json(path):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}


@contextmanager
def control(home):
    with (home / 'control.lock').open('a+') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def running(home):
    with (home / 'run.lock').open('a+') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.seek(0)
            try:
                return json.loads(handle.read())
            except json.JSONDecodeError:
                return {'starting': True}
        return None


def parse_json(text):
    text = text.strip()
    if text.startswith('```'):
        lines = text.splitlines()
        text = '\n'.join(lines[1:-1]).strip() if lines[-1] == '```' else text
    result = json.loads(text)
    if not isinstance(result, dict):
        raise ValueError('Expected a JSON object')
    return result


def parse_reply(text):
    reply = parse_json(text)
    if not isinstance(reply.get('summary'), str) or not isinstance(reply.get('comment'), str):
        raise ValueError('Model must return string summary and comment')
    if type(reply.get('screen_changed')) is not bool:
        raise ValueError('Model must return boolean screen_changed')
    if reply.get('interest') not in ('low', 'normal', 'high'):
        raise ValueError('Model interest must be low, normal or high')
    return {'summary': reply['summary'][:1000], 'comment': reply['comment'].strip()[:160],
            'screen_changed': reply['screen_changed'], 'interest': reply['interest']}


class SpeechPacer:
    """One credit/minute, at most two saved; only high-interest events can burst."""
    def __init__(self, clock=None):
        self.clock = clock or time.monotonic
        self.started = self.updated = self.clock()
        self.credits = 2.0
        self.emitted = deque()

    def snapshot(self):
        current = self.clock()
        self.credits = min(2.0, self.credits + max(0, current - self.updated) / 60)
        self.updated = current
        while self.emitted and current - self.emitted[0] >= 300:
            self.emitted.popleft()
        return dict(target_per_minute=1, available_credits=round(self.credits, 3),
                    comments_last_60s=sum(current - stamp < 60 for stamp in self.emitted),
                    seconds_since_last_comment=(round(current - self.emitted[-1], 1)
                                                if self.emitted else None),
                    max_saved_credits=2)

    def suppression(self, interest):
        stats = self.snapshot()
        if self.credits < 1:
            return 'speech_budget'
        if interest != 'high' and stats['seconds_since_last_comment'] is not None:
            if self.clock() - self.emitted[-1] < 60:
                return 'ordinary_spacing'
        return None

    def record(self):
        self.snapshot()
        self.credits -= 1
        self.emitted.append(self.clock())


def notification_command(text):
    return ['/usr/bin/osascript', '-e', 'on run argv', '-e',
            'display notification (item 1 of argv) with title (item 2 of argv)',
            '-e', 'end run', text, 'Kibitz · 小爪']


class Worker:
    def __init__(self, args, home):
        self.args, self.home = args, home
        self.stopping = threading.Event()
        self.child = None
        self.history = deque(maxlen=12)
        self.pacer = SpeechPacer()
        self.frames = deque()
        self.active_request = None
        self.state = dict(running=True, pid=os.getpid(), started_at=now(),
                          model=args.model, interval=args.interval, display=args.display,
                          frames=args.frames, timeout=args.timeout, no_notify=args.no_notify,
                          backend='openclaw-agent', agent=args.agent,
                          session_key=session_key(args.agent, args.session_key),
                          workspace=str(args.workspace),
                          cycles=0, comments=0, failures=0, summary='', last_comment='',
                          last_error=None, notification_error=None)

    def terminate_child(self, graceful=False):
        if self.child and self.child.poll() is None:
            try:
                os.killpg(self.child.pid, signal.SIGTERM if graceful else signal.SIGKILL)
            except ProcessLookupError:
                pass

    def stop(self, *_):
        self.stopping.set()
        self.terminate_child(graceful=True)

    def abort_agent(self, request_id):
        argv = [self.args.openclaw, 'gateway', 'call', 'chat.abort', '--json',
                '--timeout', '3000', '--params', json.dumps({
                    'sessionKey': self.state['session_key'], 'agentId': self.args.agent})]
        try:
            result = subprocess.run(argv, capture_output=True, text=True, timeout=5)
            self.state['abort_request_submitted'] = result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            self.state['abort_request_submitted'] = False

    def agent_turn(self, message):
        request_id = str(uuid.uuid4())
        policy = SESSION_PROMPT.read_text() + '\n\n' + PROMPT.read_text()
        params = agent_parameters(self.args, message, policy)
        message_file = self.home / ('request-' + request_id + '.md')
        message_file.write_text(policy + '\n\n' + message)
        message_file.chmod(0o600)
        self.active_request = request_id
        self.state['active_agent_request'] = request_id
        self.save()
        try:
            output = self.command(agent_command(self.args, params, message_file),
                                  timeout=self.args.timeout + 10)
        except BaseException:
            self.abort_agent(request_id)
            raise
        finally:
            self.active_request = None
            self.state['active_agent_request'] = None
            message_file.unlink(missing_ok=True)
            self.save()
        text, diagnostics = parse_agent_envelope(parse_json(output), self.state['session_key'],
                                                 self.args.workspace)
        reply = parse_reply(text)
        previous = read_json(self.home / 'session.json')
        diagnostics['session_changed'] = bool(previous.get('session_key') == diagnostics['session_key']
                                             and previous.get('session_id') != diagnostics['session_id'])
        self.state['agent_session'] = diagnostics
        self.state['usage'] = diagnostics.get('usage')
        write_json(self.home / 'session.json', diagnostics)
        return reply

    def pending_notes(self):
        inbox = self.home / 'inbox'
        return [(path, read_json(path)) for path in sorted(inbox.glob('*.json'))[:10]]

    def acknowledge_notes(self, notes):
        for path, _ in notes:
            path.unlink(missing_ok=True)
        self.state['last_notes_processed'] = [note['id'] for _, note in notes]

    def command(self, argv, timeout=60):
        if self.stopping.is_set():
            raise InterruptedError('Companion stopped')
        self.child = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      text=True, start_new_session=True)
        if self.stopping.is_set():
            self.terminate_child()
        try:
            stdout, stderr = self.child.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.terminate_child(graceful=True)
            try:
                self.child.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                self.terminate_child()
                self.child.communicate()
            raise RuntimeError('Command timed out: ' + Path(argv[0]).name) from None
        finally:
            child, self.child = self.child, None
        if self.stopping.is_set():
            raise InterruptedError('Companion stopped')
        if child.returncode:
            # Provider stderr can contain auth details. Keep it out of logs/status.
            raise RuntimeError(f'{Path(argv[0]).name} exited with code {child.returncode}; '
                               'check its permissions/model configuration')
        return stdout

    def save(self):
        self.state['updated_at'] = now()
        write_json(self.home / 'state.json', self.state)

    def event(self, event):
        log = self.home / 'events.jsonl'
        if log.exists() and log.stat().st_size > 1_000_000:
            log.replace(self.home / 'events.previous.jsonl')
        with log.open('a') as handle:
            handle.write(json.dumps(dict(time=now(), **event), ensure_ascii=False) + '\n')

    def notify(self, text):
        self.command(notification_command(text), timeout=10)

    def cycle(self, folder):
        started = time.monotonic()
        image = folder / (uuid.uuid4().hex + '.jpg')
        self.command(['/usr/sbin/screencapture', '-x', '-D', str(self.args.display),
                      '-t', 'jpg', str(image)], timeout=10)
        if not image.exists() or image.stat().st_size == 0:
            raise RuntimeError('No screenshot; check macOS Screen Recording permission')
        captured_at = now()
        self.frames.append((image, captured_at))
        expired = []
        while len(self.frames) > self.args.frames:
            path, _ = self.frames.popleft()
            path.unlink(missing_ok=True)
            expired.append(str(path))
        capture_ms = round((time.monotonic() - started) * 1000)
        context_file = self.home / 'context.md'
        context = context_file.read_text() if context_file.exists() else DEFAULT_CONTEXT.read_text()
        notes = self.pending_notes()
        message = 'Kibitz 内部观察事件。当前时间：' + now() + '\n' + json.dumps({
            'startup_required': self.state['cycles'] == 0,
            'startup_only': False,
            'session_context': context[:12000],
            'user_notes': [note for _, note in notes],
            'previous_summary': self.state['summary'],
            'recent_comments': list(self.history),
            'speaking_pace': self.pacer.snapshot(),
            'images_oldest_to_newest': [{'path': str(path), 'captured_at': stamp}
                                       for path, stamp in self.frames],
            'expired_image_paths': expired,
        }, ensure_ascii=False)
        model_started = time.monotonic()
        reply = self.agent_turn(message)
        self.acknowledge_notes(notes)
        model_ms = round((time.monotonic() - model_started) * 1000)
        self.state.update(summary=reply['summary'], last_observed_at=captured_at,
                          last_error=None, failures=0, notification_error=None)
        comment = reply['comment']
        # Exact delivery duplicate guard; semantic decision remains with the model.
        duplicate = bool(comment and self.history and comment == self.history[-1]['text'])
        suppression = None
        if comment:
            if self.state['cycles'] == 0:
                suppression = 'first_observation'
            elif not reply['screen_changed']:
                suppression = 'unchanged_screen'
            elif reply['interest'] == 'low':
                suppression = 'low_interest'
            elif duplicate:
                suppression = 'duplicate'
            else:
                suppression = self.pacer.suppression(reply['interest'])
        if comment and suppression is None:
            self.pacer.record()
            self.history.append({'time': now(), 'text': comment})
            self.state['last_comment'] = comment
            if not self.args.no_notify:
                try:
                    self.notify(comment)
                except InterruptedError:
                    raise
                except RuntimeError as error:
                    self.state['notification_error'] = str(error)
            self.state['comments'] += 1
        self.state['cycles'] += 1
        self.state['speaking_pace'] = self.pacer.snapshot()
        self.state['last_suppression'] = suppression
        elapsed_ms = round((time.monotonic() - started) * 1000)
        self.state.update(capture_ms=capture_ms, model_ms=model_ms, total_ms=elapsed_ms)
        self.event({'event': 'observation', 'comment': comment, 'duplicate': duplicate,
                    'screen_changed': reply['screen_changed'], 'interest': reply['interest'],
                    'agent_session_id': self.state['agent_session']['session_id'],
                    'usage': self.state.get('usage'),
                    'suppression': suppression, 'speaking_pace': self.state['speaking_pace'],
                    'capture_ms': capture_ms, 'model_ms': model_ms, 'total_ms': elapsed_ms,
                    'notification_error': self.state['notification_error']})
        self.save()
        return dict(reply, comment=comment if suppression is None else '',
                    suppression=suppression)

    def run(self, once=False, initialize=False):
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        with (self.home / 'run.lock').open('a+') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError('Companion is already running') from None
            lock.seek(0)
            lock.truncate()
            lock.write(json.dumps({'pid': os.getpid(), 'started_at': self.state['started_at']}))
            lock.flush()
            self.save()
            frame_root = self.args.workspace / 'state' / 'kibitz'
            frame_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            with tempfile.TemporaryDirectory(prefix='frames-', dir=frame_root) as folder:
                try:
                    if initialize:
                        notes = self.pending_notes()
                        reply = self.agent_turn(json.dumps({
                            'event': 'Kibitz 内部启动事件，不是真人消息', 'current_time': now(),
                            'startup_required': True, 'startup_only': True,
                            'user_notes': [note for _, note in notes],
                        }, ensure_ascii=False))
                        self.acknowledge_notes(notes)
                        self.state['summary'] = reply['summary']
                        self.save()
                        print(json.dumps(self.state['agent_session'], ensure_ascii=False))
                        return
                    while not self.stopping.is_set():
                        started = time.monotonic()
                        try:
                            reply = self.cycle(Path(folder))
                            if once:
                                print(json.dumps(reply, ensure_ascii=False))
                                return
                        except InterruptedError:
                            break
                        except Exception as error:
                            self.state['failures'] += 1
                            self.state['last_error'] = str(error)
                            self.event({'event': 'error', 'message': str(error)})
                            self.save()
                            if once or self.state['failures'] >= 3:
                                raise
                        delay = max(0, self.args.interval - (time.monotonic() - started))
                        self.stopping.wait(delay)
                except BaseException as error:
                    if not self.stopping.is_set():
                        self.state['last_error'] = str(error)
                    raise
                finally:
                    self.terminate_child()
                    self.state['running'] = False
                    self.state['stopped_at'] = now()
                    self.save()


def status(home):
    state = read_json(home / 'state.json')
    state['running'] = running(home) is not None
    state['pending_notes'] = len(list((home / 'inbox').glob('*.json')))
    return state


def start(args, home):
    with control(home):
        owner = running(home)
        if owner:
            return dict(status(home), already_running=True)
        argv = [sys.executable, str(SCRIPT), '--home', str(home), 'run',
                '--interval', str(args.interval), '--frames', str(args.frames),
                '--display', str(args.display), '--model', args.model,
                '--openclaw', args.openclaw, '--timeout', str(args.timeout),
                '--agent', args.agent, '--session-key', args.session_key,
                '--workspace', str(args.workspace)]
        if args.no_notify:
            argv.append('--no-notify')
        # OpenClaw owns persistent conversation history; local observation state resets.
        log = home / 'worker.log'
        if log.exists():
            log.replace(home / 'worker.previous.log')
        with log.open('w') as handle:
            child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=handle,
                                      stderr=handle, start_new_session=True)
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if child.poll() is not None:
                raise RuntimeError('Companion could not start; inspect ' + str(log))
            owner = running(home)
            if owner and owner.get('pid') == child.pid:
                state = read_json(home / 'state.json')
                if state.get('pid') == child.pid:
                    return dict(state, ready=True, note='Started; first observation is pending')
            time.sleep(0.05)
        # Do not leave a failed handshake running in the background.
        child.terminate()
        try:
            child.wait(timeout=3)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
        raise RuntimeError('Companion start handshake timed out')


def stop(home):
    with control(home):
        owner = running(home)
        if not owner:
            return dict(status(home), already_stopped=True)
        pid = owner.get('pid')
        if not isinstance(pid, int) or pid <= 1:
            raise RuntimeError('Worker is starting; retry stop shortly')
        command = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'command='],
                                 capture_output=True, text=True, check=True).stdout
        if str(SCRIPT) not in command or ' run ' not in command:
            raise RuntimeError('PID identity mismatch; no process was signalled')
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return dict(status(home), already_stopped=True)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if not running(home):
                return status(home)
            time.sleep(0.05)
        raise RuntimeError('Stop requested but worker has not exited; check status')


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path,
                        default=Path(os.environ.get('KIBITZ_HOME', '~/.kibitz')).expanduser())
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('start', 'run', 'once', 'init'):
        cmd = commands.add_parser(name)
        cmd.add_argument('--interval', type=float, default=5)
        cmd.add_argument('--frames', type=int, choices=range(1, 5), default=2)
        cmd.add_argument('--display', type=int, default=1)
        cmd.add_argument('--model', default=DEFAULT_MODEL)
        cmd.add_argument('--agent', default='main')
        cmd.add_argument('--session-key', default='kibitz')
        cmd.add_argument('--workspace', type=Path, default=DEFAULT_WORKSPACE)
        cmd.add_argument('--openclaw', default=shutil.which('openclaw') or 'openclaw')
        cmd.add_argument('--timeout', type=int, default=120)
        cmd.add_argument('--no-notify', action='store_true')
    commands.add_parser('status')
    commands.add_parser('stop')
    ctx = commands.add_parser('context')
    ctx.add_argument('--file', required=True, help='UTF-8 context file, or - for stdin')
    note = commands.add_parser('note', help='Queue an explicit user message for the companion session')
    source = note.add_mutually_exclusive_group(required=True)
    source.add_argument('--text')
    source.add_argument('--file', help='UTF-8 file, or - for stdin')
    notify = commands.add_parser('notify-test')
    notify.add_argument('--text', default='Kibitz 通知链路测试：小爪来插一句。')
    args = parser.parse_args()
    home = args.home.expanduser().resolve()
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.command in ('start', 'run', 'once', 'init'):
        args.workspace = args.workspace.expanduser().resolve()
        if not (args.workspace / 'AGENTS.md').is_file():
            parser.error('workspace must be the existing OpenClaw workspace with AGENTS.md')
        try:
            session_key(args.agent, args.session_key)
        except ValueError as error:
            parser.error(str(error))
        if not math.isfinite(args.interval) or args.interval < 0.2:
            parser.error('interval must be finite and at least 0.2 seconds')
        if not math.isfinite(args.timeout) or args.timeout <= 0 or args.display < 1:
            parser.error('timeout must be positive and display must be at least 1')
        if sys.platform != 'darwin':
            parser.error('Screen capture and notifications require macOS')
    if args.command == 'note':
        text = args.text if args.text is not None else (sys.stdin.read() if args.file == '-' else Path(args.file).read_text())
        if not text.strip() or len(text) > 12000:
            parser.error('Note must contain 1 to 12000 characters')
        inbox = home / 'inbox'
        inbox.mkdir(exist_ok=True, mode=0o700)
        note_id = datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d%H%M%S%f') + '-' + uuid.uuid4().hex
        write_json(inbox / (note_id + '.json'), dict(id=note_id, created_at=now(), text=text))
        result = dict(queued=True, note_id=note_id, running=running(home) is not None)
    elif args.command == 'context':
        text = sys.stdin.read() if args.file == '-' else Path(args.file).read_text()
        if len(text) > 12000:
            parser.error('Context must be at most 12000 characters; pass a concise handoff')
        target = home / 'context.md'
        temp = target.with_name('context.' + uuid.uuid4().hex + '.tmp')
        temp.write_text(text)
        temp.chmod(0o600)
        temp.replace(target)
        result = {'context_updated': True, 'characters': len(text)}
    elif args.command == 'start':
        result = start(args, home)
    elif args.command == 'stop':
        result = stop(home)
    elif args.command == 'status':
        result = status(home)
    elif args.command == 'notify-test':
        subprocess.run(notification_command(args.text), check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        result = {'submitted': True, 'note': 'macOS notification visibility depends on notification permissions and Focus mode'}
    else:
        Worker(args, home).run(once=args.command == 'once', initialize=args.command == 'init')
        return
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'ok': False, 'error': str(error)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
