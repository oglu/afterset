"""Small bounded stdio client for a dedicated Codex app-server process."""
from __future__ import annotations

import json
import queue
import subprocess
import threading
import time


class AppServer:
    def __init__(self, command: list[str], *, cwd: str, env: dict[str, str]):
        self.process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                        text=True, bufsize=1, start_new_session=True)
        self.queue = queue.Queue()
        self.next_id = 1
        self.events = []
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        try:
            for line in self.process.stdout:
                try:
                    self.queue.put(json.loads(line))
                except json.JSONDecodeError:
                    self.queue.put({'protocol_error': 'Invalid JSON from app server'})
        finally:
            self.queue.put({'protocol_error': 'App server exited'})

    def _send(self, message):
        self.process.stdin.write(json.dumps(message, separators=(',', ':')) + '\n')
        self.process.stdin.flush()

    def _next(self, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('App-server operation timed out')
        try:
            message = self.queue.get(timeout=remaining)
        except queue.Empty:
            raise TimeoutError('App-server operation timed out') from None
        if 'protocol_error' in message:
            raise RuntimeError(message['protocol_error'])
        # Never grant approval, run dynamic code, or answer elicitation requests.
        if 'id' in message and 'method' in message:
            self._send({'id': message['id'], 'error': {'code': -32000, 'message': 'Unavailable in synthetic evaluation'}})
        else:
            self.events.append(message)
        return message

    def request(self, method, params, timeout):
        request_id = self.next_id
        self.next_id += 1
        self._send({'id': request_id, 'method': method, 'params': params})
        deadline = time.monotonic() + timeout
        while True:
            event = self._next(deadline)
            if event.get('id') == request_id and 'method' not in event:
                if 'error' in event:
                    raise RuntimeError(f'{method} failed: {event["error"].get("message", "unknown error")}')
                return event.get('result')

    def notify(self, method, params=None):
        self._send({'method': method, 'params': params or {}})

    def wait_turn(self, thread_id, turn_id, timeout):
        deadline = time.monotonic() + timeout
        pending = list(self.events)
        while True:
            event = pending.pop(0) if pending else self._next(deadline)
            params = event.get('params') or {}
            if (event.get('method') == 'turn/completed' and params.get('threadId') == thread_id
                    and params.get('turn', {}).get('id') == turn_id):
                turn = params['turn']
                if turn.get('status') != 'completed':
                    raise RuntimeError('Codex turn did not complete successfully')
                return turn

    def close(self):
        import os
        import signal
        if self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGTERM)
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=5)
