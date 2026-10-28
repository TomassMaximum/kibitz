#!/usr/bin/env python3
"""Live integration check; temporary memory writes are performed by the agent only."""
import argparse
from contextlib import ExitStack
import importlib.util
import json
from pathlib import Path
import secrets
import struct
import sys
import tempfile
import time
import zlib

PROJECT = Path(__file__).resolve().parent
SCRIPT = PROJECT / 'skills/kibitz/scripts/kibitz.py'
sys.path.insert(0, str(SCRIPT.parent))
spec = importlib.util.spec_from_file_location('kibitz_live_check', SCRIPT)
kibitz = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kibitz)


def png(path, color):
    width, height = 320, 180
    rows = []
    for y in range(height):
        row = bytearray()
        for x in range(width):
            row.extend(color if 60 <= x < 260 and 30 <= y < 150 else (255, 255, 255))
        rows.append(b'\0' + row)
    def chunk(name, data):
        return struct.pack('!I', len(data)) + name + data + struct.pack('!I', zlib.crc32(name + data))
    path.write_bytes(b'\x89PNG\r\n\x1a\n' +
                     chunk(b'IHDR', struct.pack('!2I5B', width, height, 8, 2, 0, 0, 0)) +
                     chunk(b'IDAT', zlib.compress(b''.join(rows))) + chunk(b'IEND', b''))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, default=kibitz.DEFAULT_WORKSPACE)
    parser.add_argument('--model', default=kibitz.DEFAULT_MODEL)
    args = parser.parse_args()
    workspace = args.workspace.expanduser().resolve()
    checks = PROJECT / '.checks'
    checks.mkdir(exist_ok=True)
    unique = secrets.token_hex(8)
    marker = 'kibitz-check-' + secrets.token_hex(16)
    event_file = workspace / 'brain/events' / ('kibitz-integration-probe-' + unique + '.md')
    if event_file.exists():
        raise RuntimeError('Unexpected existing probe file; preserved')
    frame_root = workspace / 'state/kibitz'
    frame_root.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    report = {}
    with ExitStack() as stack:
        home = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='memory-check-', dir=checks)))
        frames = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='verify-', dir=frame_root)))
        nonce_file = home / 'nonce.txt'
        nonce_file.write_text(marker)
        worker_args = argparse.Namespace(agent='main', session_key='kibitz-check-' + unique,
            workspace=workspace, model=args.model, interval=5, frames=2, display=1,
            openclaw=kibitz.shutil.which('openclaw') or 'openclaw', timeout=120, no_notify=True)
        worker = kibitz.Worker(worker_args, home)
        common = ('这是用户授权的内部集成验证，不是真人新消息，不播报、不通知、不改变任务。'
                  '保持 JSON 输出约定，comment 必须为空；使用文件工具直接编辑，不用脚本搬运记忆。')
        try:
            worker.agent_turn(common + '\n' + json.dumps({
                'startup_required': True, 'startup_only': False,
                'request': f'按启动协议工作，实际读取 BOOTSTRAP.md、brain/README.md 和 '
                           f'brain/entities/projects/Kibitz.md。用 read 工具读取 {nonce_file} 中的验证标识。'
                           f'用 write 工具在 {event_file} 创建临时测试事件记录：说明这是本次 '
                           '[[Kibitz]] 通过 [[OpenClaw]] 的 [[Claw]] 会话验证记忆读写，'
                           '不是用户个人事实；包含 marker: 后跟读到的完整标识。summary 也写出这个标识，'
                           '用于下一轮验证会话连续性。该文件将在验证后删除，勿改其他记忆。',
            }, ensure_ascii=False))
            first_session = worker.state['agent_session']['session_id']
            if not event_file.exists() or marker not in event_file.read_text():
                raise AssertionError('Agent did not read nonce and write the temporary brain event')
            report['brain_read_write'] = True
            worker.agent_turn(common + '\n' + json.dumps({
                'startup_required': False, 'startup_only': False,
                'request': '不要重新读取 nonce 源文件。从本会话上一轮记住的验证标识，'
                           f'用 edit 工具在 {event_file} 末尾补一行 continuity: 后跟该完整标识。'
                           '保留其余内容，不改其他文件。screen_changed=false，interest=low。',
            }, ensure_ascii=False))
            if 'continuity: ' + marker not in event_file.read_text():
                raise AssertionError('Conversation continuation did not edit the same memory record')
            if worker.state['agent_session']['session_id'] != first_session:
                raise AssertionError('Agent session was not reused')
            report['persistent_session'] = True
            colors = {'red': (240, 20, 20), 'green': (20, 210, 30),
                      'blue': (20, 40, 240), 'yellow': (240, 220, 20)}
            chosen = secrets.SystemRandom().sample(list(colors), 2)
            image_paths = [frames / 'first.png', frames / 'second.png']
            for path, name in zip(image_paths, chosen):
                png(path, colors[name])
            reply = worker.agent_turn(common + '\n' + json.dumps({
                'startup_required': False, 'startup_only': False,
                'images_oldest_to_newest': [{'path': str(path)} for path in image_paths],
                'request': '这是图片输入验证，必须实际查看两张图；中心块主色从 red/green/blue/yellow 中选。'
                           'summary 只返回 image_colors=第一张英文颜色,第二张英文颜色，不能凭路径猜。'
                           f'同时删除你本次创建的临时记忆文件 {event_file}；只删除这个文件，'
                           '不要改其他记忆。screen_changed=true，interest=normal，comment 为空。',
            }, ensure_ascii=False))
            if 'image_colors=' + ','.join(chosen) not in reply['summary'].replace(' ', ''):
                raise AssertionError('Actual image colors were not recognized: ' + reply['summary'])
            report['image_input'] = True
            if event_file.exists():
                raise AssertionError('Temporary brain record was not cleaned by the agent')
            report['temporary_memory_cleaned'] = True
            report.update(worker.state['agent_session'])
            report['duration_seconds'] = round(time.monotonic() - start, 2)
            (checks / 'verification-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
            print(json.dumps(report, ensure_ascii=False, indent=2))
        finally:
            if event_file.exists():
                worker.agent_turn(common + '\n' + json.dumps({
                    'startup_required': False, 'startup_only': True,
                    'request': f'仅清理你本次创建的临时验证文件 {event_file}，不要改其他记忆。',
                }, ensure_ascii=False))
                if event_file.exists():
                    raise RuntimeError('Temporary probe remains; preserve and inspect ' + str(event_file))


if __name__ == '__main__':
    main()
