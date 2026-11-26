from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import numpy as np

from app import db


class SearchTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / 'app.db'
        db.initialize(path)
        self.conn = self.enterContext(db.connect(path))

    def offer(self, id_, name, *, sku=None, group=None, vendor=None, russian=0, vector=None):
        blob = np.asarray(vector, dtype='<f4').tobytes() if vector is not None else None
        with self.conn:
            self.conn.execute('''INSERT INTO offers(id,name,vendor_code,group_id,vendor,is_russian_registry,embedding)
                                 VALUES (?,?,?,?,?,?,?)''', (id_, name, sku, group, vendor, russian, blob))

    def test_literal_sku_synonym_and_fts_syntax(self):
        self.offer('exact', 'Сканер', sku='SCANER-VS-7-8192-E-F')
        self.offer('partial', 'Сканер', sku='SCANER-VS-7-8192-E-F-PLUS')
        self.offer('red', 'RedCheck')
        self.assertEqual(db.search_catalog(self.conn, 'SCANER-VS-7-8192-E-F')[0]['id'], 'exact')
        results = db.search_catalog(self.conn, '"ред чек"*:', synonyms={'ред чек': 'RedCheck'})
        self.assertEqual([row['id'] for row in results], ['red'])
        self.assertEqual(db.search_catalog(self.conn, '"*:'), [])

    def test_original_sku_is_pinned_even_when_synonym_rewrites_fts_query(self):
        self.offer('exact', 'Сканер', sku='RED-CHECK', group='g', vendor='Вендор', russian=1, vector=[0, 1])
        self.offer('alias', 'RedCheck', group='g', vendor='Вендор', russian=1, vector=[1, 0])
        self.offer('wrong-group', 'Сканер', sku='RED-CHECK', group='other', vendor='Вендор', russian=1)
        self.offer('wrong-vendor', 'Сканер', sku='RED-CHECK', group='g', vendor='Другой', russian=1)
        self.offer('not-registered', 'Сканер', sku='RED-CHECK', group='g', vendor='Вендор')
        filters = dict(group_id='g', vendor='Вендор', russian_only=True, synonyms={'red check': 'RedCheck'})
        for vector in (None, [1, 0]):
            with self.subTest(vector=vector):
                rows = db.search_catalog(self.conn, ' red-check ', query_vector=vector, **filters)
                self.assertEqual([row['id'] for row in rows], ['exact', 'alias'])
                rows = db.search_catalog(self.conn, 'RED-CHECK', query_vector=vector, limit=1, **filters)
                self.assertEqual([row['id'] for row in rows], ['exact'])

    def test_exact_sku_does_not_require_any_fts_tokens(self):
        self.offer('exact', 'Сканер', sku='---')
        rows = db.search_catalog(self.conn, '---')
        self.assertEqual([row['id'] for row in rows], ['exact'])

    def test_catalog_rrf_combines_bm25_and_cosine_before_limiting(self):
        self.offer('a-lexical', 'Защита почты')
        self.offer('b-both', 'Лицензия: защита почты для отдела', vector=[3, 1])
        self.offer('c-semantic', 'MailGuard', vector=[1, 0])
        self.offer('d-zero', 'Не относится', vector=[0, 0])
        self.offer('e-wrong-dimension', 'Не относится', vector=[1, 2, 3])
        self.offer('f-nan', 'Не относится', vector=[np.nan, 1])
        query = 'защита почты'
        self.assertEqual(db.search_catalog(self.conn, query, limit=1)[0]['id'], 'a-lexical')
        self.assertEqual(db.search_catalog(self.conn, query, query_vector=[1, 0], limit=1)[0]['id'], 'b-both')
        self.assertEqual(db.search_catalog(self.conn, 'защитить e-mail', query_vector=[1, 0], limit=1)[0]['id'],
                         'c-semantic')
        found = db.search_catalog(self.conn, query, query_vector=[1, 0], limit=10)
        self.assertEqual({row['id'] for row in found}, {'a-lexical', 'b-both', 'c-semantic'})

    def test_catalog_filters_apply_to_both_channels_and_group_variants(self):
        self.offer('a', 'Продукт', group='g', vendor='Спец', russian=1, vector=[1, 1])
        self.offer('b', 'Продукт', group='g', vendor='Спец', vector=[1, 0])
        self.offer('c', 'Продукт', group='other', vendor='Спец', russian=1, vector=[1, 0])
        self.offer('d', 'Продукт', group='g', vendor='Другой', russian=1, vector=[1, 0])
        found = db.search_catalog(self.conn, 'продукт', group_id='g', vendor='Спец', russian_only=True,
                                  query_vector=[1, 0], limit=1)
        self.assertEqual([(row['id'], row['variant_count']) for row in found], [('a', 2)])
        self.assertEqual([row['id'] for row in db.search_catalog(self.conn, '', group_id='g')], ['a', 'b', 'd'])

    def test_search_uses_embedder_with_normalized_query_and_pins_exact_sku(self):
        self.offer('a', 'Другой продукт SCANER-VS-7', sku='SCANER-VS-7-A', vector=[1, 0])
        self.offer('b', 'Искомый сканер', sku='SCANER-VS-7', vector=[0, 1])
        self.offer('red', 'RedCheck', vector=[1, 0])
        self.assertEqual(db.search_catalog(self.conn, 'SCANER-VS-7', query_vector=[1, 0], limit=1)[0]['id'], 'b')

        class QueryEmbedder:
            def query(self, text):
                if text != 'RedCheck':
                    raise AssertionError(f'expected RedCheck, got {text}')
                return [1, 0]

        found = db.search_catalog(self.conn, 'ред-чек', embedder=QueryEmbedder(), synonyms={'ред чек': 'RedCheck'})
        self.assertEqual(found[0]['id'], 'red')

    def test_kb_search_reads_new_manager_chunks_in_both_channels(self):
        with self.conn:
            self.conn.execute("INSERT INTO kb_articles(title,content,source) VALUES ('Документы','Общие условия','docs')")
        self.assertEqual(db.search_kb(self.conn, 'активация'), [])
        with self.conn:
            self.conn.execute('INSERT INTO kb_articles(title,content,source,embedding) VALUES (?,?,?,?)',
                              ('Активация', 'Код поступает по электронной почте', 'manager_added',
                               np.asarray([1, 0], dtype='<f4').tobytes()))
        for query, options in [('активация', {}), ('ключ для программы', {'query_vector': [1, 0]}),
                               ('электронной почте', {'query_vector': [1, 0]})]:
            with self.subTest(query=query):
                rows = db.search_kb(self.conn, query, **options)
                self.assertEqual([(row['title'], row['source']) for row in rows], [('Активация', 'manager_added')])
