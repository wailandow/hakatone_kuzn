import asyncio
import json
from pathlib import Path
import sqlite3
import tempfile
from threading import Event
import unittest
from unittest.mock import AsyncMock, patch

import numpy as np

from app.config import Settings
from app.db import connect, initialize
from app.agent.tools import AgentTools, DEFAULT_PAYMENT, DEFAULT_DELIVERY
from test_stage4_pdf import font_dir


class ToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = Settings(_env_file=None, db_path=self.root / 'test.db',
                                 font_dir=font_dir() or self.root / 'missing')
        initialize(self.settings.db_path)
        with connect(self.settings.db_path) as db, db:
            db.executemany('INSERT INTO chats(chat_id) VALUES (?)', [('a',), ('b',)])
            db.executemany('''INSERT INTO offers(id,name,vendor_code,group_id,vendor,price,currency,available,
                params_json,is_russian_registry) VALUES(?,?,?,?,?,?,?,?,?,?)''', [
                ('1','Астра Линукс','SKU-1','linux','Astra',100.1,'RUR',1,'{"Срок":"1 год"}',1),
                ('2','Астра Линукс','SKU-2','linux','Astra',200,'USD',1,'{}',0),
                ('3','Астра Линукс','SKU-3','linux','Astra',100,'RUB',0,'{}',0),
                ('4','Астра Линукс','SKU-4','linux','Astra',None,'RUB',1,'{}',0),
            ])
        self.files = AsyncMock()
        self.redirect = AsyncMock()
        self.tools = AgentTools(self.settings, send_file=self.files, redirect_chat=self.redirect)
        self.items = [{'offer_id':'1','quantity':2}]

    def rows(self, sql):
        with connect(self.settings.db_path) as db:
            return list(db.execute(sql))

    async def call(self, **overrides):
        return await self.tools.execute('a', 'generate_kp_pdf', {'items':self.items, **overrides})

    async def test_catalog_card_and_fresh_manager_article(self):
        cards = await self.tools.execute('a', 'search_catalog', {'query':'SKU-1','limit':100})
        self.assertEqual(cards[0]['key_params'], {'Срок':'1 год'})
        self.assertEqual(cards[0]['variant_count'], 3)
        self.assertEqual(cards[0]['currency'], 'RUR')
        self.assertEqual(await self.tools.execute('a', 'search_kb', {'query':'Криптография'}), [])
        with connect(self.settings.db_path) as db, db:
            db.execute("INSERT INTO kb_articles(title,content,source,url) VALUES ('Криптография','Ответ','manager_added','https://example.test')")
        article, = await self.tools.execute('a', 'search_kb', {'query':'Криптография'})
        self.assertEqual((article['title'],article['content'],article['source']), ('Криптография','Ответ','manager_added'))

    async def test_cancelled_search_interrupts_running_sqlite_statement(self):
        started, finished = asyncio.Event(), asyncio.Event()
        loop = asyncio.get_running_loop()
        release = Event()
        errors = []

        def long_query(conn, query, **kwargs):
            def entered():
                loop.call_soon_threadsafe(started.set)
                release.wait(5)
                return 1

            conn.create_function('entered', 0, entered)
            try:
                conn.execute('''WITH RECURSIVE seq(x) AS (
                    SELECT entered() UNION ALL SELECT x+1 FROM seq WHERE x<1000000
                ) SELECT sum(x) FROM seq''').fetchone()
                return []
            except sqlite3.OperationalError as exc:
                errors.append(exc.sqlite_errorcode)
                raise
            finally:
                loop.call_soon_threadsafe(finished.set)

        with patch('app.db.search_kb', side_effect=long_query):
            task = asyncio.create_task(self.tools.execute('a', 'search_kb', {'query': 'доставка'}))
            try:
                await asyncio.wait_for(started.wait(), 2)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            finally:
                release.set()
                await asyncio.wait_for(finished.wait(), 3)
                await loop.shutdown_default_executor()
        self.assertEqual(errors, [sqlite3.SQLITE_INTERRUPT])
        self.assertEqual(self.rows('SELECT * FROM audit_log'), [])

    async def test_cancelled_dense_search_does_not_rank_another_batch(self):
        vector = np.array([1.0], dtype='<f4')
        with connect(self.settings.db_path) as db, db:
            db.executemany('INSERT INTO kb_articles(title, content, source, embedding) VALUES (?, ?, ?, ?)',
                           [('Тема', 'Текст', 'manager_added', vector.tobytes())] * 2049)

        started, finished = asyncio.Event(), asyncio.Event()
        release = Event()
        loop = asyncio.get_running_loop()
        ranked = []
        argsort = np.argsort
        search = self.tools.search_kb

        class Embedder:
            def query(self, text):
                return vector

        def rank_batch(scores, **kwargs):
            ranked.append(len(scores))
            if len(ranked) == 1:
                loop.call_soon_threadsafe(started.set)
                release.wait(5)
            return argsort(scores, **kwargs)

        def notify_finished(**kwargs):
            try:
                return search(**kwargs)
            finally:
                loop.call_soon_threadsafe(finished.set)

        self.tools.embedder = Embedder()
        with (patch.object(self.tools, 'search_kb', side_effect=notify_finished),
              patch('app.db.np.argsort', side_effect=rank_batch)):
            task = asyncio.create_task(self.tools.execute('a', 'search_kb', {'query': 'Тема'}))
            try:
                await asyncio.wait_for(started.wait(), 2)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            finally:
                release.set()
                await asyncio.wait_for(finished.wait(), 3)
                await loop.shutdown_default_executor()
        self.assertEqual(ranked, [2048])

    async def test_discount_guard_and_strict_item_and_path_validation(self):
        for discount in (-1, '5.01'):
            self.assertEqual(await self.call(discount_percent=discount),
                             {'error':'DISCOUNT_LIMIT_EXCEEDED','max_allowed':5.0,'action_required':'escalate_to_manager'})
        for discount in ('NaN', 'Infinity', True):
            self.assertIn('error', await self.call(discount_percent=discount))
        for qty in (0, -1, True, 1.5, '2'):
            self.assertEqual((await self.call(items=[{'offer_id':'1','quantity':qty}]))['error'], 'INVALID_ARGUMENTS')
        for args in ({'path':'../../outside.pdf'}, {'confirmed':True},
                     {'items':[{'offer_id':'1','quantity':2,'price':1}]}):
            self.assertEqual((await self.call(**args))['error'], 'INVALID_ARGUMENTS')
        self.assertEqual(self.rows('SELECT * FROM quotes'), [])
        self.files.assert_not_awaited()

    async def test_catalog_rejects_missing_price_currency_unavailable_and_unknown(self):
        for key, error in (('2','UNSUPPORTED_CURRENCY'),('3','OFFER_UNAVAILABLE'),
                           ('4','INVALID_CATALOG_PRICE'),('unknown','OFFER_NOT_FOUND')):
            self.assertEqual((await self.call(items=[{'offer_id':key,'quantity':3}]))['error'], error)
        self.assertIsNone(self.tools.pending_quote('a'))

    async def test_backend_confirmation_is_bound_to_chat_items_discount_and_terms(self):
        expected = await self.call(discount_percent='5')
        self.assertEqual(expected['status'], 'confirmation_required')
        self.assertEqual(expected['quote']['total'], '190.20')
        self.assertEqual(expected['quote']['payment_terms'], DEFAULT_PAYMENT)
        self.assertEqual(expected['quote']['delivery_terms'], DEFAULT_DELIVERY)
        pending = self.tools.pending_quote('a')
        self.assertNotIn('confirmation_id', expected)
        self.assertFalse(self.tools.confirm_quote('b', pending['confirmation_id']))
        self.assertTrue(self.tools.confirm_quote('a', pending['confirmation_id']))
        self.assertEqual((await self.call(discount_percent='5', payment_terms='Постоплата'))['action_required'], 'escalate_to_manager')
        self.assertEqual((await self.call(discount_percent='5', delivery_terms='Завтра'))['action_required'], 'escalate_to_manager')
        self.assertEqual((await self.call(discount_percent='5', items=[{'offer_id':'1','quantity':3}]))['status'], 'confirmation_required')
        self.assertFalse(self.tools.confirm_quote('a', pending['confirmation_id']))
        self.assertEqual((await self.call(discount_percent='4'))['status'], 'confirmation_required')
        self.assertEqual(self.rows('SELECT * FROM quotes'), [])
        self.files.assert_not_awaited()

    async def test_catalog_price_change_requires_fresh_confirmation(self):
        await self.call()
        pending = self.tools.pending_quote('a')
        self.assertTrue(self.tools.confirm_quote('a', pending['confirmation_id']))
        with connect(self.settings.db_path) as db, db:
            db.execute("UPDATE offers SET price=300 WHERE id='1'")
        result = await self.call()
        self.assertEqual(result['quote']['total'], '600.00')
        self.assertFalse(self.tools.confirm_quote('a', pending['confirmation_id']))
        self.files.assert_not_awaited()

    async def test_confirmed_quote_is_persisted_and_delivered_only_once(self):
        if not font_dir():
            self.skipTest('requires DejaVu fonts (STAGE4_TEST_FONT_DIR)')
        await self.call(discount_percent=5)
        pending = self.tools.pending_quote('a')
        self.assertTrue(self.tools.confirm_quote('a', pending['confirmation_id']))
        result = await self.call(discount_percent='5.0')
        self.assertEqual(result['total_sum'], '190.20')
        row, = self.rows('SELECT * FROM quotes')
        self.assertEqual(json.loads(row['items_json'])[0]['name'], 'Астра Линукс')
        self.assertEqual(row['pdf_url'], result['pdf_url'])
        pdf, = list((self.root / 'pdfs').iterdir())
        self.assertRegex(pdf.name, r'^kp_[0-9a-f]{32}\.pdf$')
        self.assertTrue(pdf.read_bytes().startswith(b'%PDF-'))
        self.files.assert_awaited_once_with('a', result['pdf_url'])
        self.assertFalse(self.tools.confirm_quote('a', pending['confirmation_id']))
        self.assertEqual((await self.call(discount_percent=5))['status'], 'confirmation_required')
        self.assertEqual(len(self.rows('SELECT * FROM quotes')), 1)

    async def test_explicit_confirmation_persists_across_tool_instances(self):
        await self.call()
        pending = self.tools.pending_quote('a')
        new_tools = AgentTools(self.settings)
        self.assertTrue(new_tools.confirm_quote('a', pending['confirmation_id']))
        self.assertTrue(self.tools.pending_quote('a')['confirmed'])
        self.assertFalse(self.tools.confirm_quote('a', pending['confirmation_id']))

    async def test_send_failure_does_not_delete_the_persisted_quote_or_replay_confirmation(self):
        if not font_dir():
            self.skipTest('requires DejaVu fonts (STAGE4_TEST_FONT_DIR)')
        self.files.side_effect = OSError('transport unavailable')
        await self.call()
        pending = self.tools.pending_quote('a')
        self.assertTrue(self.tools.confirm_quote('a', pending['confirmation_id']))
        self.assertEqual((await self.call())['error'], 'FILE_DELIVERY_FAILED')
        self.assertEqual(len(self.rows('SELECT * FROM quotes')), 1)
        self.assertEqual(len(list((self.root / 'pdfs').iterdir())), 1)
        self.assertFalse(self.tools.confirm_quote('a', pending['confirmation_id']))
        self.assertEqual((await self.call())['status'], 'confirmation_required')
        self.files.assert_awaited_once()

    async def test_escalation_updates_status_and_invokes_redirect_once(self):
        args = {'reason':'ORDER_STATUS','summary':'Нужен статус заказа','collected_slots':{'product':'Астра'}}
        for _ in range(2):
            self.assertEqual((await self.tools.execute('a', 'escalate_to_manager', args))['status'], 'escalated')
        row, = self.rows('SELECT * FROM escalations')
        self.assertEqual(json.loads(row['collected_slots_json']), args['collected_slots'])
        self.assertEqual(self.rows("SELECT status FROM chats WHERE chat_id='a'")[0][0], 'escalated')
        self.redirect.assert_awaited_once_with('a')
        self.assertEqual((await self.call())['error'], 'CHAT_NOT_ACTIVE')

    async def test_existing_quotes_kept_on_upgrade_with_new_exact_totals_in_kopecks(self):
        if not font_dir():
            self.skipTest('requires DejaVu fonts (STAGE4_TEST_FONT_DIR)')
        with connect(self.settings.db_path) as db, db:
            db.execute('DROP TABLE quotes')
            db.execute('''CREATE TABLE quotes (id TEXT PRIMARY KEY NOT NULL, chat_id TEXT NOT NULL REFERENCES chats(chat_id),
                items_json TEXT NOT NULL, discount_percent REAL NOT NULL CHECK(discount_percent BETWEEN 0 AND 5),
                total_sum REAL NOT NULL, pdf_url TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
            db.execute("INSERT INTO quotes(id,chat_id,items_json,discount_percent,total_sum,pdf_url) "
                       "VALUES ('legacy','a','[]',0,1.23,'/legacy.pdf')")
            db.execute("UPDATE offers SET price=999999999.99 WHERE id='1'")
        initialize(self.settings.db_path)
        initialize(self.settings.db_path)
        self.assertEqual((await self.call())['quote']['total'], '1999999999.98')
        self.assertTrue(self.tools.confirm_quote('a', self.tools.pending_quote('a')['confirmation_id']))
        result = await self.call()
        self.assertEqual(result['total_sum'], '1999999999.98')
        legacy, current = self.rows('SELECT id, total_sum, total_sum_kopecks, typeof(total_sum_kopecks) FROM quotes ORDER BY rowid')
        self.assertEqual(tuple(legacy), ('legacy', 1.23, None, 'null'))
        self.assertEqual((current[2], current[3]), (199999999998, 'integer'))

    async def test_quote_above_practical_total_limit_does_not_save_confirmation(self):
        with connect(self.settings.db_path) as db, db:
            db.execute("UPDATE offers SET price=1000000000 WHERE id='1'")
        result = await self.call(items=[{'offer_id': '1', 'quantity': 11}])
        self.assertEqual(result.get('error'), 'QUOTE_TOTAL_LIMIT_EXCEEDED')
        self.assertEqual(result['action_required'], 'escalate_to_manager')
        self.assertIsNone(self.tools.pending_quote('a'))

    async def test_quote_rejects_overlong_pdf_fields_without_persisting_proposal(self):
        with connect(self.settings.db_path) as db, db:
            db.execute("UPDATE offers SET name=? WHERE id='1'", ('Я' * 10_000,))
        result = await self.call()
        self.assertEqual(result.get('error'), 'QUOTE_ITEM_TOO_LONG')
        self.assertIsNone(self.tools.pending_quote('a'))
