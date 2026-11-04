"""Inspect a filter request locally using fictional input and an unauthenticated sink.

The sink never forwards requests. It returns an intentional HTTP 400 so no model
is invoked. Reports contain tool names and input sizes, never headers or full text.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading

if '--installed' not in sys.argv:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openai_codex import Codex
from rightmemory.codex_sdk import CodexSdkRunner
from rightmemory.config import AgentCliConfig
from rightmemory.retrieval_filter import RetrievalFilter


def inspect(out: Path) -> dict:
    requests = []

    class Sink(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
            payload = json.loads(body)
            tools = payload.get('tools', [])
            nested_tools = []
            def find_tools(value):
                if isinstance(value, dict):
                    if 'name' in value and ('description' in value or 'parameters' in value):
                        nested_tools.append({'name': value['name'], 'schema_chars': len(json.dumps(value, ensure_ascii=False))})
                    for child in value.values():
                        find_tools(child)
                elif isinstance(value, list):
                    for child in value:
                        find_tools(child)
            find_tools(payload.get('input', []))
            items = []
            for item in payload.get('input', []):
                content = item.get('content', [])
                text = content if isinstance(content, str) else '\n'.join(
                    part.get('text', '') for part in content if isinstance(part, dict)
                )
                items.append({'role': item.get('role'), 'type': item.get('type'),
                              'text_chars': len(text)})
            requests.append({
                'authorization_header_present': 'Authorization' in self.headers,
                'model': payload.get('model'),
                'request_bytes': len(body),
                'instructions_chars': len(payload.get('instructions') or ''),
                'tool_schema_chars': len(json.dumps(tools, ensure_ascii=False)),
                'tools': [{'type': tool.get('type'), 'name': tool.get('name'),
                           'schema_chars': len(json.dumps(tool, ensure_ascii=False))}
                          for tool in tools],
                'tools_embedded_in_input': nested_tools,
                'input_items': items,
            })
            response = json.dumps({'error': {'message': 'Local request inspection complete',
                                            'type': 'invalid_request_error',
                                            'code': 'inspection_complete'}}).encode()
            self.send_response(400)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(response)))
            self.end_headers()
            self.wfile.write(response)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Sink)
    listener = threading.Thread(target=server.serve_forever, daemon=True)
    listener.start()
    endpoint = f'http://127.0.0.1:{server.server_port}/v1'
    overrides = (
        'model_provider="rightmemory_inspection"',
        'model_providers.rightmemory_inspection.name="Request inspection"',
        f'model_providers.rightmemory_inspection.base_url={json.dumps(endpoint)}',
        'model_providers.rightmemory_inspection.wire_api="responses"',
        'model_providers.rightmemory_inspection.requires_openai_auth=false',
        'model_providers.rightmemory_inspection.supports_websockets=false',
        'model_providers.rightmemory_inspection.request_max_retries=0',
        'model_providers.rightmemory_inspection.stream_max_retries=0',
    )
    runner = CodexSdkRunner(codex_factory=lambda cfg: Codex(
        replace(cfg, config_overrides=(*cfg.config_overrides, *overrides),
                env={**(cfg.env or {}), 'NO_PROXY': '127.0.0.1,localhost'})))
    error = None
    error_message = None
    try:
        RetrievalFilter(AgentCliConfig('codex', 'gpt-6-luna', 'low'), runner).run(
            'What color is the fictional Firleaf marker?',
            'Fictional test record: the Firleaf marker is amber.',
        )
    except Exception as exc:
        error = type(exc).__name__
        error_message = str(exc)
    finally:
        try:
            runner.close()
        finally:
            try:
                server.shutdown()
            finally:
                server.server_close()
                listener.join()
    result = {'endpoint': endpoint, 'requests': requests, 'terminal_error_type': error, 'terminal_error_message': error_message,
              'limitations': 'Anonymous local provider; records request construction, not the exact OpenAI server-side input or token count.'}
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    if not requests:
        raise RuntimeError(f'No request captured; terminal error: {error}: {error_message}')
    if any(row['authorization_header_present'] for row in requests):
        raise RuntimeError('Unexpected authentication header on anonymous local request')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--installed', action='store_true', help='inspect the installed runtime before replacing it')
    args = parser.parse_args()
    if args.out.exists():
        parser.error('Choose a new report path')
    result = inspect(args.out)
    print(json.dumps(result, ensure_ascii=False))
