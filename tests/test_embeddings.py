from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import numpy as np

from app.db import connect
from scripts.build_db import build_index
from test_indexing import FEED


class TestBackend:
    def __init__(self):
        self.texts = []

    def embed(self, texts, *, batch_size):
        self.texts.extend(texts)
        return ([len(text), 1] for text in texts)


class EmbeddingTests(unittest.TestCase):
    def test_e5_passage_and_query_prefixes_and_sqlite_float32(self):
        try:
            from app.embeddings import E5Embedding
        except ModuleNotFoundError:
            self.fail('stage-2 E5 wrapper missing')
        model = TestBackend()
        embedder = E5Embedding(backend=model)
        with TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as folder:
            feed, db_path = (Path(folder) / name for name in ('feed.xml', 'app.db'))
            feed.write_text(FEED, encoding='utf-8')
            build_index(db_path, feed=feed, limit=1, embedder=embedder)
            with connect(db_path) as db:
                text, blob = db.execute('SELECT search_text, embedding FROM offers').fetchone()
            self.assertEqual(model.texts, ['passage: ' + text])
            np.testing.assert_array_equal(np.frombuffer(blob, dtype='<f4'), [len(model.texts[0]), 1])
        np.testing.assert_array_equal(embedder.query('Ред ОС'), [len('query: Ред ОС'), 1])
        self.assertEqual(model.texts[-1], 'query: Ред ОС')

    def test_seed_kb_stores_passage_embeddings(self):
        from app.embeddings import E5Embedding

        model = TestBackend()
        with TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as folder:
            root = Path(folder)
            db_path = root / 'app.db'
            build_index(db_path, kb_cache=root / 'cache',
                        kb_fetch=lambda url: '<title>Справка</title><main>Доставка лицензии</main>',
                        embedder=E5Embedding(backend=model))
            with connect(db_path) as db:
                rows = list(db.execute('SELECT embedding FROM kb_articles ORDER BY id'))
        self.assertEqual(model.texts, ['passage: Справка Доставка лицензии'] * 2)
        for row in rows:
            np.testing.assert_array_equal(np.frombuffer(row['embedding'], dtype='<f4'),
                                          [len(model.texts[0]), 1])

    def test_cli_defaults_to_embeddings_and_can_build_fts_only(self):
        from contextlib import redirect_stdout
        from io import StringIO
        from unittest.mock import patch
        from scripts.build_db import main

        model = TestBackend()
        with TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as folder:
            root = Path(folder)
            feed = root / 'feed.xml'
            feed.write_text(FEED, encoding='utf-8')
            with patch('app.embeddings._fastembed_backend', return_value=model) as create, redirect_stdout(StringIO()):
                for name, fts_only in [('offline.db', True), ('dense.db', False)]:
                    path = root / name
                    main(['--feed', str(feed), '--limit', '1', '--db', str(path)]
                         + (['--fts-only'] if fts_only else []))
                    with connect(path) as db:
                        row = db.execute('SELECT embedding FROM offers').fetchone()
                    self.assertEqual(row['embedding'] is None, fts_only)
                create.assert_called_once_with()

    def test_unlisted_fastembed_model_registers_the_official_e5_export_once(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from app.embeddings import E5Embedding

        model_name = 'intfloat/multilingual-e5-small'
        config = []
        backend = TestBackend()

        class TextEmbedding:
            @staticmethod
            def list_supported_models():
                return config

            @staticmethod
            def add_custom_model(**kwargs):
                config.append(kwargs)

            def __new__(cls, *, model_name, providers):
                if model_name != 'intfloat/multilingual-e5-small' or providers != ['CPUExecutionProvider']:
                    raise AssertionError((model_name, providers))
                return backend

        modules = {
            'fastembed': SimpleNamespace(TextEmbedding=TextEmbedding),
            'fastembed.common.model_description': SimpleNamespace(ModelSource=dict,
                                                                  PoolingType=SimpleNamespace(MEAN='MEAN')),
        }
        with patch.dict('sys.modules', modules):
            E5Embedding().query('почта')
            E5Embedding().query('лицензия')
        self.assertEqual(config, [dict(model=model_name, pooling='MEAN', normalization=True,
                                       sources={'hf': model_name}, model_file='onnx/model.onnx', dim=384)])
