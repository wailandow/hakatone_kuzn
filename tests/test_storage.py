import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest


class StorageTests(unittest.TestCase):
    def test_catalog_and_knowledge_fts_survive_reopening(self):
        from app.db import connect, initialize
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'data' / 'app.db'
            initialize(path)
            initialize(path)
            with connect(path) as db, db:
                db.execute('INSERT INTO offers(id,name,vendor_code,embedding) VALUES (?,?,?,?)', ('o1','Ред ОС','ABC-123',b'vector'))
                db.execute('INSERT INTO kb_articles(title,content,source) VALUES (?,?,?)', ('Доставка','электронная лицензия','delivery'))
            with connect(path) as db:
                self.assertEqual(db.execute('SELECT offer_id FROM offers_fts WHERE offers_fts MATCH ?', ('"ABC-123"',)).fetchone()[0], 'o1')
                self.assertEqual(db.execute('SELECT count(*) FROM kb_fts WHERE kb_fts MATCH ?', ('лицензия',)).fetchone()[0], 1)
                self.assertEqual(db.execute('SELECT embedding FROM offers').fetchone()[0], b'vector')
                for table in ('chats','messages','escalations','quotes','audit_log'):
                    self.assertIsNotNone(db.execute('SELECT name FROM sqlite_master WHERE name=?', (table,)).fetchone())
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute('INSERT INTO messages(chat_id,sender,kind) VALUES (?,?,?)', ('missing','client','visitor'))

    def test_replacing_offer_removes_previous_fts_entry(self):
        from app.db import connect, initialize
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'app.db'
            initialize(path)
            with connect(path) as db, db:
                db.execute("INSERT INTO offers(id,name) VALUES ('o1','oldterm')")
                db.execute("INSERT OR REPLACE INTO offers(id,name) VALUES ('o1','newterm')")
                self.assertEqual(db.execute("SELECT count(*) FROM offers_fts WHERE offers_fts MATCH 'oldterm'").fetchone()[0], 0)
                self.assertEqual(
                    [tuple(row) for row in db.execute("SELECT rowid, offer_id FROM offers_fts WHERE offers_fts MATCH 'newterm'")],
                    [tuple(row) for row in db.execute("SELECT rowid, id FROM offers WHERE id='o1'")],
                )

    def test_updating_offer_rowid_moves_fts_entry(self):
        from app.db import connect, initialize
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'app.db'
            initialize(path)
            with connect(path) as db, db:
                db.execute("INSERT INTO offers(rowid,id,name) VALUES (1,'o1','catalogterm')")
                db.execute("UPDATE offers SET rowid=2 WHERE id='o1'")
                self.assertEqual(
                    [tuple(row) for row in db.execute("SELECT rowid, offer_id FROM offers_fts WHERE offers_fts MATCH 'catalogterm'")],
                    [(2, 'o1')],
                )

    def test_updating_article_rowid_moves_fts_entry(self):
        from app.db import connect, initialize
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'app.db'
            initialize(path)
            with connect(path) as db, db:
                db.execute("INSERT INTO kb_articles(id,title,content,source) VALUES (1,'knowledgeterm','body','docs')")
                db.execute("UPDATE kb_articles SET rowid=2 WHERE id=1")
                self.assertEqual(
                    [tuple(row) for row in db.execute("SELECT rowid, article_id FROM kb_fts WHERE kb_fts MATCH 'knowledgeterm'")],
                    [(2, 2)],
                )

    def test_settings_read_dotenv_without_requiring_secrets(self):
        from app.config import Settings
        with TemporaryDirectory() as folder:
            env = Path(folder) / '.env'
            env.write_text('PUBLIC_BASE_URL=http://localhost:8123\n', encoding='utf-8')
            settings = Settings(_env_file=env)
            self.assertEqual(settings.public_base_url, 'http://localhost:8123')
            self.assertEqual(settings.db_path, Path('data/app.db'))
