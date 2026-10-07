"""OpenAI-compatible text adapter for Snowflake SQL API. No Cortex REST calls."""
import asyncio
import hmac
import json
import os
import time
import uuid
from contextlib import asynccontextmanager
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

SQL = "SELECT AI_COMPLETE(model => ?, prompt => ?, model_parameters => PARSE_JSON(?), show_details => TRUE) AS RESPONSE"

@asynccontextmanager
async def lifespan(app):
    base = os.environ.get('SNOWFLAKE_API_BASE', '').rstrip('/')
    parsed = urlparse(base)
    if parsed.scheme != 'https' or not (parsed.hostname or '').endswith('.snowflakecomputing.com') or parsed.path or parsed.query or parsed.username:
        raise RuntimeError('SNOWFLAKE_API_BASE must be an HTTPS Snowflake account URL without a path')
    for key in ('SNOWFLAKE_API_KEY', 'PROXY_API_KEY', 'SNOWFLAKE_WAREHOUSE'):
        if not os.environ.get(key):
            raise RuntimeError(f'Missing environment variable: {key}')
    app.state.base = base
    token = os.environ['SNOWFLAKE_API_KEY'].removeprefix('pat/')
    app.state.client = httpx.AsyncClient(timeout=35, trust_env=False, headers={
        'Authorization': f'Bearer {token}', 'Content-Type': 'application/json',
        'Accept': 'application/json', 'User-Agent': 'JanitorSQLProxy/1.0',
        'X-Snowflake-Authorization-Token-Type': 'PROGRAMMATIC_ACCESS_TOKEN',
    })
    app.state.lock = asyncio.Semaphore(2)
    yield
    await app.state.client.aclose()

app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=['*'], allow_methods=['GET', 'POST'], allow_headers=['Authorization', 'Content-Type'])

def authorize(request):
    supplied = request.headers.get('authorization', '')
    expected = 'Bearer ' + os.environ.get('PROXY_API_KEY', '')
    if not os.environ.get('PROXY_API_KEY') or not hmac.compare_digest(supplied, expected):
        raise HTTPException(401, 'Invalid proxy API key')

def models():
    return [m.strip() for m in os.environ.get('SNOWFLAKE_MODELS', 'claude-opus-4-6,claude-sonnet-4-5').split(',') if m.strip()]

def prompt_from(messages):
    if not isinstance(messages, list) or not messages:
        raise HTTPException(400, 'messages must be a non-empty list')
    cleaned = []
    for message in messages:
        if not isinstance(message, dict) or message.get('role') not in ('system', 'developer', 'user', 'assistant') or not isinstance(message.get('content'), str):
            raise HTTPException(400, 'Only text system/developer/user/assistant messages are supported')
        cleaned.append({'role': message['role'], 'content': message['content']})
    history = json.dumps(cleaned, ensure_ascii=False)
    if len(history.encode('utf-8')) > 1000000:
        raise HTTPException(413, 'Conversation exceeds 1 MB; reduce context')
    return ('Continue the conversation provided below as the assistant. The JSON array contains '
            'messages in chronological order. Use system/developer messages as instructions, '
            'user messages as user input, and assistant messages as prior replies. '
            'Return only the next assistant reply, without JSON wrapping or role labels.\n\n' + history)

def parse_completion(cell):
    if cell is None:
        raise HTTPException(502, 'AI_COMPLETE returned NULL. Test the same model in a SQL worksheet.')
    try:
        result = json.loads(cell) if isinstance(cell, str) else cell
        choice = result['choices'][0]
        content = choice.get('messages') or choice.get('message', {}).get('content')
        if not isinstance(content, str) or not content:
            raise ValueError()
        return content, result.get('usage', {})
    except (ValueError, KeyError, TypeError, IndexError):
        raise HTTPException(502, 'Unexpected AI_COMPLETE detailed response format')

