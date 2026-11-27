import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

import httpx
import numpy as np
from openai import APIConnectionError, AsyncOpenAI
from pydantic import ValidationError

from app.agent.llm_client import Agent, MAX_STEPS
from app.agent.tools import AgentTools
from app.config import Settings
from app.db import connect, initialize
from test_stage4_pdf import font_dir


def call(name, arguments, id_='call_1'):
    return NS(id=id_, type='function', function=NS(name=name, arguments=arguments))


def completion(content=None, calls=(), usage=True):
    return NS(choices=[NS(message=NS(content=content, tool_calls=calls))],
              usage=NS(prompt_tokens=1000, completion_tokens=200) if usage else None)


class SettingsTests(unittest.TestCase):
    def test_nonfinite_rates_are_rejected_from_environment(self):
        for field in ('llm_price_in_per_1m', 'llm_price_out_per_1m', 'usd_to_rub'):
            for value in ('NaN', 'Infinity', '-Infinity', '1e999'):
                with self.subTest(field=field, value=value), patch.dict('os.environ', {field.upper(): value}):
                    with self.assertRaises(ValidationError) as error:
                        Settings(_env_file=None)
                    self.assertIn((field,), [item['loc'] for item in error.exception.errors()])


class AgentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.settings = Settings(_env_file=None, db_path=Path(tmp.name) / 'test.db', llm_model='test-model',
                                 llm_price_in_per_1m=2, llm_price_out_per_1m=8)
        initialize(self.settings.db_path)
        with connect(self.settings.db_path) as db, db:
            db.execute('INSERT INTO chats(chat_id,page_url) VALUES (?,?)', ('a','https://store.softline.ru/redcheck/'))
        self.redirect = AsyncMock()
        self.tools = AgentTools(self.settings, redirect_chat=self.redirect)
        self.create = AsyncMock()
        self.client = NS(chat=NS(completions=NS(create=self.create)))
        self.agent = Agent(self.settings, self.tools, client=self.client)

    def rows(self, sql):
        with connect(self.settings.db_path) as db:
            return list(db.execute(sql))

    async def test_tool_results_returned_to_model_and_each_call_audits_cost(self):
        self.create.side_effect = [completion(calls=[call('search_catalog','{"query":"RedCheck"}')]),
                                   completion('Уточните число IP.')]
        result = await self.agent.run('a', [{'role':'user','content':'Сколько на 64 IP?'}])
        self.assertEqual(result, 'Уточните число IP.')
        first, second = [entry.kwargs for entry in self.create.await_args_list]
        self.assertEqual(first['temperature'], 0.2)
        self.assertEqual(first['model'], 'test-model')
        self.assertIn('https://store.softline.ru/redcheck/', str(first['messages']))
        self.assertEqual([r['role'] for r in second['messages'][-3:]], ['user','assistant','tool'])
        self.assertEqual(second['messages'][-1], {'role':'tool','tool_call_id':'call_1','content':'[]'})
        rows = self.rows("SELECT * FROM audit_log WHERE event_type='llm_call' ORDER BY id")
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual((row['prompt_tokens'], row['completion_tokens']), (1000, 200))
            self.assertAlmostEqual(row['cost_usd'], 0.0036)
            self.assertGreaterEqual(row['latency_ms'], 0)

    async def test_cost_calculation_failure_keeps_reply_and_token_audit(self):
        self.create.return_value = completion('Ответ')
        # Model-copy updates bypass validation; 0 * Infinity raises in Decimal.
        self.create.return_value.usage.prompt_tokens = 0
        self.agent.settings = self.settings.model_copy(update={'llm_price_in_per_1m': float('inf')})
        self.assertEqual(await self.agent.run('a', []), 'Ответ')
        row, = self.rows("SELECT prompt_tokens, completion_tokens, cost_usd FROM audit_log WHERE event_type='llm_call'")
        self.assertEqual(tuple(row), (0, 200, None))

    async def test_cost_outside_float_range_is_audited_as_unavailable(self):
        self.create.return_value = completion('Ответ')
        self.create.return_value.usage.prompt_tokens = 10_000_000
        self.agent.settings = self.settings.model_copy(update={'llm_price_in_per_1m': 1e308})
        self.assertEqual(await self.agent.run('a', []), 'Ответ')
        row, = self.rows("SELECT prompt_tokens, cost_usd FROM audit_log WHERE event_type='llm_call'")
        self.assertEqual(tuple(row), (10_000_000, None))

    async def test_provider_failure_logs_without_credentials_and_escalates(self):
        self.create.side_effect = APIConnectionError(message='sensitive error detail', request=httpx.Request('GET','https://example.test'))
        result = await self.agent.run('a', [{'role':'user','content':'Помогите'}])
        self.assertIn('менеджер', result.lower())
        row, = self.rows('SELECT reason FROM escalations')
        self.assertEqual(row['reason'], 'LLM_PROVIDER_ERROR')
        attempt, = self.rows("SELECT * FROM audit_log WHERE event_type='llm_call'")
        self.assertIsNone(attempt['cost_usd'])
        self.assertNotIn('sensitive', str([tuple(r) for r in self.rows('SELECT * FROM audit_log')]))
        self.redirect.assert_awaited_once_with('a')

    async def test_step_limit_stops_an_endless_model_tool_loop(self):
        self.create.side_effect = [completion(calls=[call('search_kb','{"query":"доставка"}', str(i))])
                                   for i in range(MAX_STEPS)]
        result = await self.agent.run('a', [{'role':'user','content':'Доставка?'}])
        self.assertIn('менеджер', result.lower())
        self.assertEqual(MAX_STEPS, 5)
        self.assertEqual(self.create.await_count, MAX_STEPS)
        row, = self.rows('SELECT reason FROM escalations')
        self.assertEqual(row['reason'], 'MAX_STEPS_EXCEEDED')

    async def test_one_deadline_covers_all_model_calls_and_cancels_pending_call(self):
        cancelled = asyncio.Event()

        async def slow_call(**kwargs):
            if self.create.await_count == 1:
                return completion(calls=[call('unknown', '{}')])
            try:
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        self.create.side_effect = slow_call
        with (patch('app.agent.llm_client.LLM_TURN_TIMEOUT', 0.50),
              patch('app.agent.llm_client.asyncio.timeout_at', wraps=asyncio.timeout_at) as timeout):
            result = await self.agent.run('a', [])
        self.assertIn('менеджер', result)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(self.create.await_count, 2)
        self.assertEqual(timeout.call_args_list[0], timeout.call_args_list[1])
        row, = self.rows('SELECT reason FROM escalations')
        self.assertEqual(row['reason'], 'LLM_TIMEOUT')

    async def test_turn_deadline_cancels_sdk_retry_backoff(self):
        requests = []

        async def unavailable(request):
            requests.append(request)
            return httpx.Response(503, headers={'Retry-After': '10'}, json={'error': 'unavailable'})

        transport = httpx.MockTransport(unavailable)
        async with (httpx.AsyncClient(transport=transport) as http,
                    AsyncOpenAI(api_key='test', base_url='https://example.test/v1',
                                http_client=http, max_retries=2) as sdk):
            agent = Agent(self.settings, self.tools, client=sdk)
            # The first SDK call can spend over a second loading resources on Windows.
            # Still finish well before the mocked 10-second backoff could elapse.
            with patch('app.agent.llm_client.LLM_TURN_TIMEOUT', 3):
                self.assertIn('менеджер', await asyncio.wait_for(agent.run('a', []), 8))
        self.assertEqual(len(requests), 1)
        row, = self.rows('SELECT reason FROM escalations')
        self.assertEqual(row['reason'], 'LLM_TIMEOUT')

    async def test_timed_out_search_keeps_its_worker_slot_until_native_work_finishes(self):
        started, release = asyncio.Event(), Event()
        loop = asyncio.get_running_loop()
        queries = []
        dense_calls = []

        class BlockedVector:
            def __array__(self, dtype=None):
                dense_calls.append('blocked')
                return np.array([1.0], dtype=dtype)

        class BlockingEmbedder:
            def query(self, text):
                queries.append(text)
                if text == 'blocked':
                    loop.call_soon_threadsafe(started.set)
                    release.wait(5)
                    return BlockedVector()
                return np.array([1.0])

        embedder = BlockingEmbedder()
        self.tools.embedder = embedder
        other = AgentTools(self.settings, embedder=embedder)
        self.create.return_value = completion(calls=[call('search_kb', '{"query":"blocked"}')])
        with patch('app.agent.llm_client.LLM_TURN_TIMEOUT', 1):
            turn = asyncio.create_task(self.agent.run('a', []))
            try:
                await asyncio.wait_for(started.wait(), 2)
                self.assertIn('менеджер', await asyncio.wait_for(turn, 2))
                row, = self.rows('SELECT reason FROM escalations')
                self.assertEqual(row['reason'], 'LLM_TIMEOUT')
                self.assertEqual(self.create.await_count, 1)
                for _ in range(3):
                    result = await other.execute('a', 'search_catalog', {'query': 'next'})
                    self.assertEqual(result.get('error') if isinstance(result, dict) else result, 'SEARCH_BUSY')
                self.assertEqual(queries, ['blocked'])
            finally:
                release.set()
                await turn

        async def retry_until_free():
            while True:
                result = await other.execute('a', 'search_catalog', {'query': 'next'})
                if result == []:
                    return
                self.assertEqual(result.get('error'), 'SEARCH_BUSY')
                await asyncio.sleep(0.01)

        await asyncio.wait_for(retry_until_free(), 2)
        self.assertEqual(queries, ['blocked', 'next'])
        self.assertEqual(dense_calls, [])

    async def test_model_cannot_ignore_discount_escalation_or_execute_later_calls(self):
        self.create.return_value = completion(calls=[call('generate_kp_pdf', '{"items":[],"discount_percent":5.01}'),
                                                     call('search_kb','{"query":"доставка"}', 'later')])
        result = await self.agent.run('a', [{'role':'user','content':'Дайте скидку'}])
        self.assertIn('менеджер', result.lower())
        self.assertEqual(self.create.await_count, 1)
        row, = self.rows('SELECT reason FROM escalations')
        self.assertEqual(row['reason'], 'DISCOUNT_LIMIT_EXCEEDED')
        audited = [json.loads(r[0])['tool'] for r in self.rows("SELECT payload_json FROM audit_log WHERE event_type='tool_call'")]
        self.assertEqual(audited, ['generate_kp_pdf','escalate_to_manager'])

    async def test_provider_requests_bound_history_context_tool_results_and_completion(self):
        with connect(self.settings.db_path) as db, db:
            db.execute('INSERT INTO kb_articles(title,content,source) VALUES (?,?,?)',
                       ('Доставка', 'Полезный ответ ' + 'x' * 100_000, 'manager_added'))
        self.create.side_effect = [completion(calls=[call('search_kb', '{"query":"доставка"}')]),
                                   completion('short answer')]
        history = [{'role': 'user', 'content': f'old marker {i}:' + 'x' * 20_000} for i in range(60)]
        history.append({'role': 'user', 'content': 'Newest request'})
        self.assertEqual(await self.agent.run('a', history, page_url='/redcheck/' + 'y' * 30_000), 'short answer')
        for entry in self.create.await_args_list:
            args = entry.kwargs
            self.assertLessEqual(len(json.dumps(args['messages'], ensure_ascii=False)), 24_000)
            self.assertFalse(any('old marker 0:' in str(item) for item in args['messages']))
            self.assertIn('Newest request', str(args['messages']))
            self.assertEqual(args.get('max_tokens'), 4096)
        result = json.loads(self.create.await_args.kwargs['messages'][-1]['content'])
        self.assertTrue(result[0]['content'].startswith('Полезный ответ'))
        self.assertLessEqual(len(result[0]['content']), 1200)

    async def test_large_tool_batch_keeps_latest_tool_results_paired_in_context(self):
        with connect(self.settings.db_path) as db, db:
            db.execute('INSERT INTO kb_articles(title,content,source) VALUES (?,?,?)',
                       ('Доставка', 'я' * 1200, 'manager_added'))
        args = json.dumps({'query': 'доставка', 'padding': 'a' * 1270}, ensure_ascii=False)
        calls = [call('search_kb', '{"query":"доставка"}', 'useful')]
        calls += [call('search_kb', args, f'extra_{index}') for index in range(7)]
        self.create.side_effect = [completion('\x00' * 1000, calls), completion('Ответ')]
        history = [{'role': 'user', 'content': 'я' * 1200}]
        self.assertEqual(await self.agent.run('a', history), 'Ответ')
        messages = self.create.await_args.kwargs['messages']
        self.assertLessEqual(len(json.dumps(messages, ensure_ascii=False).encode()), 24_000)
        self.assertEqual([item['tool_call_id'] for item in messages if item['role'] == 'tool'],
                         [item.id for item in calls])
        self.assertTrue(json.loads(messages[-8]['content'])[0]['content'])

    async def test_provider_length_stop_is_not_returned_as_complete_answer(self):
        response = completion('Незаконченный ответ')
        response.choices[0].finish_reason = 'length'
        self.create.return_value = response
        self.assertIn('менеджер', await self.agent.run('a', []))
        row, = self.rows('SELECT reason FROM escalations')
        self.assertEqual(row['reason'], 'LLM_INVALID_RESPONSE')

    async def test_last_step_quote_preparation_keeps_confirmation_and_ends_batch(self):
        self._last_step_quote()
        result = await self.agent.run('a', [])
        self.assertIn('Подтвердите', result)
        self.assertEqual(self.tools.pending_quote('a')['quote']['total'], '123.45')
        self.assertEqual(self.rows('SELECT * FROM escalations'), [])
        self.assertEqual(self.create.await_count, MAX_STEPS)

    async def test_last_step_generated_quote_is_not_followed_by_escalation(self):
        if not font_dir():
            self.skipTest('requires DejaVu fonts (STAGE4_TEST_FONT_DIR)')
        self._last_step_quote()
        self.settings.font_dir = font_dir()
        self.tools.send_file = AsyncMock()
        await self.tools.generate_kp_pdf('a', [{'offer_id': 'sku', 'quantity': 1}])
        pending = self.tools.pending_quote('a')
        self.assertTrue(self.tools.confirm_quote('a', pending['confirmation_id']))
        self.assertIn('123.45', await self.agent.run('a', []))
        row, = self.rows('SELECT pdf_url FROM quotes')
        self.tools.send_file.assert_awaited_once_with('a', row['pdf_url'])
        self.assertEqual(self.rows('SELECT * FROM escalations'), [])
        self.assertIsNone(self.tools.pending_quote('a'))

    def _last_step_quote(self):
        with connect(self.settings.db_path) as db, db:
            db.execute("INSERT INTO offers(id,name,price,currency) VALUES ('sku','Лицензия',123.45,'RUB')")
        search = completion(calls=[call('search_kb', '{"query":"доставка"}')])
        self.create.side_effect = [search] * (MAX_STEPS - 1) + [completion(calls=[
            call('generate_kp_pdf', '{"items":[{"offer_id":"sku","quantity":1}]}'),
            call('escalate_to_manager', '{"reason":"late","summary":"late","collected_slots":{}}', 'later'),
        ])]

    async def test_model_escalation_ends_turn_and_nonbot_chat_stays_silent(self):
        self.create.return_value = completion(calls=[call('escalate_to_manager',
            '{"reason":"ORDER_STATUS","summary":"Статус заказа","collected_slots":{}}')])
        self.assertIn('менеджер', (await self.agent.run('a', [])).lower())
        self.assertIsNone(await self.agent.run('a', []))
        self.assertEqual(self.create.await_count, 1)
        self.redirect.assert_awaited_once_with('a')

    async def test_invalid_json_arguments_are_returned_as_tool_error(self):
        bad = '{"items":[],"discount_percent":1e-99999999999999999999}'
        self.create.side_effect = [completion(calls=[call('search_kb','{'), call('generate_kp_pdf', bad, 'call_2')]),
                                   completion('Попробуем ещё раз.')]
        self.assertEqual(await self.agent.run('a', []), 'Попробуем ещё раз.')
        results = self.create.await_args_list[1].kwargs['messages'][-2:]
        for result in results:
            self.assertEqual(json.loads(result['content']), {'error':'INVALID_ARGUMENTS'})

    async def test_escalation_accepts_fractional_slots_and_reports_redirect_failure(self):
        self.redirect.side_effect = OSError('transport unavailable')
        self.create.side_effect = [completion(calls=[call('escalate_to_manager',
            '{"reason":"SPECIAL_TERMS","summary":"Особые условия","collected_slots":{"percent":1.25}}')]),
            completion('Ещё один ответ')]
        self.assertIn('перевести чат автоматически не удалось', await self.agent.run('a', []))
        row, = self.rows('SELECT collected_slots_json FROM escalations')
        self.assertEqual(json.loads(row[0]), {'percent': 1.25})
        self.assertEqual(self.create.await_count, 1)

    async def test_failed_redirect_is_retried_after_restart_without_llm_or_duplicate_escalation(self):
        self.redirect.side_effect = [OSError('transport unavailable'), None]
        self.create.side_effect = OSError('provider unavailable')
        self.assertIn('перевести чат автоматически не удалось', await self.agent.run('a', []))
        restarted = Agent(self.settings, AgentTools(self.settings, redirect_chat=self.redirect), client=self.client)
        self.assertIn('менеджер', (await restarted.run('a', [])) or '')
        self.assertIsNone(await restarted.run('a', []))
        self.assertEqual(len(self.rows('SELECT * FROM escalations')), 1)
        self.assertEqual(self.redirect.await_count, 2)
        self.assertEqual(self.create.await_count, 1)

    async def test_local_escalation_does_not_claim_redirect_or_retry_on_next_turn(self):
        local = Agent(self.settings, AgentTools(self.settings), client=self.client)
        self.create.side_effect = OSError('provider unavailable')
        self.assertIn('Запрос сохранён', await local.run('a', []))
        self.assertIsNone(await self.agent.run('a', []))
        self.redirect.assert_not_awaited()

    async def test_redirect_timeout_remains_pending(self):
        async def slow_redirect(chat_id):
            await asyncio.sleep(3)

        self.create.side_effect = OSError('provider unavailable')
        self.redirect.side_effect = slow_redirect
        with patch('app.agent.tools.TRANSPORT_TIMEOUT', 0.05):
            result = await asyncio.wait_for(self.agent.run('a', []), 2)
        self.assertIn('перевести чат автоматически не удалось', result)
        row, = self.rows('SELECT status FROM escalation_redirects')
        self.assertEqual(row['status'], 'pending')

    async def test_sqlite_audit_rejection_does_not_prevent_fallback_or_hide_failed_redirect(self):
        with connect(self.settings.db_path) as db, db:
            db.execute("""CREATE TRIGGER reject_audit BEFORE INSERT ON audit_log
                           BEGIN SELECT RAISE(FAIL, 'audit unavailable'); END""")
        self.create.side_effect = OSError('provider unavailable')
        self.redirect.side_effect = OSError('transport unavailable')
        with self.assertLogs('app.agent.tools', level='WARNING') as audit_log:
            self.assertIn('перевести чат автоматически не удалось', await self.agent.run('a', []))
        self.assertTrue(any('Audit write failed' in line for line in audit_log.output))
        row, = self.rows('SELECT reason FROM escalations')
        self.assertEqual(row['reason'], 'LLM_PROVIDER_ERROR')
        self.redirect.assert_awaited_once_with('a')

    async def test_failed_escalation_write_does_not_claim_manager_was_notified(self):
        with connect(self.settings.db_path) as db, db:
            db.execute("""CREATE TRIGGER reject_escalation BEFORE INSERT ON escalations
                           BEGIN SELECT RAISE(FAIL, 'escalation unavailable'); END""")
        self.create.side_effect = OSError('provider unavailable')
        self.assertIn('Не удалось подключить', await self.agent.run('a', []))
        self.assertEqual(self.rows('SELECT status FROM chats')[0][0], 'bot')
        self.redirect.assert_not_awaited()

    async def test_key_absent_falls_back_without_constructing_sdk_client(self):
        with patch('app.agent.llm_client.AsyncOpenAI') as sdk:
            self.assertIn('менеджер', (await Agent(self.settings, self.tools).run('a', [])).lower())
        sdk.assert_not_called()
        row, = self.rows('SELECT reason FROM escalations')
        self.assertEqual(row['reason'], 'LLM_NOT_CONFIGURED')

    async def test_sdk_client_uses_configured_proxy_and_is_closed(self):
        settings = self.settings.model_copy(update={'llm_api_key':Settings(llm_api_key='test-key').llm_api_key,
                                                   'llm_proxy':Settings(llm_proxy='http://127.0.0.1:1234').llm_proxy})
        close = AsyncMock()
        self.client.close = close
        self.create.return_value = completion('Ответ')
        with patch('app.agent.llm_client.AsyncOpenAI', return_value=self.client) as sdk:
            agent = Agent(settings, self.tools)
            try:
                self.assertEqual(await agent.run('a', []), 'Ответ')
                args = sdk.call_args.kwargs
                self.assertEqual((args['base_url'], args['timeout'], args['max_retries']), (settings.llm_base_url, 30.0, 2))
                self.assertIsInstance(args['http_client'], httpx.AsyncClient)
            finally:
                await agent.aclose()
                await args['http_client'].aclose()
        close.assert_awaited_once()

    async def test_recent_catalog_offers_persist_into_next_turn_context(self):
        self.create.side_effect = [
            completion(calls=[tool_call('1', 'search_catalog', {'query': 'РЕД'})]),
            completion('Нашёл вариант'),
            completion('Принято'),
        ]
        reply1 = await self.agent.run('a', [{'role': 'user', 'content': 'Нужна РЕД ОС'}])
        self.assertEqual(reply1, 'Нашёл вариант')
        reply2 = await self.agent.run('a', [
            {'role': 'user', 'content': 'Нужна РЕД ОС'},
            {'role': 'assistant', 'content': 'Нашёл вариант'},
            {'role': 'user', 'content': 'Подготовьте КП'},
        ])
        self.assertEqual(reply2, 'Принято')
        turn2_messages = self.create.call_args_list[2].kwargs['messages']
        ctx_text = turn2_messages[1]['content']
        self.assertIn('recent_catalog_offers', ctx_text)
        ctx = json.loads(ctx_text.split(': ', 1)[1])
        offers = ctx['recent_catalog_offers']
        self.assertEqual(len(offers), 1)
        self.assertEqual(offers[0]['id'], '1')
        self.assertEqual(offers[0]['vendor_code'], 'RED-1')
        self.assertEqual(offers[0]['price'], 100.0)
