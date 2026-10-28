import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy

SCRIPT = Path(__file__).parent / 'skills/kibitz/scripts/kibitz.py'
sys.path.insert(0, str(SCRIPT.parent))
import agent_backend
spec = importlib.util.spec_from_file_location('kibitz', SCRIPT)
kibitz = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kibitz)


TEST_WORKSPACE = Path('/test/openclaw/workspace')


def agent_envelope(text, workspace=TEST_WORKSPACE, key='agent:main:kibitz'):
    return {'status': 'ok', 'result': {'payloads': [{'text': text}], 'meta': {
        'agentMeta': {'sessionId': 'test-persistent-session', 'model': 'vision',
                      'provider': 'test', 'usage': {'input': 100, 'output': 10}},
        'systemPromptReport': {
            'sessionKey': key, 'workspaceDir': str(workspace),
            'injectedWorkspaceFiles': [{'name': name, 'missing': False, 'truncated': False}
                                       for name in agent_backend.REQUIRED_CONTEXT],
            'tools': {'entries': [{'name': name} for name in ('read', 'write', 'edit')]},
        }}}}


def envelope(summary='正在观察', comment='', changed=True, interest='normal'):
    return json.dumps(agent_envelope(json.dumps(
        {'summary': summary, 'comment': comment, 'screen_changed': changed,
         'interest': interest}, ensure_ascii=False)), ensure_ascii=False)


def request_payload(argv):
    text = Path(argv[argv.index('--message-file') + 1]).read_text()
    return text, json.loads(text.splitlines()[-1])


def options(**changes):
    values = dict(model='test/vision', interval=5, frames=2,
                  display=1, openclaw='openclaw', timeout=10, no_notify=False,
                  agent='main', session_key='kibitz', workspace=TEST_WORKSPACE)
    values.update(changes)
    return argparse.Namespace(**values)


