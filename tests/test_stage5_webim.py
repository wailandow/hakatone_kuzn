import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from secrets import token_urlsafe
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx

from app.config import Settings
from app.db import connect
from test_stage4_agent import completion
from test_stage4_pdf import font_dir


class WebimTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app.main import create_app

        tmp = tempfile.TemporaryDirectory(dir=os.getenv('TMPDIR'))
        self.addCleanup(tmp.cleanup)
        self.secret, self.bot_token = token_urlsafe(), token_urlsafe()
        self.settings = Settings(_env_file=None, db_path=Path(tmp.name) / 'app.db',
                                 webim_webhook_token=self.secret, webim_bot_token=self.bot_token,
                                 llm_model='', llm_api_key=None)
        self.app = create_app(self.settings)
        await self.enterAsyncContext(self.app.router.lifespan_context(self.app))
        self.http = await self.enterAsyncContext(httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url='http://127.0.0.1:8000'))
        self.headers = {'Authorization': f'Token {self.secret}'}

    def rows(self, sql, params=()):
        with connect(self.settings.db_path) as db:
            return [tuple(row) for row in db.execute(sql, params)]

    async def hook(self, payload):
        return await self.http.post('/webhook/webim', json=payload, headers=self.headers)

    @staticmethod
    def message(mid, text, chat=42, kind='visitor'):
        return {'event': 'new_message', 'chat_id': chat,
                'message': {'id': mid, 'kind': kind, 'text': text}}

    async def wait_for(self, pred):
        async with asyncio.timeout(4):
            while not pred():
                await asyncio.sleep(.01)

    async def test_auth_before_input_and_no_mutation(self):
        payload = self.message('one', 'текст')
        reply = await self.http.post('/webhook/webim', json=payload)
        self.assertEqual(reply.status_code, 403)
        self.assertEqual(self.rows('SELECT chat_id FROM chats'), [])
        bad = await self.hook(self.message('x', 'bad', chat=True))
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(self.rows('SELECT chat_id FROM chats'), [])

    async def test_assignment_and_ordered_dedup_across_event_shapes(self):
        started, release = asyncio.Event(), asyncio.Event()
        histories = []

        async def run(chat_id, history, **kwargs):
            histories.append([item['content'] for item in history])
            if len(histories) == 1:
                started.set()
                await release.wait()
            return 'Ответ'

        self.app.state.agent.run = run
        assignment = {'event': 'new_chat', 'chat': {'id': 42}, 'page_url': 'https://store.softline.ru/office/?sid=private',
                      'visitor': {'id': 'visitor-id', 'channel': None, 'fields': {'phone': '+70000000000'}},
                      'messages': [self.message('one', 'customer-private')['message']]}
        try:
            reply = await asyncio.wait_for(self.http.post(f'/webhook/webim/{self.secret}', json=assignment), 1)
            self.assertEqual((reply.status_code, reply.json()), (200, {'result': 'ok'}))
            await asyncio.wait_for(started.wait(), 1)
            self.assertEqual((await self.hook(self.message('one', 'duplicate'))).status_code, 200)
            self.assertEqual((await self.hook(self.message('two', 'next'))).status_code, 200)
            self.assertEqual((await self.hook(self.message('echo', 'echo', kind='operator'))).status_code, 200)
            self.assertEqual(histories, [['customer-private']])
        finally:
            release.set()
        await self.wait_for(lambda: len(self.rows("SELECT id FROM messages WHERE sender='bot'")) == 2)
        self.assertEqual(histories, [['customer-private'], ['customer-private', 'Ответ', 'next']])
        self.assertEqual(self.rows('SELECT status,page_url FROM chats'), [('bot', 'https://store.softline.ru/office/')])
        audit = repr(self.rows('SELECT event_type,payload_json FROM audit_log'))
        for value in ('customer-private', '+70000000000', 'visitor-id', 'sid=private', self.secret, self.bot_token):
            self.assertNotIn(value, audit)
        self.assertIn('webim_in', audit)
        self.assertIn('webim_out', audit)

    async def test_stage4_handoff_updates_mock_and_never_interrupts_manager(self):
        self.assertEqual((await self.hook(self.message('one', 'Нужен менеджер'))).status_code, 200)
        await self.wait_for(lambda: self.rows('SELECT status FROM chats') == [('manager',)])
        calls = []

        async def record(*args, **kwargs):
            calls.append(args)

        self.app.state.agent.run = record
        self.assertEqual((await self.hook(self.message('two', 'Уточнение для менеджера'))).status_code, 200)
        await self.wait_for(lambda: not self.app.state.webhook.workers)
        self.assertEqual(calls, [])
        self.assertEqual(self.rows('SELECT sender FROM messages ORDER BY id'), [('client',), ('client',)])
        self.assertEqual(len(self.rows('SELECT id FROM escalations')), 1)
        self.assertIn('redirect_chat', repr(self.rows("SELECT payload_json FROM audit_log WHERE event_type='webim_out'")))
        self.assertNotIn('webim_turn', repr(self.rows("SELECT payload_json FROM audit_log WHERE event_type='error'")))

    async def test_authenticated_reassignment_resumes_next_turn(self):
        assignment = {'event': 'new_chat', 'chat': {'id': 42}, 'visitor': {'id': 'visitor-id'},
                      'messages': [self.message('one', 'Нужен менеджер')['message']]}
        self.assertEqual((await self.hook(assignment)).status_code, 200)
        await self.wait_for(lambda: not self.app.state.webhook.workers)
        self.assertEqual(self.rows('SELECT status FROM chats'), [('manager',)])
        calls = []

        async def run(chat_id, history, **kwargs):
            calls.append(history[-1]['content'])
            return 'Ответ'

        self.app.state.agent.run = run
        reassignment = dict(assignment, messages=[])
        self.assertEqual((await self.hook(assignment)).status_code, 200)  # old callback
        self.assertEqual((await self.hook(dict(reassignment, visitor={'id': 'other'}))).status_code, 200)
        mock_headers = {'Authorization': f'Token {self.bot_token}'}
        self.assertEqual((await self.http.post('/webhook/webim', headers=mock_headers, json=reassignment)).status_code, 403)
        self.assertEqual(self.rows('SELECT status FROM chats'), [('manager',)])
        self.assertEqual((await self.hook(reassignment)).status_code, 200)
        self.assertEqual(self.rows('SELECT status FROM chats'), [('bot',)])
        self.assertEqual(self.rows('SELECT status FROM escalation_redirects'), [])
        self.assertEqual((await self.hook(self.message('two', 'Вернулся'))).status_code, 200)
        await self.wait_for(lambda: not self.app.state.webhook.workers)
        self.assertEqual(calls, ['Вернулся'])
        self.assertEqual(self.rows("SELECT text FROM messages WHERE sender='bot'"), [('Ответ',)])

        await self.app.state.agent.tools.escalate_to_manager('42', 'reason', 'summary', {})
        self.assertEqual((await self.hook(reassignment)).status_code, 200)  # replay after second handoff
        stale = dict(assignment, messages=[self.message('two', 'Вернулся')['message']])
        self.assertEqual((await self.hook(stale)).status_code, 200)
        self.assertEqual(self.rows('SELECT status FROM chats'), [('manager',)])
        fresh = dict(assignment, messages=[self.message('three', 'Новый вопрос')['message']])
        self.assertEqual((await self.hook(fresh)).status_code, 200)
        await self.wait_for(lambda: not self.app.state.webhook.workers)
        self.assertEqual(calls, ['Вернулся', 'Новый вопрос'])

    async def test_real_agent_text_reaches_mock(self):
        self.settings.llm_model = 'test-model'
        create = AsyncMock(return_value=completion('Ответ агента'))
        agent = self.app.state.agent
        agent._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        agent._owned_client = False
        self.assertEqual((await self.hook(self.message('one', 'Тестовый вопрос'))).status_code, 200)
        await self.wait_for(lambda: not self.app.state.webhook.workers)
        self.assertEqual(self.rows('SELECT sender,text FROM messages ORDER BY id'),
                         [('client', 'Тестовый вопрос'), ('bot', 'Ответ агента')])
        create.assert_awaited_once()

    async def test_mock_requires_bot_token_and_rejects_unsupported_redirect_target(self):
        base = '/mock-webim/api/bot/v2/'
        with connect(self.settings.db_path) as db, db:
            db.execute("INSERT INTO chats(chat_id,status) VALUES ('42','escalated')")
        response = await self.http.post(base + 'redirect_chat', headers=self.headers, json={'chat_id': 42})
        self.assertEqual(response.status_code, 403)
        headers = {'Authorization': f'Token {self.bot_token}'}
        response = await self.http.post(base + 'redirect_chat', headers=headers, json={'chat_id': 42, 'operator_id': 4})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.rows('SELECT status FROM chats'), [('escalated',)])

    async def test_each_queued_message_uses_its_own_page_context(self):
        started, release = asyncio.Event(), asyncio.Event()
        pages = []

        async def run(chat_id, history, *, page_url=None):
            pages.append(page_url)
            if len(pages) == 1:
                started.set()
                await release.wait()

        self.app.state.agent.run = run
        expected = [f'https://store.softline.ru/page-{i}/' for i in range(3)]
        try:
            for i, page in enumerate(expected):
                self.assertEqual((await self.hook(dict(self.message(str(i), 'text'), page_url=page))).status_code, 200)
                if i == 0:
                    await asyncio.wait_for(started.wait(), 1)
        finally:
            release.set()
        await self.wait_for(lambda: not self.app.state.webhook.workers)
        self.assertEqual(pages, expected)

    async def test_mock_rejects_non_loopback_even_with_token(self):
        transport = httpx.ASGITransport(app=self.app, client=('192.0.2.10', 12345))
        async with httpx.AsyncClient(transport=transport, base_url='http://127.0.0.1:8000') as client:
            response = await client.post('/mock-webim/api/bot/v2/redirect_chat', json={'chat_id': 42},
                                         headers={'Authorization': f'Token {self.bot_token}'})
        self.assertEqual(response.status_code, 403)

    async def test_pdf_only_for_registered_quote_and_mock_rejects_other_chat(self):
        path = self.settings.db_path.parent / 'pdfs'
        path.mkdir()
        quote = 'a' * 32
        name = f'kp_{quote}.pdf'
        content = b'%PDF-1.4\n'
        (path / name).write_bytes(content)
        self.assertEqual((await self.http.get(f'/pdfs/{name}')).status_code, 404)
        url = f'{self.settings.public_base_url.rstrip("/")}/pdfs/{name}'
        with connect(self.settings.db_path) as db, db:
            db.executemany('INSERT INTO chats(chat_id) VALUES (?)', [('42',), ('43',)])
            db.execute('INSERT INTO quotes(id,chat_id,items_json,discount_percent,total_sum,pdf_url) VALUES (?,?,?,?,?,?)',
                       (quote, '42', '[]', 0, 1, url))
        self.assertEqual((await self.http.get('/pdfs/')).status_code, 404)
        response = await self.http.get(f'/pdfs/{name}')
        self.assertEqual((response.status_code, response.content), (200, content))
        self.assertEqual(response.headers['cache-control'], 'private, no-store')
        payload = {'chat_id': 43, 'message': {'kind': 'file_operator', 'data': {
            'url': url, 'name': 'KP_Softline.pdf', 'media_type': 'application/pdf'}}}
        response = await self.http.post('/mock-webim/api/bot/v2/send_message', json=payload,
                                        headers={'Authorization': f'Token {self.bot_token}'})
        self.assertEqual(response.status_code, 400)
        await self.app.state.agent.tools.send_file('42', url)
        self.assertEqual(self.rows('SELECT chat_id,kind,file_url FROM messages'), [('42', 'file_operator', url)])
        self.assertNotIn(url, repr(self.rows('SELECT payload_json FROM audit_log')))

    async def test_mock_matches_exact_registered_url_with_explicit_https_port(self):
        self.settings.public_base_url = 'https://files.example.invalid:443'
        path = self.settings.db_path.parent / 'pdfs'
        path.mkdir()
        quote = 'b' * 32
        name = f'kp_{quote}.pdf'
        (path / name).write_bytes(b'%PDF-1.4\n')
        url = f'{self.settings.public_base_url}/pdfs/{name}'
        with connect(self.settings.db_path) as db, db:
            db.executemany('INSERT INTO chats(chat_id) VALUES (?)', [('42',), ('43',)])
            db.execute('INSERT INTO quotes(id,chat_id,items_json,discount_percent,total_sum,pdf_url) VALUES (?,?,?,?,?,?)',
                       (quote, '42', '[]', 0, 1, url))
        headers = {'Authorization': f'Token {self.bot_token}'}
        for chat_id, other in [(43, url), (42, url.replace(':443', '')), (42, url + '?extra=1'),
                               (42, f'https://files.example.invalid:443/other/../pdfs/{name}'), (42, 'not-a-url')]:
            with self.subTest(chat_id=chat_id, url=other):
                response = await self.http.post('/mock-webim/api/bot/v2/send_message', headers=headers, json={
                    'chat_id': chat_id, 'message': {'kind': 'file_operator', 'data': {
                        'url': other, 'name': 'KP_Softline.pdf', 'media_type': 'application/pdf'}}})
                self.assertEqual(response.status_code, 400)
        await self.app.state.webim.send_file('42', url)
        self.assertEqual(self.rows('SELECT chat_id,kind,file_url FROM messages'), [('42', 'file_operator', url)])

    async def test_generated_stage4_pdf_reaches_mock_and_download(self):
        if not (fonts := font_dir()):
            self.skipTest('requires DejaVu fonts (STAGE4_TEST_FONT_DIR)')
        self.settings.font_dir = fonts
        with connect(self.settings.db_path) as db, db:
            db.execute("INSERT INTO chats(chat_id) VALUES ('42')")
            db.execute("INSERT INTO offers(id,name,price,currency) VALUES ('sku','Лицензия',123.45,'RUB')")
        tools = self.app.state.agent.tools
        items = [{'offer_id': 'sku', 'quantity': 1}]
        self.assertEqual((await tools.generate_kp_pdf('42', items))['status'], 'confirmation_required')
        self.assertTrue(tools.confirm_quote('42', tools.pending_quote('42')['confirmation_id']))
        result = await tools.generate_kp_pdf('42', items)
        self.assertEqual(result['status'], 'generated')
        self.assertEqual(self.rows('SELECT kind,file_url FROM messages'), [('file_operator', result['pdf_url'])])
        downloaded = await self.http.get(result['pdf_url'])
        self.assertEqual(downloaded.status_code, 200)
        self.assertTrue(downloaded.content.startswith(b'%PDF-'))

    async def test_inbox_failure_is_not_acknowledged(self):
        with connect(self.settings.db_path) as db, db:
            db.execute("CREATE TRIGGER audit_failure BEFORE INSERT ON audit_log BEGIN SELECT raise(ABORT,'test'); END")
        self.assertEqual((await self.hook(self.message('one', 'text'))).status_code, 503)
        self.assertEqual(self.rows('SELECT chat_id FROM chats'), [])
        self.assertEqual(self.rows('SELECT message_id FROM webim_inbox'), [])

    async def test_echo_for_unknown_chat_does_not_create_chat(self):
        self.assertEqual((await self.hook(self.message('echo', 'ignored', chat=123, kind='operator'))).status_code, 200)
        self.assertEqual(self.rows('SELECT chat_id FROM chats'), [])
        self.assertEqual(self.rows('SELECT event_type FROM audit_log'), [('webim_in',)])

    async def test_duplicate_id_for_other_chat_does_not_create_chat(self):
        async def run(*args, **kwargs):
            return None

        self.app.state.agent.run = run
        self.assertEqual((await self.hook(self.message('one', 'first'))).status_code, 200)
        self.assertEqual((await self.hook(self.message('one', 'duplicate', chat=43))).status_code, 200)
        await self.wait_for(lambda: not self.app.state.webhook.workers)
        self.assertEqual(self.rows('SELECT chat_id FROM chats'), [('42',)])

    async def test_reused_credentials_are_rejected_before_startup(self):
        from app.main import create_app

        other = create_app(self.settings.model_copy(update={'webim_bot_token': self.settings.webim_webhook_token}))
        with self.assertRaisesRegex(ValueError, 'distinct'):
            async with other.router.lifespan_context(other):
                pass

    async def test_inflight_reply_is_suppressed_after_manager_takes_chat(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def run(*args, **kwargs):
            started.set()
            await release.wait()
            return 'Ответ не должен уйти'

        self.app.state.agent.run = run
        try:
            self.assertEqual((await self.hook(self.message('one', 'text'))).status_code, 200)
            await asyncio.wait_for(started.wait(), 1)
            with connect(self.settings.db_path) as db, db:
                db.execute("UPDATE chats SET status='manager' WHERE chat_id='42'")
        finally:
            release.set()
        await self.wait_for(lambda: not self.app.state.webhook.workers)
        self.assertEqual(self.rows('SELECT sender FROM messages'), [('client',)])

    async def test_real_client_distinguishes_webim_200_error_and_success(self):
        from app.webim.client import DeliveryError, WebimClient

        seen = []

        def respond(request):
            seen.append(request)
            return httpx.Response(200, json={'error': 'chat-not-found'} if len(seen) == 1 else {})

        settings = self.settings.model_copy(update={'webim_base_url': 'https://example.invalid'})
        client = WebimClient(settings, transport=httpx.MockTransport(respond))
        self.addAsyncCleanup(client.aclose)
        with connect(settings.db_path) as db, db:
            db.execute("INSERT INTO chats(chat_id) VALUES ('42')")
        with self.assertRaises(DeliveryError):
            await client.send_text('42', 'Ответ')
        self.assertEqual(self.rows('SELECT id FROM messages'), [])
        await client.send_text('42', 'Ответ')
        self.assertEqual(seen[-1].url.path, '/api/bot/v2/send_message')
        self.assertEqual(seen[-1].headers['authorization'], f'Token {self.bot_token}')
        self.assertEqual(self.rows('SELECT sender,text FROM messages'), [('bot', 'Ответ')])
