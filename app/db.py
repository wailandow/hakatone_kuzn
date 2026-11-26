from contextlib import contextmanager
from pathlib import Path
import sqlite3


@contextmanager
def connect(path):
    db = sqlite3.connect(path, timeout=15)
    try:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys = ON')
        # REPLACE fires DELETE triggers for the displaced row only with this enabled.
        db.execute('PRAGMA recursive_triggers = ON')
        yield db
    finally:
        db.close()


def initialize(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as db:
        db.executescript(Path(__file__).with_name('schema.sql').read_text(encoding='utf-8'))
