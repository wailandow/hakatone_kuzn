"""Seed knowledge pages with an explicit online fetch and offline cache."""
from html.parser import HTMLParser
import json
from pathlib import Path
from tempfile import NamedTemporaryFile


PAGES = {
    'docs': 'https://docs.softline.com/ru-ru',
    'delivery': 'https://store.softline.ru/delivery_info/',
}


class PageText(HTMLParser):
    SKIP = {'head', 'script', 'style', 'nav', 'header', 'footer', 'noscript', 'template', 'svg'}
    BLOCK = {'article', 'div', 'br', 'p', 'li', 'td', 'section', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'}

    def __init__(self):
        super().__init__()
        self.skip = []
        self.in_title = False
        self.in_main = False
        self.title = []
        self.body = []
        self.main = []

    def handle_starttag(self, tag, attrs):
        if tag == 'title':
            self.in_title = True
        if tag in self.SKIP:
            self.skip.append(tag)
        if not self.skip:
            if tag == 'main':
                self.in_main = True
            if tag in self.BLOCK:
                self.handle_data(' ')

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag == 'title':
            self.in_title = False
        if self.skip and self.skip[-1] == tag:
            self.skip.pop()
        if tag in self.BLOCK:
            self.handle_data(' ')
        if tag == 'main':
            self.in_main = False

    def handle_data(self, data):
        if self.in_title:
            self.title.append(data)
        elif not self.skip:
            self.body.append(data)
            if self.in_main:
                self.main.append(data)


def chunks(content):
    """Balance chunks of 600–900 chars, with exactly 100 chars of overlap.

    A short page stays one chunk. Prefer a nearby word boundary when possible.
    """
    length = len(content)
    if length <= 900:
        if content:
            yield content
        return
    count = (length - 100 + 799) // 800
    start = 0
    for remaining in range(count - 1, 0, -1):
        low = max(start + 600, length - 800 * remaining)
        high = min(start + 900, length - 500 * remaining)
        target = min(high, max(low, start + 100 + (length - start - 100) // (remaining + 1)))
        boundary = content.rfind(' ', max(low, target - 35), target + 1)
        end = boundary if boundary >= low else target
        yield content[start:end]
        start = end - 100
    yield content[start:]


def fetch_page(url):
    import httpx

    with httpx.Client(timeout=20, follow_redirects=True) as client:
        with client.stream('GET', url) as response:
            response.raise_for_status()
            data = bytearray()
            for block in response.iter_bytes(chunk_size=65536):
                data.extend(block)
                if len(data) > 5 * 1024 * 1024:
                    raise ValueError(f'KB page exceeds size limit: {url}')
            return data.decode(response.encoding or 'utf-8', errors='replace')


def iter_pages(cache_dir, fetch=None):
    cache_dir = Path(cache_dir)
    for source, url in PAGES.items():
        cache = cache_dir / f'{source}.json'
        if cache.exists():
            page = json.loads(cache.read_text(encoding='utf-8'))
        elif fetch is not None:
            parser = PageText()
            parser.feed(fetch(url))
            parser.close()
            content = ' '.join(''.join(parser.main).split()) or ' '.join(''.join(parser.body).split())
            page = dict(title=' '.join(''.join(parser.title).split()) or url, content=content)
            if not page['content']:
                raise ValueError(f'KB page has no extractable text: {url}')
            cache_dir.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(mode='w', encoding='utf-8', dir=cache_dir, delete=False) as tmp:
                temp_path = Path(tmp.name)
                try:
                    json.dump(page, tmp, ensure_ascii=False)
                    tmp.close()
                    temp_path.replace(cache)
                finally:
                    tmp.close()
                    temp_path.unlink(missing_ok=True)
        else:
            raise FileNotFoundError(f'KB cache missing: {cache}; use --fetch-kb to populate it')
        if not isinstance(page.get('title'), str) or not isinstance(page.get('content'), str) or not page['content'].strip():
            raise ValueError(f'invalid KB cache: {cache}')
        yield source, url, page['title'], page['content']
