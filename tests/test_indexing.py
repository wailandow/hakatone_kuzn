import gzip
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app.db import connect


FEED = '''<yml_catalog xmlns="urn:test"><shop><offers>
<offer id="A" group_id="g" available="false"><name>Ред ОС</name><vendor>Ред Софт</vendor>
<vendorCode>ABC-123</vendorCode><price>2.50</price><currencyId>RUB</currencyId>
<description><![CDATA[<b>Для серверов</b>]]></description>
<param name="Номер записи в реестре отечественного ПО">231</param>
<param name="срок">год</param><param name="срок">два года</param>
<param name="пусто"> </param><param name="мусор">Другое или отсутствует</param>
<param name="неизвестно">Не определено</param></offer>
<offer id="B"><model>Защита (рег (№ 231))</model><price>Передайте запрос</price></offer>
<offer id="C"><name>Коробка № 231</name><param name="Номер записи в реестре отечественного ПО">не определено</param></offer>
</offers></shop></yml_catalog>'''


class IndexingTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.db = self.root / 'app.db'
        self.feed = self.root / 'catalog.xml'

    def test_streams_plain_and_gzip_offers_with_clean_params_and_registry(self):
        try:
            from scripts.build_db import iter_offers
        except ModuleNotFoundError:
            self.fail('stage-2 offer parser is missing')
        for data in (FEED.encode(), gzip.compress(FEED.encode())):
            self.feed.write_bytes(data)
            first, second, third = iter_offers(self.feed)
            self.assertEqual((first['id'], first['group_id'], first['available'], first['price'], first['currency']), ('A', 'g', 0, 2.5, 'RUB'))
            self.assertEqual(first['description'], 'Для серверов')
            params = json.loads(first['params_json'])
            self.assertEqual(set(params), {'срок', 'Номер записи в реестре отечественного ПО'})
            self.assertEqual(params['срок'], ['год', 'два года'])
            self.assertIn('Ред Софт Ред ОС ABC-123 Для серверов', first['search_text'])
            self.assertEqual([x['is_russian_registry'] for x in (first, second, third)], [1, 1, 0])
            self.assertIsNone(second['price'])

    def test_registry_record_number_in_description_sets_registry_flag(self):
        from scripts.build_db import iter_offers
        self.feed.write_text('''<yml_catalog><shop><offers>
            <offer id="registered"><name>Лицензия</name>
                <description>Номер записи в реестре отечественного ПО: 231</description></offer>
            <offer id="dash"><name>Лицензия</name>
                <description>Номер записи в реестре отечественного ПО — 231</description></offer>
            <offer id="space"><name>Лицензия</name>
                <description>Номер записи в реестре отечественного ПО 231</description></offer>
            <offer id="negated"><name>Лицензия</name>
                <description>Не входит в реестр № 231</description></offer>
            <offer id="unknown"><name>Лицензия</name>
                <description>Номер записи в реестре отечественного ПО: не определено</description></offer>
            <offer id="unrelated"><name>Лицензия</name>
                <description>Номер заказа: 231</description></offer>
            </offers></shop></yml_catalog>''', encoding='utf-8')
        flags = {row['id']: row['is_russian_registry'] for row in iter_offers(self.feed)}
        self.assertEqual(flags, {'registered': 1, 'dash': 1, 'space': 1, 'negated': 0,
                                 'unknown': 0, 'unrelated': 0})

    def test_bounded_build_stops_before_malformed_tail_and_updates_fts(self):
        try:
            from scripts.build_db import build_index
        except ModuleNotFoundError:
            self.fail('stage-2 index builder is missing')
        def write(name):
            self.feed.write_text(f'<yml_catalog><shop><offers><offer id="x"><name>{name}</name></offer>' + ' ' * 80000 + '<broken', encoding='utf-8')
        for name in ('primero', 'segundo'):
            write(name)
            self.assertEqual(build_index(self.db, feed=self.feed, limit=1)['offers'], 1)
        with connect(self.db) as db:
            self.assertEqual(tuple(db.execute('SELECT id, name, embedding FROM offers').fetchone()), ('x', 'segundo', None))
            self.assertEqual(db.execute("SELECT count(*) FROM offers_fts WHERE offers_fts MATCH 'primero'").fetchone()[0], 0)
        with self.assertRaises(ValueError):
            build_index(self.db, feed=self.feed, limit=-1)

    def test_knowledge_index_falls_back_to_body_when_main_normalizes_to_empty(self):
        from scripts.build_db import build_index
        cache = self.root / 'kb_cache'
        html = ('<title>Справка</title><body><nav>Меню</nav>'
                '<main> \n <div>&nbsp;</div><script>Скрытый текст</script></main>'
                '<article>Электронная лицензия</article></body>')
        try:
            stats = build_index(self.db, kb_cache=cache, kb_fetch=lambda _: html)
        except ValueError as exc:
            self.fail(f'Текст вне пустого main не проиндексирован: {exc}')
        self.assertEqual(stats['kb_chunks'], 2)
        with connect(self.db) as db:
            rows = db.execute('SELECT content FROM kb_articles ORDER BY id').fetchall()
        self.assertEqual([row['content'] for row in rows], ['Электронная лицензия'] * 2)

    def test_knowledge_cache_rebuild_is_offline_with_overlap_and_keeps_manager_articles(self):
        from scripts.build_db import build_index
        cache = self.root / 'kb_cache'
        body = ' '.join(f'Параграф {i}: электронная лицензия.' for i in range(63))
        fetched = []

        def fetch(url):
            fetched.append(url)
            return f'<title>Справка</title><nav>menuonly</nav><main><h1>Условия</h1><p>{body}</p><script>hiddenonly</script></main>'

        try:
            count = build_index(self.db, kb_cache=cache, kb_fetch=fetch)['kb_chunks']
        except TypeError as exc:
            self.fail(f'KB builder is missing: {exc}')
        self.assertEqual(set(fetched), {'https://docs.softline.com/ru-ru', 'https://store.softline.ru/delivery_info/'})
        with connect(self.db) as db, db:
            db.execute("INSERT INTO kb_articles(title,source,content) VALUES ('Менеджер','manager_added','Уточнение')")
        self.assertEqual(build_index(self.db, kb_cache=cache)['kb_chunks'], count)
        with connect(self.db) as db:
            docs = list(db.execute("SELECT title, url, content FROM kb_articles WHERE source='docs' ORDER BY id"))
            self.assertGreater(len(docs), 1)
            self.assertEqual(docs[0]['title'], 'Справка')
            self.assertEqual(docs[0]['url'], 'https://docs.softline.com/ru-ru')
            for row in docs:
                self.assertTrue(600 <= len(row['content']) <= 900)
                self.assertNotIn('hiddenonly', row['content'])
                self.assertNotIn('menuonly', row['content'])
            for a, b in zip(docs, docs[1:]):
                self.assertEqual(a['content'][-100:], b['content'][:100])
            self.assertEqual(db.execute("SELECT count(*) FROM kb_fts WHERE kb_fts MATCH 'лицензия'").fetchone()[0], count)
            self.assertEqual(db.execute("SELECT count(*) FROM kb_articles WHERE source='manager_added'").fetchone()[0], 1)
