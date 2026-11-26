"""Stage 4 tool dispatch; confirmation is exclusively a trusted adapter action."""
import asyncio
from decimal import Decimal, InvalidOperation
import json
from itertools import islice
import logging
import sqlite3
from threading import BoundedSemaphore, Event
from time import monotonic
from uuid import uuid4

from app import db
from app.pdf.kp_generator import QuoteLine, generate_pdf

DEFAULT_PAYMENT = 'Предоплата 100%'
DEFAULT_DELIVERY = 'В соответствии с условиями поставки вендора (обычно 1-5 рабочих дней)'
MANAGER_REPLY = 'Подключу менеджера, чтобы он помог с вашим запросом.'
TRANSPORT_TIMEOUT = 8.0
MAX_QUOTE_ITEMS = 12
MAX_QUOTE_QUANTITY = 1_000_000
MAX_QUOTE_TOTAL = Decimal('10000000000.00')
logger = logging.getLogger(__name__)
_SEARCH_SLOT = BoundedSemaphore(1)  # Process-wide; the shared embedder can initialize lazily.


async def _run_search(handler, arguments):
    if not _SEARCH_SLOT.acquire(blocking=False):
        return {'error': 'SEARCH_BUSY'}
    cancelled = Event()

    def run():
        try:
            if not cancelled.is_set():
                return handler(**arguments, _cancelled=cancelled.is_set)
        finally:
            _SEARCH_SLOT.release()

    try:
        work = asyncio.get_running_loop().run_in_executor(None, run)
    except BaseException:
        _SEARCH_SLOT.release()
        raise
    try:
        # Cancellation abandons the result, not the slot or its owning worker.
        return await asyncio.shield(work)
    except asyncio.CancelledError:
        cancelled.set()
        work.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        raise


def _object(properties, required=()):
    return {'type': 'object', 'properties': properties, 'required': list(required), 'additionalProperties': False}


def _tool(name, description, properties, required):
    return {'type': 'function', 'function': {'name': name, 'description': description,
                                            'parameters': _object(properties, required)}}


_STR = {'type': 'string'}
_INT = {'type': 'integer'}
_ITEM = _object({'offer_id': {**_STR, 'maxLength': 240},
                 'quantity': {**_INT, 'minimum': 1, 'maximum': MAX_QUOTE_QUANTITY}}, ('offer_id', 'quantity'))
TOOL_SCHEMAS = [
    _tool('search_catalog', 'Поиск товаров и вариантов; пустой запрос допустим с group_id.',
          {'query': _STR, 'group_id': {'type': ['string', 'null']}, 'vendor': {'type': ['string', 'null']},
           'russian_only': {'type': 'boolean'}, 'limit': {**_INT, 'minimum': 1, 'maximum': 6}}, ('query',)),
    _tool('search_kb', 'Поиск по базе знаний, включая статьи менеджера.',
          {'query': _STR, 'limit': {**_INT, 'minimum': 1, 'maximum': 4}}, ('query',)),
    _tool('generate_kp_pdf', 'Подготовить состав КП для подтверждения или выпустить подтверждённый PDF. '
          'Не передавай цены, пути или факт подтверждения; подтверждение регистрирует интерфейс.',
          {'items': {'type': 'array', 'minItems': 1, 'maxItems': MAX_QUOTE_ITEMS, 'items': _ITEM},
           'discount_percent': {'type': 'number'}, 'payment_terms': {**_STR, 'default': DEFAULT_PAYMENT},
           'delivery_terms': {**_STR, 'default': DEFAULT_DELIVERY}}, ('items',)),
    _tool('escalate_to_manager', 'Передать диалог менеджеру.',
          {'reason': _STR, 'summary': _STR, 'collected_slots': {'type': 'object'}},
          ('reason', 'summary', 'collected_slots')),
]
_SCHEMAS = {tool['function']['name']: tool['function']['parameters'] for tool in TOOL_SCHEMAS}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _text(value, max_length):
    return isinstance(value, str) and 0 < len(value) <= max_length and bool(value.strip())


def _manager_error(code, **extra):
    return {'error': code, **extra, 'action_required': 'escalate_to_manager'}


