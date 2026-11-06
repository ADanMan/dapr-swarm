"""Exercise retries through the actual Dapr/OpenAI clients against a local HTTP fixture."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.agents import BeeGateClient
from app.store import TaskStore
from app.tools import task_context

UPSTREAM = ('litellm.AuthenticationError: AuthenticationError: OpenAIException - '
            '<html><h1>401 Authorization Required</h1><center>nginx</center></html>. '
            'Received Model Group=glm-5.3-flash-uncensored-df')


@pytest.mark.parametrize('message,failures,calls,success', [
    ('DELEGATE', 0, 1, True), ('MIXED', 0, 2, True), ('PREPARE', 0, 1, True), ('NO_DELEGATE', 0, 1, False),
    ('LENGTH_POSITIONAL', 1, 2, True), ('LENGTH_STRING', 1, 2, True), ('LENGTH_DICT', 1, 2, True), ('LENGTH_NGINX', 9, 3, False),
    ('LENGTH', 2, 3, True), ('LENGTH', 9, 3, False), ('LENGTH_TOOLS', 1, 2, True), ('LENGTH_TEXT', 1, 2, True),
    ('EMPTY', 2, 3, True), ('EMPTY', 9, 3, False), (UPSTREAM, 2, 3, True), (UPSTREAM, 4, 5, True), (UPSTREAM, 9, 5, False),
    ('Invalid API key', 9, 1, False),
    ('Invalid API key. Received Model Group=glm', 9, 1, False),
    ('<html><h1>401 Authorization Required</h1>nginx</html>', 9, 1, False),
])
def test_retry_is_bounded_and_keeps_request_key_and_budget(tmp_path, monkeypatch, message, failures, calls, success):
    requests, waits = [], []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            requests.append((self.headers['Authorization'], json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
            failed = len(requests) <= failures
            payload = {'error': {'message': message, 'code': '401'}} if failed else {
                'id': 'test', 'object': 'chat.completion', 'created': 1, 'model': 'test',
                'choices': [{'index': 0, 'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': 'Recovered'}}],
                'usage': {'prompt_tokens': 2, 'completion_tokens': 1, 'total_tokens': 3}}
            if failed and (message == 'EMPTY' or message.startswith('LENGTH')):
                payload = {'id':'empty','object':'chat.completion','created':1,'model':'test','choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':None}}]}
            if failed and message.startswith('LENGTH'):
                payload['choices'][0]['finish_reason'] = 'length'
                if message == 'LENGTH_TEXT':
                    payload['choices'][0]['message']['content'] = 'Truncated final answer'
                if message == 'LENGTH_TOOLS':
                    payload['choices'][0]['message']['tool_calls'] = [{'id':'partial','type':'function','function':{'name':'write_file','arguments':'{"path":"outputs/partial.json","content":"{}"}'}}]
            if message in ('DELEGATE', 'PREPARE', 'MIXED'):
                payload['choices'][0].update(finish_reason='tool_calls', message={'role':'assistant','content':None,'tool_calls':[{'id':'delegate-1','type':'function','function':{'name':('read_file' if message == 'PREPARE' else 'swarm_builder'),'arguments':('{"path":"inputs/contract.md"}' if message == 'PREPARE' else '{"task":"Write result"}')}}]})
            if message == 'MIXED' and len(requests) == 1:
                payload['choices'][0]['message']['tool_calls'].append({'id':'read-1','type':'function','function':{'name':'read_file','arguments':'{"path":"inputs/contract.md"}'}})
            status = 401 if failed and message != 'EMPTY' and not message.startswith('LENGTH') else 200
            if message == 'LENGTH_NGINX' and len(requests) > 2:
                payload = {'error':{'message':UPSTREAM,'code':'401'}}
                status = 401
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    store = TaskStore(tmp_path)
    task = store.create('Test', 2, 10)
    store.update(task['id'], 'running')
    client = BeeGateClient(model='test', api_key='test-key', base_url=f'http://127.0.0.1:{server.server_port}/v1')
    client.client.max_retries = 0
    client._store = store
    monkeypatch.setattr('app.agents.time.sleep', waits.append)
    options = {}
    if message in ('DELEGATE','NO_DELEGATE','PREPARE','MIXED'):
        with store.connect() as db:
            db.execute("UPDATE flows SET agent='swarm_coordinator' WHERE id=?", (task['id'],))
        options['tools'] = [{'type':'function','function':{'name':'swarm_builder','description':'Delegate work','parameters':{'type':'object','properties':{'task':{'type':'string'}},'required':['task']}}}]
    if message == 'PREPARE':
        options['tools'].append({'type':'function','function':{'name':'read_file','description':'Read input','parameters':{'type':'object','properties':{'path':{'type':'string'}},'required':['path']}}})
    token = task_context.set((task['id'], task['id']))
    try:
        if success:
            if message == 'LENGTH_POSITIONAL':
                result = client.generate([{'role':'user','content':'Say Recovered'}], **options)
            else:
                messages = 'Say Recovered' if message == 'LENGTH_STRING' else {'role':'user','content':'Say Recovered'} if message == 'LENGTH_DICT' else [{'role':'user','content':'Say Recovered'}]
                result = client.generate(messages=messages, **options)
            assert result.get_message().content == 'Recovered' if message.startswith('LENGTH') else result
        else:
            with pytest.raises(RuntimeError) as failure:
                client.generate(messages=[{'role': 'user', 'content': 'Say Recovered'}], **options)
        if not success:
            with store.connect() as db:
                records = [dict(r) for r in db.execute('SELECT * FROM model_failures WHERE root=?',(task['id'],))]
            recoverable = message.startswith('LENGTH') or message == 'EMPTY' or message == UPSTREAM
            assert len(records) == int(recoverable)
            if recoverable:
                assert 'SWARM_RECOVERABLE:'+records[0]['token'] in str(failure.value)
        if options:
            assert requests[0][1]['tool_choice'] == 'required'
            assert requests[0][1]['reasoning_effort'] == 'low'
        assert len(requests) == calls
        if message == 'MIXED':
            assert 'separate' in requests[1][1]['messages'][-1]['content']
        elif message.startswith('LENGTH'):
            assert [r[1]['max_tokens'] for r in requests] == [8192, 16384, 32768][:calls]
            assert all(r[0] == 'Bearer test-key' for r in requests)
            assert 'NOT executed' in requests[1][1]['messages'][-1]['content']
            assert requests[1][1]['messages'][:-1] == requests[0][1]['messages']
        else:
            assert all(request == requests[0] for request in requests)
        assert store.get(task['id'])['llm_calls'] == calls
        assert waits == ([2, 4, 8, 16] if calls == 5 else [2, 4] if calls == 3 else [2] if calls == 2 else [])
    finally:
        task_context.reset(token)
        client.client.close()
        server.shutdown()
        server.server_close()
        thread.join()
