from contextlib import contextmanager
from pathlib import Path
import re
import sqlite3

import numpy as np

from app.embeddings import VECTOR_DTYPE


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


_TOKEN = re.compile(r'[^\W_]+')
_OFFER_FIELDS = '''doc.id, doc.group_id, doc.vendor, doc.vendor_code, doc.name, doc.price,
    doc.currency, doc.url, doc.available, doc.is_russian_registry, doc.params_json,
    CASE WHEN doc.group_id IS NULL THEN 0 ELSE
        (SELECT count(*) - 1 FROM offers AS variant WHERE variant.group_id = doc.group_id)
    END AS variant_count'''


def _normalize_query(query, synonyms):
    if not synonyms:
        return query.strip()
    # Replace whole phrases once, longest first, without cascading aliases.
    normalized = {tuple(_TOKEN.findall(key.casefold())): value for key, value in synonyms.items()}
    aliases = sorted(filter(None, normalized), key=len, reverse=True)
    if not aliases:
        return query.strip()
    alternatives = '|'.join(r'[\W_]+'.join(map(re.escape, key)) for key in aliases)
    pattern = rf'(?<![^\W_])(?:{alternatives})(?![^\W_])'
    return re.sub(pattern, lambda match: normalized[tuple(_TOKEN.findall(match[0]))], query.casefold()).strip()


def _match_expression(query):
    return ' AND '.join(f'"{token}"' for token in dict.fromkeys(_TOKEN.findall(query)))


def _catalog_filters(group_id, vendor, russian_only):
    clauses, params = [], []
    for field, value in (('group_id', group_id), ('vendor', vendor),
                         ('is_russian_registry', 1 if russian_only else None)):
        if value is not None:
            clauses.append(f'doc.{field} = ?')
            params.append(value)
    return ''.join(' AND ' + clause for clause in clauses), params


def _dense_ids(db, table, filters, params, vector, limit):
    vector = np.asarray(vector, dtype=np.float64)
    if vector.ndim != 1 or not vector.size or not np.isfinite(vector).all():
        raise ValueError('query_vector must be a finite one-dimensional nonempty vector')
    scale = np.abs(vector).max()
    if not scale:
        return []
    vector = vector / scale
    vector /= np.linalg.norm(vector)
    byte_count = vector.size * VECTOR_DTYPE.itemsize
    cursor = db.execute(f'''SELECT doc.id, doc.embedding FROM {table} AS doc
        WHERE typeof(doc.embedding) = 'blob' AND length(doc.embedding) = ? {filters}
        ORDER BY doc.id''', [byte_count, *params])
    best = []
    while batch := cursor.fetchmany(2048):
        matrix = np.frombuffer(b''.join(row[1] for row in batch), dtype=VECTOR_DTYPE).reshape(-1, vector.size)
        finite = np.flatnonzero(np.isfinite(matrix).all(axis=1))
        matrix = matrix[finite].astype(np.float64)
        norms = np.linalg.norm(matrix, axis=1)
        valid = norms > 0
        scores = (matrix[valid] @ vector) / norms[valid]
        ids = [batch[i][0] for i in finite[valid]]
        top = np.argsort(-scores, kind='stable')[:limit]
        best.extend((-float(scores[i]), ids[i]) for i in top)
        best = sorted(best)[:limit]
    return [id_ for _, id_ in best]


def _fuse(lexical, dense, limit):
    scores = {}
    for ranks in (lexical, dense):
        for rank, id_ in enumerate(ranks, 1):
            scores[id_] = scores.get(id_, 0.0) + 1 / (60 + rank)
    lexical_ranks = {id_: index for index, id_ in enumerate(lexical)}
    return sorted(scores, key=lambda id_: (-scores[id_], lexical_ranks.get(id_, len(lexical)), id_))[:limit]


def _get_rows(db, table, fields, ids):
    # Keep large result sets below the SQLite parameter limit.
    result = []
    for start in range(0, len(ids), 500):
        batch = ids[start:start + 500]
        marks = ','.join('?' for _ in batch)
        rows = {row['id']: row for row in db.execute(
            f'SELECT {fields} FROM {table} AS doc WHERE doc.id IN ({marks})', batch)}
        result.extend(rows[id_] for id_ in batch if id_ in rows)
    return result


def _query_vector(normalized, embedder, query_vector):
    if embedder is not None and query_vector is not None:
        raise ValueError('pass either embedder or query_vector, not both')
    return embedder.query(normalized) if embedder is not None else query_vector


def search_catalog(db, query, *, group_id=None, vendor=None, russian_only=False,
                   limit=6, synonyms=None, embedder=None, query_vector=None):
    """Pin exact SKU matches, then fuse BM25 and optional E5 cosine rankings.

    An empty query with group_id lists that group's variants.
    """
    if limit <= 0:
        return []
    filters, params = _catalog_filters(group_id, vendor, russian_only)
    original = query.strip()
    if not original:
        if group_id is None:
            return []
        return list(db.execute(f'SELECT {_OFFER_FIELDS} FROM offers AS doc WHERE 1=1 {filters} ORDER BY doc.id LIMIT ?',
                               [*params, limit]))
    exact_ids = [row[0] for row in db.execute(f'''SELECT doc.id FROM offers AS doc
        WHERE doc.vendor_code = ? COLLATE NOCASE {filters}
        ORDER BY doc.id LIMIT ?''', [original, *params, limit])]
    normalized = _normalize_query(query, synonyms)
    match = _match_expression(normalized)
    if not match:
        return _get_rows(db, 'offers', _OFFER_FIELDS, exact_ids)
    vector = _query_vector(normalized, embedder, query_vector)
    candidates = max(50, limit) if vector is not None else limit
    lexical_ids = [row[0] for row in db.execute(f'''SELECT doc.id
        FROM offers_fts JOIN offers AS doc ON doc.rowid = offers_fts.rowid
        WHERE offers_fts MATCH ? {filters}
        ORDER BY bm25(offers_fts, 0, 5, 2, 8, 1, 1), doc.id LIMIT ?''', [match, *params, candidates])]
    ranked_ids = lexical_ids
    if vector is not None:
        dense = _dense_ids(db, 'offers', filters, params, vector, candidates)
        ranked_ids = _fuse(lexical_ids, dense, limit)
    ids = list(dict.fromkeys(exact_ids + ranked_ids))[:limit]
    return _get_rows(db, 'offers', _OFFER_FIELDS, ids)


def search_kb(db, query, *, limit=4, embedder=None, query_vector=None):
    """Search all knowledge sources, including newly committed manager articles."""
    match = _match_expression(query)
    if not match or limit <= 0:
        return []
    vector = _query_vector(query.strip(), embedder, query_vector)
    candidates = max(50, limit) if vector is not None else limit
    lexical = [row[0] for row in db.execute('''SELECT doc.id FROM kb_fts
        JOIN kb_articles AS doc ON doc.id = kb_fts.rowid WHERE kb_fts MATCH ?
        ORDER BY bm25(kb_fts, 0, 4, 1), doc.id LIMIT ?''', [match, candidates])]
    if vector is not None:
        lexical = _fuse(lexical, _dense_ids(db, 'kb_articles', '', [], vector, candidates), limit)
    return _get_rows(db, 'kb_articles', 'doc.id, doc.title, doc.url, doc.content, doc.source', lexical)