class CompanionTests(unittest.TestCase):
    def test_provider_and_model_failures_are_not_comments(self):
        for output in ('{"ok": false}', 'oops', '[]', '{"summary":"x","comment":"x"}'):
            with self.assertRaises((ValueError, TypeError)):
                kibitz.parse_reply(output)
        text, _ = agent_backend.parse_envelope(json.loads(envelope()),
                                               'agent:main:kibitz', TEST_WORKSPACE)
        self.assertEqual(kibitz.parse_reply(text)['comment'], '')

    def test_recent_frames_context_silence_and_duplicate_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            worker = kibitz.Worker(options(), home)
            moment = [0.0]
            worker.pacer = kibitz.SpeechPacer(clock=lambda: moment[0])
            replies = iter([envelope('建立基线', '第一帧不能直接评论。'),
                            envelope('发现新区域', '这地方有点意思。'),
                            envelope('还在同一区域', '这地方有点意思。'),
                            envelope('正在浏览', ''), envelope('切换活动', '换个口味了。')])
            prompts, frames, notices = [], [], []

            def command(argv, timeout=60):
                if argv[0].endswith('screencapture'):
                    Path(argv[-1]).write_bytes(b'fake-image')
                    return ''
                if argv[0].endswith('osascript'):
                    notices.append(argv[-2])
                    return ''
                text, payload = request_payload(argv)
                prompts.append(text)
                frames.append(len(payload['images_oldest_to_newest']))
                return next(replies)

            worker.command = command
            for index, seconds in enumerate((0, 5, 10, 15, 70)):
                moment[0] = seconds
                if index == 3:
                    (home / 'context.md').write_text('用户刚说：先让我安静看一会。')
                reply = worker.cycle(home)
                if index == 0:
                    self.assertEqual(reply['comment'], '')
                    self.assertEqual(reply['suppression'], 'first_observation')
            self.assertEqual(frames, [1, 2, 2, 2, 2])
            self.assertEqual(len(list(home.glob('*.jpg'))), 2)
            self.assertEqual(notices, ['这地方有点意思。', '换个口味了。'])
            self.assertIn('发现新区域', prompts[2])
            self.assertIn('先让我安静看一会', prompts[3])
            self.assertEqual(worker.state['cycles'], 5)
            self.assertEqual(worker.state['comments'], 2)

    def test_one_per_minute_with_bounded_interesting_burst(self):
        moment = [0.0]
        pacer = kibitz.SpeechPacer(clock=lambda: moment[0])
        self.assertIsNone(pacer.suppression('normal'))
        pacer.record()
        moment[0] = 5
        self.assertEqual(pacer.suppression('normal'), 'ordinary_spacing')
        self.assertIsNone(pacer.suppression('high'))
        pacer.record()
        moment[0] = 10
        self.assertEqual(pacer.suppression('high'), 'speech_budget')
        moment[0] = 60
        self.assertIsNone(pacer.suppression('high'))
        pacer.record()
        moment[0] = 65
        self.assertEqual(pacer.suppression('normal'), 'speech_budget')
        moment[0] = 600
        self.assertEqual(pacer.snapshot()['available_credits'], 2)
        self.assertEqual(pacer.snapshot()['comments_last_60s'], 0)

        # Even a model claiming every candidate is exciting cannot keep spamming.
        moment[0] = 0
        pacer = kibitz.SpeechPacer(clock=lambda: moment[0])
        emitted = []
        for seconds in range(0, 601, 5):
            moment[0] = seconds
            if pacer.suppression('high') is None:
                pacer.record()
                emitted.append(seconds)
        self.assertLessEqual(len(emitted), 12)  # 10 minutes + at most 2 initial credits
        for stamp in emitted:
            self.assertLessEqual(sum(0 <= stamp - previous < 60 for previous in emitted), 2)

    def test_uninteresting_unchanged_and_overbudget_candidates_are_not_notified(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            worker = kibitz.Worker(options(), home)
            moment = [0.0]
            worker.pacer = kibitz.SpeechPacer(clock=lambda: moment[0])
            replies = iter([envelope(comment='基线'),
                            envelope(comment='有趣的新事件一', interest='high'),
                            envelope(comment='有趣的新事件二', interest='high'),
                            envelope(comment='不能刷屏', interest='high'),
                            envelope(comment='画面没变也不能说', changed=False, interest='high'),
                            envelope(comment='低兴趣不说', interest='low')])
            notices, prompts, reasons = [], [], []

            def command(argv, timeout=60):
                if argv[0].endswith('screencapture'):
                    Path(argv[-1]).write_bytes(b'fake-image')
                    return ''
                if argv[0].endswith('osascript'):
                    notices.append(argv[-2])
                    return ''
                prompts.append(request_payload(argv)[0])
                return next(replies)

            worker.command = command
            for seconds in (0, 5, 10, 15, 20, 25):
                moment[0] = seconds
                reasons.append(worker.cycle(home)['suppression'])
            self.assertEqual(notices, ['有趣的新事件一', '有趣的新事件二'])
            self.assertEqual(reasons, ['first_observation', None, None, 'speech_budget',
                                      'unchanged_screen', 'low_interest'])
            self.assertIn('"comments_last_60s": 2', prompts[3])
            self.assertEqual(worker.state['comments'], 2)

    def test_single_frame_mode_only_suppresses_first_observation(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            worker = kibitz.Worker(options(frames=1, no_notify=True), home)

            def command(argv, timeout=60):
                if argv[0].endswith('screencapture'):
                    Path(argv[-1]).write_bytes(b'fake-image')
                    return ''
                return envelope(comment='一个新事件')

            worker.command = command
            self.assertEqual(worker.cycle(home)['suppression'], 'first_observation')
            self.assertIsNone(worker.cycle(home)['suppression'])

    def test_timeout_reaps_the_process(self):
        with tempfile.TemporaryDirectory() as directory:
            worker = kibitz.Worker(options(), Path(directory))
            with self.assertRaisesRegex(RuntimeError, 'timed out'):
                worker.command([sys.executable, '-c', 'import time; time.sleep(30)'], timeout=0.05)
            self.assertIsNone(worker.child)

    def test_notification_text_is_data_not_script(self):
        text = '\"; do shell script "touch /tmp/not-allowed"'
        argv = kibitz.notification_command(text)
        self.assertEqual(argv[-2], text)
        self.assertNotIn(text, argv[1:-2])

    def test_context_input_and_stopped_state(self):
        with tempfile.TemporaryDirectory() as directory:
            command = [sys.executable, str(SCRIPT), '--home', directory]
            subprocess.run(command + ['context', '--file', '-'], input='新前情：正在看视频。',
                           text=True, check=True, capture_output=True)
            self.assertEqual((Path(directory) / 'context.md').read_text(), '新前情：正在看视频。')
            result = subprocess.run(command + ['status'], check=True, capture_output=True, text=True)
            self.assertFalse(json.loads(result.stdout)['running'])

    def test_agent_transport_reuses_ordinary_session_with_tools(self):
        args = options()
        params = agent_backend.parameters(args, 'observation', 'companion policy')
        argv = agent_backend.command(args, params, Path('/private/request.md'))
        self.assertEqual(argv[1], 'agent')
        self.assertEqual(argv[argv.index('--session-key') + 1], 'agent:main:kibitz')
        self.assertNotIn('--local', argv)
        self.assertNotIn('--deliver', argv)
        args.model = 'inherit'
        argv = agent_backend.command(args, agent_backend.parameters(args, 'next', 'policy'),
                                     Path('/private/request.md'))
        self.assertNotIn('--model', argv)
        for key in ('main', 'global', 'unknown', 'subagent:x', 'cron:x', 'agent:other:kibitz'):
            with self.assertRaises(ValueError):
                agent_backend.session_key('main', key)

    def test_missing_context_wrong_workspace_or_fallback_fails_closed(self):
        valid = agent_envelope('{"summary":"ok"}')
        variants = []
        value = deepcopy(valid)
        value['result']['meta']['systemPromptReport']['sessionKey'] = 'agent:main:main'
        variants.append(value)
        value = deepcopy(valid)
        value['result']['meta']['systemPromptReport']['workspaceDir'] = '/wrong/workspace'
        variants.append(value)
        value = deepcopy(valid)
        value['result']['meta']['systemPromptReport']['injectedWorkspaceFiles'].pop()
        variants.append(value)
        value = deepcopy(valid)
        value['result']['meta']['systemPromptReport']['injectedWorkspaceFiles'][0]['truncated'] = True
        variants.append(value)
        value = deepcopy(valid)
        value['result']['meta']['systemPromptReport']['tools']['entries'] = [{'name': 'read'}]
        variants.append(value)
        value = deepcopy(valid)
        value['result']['meta']['fallbackFrom'] = 'gateway'
        variants.append(value)
        value = deepcopy(valid)
        value['status'] = 'in_flight'
        variants.append(value)
        variants.append({'ok': True, 'outputs': [{'text': 'raw inference'}]})
        for data in variants:
            with self.assertRaises(ValueError):
                agent_backend.parse_envelope(data, 'agent:main:kibitz', TEST_WORKSPACE)

    def test_cancellation_aborts_remote_and_removes_request_file(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            worker = kibitz.Worker(options(), home)
            cancelled = []
            worker.abort_agent = lambda request_id: cancelled.append(request_id)
            def command(argv, timeout=60):
                self.assertTrue(Path(argv[argv.index('--message-file') + 1]).exists())
                raise InterruptedError('cancelled')
            worker.command = command
            with self.assertRaises(InterruptedError):
                worker.agent_turn('internal event')
            self.assertEqual(len(cancelled), 1)
            self.assertEqual(list(home.glob('request-*.md')), [])

    def test_persistent_session_metadata_survives_worker_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            for _ in range(2):
                worker = kibitz.Worker(options(), home)
                worker.command = lambda argv, timeout=60: envelope()
                worker.agent_turn('internal observation')
                self.assertFalse(worker.state['agent_session']['session_changed'])
                self.assertEqual(worker.state['usage'], {'input': 100, 'output': 10})
            self.assertEqual(json.loads((home / 'session.json').read_text())['session_id'],
                             'test-persistent-session')

    def test_explicit_notes_are_acknowledged_only_after_agent_success(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            cli = [sys.executable, str(SCRIPT), '--home', str(home)]
            queued = subprocess.run(cli + ['note', '--text', '请记住本次明确交接。'],
                                    capture_output=True, text=True, check=True)
            note_id = json.loads(queued.stdout)['note_id']
            worker = kibitz.Worker(options(), home)
            worker.abort_agent = lambda _: None
            def failing(argv, timeout=60):
                if argv[0].endswith('screencapture'):
                    Path(argv[-1]).write_bytes(b'fake-image')
                    return ''
                raise RuntimeError('agent unavailable')
            worker.command = failing
            with self.assertRaises(RuntimeError):
                worker.cycle(home)
            self.assertEqual(len(worker.pending_notes()), 1)
            def succeeds(argv, timeout=60):
                if argv[0].endswith('screencapture'):
                    Path(argv[-1]).write_bytes(b'fake-image')
                    return ''
                _, payload = request_payload(argv)
                self.assertEqual(payload['user_notes'][0]['id'], note_id)
                return envelope(summary='已处理用户明确交接。')
            worker.command = succeeds
            worker.cycle(home)
            self.assertEqual(worker.pending_notes(), [])
            self.assertEqual(worker.state['last_notes_processed'], [note_id])


if __name__ == '__main__':
    unittest.main()
