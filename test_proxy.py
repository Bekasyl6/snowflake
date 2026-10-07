"""Offline protocol tests; no live Snowflake account or credits required."""
import json
import os
import unittest
import httpx
from fastapi.testclient import TestClient
from app import app, SQL

class ProxyTest(unittest.TestCase):
    def setUp(self):
        os.environ.update(SNOWFLAKE_API_BASE='https://demo.snowflakecomputing.com', SNOWFLAKE_API_KEY='pat/secret', PROXY_API_KEY='client-secret', SNOWFLAKE_WAREHOUSE='JANITOR_SQL_WH')
        self.client = TestClient(app)
        self.client.__enter__()
        self.requests = []
        self.mode = 'ok'
        self.posts = 0
        def handler(req):
            self.requests.append(req)
            if req.method == 'POST' and req.url.path.endswith('/cancel'):
                return httpx.Response(200, json={})
            if self.mode == 'error':
                return httpx.Response(403, json={'code': '003001', 'message': 'sensitive raw prompt'})
            if req.method == 'POST':
                self.posts += 1
                if self.mode == 'async':
                    return httpx.Response(202, json={'statementHandle': 'handle1'})
                payload = json.loads(req.content)
                if payload['statement'] == 'SELECT 1 AS OK':
                    return httpx.Response(200, json={'data': [['1']]})
            if self.mode == 'null':
                return httpx.Response(200, json={'data': [[None]]})
            cell = json.dumps({'choices': [{'messages': 'Привет'}], 'usage': {'prompt_tokens': 4, 'completion_tokens': 1, 'total_tokens': 5}})
            return httpx.Response(200, json={'data': [[cell]]})
        self.original = app.state.client
        app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), headers={'Authorization': 'Bearer secret'})
        self.headers = {'Authorization': 'Bearer client-secret'}
        self.body = {'model': 'claude-opus-4-6', 'messages': [{'role': 'system', 'content': 'Character'}, {'role': 'user', 'content': "привет '; DROP DATABASE X; --"}], 'max_tokens': 32}
    def tearDown(self):
        self.client.portal.call(self.original.aclose)
        self.client.__exit__(None, None, None)
    def test_auth(self):
        self.assertEqual(self.client.post('/v1/chat/completions', json=self.body).status_code, 401)
        self.assertFalse(self.requests)
    def test_binding_and_unicode(self):
        r = self.client.post('/v1/chat/completions', headers=self.headers, json=self.body)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['choices'][0]['message']['content'], 'Привет')
        payload = json.loads(self.requests[0].content)
        self.assertEqual(payload['statement'], SQL)
        self.assertNotIn('DROP', payload['statement'])
        self.assertIn('DROP', payload['bindings']['2']['value'])
        self.assertIn('Character', payload['bindings']['2']['value'])
        self.assertEqual(self.posts, 1)
    def test_async(self):
        self.mode = 'async'
        r = self.client.post('/v1/chat/completions', headers=self.headers, json=self.body)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.posts, 1)
        self.assertEqual(self.requests[-1].method, 'GET')
    def test_sse(self):
        r = self.client.post('/v1/chat/completions', headers=self.headers, json={**self.body, 'stream': True})
        self.assertIn('text/event-stream', r.headers['content-type'])
        self.assertIn('Привет', r.text)
        self.assertTrue(r.text.endswith('data: [DONE]\n\n'))
    def test_error_redaction(self):
        self.mode = 'error'
        r = self.client.post('/v1/chat/completions', headers=self.headers, json=self.body)
        self.assertEqual(r.status_code, 502)
        self.assertIn('003001', r.text)
        self.assertNotIn('sensitive', r.text)
        self.assertEqual(self.posts, 0)
    def test_null(self):
        self.mode = 'null'
        self.assertEqual(self.client.post('/v1/chat/completions', headers=self.headers, json=self.body).status_code, 502)
    def test_invalid_parameters(self):
        for change in ({'temperature': 1.5}, {'model': 'unapproved'}, {'messages': []}, {'tools': [{'type': 'function'}]}):
            self.assertEqual(self.client.post('/v1/chat/completions', headers=self.headers, json={**self.body, **change}).status_code, 400)
        self.assertFalse(self.requests)
    def test_sql_probe(self):
        r = self.client.post('/diagnostics/sql', headers=self.headers)
        self.assertEqual(r.json()['sql_api'], 'ok')
        self.assertEqual(json.loads(self.requests[0].content)['statement'], 'SELECT 1 AS OK')

if __name__ == '__main__':
    unittest.main()