def _discount(value):
    if type(value) not in (int, float, str, Decimal):
        raise ValueError
    if isinstance(value, str) and len(value) > 64:
        raise ValueError
    discount = Decimal(str(value))
    if not discount.is_finite():
        raise ValueError
    return discount


def _active(conn, chat_id):
    return conn.execute("SELECT 1 FROM chats WHERE chat_id=? AND status='bot'", (chat_id,)).fetchone() is not None


class AgentTools:
    def __init__(self, settings, *, embedder=None, send_file=None, redirect_chat=None, send_text=None):
        self.settings = settings
        self.embedder = embedder
        self.send_file = send_file  # async (chat_id, public_url)
        self.redirect_chat = redirect_chat  # async (chat_id)
        self.send_text = send_text  # async (chat_id, text)

    def audit(self, chat_id, event_type, payload, *, prompt_tokens=None, completion_tokens=None,
              cost_usd=None, latency_ms=None):
        try:
            with db.connect(self.settings.db_path, timeout=0.1) as conn, conn:
                conn.execute('''INSERT INTO audit_log(chat_id,event_type,payload_json,prompt_tokens,completion_tokens,
                    cost_usd,latency_ms) VALUES (?,?,?,?,?,?,?)''',
                    (chat_id, event_type, _json(payload), prompt_tokens, completion_tokens, cost_usd, latency_ms))
        except (sqlite3.Error, OverflowError) as exc:
            logger.warning('Audit write failed (%s, %s)', event_type, type(exc).__name__)

    async def execute(self, chat_id, name, arguments):
        start = monotonic()
        schema = _SCHEMAS.get(name)
        if schema is None:
            result = {'error': 'UNKNOWN_TOOL'}
        elif (not isinstance(arguments, dict) or set(arguments) - schema['properties'].keys()
              or not set(schema['required']) <= arguments.keys()):
            result = {'error': 'INVALID_ARGUMENTS'}
        else:
            try:
                if name == 'generate_kp_pdf':
                    result = await self.generate_kp_pdf(chat_id, **arguments)
                elif name == 'escalate_to_manager':
                    result = await self.escalate_to_manager(chat_id, **arguments)
                else:
                    handler = self.search_catalog if name == 'search_catalog' else self.search_kb
                    result = await _run_search(handler, arguments)
            except Exception as exc:
                self.audit(chat_id, 'error', {'stage': 'tool', 'tool': name, 'exception': type(exc).__name__})
                result = _manager_error('TOOL_FAILURE')
        self.audit(chat_id, 'tool_call', {'tool': name, 'outcome':
                   result.get('error', result.get('status', 'ok')) if isinstance(result, dict) else 'ok'},
                   latency_ms=int((monotonic() - start) * 1000))
        return result

    def search_catalog(self, query, group_id=None, vendor=None, russian_only=False, limit=6, *, _cancelled=None):
        if (not isinstance(query, str) or len(query) > 500 or type(russian_only) is not bool
                or type(limit) is not int or limit < 1
                or any(value is not None and not _text(value, 240) for value in (group_id, vendor))):
            return {'error': 'INVALID_ARGUMENTS'}
        with db.connect(self.settings.db_path, timeout=2) as conn:
            if _cancelled is not None:
                conn.set_progress_handler(_cancelled, 1000)
            rows = db.search_catalog(conn, query, group_id=group_id, vendor=vendor,
                                     russian_only=russian_only, limit=min(6, limit), embedder=self.embedder,
                                     cancelled=_cancelled)
        result = []
        for row in rows:
            card = dict(row)
            if any(isinstance(card[field], str) and len(card[field]) > 240
                   for field in ('id', 'group_id', 'vendor_code')):
                continue  # Never return a partial identifier the model could reuse as exact.
            for field, limit_ in (('name', 240), ('vendor', 120), ('url', 512), ('currency', 12)):
                value = card[field]
                if isinstance(value, str) and len(value) > limit_:
                    card[field] = None if field in ('url', 'currency') else value[:limit_]
            try:
                raw_params = card.pop('params_json')
                params = json.loads(raw_params) if isinstance(raw_params, str) and len(raw_params) <= 16_000 else {}
            except (ValueError, RecursionError):
                params = {}
            card['key_params'] = {k[:80]: str(v)[:160] for k, v in islice(params.items(), 6)} if isinstance(params, dict) else {}
            card['is_russian_registry'] = card['is_russian_registry'] == 1
            card['available'] = card['available'] == 1
            if isinstance(card['price'], float) and not Decimal(str(card['price'])).is_finite():
                card['price'] = None
            result.append(card)
        return result

    def search_kb(self, query, limit=4, *, _cancelled=None):
        if not _text(query, 500) or type(limit) is not int or limit < 1:
            return {'error': 'INVALID_ARGUMENTS'}
        with db.connect(self.settings.db_path, timeout=2) as conn:
            if _cancelled is not None:
                conn.set_progress_handler(_cancelled, 1000)
            rows = db.search_kb(conn, query, limit=min(4, limit), embedder=self.embedder, cancelled=_cancelled)
        return [dict(row) | {'title': row['title'][:240], 'content': row['content'][:1200],
                             'url': row['url'] if row['url'] and len(row['url']) <= 512 else None}
                for row in rows]

    def pending_quote(self, chat_id):
        """Trusted adapter only: display this exact proposal with an explicit confirmation action."""
        with db.connect(self.settings.db_path, timeout=2) as conn:
            row = conn.execute('''SELECT confirmation_id,proposal_json,confirmed FROM quote_confirmations
                JOIN chats USING(chat_id) WHERE chat_id=? AND status='bot' ''', (chat_id,)).fetchone()
        if row:
            return {'confirmation_id': row['confirmation_id'], 'quote': json.loads(row['proposal_json']),
                    'confirmed': bool(row['confirmed'])}
        return None

    def confirm_quote(self, chat_id, confirmation_id):
        """Trusted adapter only: call on an authenticated customer action for this displayed ID."""
        with db.connect(self.settings.db_path) as conn, conn:
            cursor = conn.execute('''UPDATE quote_confirmations SET confirmed=1
                WHERE chat_id=? AND confirmation_id=? AND confirmed=0
                AND EXISTS(SELECT 1 FROM chats WHERE chat_id=? AND status='bot')''',
                                  (chat_id, confirmation_id, chat_id))
            return cursor.rowcount == 1

    async def generate_kp_pdf(self, chat_id, items, discount_percent=0, payment_terms=DEFAULT_PAYMENT,
                              delivery_terms=DEFAULT_DELIVERY):
        try:
            discount = _discount(discount_percent)
        except (ValueError, InvalidOperation):
            return {'error': 'INVALID_ARGUMENTS'}
        if not 0 <= discount <= 5:
            return _manager_error('DISCOUNT_LIMIT_EXCEEDED', max_allowed=5.0)
        if discount != discount.quantize(Decimal('0.01')):
            return {'error': 'INVALID_ARGUMENTS'}
        if not isinstance(items, list) or not items:
            return {'error': 'INVALID_ARGUMENTS'}
        if len(items) > MAX_QUOTE_ITEMS:
            return _manager_error('QUOTE_ITEM_LIMIT_EXCEEDED', max_allowed=MAX_QUOTE_ITEMS)
        quantities = {}
        for item in items:
            if (not isinstance(item, dict) or item.keys() != {'offer_id', 'quantity'}
                    or not _text(item['offer_id'], 240) or len(_json(item['offer_id']).encode('utf-8')) > 256
                    or type(item['quantity']) is not int
                    or not 1 <= item['quantity'] <= MAX_QUOTE_QUANTITY or item['offer_id'] in quantities):
                return {'error': 'INVALID_ARGUMENTS'}
            quantities[item['offer_id']] = item['quantity']
        if not _text(payment_terms, 240) or not _text(delivery_terms, 240):
            return {'error': 'INVALID_ARGUMENTS'}
        if payment_terms != DEFAULT_PAYMENT or delivery_terms != DEFAULT_DELIVERY:
            return _manager_error('SPECIAL_TERMS')
        result = await asyncio.to_thread(self._quote, chat_id, quantities, discount, payment_terms, delivery_terms)
        if result.get('status') == 'generated' and self.send_file is not None:
            try:
                async with asyncio.timeout(TRANSPORT_TIMEOUT):
                    await self.send_file(chat_id, result['pdf_url'])
            except Exception as exc:
                self.audit(chat_id, 'error', {'stage': 'send_file', 'exception': type(exc).__name__})
                return _manager_error('FILE_DELIVERY_FAILED', **result)
        return result

    def _quote(self, chat_id, quantities, discount, payment, delivery):
        # Reserve writes through catalog read, confirmation consumption and quote insert.
        path = None
        try:
            with db.connect(self.settings.db_path, timeout=2) as conn, conn:
                conn.execute('BEGIN IMMEDIATE')
                if not _active(conn, chat_id):
                    return {'error': 'CHAT_NOT_ACTIVE'}
                return_result = self._proposal(conn, quantities, discount, payment, delivery)
                if isinstance(return_result, dict):
                    return return_result
                proposal, lines = return_result
                encoded = _json(proposal)
                pending = conn.execute('SELECT proposal_json, confirmed FROM quote_confirmations WHERE chat_id=?',
                                       (chat_id,)).fetchone()
                if not pending or pending['proposal_json'] != encoded:
                    conn.execute('''INSERT INTO quote_confirmations(chat_id,confirmation_id,proposal_json)
                        VALUES (?,?,?) ON CONFLICT(chat_id) DO UPDATE SET confirmation_id=excluded.confirmation_id,
                        proposal_json=excluded.proposal_json,confirmed=0''', (chat_id, uuid4().hex, encoded))
                    return {'status': 'confirmation_required', 'quote': proposal}
                if not pending['confirmed']:
                    return {'status': 'confirmation_required', 'quote': proposal}

                quote_id = uuid4().hex
                path = self.settings.db_path.parent / 'pdfs' / f'kp_{quote_id}.pdf'
                url = f'{self.settings.public_base_url.rstrip("/")}/pdfs/{path.name}'
                total = generate_pdf(path, self.settings.font_dir, lines, discount, payment, delivery)
                conn.execute('''INSERT INTO quotes(id,chat_id,items_json,discount_percent,total_sum,pdf_url,total_sum_kopecks)
                    VALUES (?,?,?,?,?,?,?)''', (quote_id, chat_id, _json(proposal['items']), str(discount),
                                                str(total), url, int(total * 100)))
                conn.execute('DELETE FROM quote_confirmations WHERE chat_id=?', (chat_id,))
        except Exception:
            if path is not None:
                path.unlink(missing_ok=True)
            raise
        return {'status': 'generated', 'pdf_url': url, 'total_sum': f'{total:.2f}'}

    @staticmethod
    def _proposal(conn, quantities, discount, payment, delivery):
        items, lines = [], []
        total = Decimal('0.00')
        for offer_id, quantity in sorted(quantities.items()):
            row = conn.execute('''SELECT name,vendor_code,price,currency,available,is_russian_registry
                FROM offers WHERE id=?''', (offer_id,)).fetchone()
            if row is None:
                return {'error': 'OFFER_NOT_FOUND', 'offer_id': offer_id}
            if row['available'] != 1:
                return {'error': 'OFFER_UNAVAILABLE', 'offer_id': offer_id}
            if row['currency'] not in ('RUB', 'RUR'):
                return _manager_error('UNSUPPORTED_CURRENCY', offer_id=offer_id)
            try:
                price = Decimal(str(row['price']))
            except InvalidOperation:
                return _manager_error('INVALID_CATALOG_PRICE', offer_id=offer_id)
            if not price.is_finite() or not 0 < price <= MAX_QUOTE_TOTAL:
                return _manager_error('INVALID_CATALOG_PRICE', offer_id=offer_id)
            sku, name = row['vendor_code'] or '—', row['name']
            if not _text(sku, 96) or not _text(name, 240) or any(ord(ch) < 32 for ch in sku + name):
                return _manager_error('QUOTE_ITEM_TOO_LONG', offer_id=offer_id)
            line = QuoteLine(sku, name, quantity, price, row['is_russian_registry'] == 1)
            unit, subtotal, vat = line.amounts(discount)
            if unit <= 0:
                return _manager_error('INVALID_CATALOG_PRICE', offer_id=offer_id)
            lines.append(line)
            total += subtotal
            if total > MAX_QUOTE_TOTAL:
                return _manager_error('QUOTE_TOTAL_LIMIT_EXCEEDED')
            items.append({'offer_id': offer_id, 'quantity': quantity, 'name': line.name, 'vendor_code': line.sku,
                          'catalog_price': format(price.normalize(), 'f'), 'unit_price': f'{unit:.2f}',
                          'subtotal': f'{subtotal:.2f}', 'vat': f'{vat:.2f}', 'registry': line.registry})
        return ({'items': items, 'discount_percent': format(discount.normalize(), 'f'), 'payment_terms': payment,
                 'delivery_terms': delivery, 'currency': 'RUB', 'total': f'{total:.2f}'}, lines)

    async def escalate_to_manager(self, chat_id, reason, summary, collected_slots):
        if not _text(reason, 240) or not _text(summary, 2000) or not isinstance(collected_slots, dict):
            return {'error': 'INVALID_ARGUMENTS'}
        try:
            payload = _json(collected_slots)
        except (ValueError, TypeError, RecursionError):
            return {'error': 'INVALID_ARGUMENTS'}
        if len(payload) > 4000:
            return {'error': 'INVALID_ARGUMENTS'}
        if self.send_text is not None:
            with db.connect(self.settings.db_path, timeout=2) as conn:
                notify = _active(conn, chat_id)
            if notify:
                try:
                    async with asyncio.timeout(TRANSPORT_TIMEOUT):
                        await self.send_text(chat_id, MANAGER_REPLY)
                except Exception as exc:
                    self.audit(chat_id, 'error', {'stage': 'escalate_notice', 'exception': type(exc).__name__})
        with db.connect(self.settings.db_path, timeout=2) as conn, conn:
            changed = conn.execute("UPDATE chats SET status='escalated' WHERE chat_id=? AND status='bot'", (chat_id,))
            if changed.rowcount:
                conn.execute('INSERT INTO escalations(chat_id,reason,summary,collected_slots_json) VALUES (?,?,?,?)',
                             (chat_id, reason, summary, payload))
                conn.execute('DELETE FROM quote_confirmations WHERE chat_id=?', (chat_id,))
                if self.redirect_chat is not None:
                    conn.execute("INSERT INTO escalation_redirects(chat_id,status) VALUES (?,'pending') "
                                 "ON CONFLICT(chat_id) DO UPDATE SET status='pending'", (chat_id,))
                else:
                    conn.execute('DELETE FROM escalation_redirects WHERE chat_id=?', (chat_id,))
            else:
                row = conn.execute('SELECT status FROM chats WHERE chat_id=?', (chat_id,)).fetchone()
                if not row or row['status'] not in ('escalated', 'manager'):
                    return {'error': 'CHAT_NOT_ACTIVE'}
                if row['status'] == 'manager':
                    return {'status': 'escalated', 'redirect': 'sent'}
        return await self.retry_redirect(chat_id) or {'status': 'escalated', 'redirect': 'local'}

    async def retry_redirect(self, chat_id):
        """Retry only persisted remote handoffs; adapter serializes per chat."""
        with db.connect(self.settings.db_path, timeout=2) as conn:
            row = conn.execute('''SELECT escalation_redirects.status FROM escalation_redirects JOIN chats USING(chat_id)
                WHERE chat_id=? AND chats.status='escalated' ''', (chat_id,)).fetchone()
        if not row:
            return None
        if row['status'] == 'sent':
            return {'status': 'escalated', 'redirect': 'sent'}
        if self.redirect_chat is None:
            return {'status': 'escalated', 'redirect': 'failed'}
        try:
            async with asyncio.timeout(TRANSPORT_TIMEOUT):
                await self.redirect_chat(chat_id)
        except Exception as exc:
            self.audit(chat_id, 'error', {'stage': 'redirect_chat', 'exception': type(exc).__name__})
            return {'status': 'escalated', 'redirect': 'failed'}
        try:
            with db.connect(self.settings.db_path, timeout=2) as conn, conn:
                conn.execute("UPDATE escalation_redirects SET status='sent' WHERE chat_id=?", (chat_id,))
        except sqlite3.Error as exc:
            # Delivery succeeded; the adapter must tolerate repeated redirects after ambiguous writes.
            logger.warning('Redirect state write failed (%s)', type(exc).__name__)
        return {'status': 'escalated', 'redirect': 'sent'}
