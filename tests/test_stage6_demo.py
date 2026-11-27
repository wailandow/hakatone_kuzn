import asyncio
import json
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

import httpx
import numpy as np
from pydantic import SecretStr

from app.config import Settings
from app.db import connect
from app.main import create_app


class DemoTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = create_app(Settings(_env_file=None, db_path=Path(self.temp.name) / 'test.db',
                                       demo_enabled=True, llm_api_key=None, llm_model='',
                                       webim_webhook_token='fixture-inbound', webim_bot_token='fixture-outbound',
                                       manager_token='fixture-manager'))
        self.life = self.app.router.lifespan_context(self.app)
        await self.life.__aenter__()
        self.addAsyncCleanup(self.life.__aexit__, None, None, None)
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='http://test')
        self.addAsyncCleanup(self.http.aclose)
        self.manager = {'Authorization': 'Token fixture-manager'}

    async def test_split_screen_is_served_without_manager_credentials(self):
        page = await self.http.get('/')
        self.assertEqual(page.status_code, 200)
        self.assertIn('Клиентский чат', page.text)
        self.assertIn('Webim v2.0', page.text)
        self.assertNotIn('fixture-manager', page.text)
        self.assertEqual((await self.http.get('/demo.js')).status_code, 200)

    async def test_client_page_keeps_only_quote_and_handoff_quick_actions(self):
        page = await self.http.get('/')
        self.assertEqual(page.status_code, 200)
        self.assertNotIn('>Тестовый пилот</button>', page.text)
        self.assertNotIn('>Товары</button>', page.text)
        self.assertIn('data-ask="Подготовьте КП">КП</button>', page.text)
        self.assertIn('data-ask="Переведите на менеджера.">Эскалация</button>', page.text)
        self.assertNotIn('Нужна скидка больше 5%', page.text)
        js = (await self.http.get('/demo.js')).text
        self.assertIn('md-table', js)
        self.assertIn('safeLink', js)
        self.assertIn(r'\/pdfs\/kp_[a-f0-9]{32}', js)

    async def test_quote_confirmation_requires_both_chat_capability_and_exact_proposal(self):
        chat, auth = await self.new()
        other, _ = await self.new()
        proposal = {'items': [{'name': 'Учебная лицензия', 'quantity': 1, 'subtotal': '100.00',
                               'offer_id': 'test'}], 'total': '100.00', 'currency': 'RUB'}
        with connect(self.app.state.webim.settings.db_path) as db, db:
            db.execute('INSERT INTO quote_confirmations(chat_id,confirmation_id,proposal_json) VALUES (?,?,?)',
                       (chat, 'fixture-proposal', json.dumps(proposal)))
        state = (await self.http.get(f'/api/demo/chats/{chat}', headers=auth)).json()
        self.assertEqual(state['quote']['quote'], proposal)
        for target, identifier, code in [(other, 'fixture-proposal', 404), (chat, 'wrong', 409),
                                         (chat, 'fixture-proposal', 200), (chat, 'fixture-proposal', 409)]:
            res = await self.http.post(f'/api/demo/chats/{target}/confirm-quote', headers=auth,
                                       json={'confirmation_id': identifier})
            self.assertEqual(res.status_code, code, res.text)

    async def new(self):
        response = await self.http.post('/api/demo/chats', json={'page': 'red-os'})
        self.assertEqual(response.status_code, 201, response.text)
        data = response.json()
        return data['chat_id'], {'Authorization': f"Token {data['token']}"}

    async def turn(self, chat, auth, text):
        response = await self.http.post(f'/api/demo/chats/{chat}/messages', headers=auth, json={'text': text})
        self.assertEqual(response.status_code, 202, response.text)
        for _ in range(100):
            state = (await self.http.get(f'/api/demo/chats/{chat}', headers=auth)).json()
            if not state['busy']:
                return state
            await asyncio.sleep(.02)
        self.fail('Webim worker did not finish')

    async def test_demo_offline_handoff_exercises_real_mock_webim(self):
        chat, auth = await self.new()
        state = await self.turn(chat, auth, 'Как оформить тестовый пилот?')
        self.assertEqual(state['status'], 'manager')
        self.assertEqual(state['mode'], 'offline')
        self.assertEqual(state['page_url'], 'https://store.softline.ru/redsoft/red-os-1/')
        self.assertEqual(state['messages'][-1]['sender'], 'bot')
        self.assertIn('менеджер', state['messages'][-1]['text'].lower())
        with connect(self.app.state.webim.settings.db_path) as db:
            events = [row[0] for row in db.execute('SELECT payload_json FROM audit_log WHERE chat_id=?', (chat,))]
        self.assertTrue(any('new_message' in event for event in events))
        self.assertTrue(any('send_message' in event and 'delivered' in event for event in events))
        self.assertTrue(any('redirect_chat' in event and 'delivered' in event for event in events))
        self.assertFalse(any('тестовый пилот' in event for event in events))

    async def test_demo_capability_does_not_read_other_chats_or_allow_external_callback(self):
        chat, auth = await self.new()
        other, _ = await self.new()
        self.assertEqual((await self.http.get(f'/api/demo/chats/{other}', headers=auth)).status_code, 404)
        self.assertEqual((await self.http.get(f'/api/demo/chats/{chat}')).status_code, 404)
        response = await self.http.post('/webhook/webim', headers={'Authorization': 'Token fixture-inbound'},
                                        json={'event': 'new_message', 'chat_id': int(chat),
                                              'message': {'id': 'external', 'kind': 'visitor', 'text': 'external'}})
        self.assertEqual(response.status_code, 403)

    async def test_manager_reply_is_audited_and_confirmed_article_is_live_in_new_chat(self):
        question = 'Как оформить тестовый пилот?'
        answer = 'Выберите продукт и согласуйте тестовый срок.'
        chat, auth = await self.new()
        await self.turn(chat, auth, question)
        reply = await self.http.post(f'/api/manager/chats/{chat}/reply', headers=self.manager, json={'text': answer})
        self.assertEqual(reply.status_code, 201, reply.text)
        state = (await self.http.get(f'/api/demo/chats/{chat}', headers=auth)).json()
        self.assertEqual(state['messages'][-1]['text'], answer)
        draft = await self.http.post('/api/manager/kb-from-reply', headers=self.manager,
                                     json={'chat_id': chat, 'reply_id': reply.json()['reply_id']})
        self.assertEqual(draft.status_code, 201, draft.text)
        self.assertEqual(draft.json()['mode'], 'offline')
        self.assertEqual(self.app.state.agent.tools.search_kb(question), [])
        path = f"/api/manager/kb-drafts/{draft.json()['id']}/confirm"
        self.assertEqual((await self.http.post(path, headers=self.manager)).status_code, 201)
        self.assertEqual((await self.http.post(path, headers=self.manager)).status_code, 409)
        new_chat, new_auth = await self.new()
        state = await self.turn(new_chat, new_auth, question)
        self.assertEqual(state['status'], 'bot')
        self.assertIn(answer, state['messages'][-1]['text'])
        inspector = await self.http.get(f'/api/manager/chats/{chat}/inspector', headers=self.manager)
        data = inspector.json()
        self.assertIsNone(data['usage']['total_tokens'])
        self.assertIsNone(data['usage']['average_cost_usd'])
        self.assertNotIn(answer, inspector.text)
        self.assertNotIn(question, inspector.text)
        self.assertTrue(any(event['type'] == 'manager_reply' for event in data['events']))

    async def test_manager_routes_require_separate_auth_and_do_not_list_real_chats(self):
        chat, auth = await self.new()
        await self.turn(chat, auth, 'Нужна консультация по пилоту')
        with connect(self.app.state.webim.settings.db_path) as db, db:
            db.execute("INSERT INTO chats(chat_id,status) VALUES ('42','manager')")
            db.execute("INSERT INTO escalations(chat_id,reason,summary) VALUES ('42','private','PRIVATE CONTACT')")
        for method, path in [('GET', 'escalations'), ('GET', f'chats/{chat}'),
                             ('GET', f'chats/{chat}/inspector'), ('POST', f'chats/{chat}/reply'),
                             ('POST', 'kb-from-reply'), ('POST', 'kb-drafts/unknown/confirm')]:
            response = await self.http.request(method, f'/api/manager/{path}', headers=auth)
            self.assertEqual(response.status_code, 403, path)
        queue = await self.http.get('/api/manager/escalations', headers=self.manager)
        self.assertEqual([item['chat_id'] for item in queue.json()['escalations']], [chat])
        for suffix in ('', '/inspector', '/reply'):
            response = await self.http.request('POST' if suffix == '/reply' else 'GET',
                                               f'/api/manager/chats/42{suffix}', headers=self.manager,
                                               json={'text': 'no'} if suffix == '/reply' else None)
            self.assertEqual(response.status_code, 404)

    async def test_browser_demo_rejects_contact_fields_and_callbacks_to_real_chat(self):
        chat, auth = await self.new()
        res = await self.http.post(f'/api/demo/chats/{chat}/messages', headers=auth,
                                   json={'text': 'Перезвоните на +7 (999) 123-45-67'})
        self.assertEqual(res.status_code, 422)
        res = await self.http.post('/webhook/webim', headers={'Authorization': 'Token fixture-inbound'},
                                   json={'event': 'new_message', 'chat_id': 42,
                                         'message': {'id': 'external', 'kind': 'visitor', 'text': 'external'}})
        self.assertEqual(res.status_code, 403)
        with connect(self.app.state.webim.settings.db_path) as db, db:
            db.execute("INSERT INTO chats(chat_id) VALUES ('42')")
        res = await self.http.post('/mock-webim/api/bot/v2/send_message',
                                   headers={'Authorization': 'Token fixture-outbound'},
                                   json={'chat_id': 42, 'message': {'kind': 'operator', 'text': 'external'}})
        self.assertEqual(res.json(), {'error': 'chat-not-found'})

    async def test_manager_reply_rolls_back_if_audit_cannot_be_written(self):
        chat, auth = await self.new()
        await self.turn(chat, auth, 'Нужна консультация')
        with connect(self.app.state.webim.settings.db_path) as db, db:
            db.execute("""CREATE TRIGGER reject_manager_audit BEFORE INSERT ON audit_log
                WHEN NEW.event_type='manager_reply' BEGIN SELECT RAISE(FAIL, 'fixture'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            await self.http.post(f'/api/manager/chats/{chat}/reply', headers=self.manager,
                                 json={'text': 'Не должно сохраниться'})
        state = (await self.http.get(f'/api/demo/chats/{chat}', headers=auth)).json()
        self.assertNotIn('manager', [item['sender'] for item in state['messages']])

    async def test_confirmed_embedding_is_visible_to_dense_search_in_same_process(self):
        chat, auth = await self.new()
        await self.turn(chat, auth, 'Как оформить пробный доступ?')
        reply = await self.http.post(f'/api/manager/chats/{chat}/reply', headers=self.manager,
                                     json={'text': 'Согласуйте срок пилота.'})
        draft = (await self.http.post('/api/manager/kb-from-reply', headers=self.manager,
                                     json={'chat_id': chat, 'reply_id': reply.json()['reply_id']})).json()
        vector = np.array([1, 0], dtype='<f4')
        self.app.state.agent.tools.embedder = SimpleNamespace(passages=lambda _: [vector], query=lambda _: vector)
        res = await self.http.post(f"/api/manager/kb-drafts/{draft['id']}/confirm", headers=self.manager)
        self.assertEqual(res.json()['search'], 'hybrid')
        self.assertEqual(self.app.state.agent.tools.search_kb('несовпадающий')[0]['content'], 'Согласуйте срок пилота.')

    async def test_usage_is_per_visitor_turn_and_ignores_unknown_price(self):
        agent = self.app.state.agent
        agent.settings.llm_model = 'fixture'
        agent.settings.llm_api_key = SecretStr('not-a-provider-key')
        usage = SimpleNamespace(prompt_tokens=20, completion_tokens=10)
        async def complete(**kwargs):
            return SimpleNamespace(usage=usage, choices=[SimpleNamespace(finish_reason='stop',
                message=SimpleNamespace(tool_calls=None, content='Ответ для теста'))])
        agent._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=complete)))
        agent._owned_client = False
        chat, auth = await self.new()
        await self.turn(chat, auth, 'Первый вопрос')
        agent.settings.llm_price_in_per_1m = 1
        agent.settings.llm_price_out_per_1m = 2
        agent.settings.usd_to_rub = 100
        await self.turn(chat, auth, 'Второй вопрос')
        data = (await self.http.get(f'/api/manager/chats/{chat}/inspector', headers=self.manager)).json()
        stats = data['usage']
        self.assertEqual((stats['requests'], stats['metered'], stats['priced']), (2, 2, 1))
        self.assertEqual(stats['total_tokens'], 60)
        self.assertEqual(stats['average_tokens'], 30)
        self.assertAlmostEqual(stats['average_cost_usd'], .00004)
        self.assertAlmostEqual(stats['average_cost_rub'], .004)

    async def test_kb_draft_with_llm_uses_full_completion_budget(self):
        from app.agent.llm_client import MAX_COMPLETION_TOKENS

        chat, auth = await self.new()
        await self.turn(chat, auth, 'Как оформить тестовый пилот?')
        reply = await self.http.post(f'/api/manager/chats/{chat}/reply', headers=self.manager,
                                     json={'text': 'Согласуйте пилот через менеджера.'})
        self.assertEqual(reply.status_code, 201, reply.text)
        seen = {}
        agent = self.app.state.agent
        agent.settings.llm_model = 'fixture'
        agent.settings.llm_api_key = SecretStr('not-a-provider-key')
        async def complete(**kwargs):
            seen.update(kwargs)
            return SimpleNamespace(
                usage=SimpleNamespace(prompt_tokens=50, completion_tokens=600),
                choices=[SimpleNamespace(
                    finish_reason='stop',
                    message=SimpleNamespace(content=json.dumps({
                        'question': 'Как оформить тестовый пилот?',
                        'answer': 'Согласуйте тестовый пилот через менеджера.',
                    }, ensure_ascii=False)),
                )],
            )
        agent._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=complete)))
        agent._owned_client = False
        draft = await self.http.post('/api/manager/kb-from-reply', headers=self.manager,
                                     json={'chat_id': chat, 'reply_id': reply.json()['reply_id']})
        self.assertEqual(draft.status_code, 201, draft.text)
        self.assertEqual(draft.json()['mode'], 'llm')
        self.assertEqual(seen.get('max_tokens'), MAX_COMPLETION_TOKENS)
