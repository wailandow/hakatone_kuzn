"""Build the catalog and knowledge index: python -m scripts.build_db."""
import argparse
from contextlib import closing
import gzip
from html.parser import HTMLParser
from itertools import islice
import json
import math
from pathlib import Path
import re
import xml.etree.ElementTree as ET

from app.config import Settings
from app.db import connect, initialize
from app.embeddings import E5Embedding
from app.knowledge import chunks, fetch_page, iter_pages


REGISTRY_PARAM = 'номер записи в реестре отечественного по'
REGISTRY_NUMBER = re.compile(
    r'\b(?:рег\.?|реестр\w*(?:\s+отечественного\s+ПО)?)\s*[:(\s]*№\s*\d+\b'
    rf'|\b{REGISTRY_PARAM}(?:\s*[:—–-]\s*|\s+)№?\s*\d+\b', re.I
)
REGISTRY_NEGATION = re.compile(
    r'\b(?:не\s+(?:вход\w*|включ\w*|внес\w*|состо\w*|зарегистр\w*|числ\w*|знач\w*)'
    r'\s+(?:в\s+)?реестр\w*|отсутств\w*\s+в\s+реестр\w*)\b', re.I
)
JUNK_PARAM = {'другое или отсутствует', 'не определено'}


class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def _plain(text):
    parser = TextExtractor()
    parser.feed(text)
    parser.close()
    return ' '.join(' '.join(parser.parts).split())


def _tag(element):
    return element.tag.rsplit('}', 1)[-1]


def _offer(element):
    fields, params = {}, {}
    for child in element:
        tag = _tag(child)
        raw = ' '.join(child.itertext())
        value = _plain(raw) if tag == 'description' else ' '.join(raw.split())
        if tag != 'param':
            fields[tag] = value
            continue
        key = ' '.join(child.get('name', '').split())
        if not key or not value or value.casefold() in JUNK_PARAM:
            continue
        params.setdefault(key, [])
        if value not in params[key]:
            params[key].append(value)

    name = fields.get('name') or ' '.join(filter(None, (fields.get('vendor'), fields.get('model'))))
    offer_id = element.get('id', '').strip()
    if not offer_id or not name:
        return None
    vendor, code, desc = (fields.get(field) or None for field in ('vendor', 'vendorCode', 'description'))
    try:
        price = float(fields.get('price', '').replace(',', '.'))
        if not math.isfinite(price):
            price = None
    except ValueError:
        price = None
    registry_text = name + ' ' + (desc or '')
    registered = any(key.casefold() == REGISTRY_PARAM for key in params) or (
        bool(REGISTRY_NUMBER.search(registry_text)) and not REGISTRY_NEGATION.search(registry_text)
    )
    search_text = ' '.join(filter(None, (vendor, name, code, desc, *(
        f'{key} {" ".join(values)}' for key, values in params.items()
    ))))
    return dict(id=offer_id, group_id=element.get('group_id') or None, name=name,
                vendor=vendor, vendor_code=code, url=fields.get('url') or None,
                price=price, currency=fields.get('currencyId') or None,
                available=int(element.get('available', 'true').strip().lower() not in {'0', 'false'}),
                description=desc, params_json=json.dumps({k: v[0] if len(v) == 1 else v for k, v in params.items()}, ensure_ascii=False),
                is_russian_registry=int(registered), search_text=search_text, embedding=None)


def iter_offers(path, *, limit=None):
    """Yield valid catalog offers; limit bounds XML offers visited, even invalid ones."""
    if limit is not None and limit < 0:
        raise ValueError('limit must be non-negative')
    if limit == 0:
        return
    with Path(path).open('rb') as raw:
        stream = gzip.GzipFile(fileobj=raw) if raw.peek(2)[:2] == b'\x1f\x8b' else raw
        with stream:
            stack = []
            in_offer = False
            for event, element in ET.iterparse(stream, events=('start', 'end')):
                if event == 'start':
                    stack.append(element)
                    if _tag(element) == 'offer' and len(stack) > 1 and _tag(stack[-2]) == 'offers':
                        in_offer = True
                    continue
                stack.pop()
                is_offer = in_offer and _tag(element) == 'offer' and stack and _tag(stack[-1]) == 'offers'
                if is_offer:
                    in_offer = False
                    row = _offer(element)
                    if limit is not None:
                        limit -= 1
                if not in_offer and stack:
                    stack[-1].remove(element)
                    element.clear()
                if is_offer:
                    if row is not None:
                        yield row
                    if limit == 0:
                        return


