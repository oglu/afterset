"""Opt-in Codex synthetic conversation evaluation; no real Garmin connection."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import secrets
import subprocess
import sys
import tempfile
import tomllib

from .fixtures import initialize_fixture
from .observer import snapshot
from .rpc import AppServer
from .scenarios import CASES, ready_for_approval, score

ROOT = Path(__file__).resolve().parents[2]
FEATURES_OFF = ('shell_tool', 'unified_exec', 'apps', 'plugins', 'remote_plugin', 'multi_agent',
                'multi_agent_v2', 'browser_use', 'browser_use_external', 'computer_use', 'view_image',
                'image_generation', 'goals', 'workspace_dependencies', 'hooks', 'memories', 'skill_search', 'shell_snapshot')


def disabled_user_servers() -> list[str]:
    """Read names only; values, including possible secrets, never enter the report."""
    path = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))) / 'config.toml'
    if not path.exists():
        return []
    with path.open('rb') as f:
        config = tomllib.load(f)
    return list((config.get('mcp_servers') or {}).keys())


def command(codex: str, scratch: Path, fixture: Path, server_name: str) -> list[str]:
    args = [codex, '--no-daemon', 'app-server', '--stdio']
    overrides = [f'features.{name}=false' for name in FEATURES_OFF]
    overrides += ['features.code_mode_host=true', 'features.skip_host_skill_discovery=true', 'project_doc_max_bytes=0',
                  'developer_instructions=""', 'web_search="disabled"', 'notify=[]',
                  f'sqlite_home={json.dumps(str(scratch))}', f'log_dir={json.dumps(str(scratch))}']
    for name in disabled_user_servers():
        if not re.fullmatch(r'[A-Za-z0-9_-]+', name):
            raise RuntimeError('Unsupported configured MCP server name; refusing unsafe override')
        overrides.append(f'mcp_servers.{name}.enabled=false')
    overrides += [f'mcp_servers.{server_name}.command={json.dumps(sys.executable)}',
                  f'mcp_servers.{server_name}.args={json.dumps([str(ROOT / "scripts/conversation_eval/server.py")])}',
                  f'mcp_servers.{server_name}.env.AFTERSET_EVAL_ROOT={json.dumps(str(fixture))}',
                  f'mcp_servers.{server_name}.enabled=true',
                  f'mcp_servers.{server_name}.default_tools_approval_mode="approve"']
    for value in overrides:
        args.extend(('-c', value))
    return args


def _calls(root: Path, start: int, turn: int) -> tuple[list[dict], int]:
    path = root / 'calls.jsonl'
    all_calls = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    return [{**c, 'turn': turn} for c in all_calls[start:]], len(all_calls)


def _response(events: list[dict], thread: str, turn: str) -> str:
    items = []
    for event in events:
        params = event.get('params') or {}
        item = params.get('item') or {}
        if (event.get('method') == 'item/completed' and params.get('threadId') == thread
                and params.get('turnId') == turn and item.get('type') == 'agentMessage'
                and item.get('phase') in (None, 'final_answer')):
            items.append(item.get('text', ''))
    return '\n'.join(items)


def verify_trace(events: list[dict], thread: str, turn: str, calls: list[dict], server_name: str = 'afterset') -> None:
    """Refuse scores if the observer and app server disagree or another tool executes."""
    observed = []
    for event in events:
        params = event.get('params') or {}
        if event.get('method') != 'item/completed' or params.get('threadId') != thread or params.get('turnId') != turn:
            continue
        item = params.get('item') or {}
        kind = item.get('type')
        if kind == 'mcpToolCall':
            if item.get('server') != server_name:
                raise RuntimeError('Unexpected MCP server call; refusing score')
            observed.append(item)
        elif kind not in {'userMessage', 'agentMessage', 'reasoning', 'plan'}:
            raise RuntimeError('Unexpected execution or unknown item; refusing score')
    if [item.get('tool') for item in observed] != [call['tool'] for call in calls]:
        raise RuntimeError('App-server and synthetic MCP traces differ; refusing score')
    for item, call in zip(observed, calls):
        if call.get('outcome') == 'success' and item.get('status') != 'completed':
            raise RuntimeError('MCP success was not delivered to the model; refusing score')
        result = item.get('result') or {}
        if call.get('outcome') == 'success' and result.get('structuredContent') is not None and result['structuredContent'] != call.get('result'):
            raise RuntimeError('MCP result mismatch; refusing score')


def _inventory(client: AppServer, thread: str, timeout: int, server_name: str) -> dict:
    result = client.request('mcpServerStatus/list', {'threadId': thread, 'detail': 'toolsAndAuthOnly'}, timeout)
    if result.get('nextCursor'):
        raise RuntimeError('MCP inventory is paginated; refusing unverified tool exposure')
    data = result.get('data', [])
    synthetic = [entry for entry in data if entry.get('name') == server_name]
    unexpected = [entry for entry in data if entry.get('name') != server_name and
                  (entry.get('runtimeStatus') != 'disabled' or entry.get('tools'))]
    if len(synthetic) != 1 or unexpected:
        raise RuntimeError('Unexpected active MCP servers; refusing model turn')
    tools = synthetic[0].get('tools') or {}
    names = set(tools) if isinstance(tools, dict) else {t['name'] for t in tools}
    from afterset.server import create_server
    expected = set(create_server()._tool_manager._tools)
    if names != expected:
        raise RuntimeError('Synthetic MCP tool inventory differs; refusing model turn')
    return {'server': server_name, 'tools': sorted(names)}


def child_environment() -> dict[str, str]:
    # Preserve the user's auth location unchanged; never forward unrelated credentials.
    allowed = {'PATH', 'HOME', 'CODEX_HOME', 'TMPDIR', 'LANG', 'LC_ALL', 'SSL_CERT_FILE',
               'SSL_CERT_DIR', 'REQUESTS_CA_BUNDLE', 'OPENAI_API_KEY'}
    return {key: value for key, value in os.environ.items() if key in allowed}


def tool_events(events: list[dict]) -> list[dict]:
    result = []
    for event in events:
        params = event.get('params') or {}
        item = params.get('item') or {}
        if event.get('method') == 'item/completed' and item.get('type') == 'mcpToolCall':
            result.append({'threadId': params.get('threadId'), 'turnId': params.get('turnId'),
                           'server': item.get('server'), 'tool': item.get('tool'), 'status': item.get('status')})
    return result


def failure_code(exc: Exception) -> str:
    allowed = {'Isolation preflight failed; refusing model turn': 'isolation_preflight',
               'Unexpected active MCP servers; refusing model turn': 'unexpected_mcp_server',
               'Synthetic MCP tool inventory differs; refusing model turn': 'tool_inventory',
               'MCP inventory is paginated; refusing unverified tool exposure': 'inventory_pagination',
               'App-server and synthetic MCP traces differ; refusing score': 'trace_mismatch',
               'MCP success was not delivered to the model; refusing score': 'undelivered_mcp_result',
               'MCP result mismatch; refusing score': 'result_mismatch',
               'Unexpected execution or unknown item; refusing score': 'unexpected_item',
               'App-server operation timed out': 'timeout',
               'Codex turn did not complete successfully': 'turn_failed'}
    return allowed.get(str(exc), 'unclassified')


def run_case(case: str, model: str, timeout: int, codex: str) -> dict:
    with tempfile.TemporaryDirectory(prefix='afterset-eval-') as temp:
        root = Path(temp)
        cwd = root / 'cwd'
        cwd.mkdir()
        fixture = root / 'observer'
        initialize_fixture(fixture, CASES[case]['fixture'])
        before = snapshot(fixture)
        server_name = 'afterset_eval_' + secrets.token_hex(8)
        client = AppServer(command(codex, root, fixture, server_name), cwd=str(cwd), env=child_environment())
        calls, responses, snapshots, sent_prompts, offset = [], [], [before], [], 0
        started, inventory, index = {}, {}, 0
        stage = 'initialize'
        try:
            client.request('initialize', {'clientInfo': {'name': 'afterset-synthetic-eval', 'version': '1'},
                                          'capabilities': {'experimentalApi': True}}, timeout)
            client.notify('initialized')
            stage = 'thread_preflight'
            def new_thread():
                started = client.request('thread/start', {'cwd': str(cwd), 'ephemeral': True, 'environments': [],
                    'runtimeWorkspaceRoots': [], 'approvalPolicy': 'never', 'sandbox': 'read-only',
                    'model': model, 'config': {'project_doc_max_bytes': 0}}, timeout)
                thread = started['thread']
                if (not thread.get('ephemeral') or started.get('instructionSources') != []
                        or started.get('runtimeWorkspaceRoots') != [] or started.get('cwd') != str(cwd)
                        or started.get('approvalPolicy') != 'never'
                        or started.get('sandbox') != {'type': 'readOnly', 'networkAccess': False}):
                    raise RuntimeError('Isolation preflight failed; refusing model turn')
                return started, _inventory(client, thread['id'], timeout, server_name)
            started, inventory = new_thread()
            thread_id = started['thread']['id']
            stopped = None
            for index, prompt in enumerate(CASES[case]['turns']):
                if case == 'reconstruct' and index == 1 and not ready_for_approval(calls, responses[0], snapshots[0], snapshots[1]):
                    stopped = 'Approval withheld: no sufficiently visible draft; semantic review still required.'
                    break
                if case == 'reconstruct' and index == 2:
                    recall, _ = new_thread()
                    thread_id = recall['thread']['id']
                stage = f'turn_{index}'
                sent_prompts.append(prompt)
                started_turn = client.request('turn/start', {'threadId': thread_id, 'cwd': str(cwd),
                    'environments': [], 'input': [{'type': 'text', 'text': prompt, 'text_elements': []}]}, timeout)
                turn_id = started_turn['turn']['id']
                client.wait_turn(thread_id, turn_id, timeout)
                responses.append(_response(client.events, thread_id, turn_id))
                new_calls, offset = _calls(fixture, offset, index)
                calls.extend(new_calls)
                verify_trace(client.events, thread_id, turn_id, new_calls, server_name)
                snapshots.append(snapshot(fixture))
            return {'case': case, 'requested_model': model, 'actual_model': started.get('model'),
                    'model_provider': started.get('modelProvider'), 'service_tier': started.get('serviceTier'),
                    'inventory': inventory, 'sent_prompts': sent_prompts, 'calls': calls, 'event_tools': tool_events(client.events), 'responses': responses, 'snapshots': snapshots,
                    'assessment': score(case, calls, responses, snapshots), 'stopped': stopped,
                    'hosted_chatgpt': 'unverified'}
        except Exception as exc:
            try:
                partial, _ = _calls(fixture, offset, index)
                calls.extend(partial)
                snapshots.append(snapshot(fixture))
            except Exception:
                pass
            return {'case': case, 'requested_model': model, 'actual_model': started.get('model'),
                    'model_provider': started.get('modelProvider'), 'service_tier': started.get('serviceTier'),
                    'inventory': inventory, 'sent_prompts': sent_prompts, 'calls': calls, 'event_tools': tool_events(client.events),
                    'responses': responses, 'snapshots': snapshots, 'failure_stage': stage,
                    'infrastructure_error': type(exc).__name__, 'failure_code': failure_code(exc),
                    'assessment': {'overall': 'unverified', 'semantic_review': 'unverified'}}
        finally:
            client.close()


def metadata_fingerprints() -> dict[str, str]:
    from afterset.server import INSTRUCTIONS, create_server
    tools = create_server()._tool_manager._tools
    metadata = {name: {'description': tool.description, 'parameters': tool.parameters,
                       'annotations': tool.annotations.model_dump(mode='json') if tool.annotations else None}
                for name, tool in sorted(tools.items())}
    encoded = json.dumps(metadata, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
    return {'instructions_sha256': hashlib.sha256(INSTRUCTIONS.encode()).hexdigest(),
            'tool_metadata_sha256': hashlib.sha256(encoded).hexdigest()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Explicitly enable model calls, which may incur cost.')
    parser.add_argument('--case', choices=sorted(CASES), required=True)
    parser.add_argument('--model', required=True, help='Explicit Codex model to evaluate.')
    parser.add_argument('--repeat', type=int, default=3, choices=range(1, 4))
    parser.add_argument('--timeout', type=int, default=90, choices=range(10, 181), metavar='10..180')
    parser.add_argument('--output', type=Path, required=True, help='New synthetic report file; never overwrites.')
    args = parser.parse_args(argv)
    if not args.live:
        parser.error('Pass --live to authorize bounded model calls')
    if not re.fullmatch(r'[A-Za-z0-9._-]{1,100}', args.model):
        parser.error('Invalid model name')
    codex = shutil.which('codex')
    if not codex:
        parser.error('codex executable not found')
    version = subprocess.run([codex, '--version'], capture_output=True, text=True, timeout=10, check=True).stdout.strip()
    if version != 'codex-cli 0.165.0':
        parser.error('This harness requires validated codex-cli 0.165.0; revalidate before using another version')
    # Reserve the report before model calls; do not spend tokens if the destination exists.
    with args.output.open('x', encoding='utf-8') as f:
        reports = []
        for _ in range(args.repeat):
            try:
                reports.append(run_case(args.case, args.model, args.timeout, codex))
                if reports[-1].get('infrastructure_error'):
                    break
            except Exception as exc:
                reports.append({'case': args.case, 'infrastructure_error': type(exc).__name__,
                                'assessment': {'overall': 'unverified', 'semantic_review': 'unverified'}})
        document = {'generated_at_utc': datetime.now(timezone.utc).isoformat(), 'codex_version': version,
                    'fixture_sha256': hashlib.sha256((ROOT / 'scripts/conversation_eval/fixtures.py').read_bytes()).hexdigest(),
                    'checker_sha256': hashlib.sha256((ROOT / 'scripts/conversation_eval/scenarios.py').read_bytes()).hexdigest(),
                    'server_sha256': hashlib.sha256((ROOT / 'afterset/server.py').read_bytes()).hexdigest(),
                    'metadata': metadata_fingerprints(), 'repeat_requested': args.repeat, 'runs': reports}
        json.dump(document, f, indent=2, ensure_ascii=False)
    print(f'Wrote synthetic evaluation report to {args.output}; semantic review remains unverified.')
    return 1 if any(r.get('infrastructure_error') or r.get('assessment', {}).get('overall') == 'fail' for r in reports) else 0


if __name__ == '__main__':
    raise SystemExit(main())
