"""Full OpenClaw agent transport; no private config or credential reads."""
from pathlib import Path
import re

REQUIRED_CONTEXT = {'AGENTS.md', 'SOUL.md', 'TOOLS.md', 'IDENTITY.md', 'USER.md'}


def session_key(agent, key):
    if not re.fullmatch(r'[a-zA-Z0-9_-]+', agent):
        raise ValueError('Invalid agent id')
    prefix = f'agent:{agent}:'
    if key.startswith('agent:'):
        if not key.startswith(prefix):
            raise ValueError('Session key must belong to the selected agent')
        key = key[len(prefix):]
    if not re.fullmatch(r'[a-zA-Z0-9_-]+', key) or key.lower() in ('main', 'global', 'unknown'):
        raise ValueError('Use a dedicated ordinary session name, such as kibitz')
    return prefix + key


def parameters(args, message, policy):
    params = dict(agentId=args.agent, sessionKey=session_key(args.agent, args.session_key),
                  message=message, policy=policy, timeout=int(args.timeout))
    if args.model and args.model != 'inherit':
        params['model'] = args.model
    return params


def command(args, params, message_file):
    # The official agent CLI owns Gateway authentication and model-override routing.
    # Default agent turns use full bootstrap/tools; never use infer/modelRun here.
    argv = [args.openclaw, 'agent', '--agent', params['agentId'],
            '--session-key', params['sessionKey'], '--message-file', str(message_file),
            '--json', '--thinking', 'off', '--timeout', str(params['timeout'])]
    if params.get('model'):
        argv.extend(['--model', params['model']])
    return argv


def parse_envelope(data, expected_key, workspace):
    if data.get('status') != 'ok' or not isinstance(data.get('result'), dict):
        raise ValueError('Full OpenClaw agent turn did not complete successfully')
    result = data['result']
    meta = result.get('meta') or {}
    if meta.get('aborted') or meta.get('fallbackFrom'):
        raise ValueError('Agent turn was aborted or fell back; continuity is not confirmed')
    report = meta.get('systemPromptReport') or {}
    if report.get('sessionKey') != expected_key:
        raise ValueError('Unexpected agent session; refusing unverified context')
    actual_workspace = report.get('workspaceDir')
    if not actual_workspace or Path(actual_workspace).resolve() != Path(workspace).resolve():
        raise ValueError('Agent workspace differs from the configured shared memory workspace')
    files = {item.get('name'): item for item in report.get('injectedWorkspaceFiles', [])}
    if any(name not in files or files[name].get('missing') or files[name].get('truncated')
           for name in REQUIRED_CONTEXT):
        raise ValueError('Required startup context is missing or truncated')
    tools = {item.get('name') for item in report.get('tools', {}).get('entries', [])}
    if 'read' not in tools or not tools.intersection({'edit', 'write', 'apply_patch'}):
        raise ValueError('Shared-memory read/write tools are unavailable')
    info = meta.get('agentMeta') or {}
    if not info.get('sessionId'):
        raise ValueError('Agent did not confirm a persistent session id')
    text = '\n'.join(item.get('text', '') for item in result.get('payloads', [])
                     if isinstance(item, dict) and isinstance(item.get('text', ''), str))
    if not text.strip():
        raise ValueError('Agent returned no final observation')
    diagnostics = dict(backend='openclaw-agent', session_key=expected_key,
                       session_id=info['sessionId'], workspace=str(Path(actual_workspace).resolve()),
                       injected_files=sorted(name for name, item in files.items()
                                             if not item.get('missing')),
                       memory_tools=sorted(tools.intersection({'read', 'edit', 'write', 'apply_patch'})),
                       model=info.get('model'), provider=info.get('provider'),
                       usage=info.get('usage'), startup_verified=True)
    return text, diagnostics