UPSERT_OFFER = '''INSERT INTO offers(id,group_id,name,vendor,vendor_code,url,price,currency,
    available,description,params_json,is_russian_registry,search_text,embedding)
VALUES(:id,:group_id,:name,:vendor,:vendor_code,:url,:price,:currency,:available,:description,
    :params_json,:is_russian_registry,:search_text,:embedding)
ON CONFLICT(id) DO UPDATE SET group_id=excluded.group_id, name=excluded.name,
    vendor=excluded.vendor, vendor_code=excluded.vendor_code, url=excluded.url,
    price=excluded.price, currency=excluded.currency, available=excluded.available,
    description=excluded.description, params_json=excluded.params_json,
    is_russian_registry=excluded.is_russian_registry, search_text=excluded.search_text,
    embedding=excluded.embedding'''


def _batches(rows, size=128):
    rows = iter(rows)
    while batch := list(islice(rows, size)):
        yield batch


def build_index(path, *, feed=None, limit=None, kb_cache=None, kb_fetch=None, embedder=None):
    if limit is not None and limit < 0:
        raise ValueError('limit must be non-negative')
    initialize(path)
    stats = {'offers': 0, 'kb_chunks': 0}
    pages = list(iter_pages(kb_cache, kb_fetch)) if kb_cache is not None else []
    with connect(path) as db, db:
        if feed is not None:
            with closing(iter_offers(feed, limit=limit)) as rows:
                for batch in _batches(rows):
                    if embedder is not None:
                        vectors = embedder.passages(row['search_text'] for row in batch)
                        for row, vector in zip(batch, vectors, strict=True):
                            row['embedding'] = vector.tobytes()
                    db.executemany(UPSERT_OFFER, batch)
                    stats['offers'] += len(batch)
        for source, url, title, content in pages:
            db.execute('DELETE FROM kb_articles WHERE source=? AND url=?', (source, url))
            for batch in _batches(chunks(content)):
                blobs = ([vector.tobytes() for vector in embedder.passages(f'{title} {text}' for text in batch)]
                         if embedder is not None else [None] * len(batch))
                rows = [(title, url, text, source, blob) for text, blob in zip(batch, blobs, strict=True)]
                db.executemany('INSERT INTO kb_articles(title,url,content,source,embedding) VALUES (?,?,?,?,?)', rows)
                stats['kb_chunks'] += len(rows)
    return stats


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--feed', type=Path)
    parser.add_argument('--db', type=Path, default=Settings().db_path)
    parser.add_argument('--limit', type=int, help='maximum number of feed offers read')
    parser.add_argument('--kb-cache', type=Path, help='rebuild KB from this local cache')
    parser.add_argument('--fetch-kb', action='store_true', help='fetch only missing seed pages')
    parser.add_argument('--fts-only', action='store_true', help='build selected rows without dense embeddings')
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 0:
        parser.error('--limit must be non-negative')
    kb_cache = args.kb_cache or (Path('raw_data/kb_cache') if args.fetch_kb else None)
    if args.feed is None and kb_cache is None:
        parser.error('provide --feed, --kb-cache or --fetch-kb')
    print(json.dumps(build_index(args.db, feed=args.feed, limit=args.limit,
                                 kb_cache=kb_cache, kb_fetch=fetch_page if args.fetch_kb else None,
                                 embedder=None if args.fts_only else E5Embedding())))


if __name__ == '__main__':
    main()