async def execute(statement, values=()):
    body = {'statement': statement, 'timeout': 180,
            'warehouse': os.environ['SNOWFLAKE_WAREHOUSE'],
            'role': os.environ.get('SNOWFLAKE_ROLE', 'JANITOR_CORTEX_ROLE'),
            'parameters': {'QUERY_TAG': 'janitor_sql_proxy', 'USE_CACHED_RESULT': 'false'}}
    if values:
        body['bindings'] = {str(i): {'type': 'TEXT', 'value': value} for i, value in enumerate(values, 1)}
    client = app.state.client
    path = '/api/v2/statements'
    response = await client.post(app.state.base + path, params={'async': 'true', 'requestId': str(uuid.uuid4())}, json=body)
    handle = None
    try:
        deadline = time.monotonic() + 190
        while True:
            try:
                payload = response.json()
            except ValueError:
                raise HTTPException(502, f'Snowflake returned HTTP {response.status_code} without JSON')
            handle = payload.get('statementHandle') or handle
            if response.status_code == 200 and 'data' in payload:
                return payload['data']
            if response.status_code not in (200, 202):
                # Do not expose raw messages: they can contain SQL prompts or token text.
                code = str(payload.get('code', 'unknown'))
                raise HTTPException(502, f'Snowflake SQL API HTTP {response.status_code}, code {code}. Check Snowflake Query History (tag janitor_sql_proxy).')
            if not handle:
                raise HTTPException(502, 'Snowflake returned no statement handle')
            if time.monotonic() >= deadline:
                raise HTTPException(504, 'SQL generation timed out; avoid repeated retries')
            await asyncio.sleep(1)
            response = await client.get(app.state.base + '/api/v2/statements/' + handle)
    except (asyncio.CancelledError, HTTPException):
        if handle:
            try:
                await client.post(app.state.base + '/api/v2/statements/' + handle + '/cancel', timeout=5)
            except httpx.HTTPError:
                pass
        raise

@app.get('/health')
async def health():
    return {'status': 'ok', 'backend': 'snowflake-sql', 'live_connection_tested': False}

@app.get('/v1/models')
async def model_list(request: Request):
    authorize(request)
    return {'object': 'list', 'data': [{'id': m, 'object': 'model', 'owned_by': 'snowflake-sql'} for m in models()]}

@app.post('/diagnostics/sql')
async def diagnostics(request: Request):
    authorize(request)
    try:
        data = await execute('SELECT 1 AS OK')
        return {'sql_api': 'ok', 'result': data}
    except httpx.HTTPError:
        raise HTTPException(502, 'SQL API network error; no automatic retry was made')

@app.post('/v1/chat/completions')
async def chat(request: Request):
    authorize(request)
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError()
    except (ValueError, TypeError):
        raise HTTPException(400, 'Expected a JSON object')
    model = body.get('model', 'claude-opus-4-6')
    if model not in models():
        raise HTTPException(400, 'Model is not in SNOWFLAKE_MODELS')
    if body.get('tools') or body.get('response_format'):
        raise HTTPException(400, 'Tools and structured output are not supported by this adapter')
    prompt = prompt_from(body.get('messages'))
    try:
        limit = int(body.get('max_completion_tokens') or body.get('max_tokens') or 2048)
        temperature = float(body.get('temperature', 0.8))
        if not 1 <= limit <= 8192 or not 0 <= temperature <= 1:
            raise ValueError()
    except (ValueError, TypeError, OverflowError):
        raise HTTPException(400, 'max_tokens must be 1..8192; temperature must be 0..1')
    options = json.dumps({'max_tokens': limit, 'temperature': temperature})
    async def generate():
        try:
            async with app.state.lock:
                data = await execute(SQL, (model, prompt, options))
            return parse_completion(data[0][0])
        except httpx.HTTPError:
            raise HTTPException(502, 'SQL API network error; no automatic retry was made')
        except IndexError:
            raise HTTPException(502, 'SQL API returned no rows')
    identity = 'chatcmpl-' + uuid.uuid4().hex
    created = int(time.time())
    if not body.get('stream'):
        content, usage = await generate()
        return {'id': identity, 'object': 'chat.completion', 'created': created, 'model': model,
                'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': content}, 'finish_reason': 'stop'}], 'usage': usage}
    async def events():
        task = asyncio.create_task(generate())
        def chunk(delta, finish=None):
            return 'data: ' + json.dumps({'id': identity, 'object': 'chat.completion.chunk', 'created': created, 'model': model,
                'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]}, ensure_ascii=False) + '\n\n'
        try:
            while not task.done():
                await asyncio.wait({task}, timeout=10)
                if not task.done():
                    yield ': waiting for SQL generation\n\n'
            content, _ = task.result()
            yield chunk({'role': 'assistant', 'content': content})
            yield chunk({}, 'stop')
            yield 'data: [DONE]\n\n'
        except HTTPException as exc:
            yield 'data: ' + json.dumps({'error': {'message': exc.detail, 'type': 'upstream_error', 'code': exc.status_code}}) + '\n\n'
            yield 'data: [DONE]\n\n'
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
    return StreamingResponse(events(), media_type='text/event-stream', headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})
