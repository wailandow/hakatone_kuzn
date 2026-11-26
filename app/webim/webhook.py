"""Persist authenticated callbacks before ACK; drain one FIFO per chat."""
import asyncio
import hashlib
import hmac
import json
import logging
import sqlite3

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.db import connect
from app.webim.client import IneligibleChat
from app.webim.schemas import NewChat, authorized, incoming, matches, page_context, parse_body

router = APIRouter(include_in_schema=False)
logger = logging.getLogger(__name__)


class DemoScopeError(ValueError):
    pass


@router.post('/webhook/webim')
@router.post('/webhook/webim/{secret}')
async def webhook(request: Request, secret: str | None = None):
    service = request.app.state.webhook
    expected = service.settings.webim_webhook_token
    allowed = matches(expected, secret) if secret is not None else authorized(expected, request.headers.get('authorization'))
    if not allowed:
        return JSONResponse({'error': 'unauthorized'}, status_code=403)
    event = await parse_body(request, incoming.validate_python)
    if event is None:
        return JSONResponse({'error': 'incorrect-request'}, status_code=400)
    try:
        chat_id = await asyncio.to_thread(service.accept, event)
    except DemoScopeError:
        return JSONResponse({'error': 'unauthorized'}, status_code=403)
    except sqlite3.Error:
        return JSONResponse({'error': 'storage-unavailable'}, status_code=503)
    if chat_id is not None:
        service.kick(chat_id)
    return {'result': 'ok'}


class Webhook:
    def __init__(self, settings, agent, webim):
        self.settings, self.agent, self.webim = settings, agent, webim
        self.workers = {}

    def accept(self, event, *, demo=False):
        assignment = isinstance(event, NewChat)
        chat_id = str(event.chat.id if assignment else event.chat_id)
        url = page_context(event.page_url or (event.chat.page_url if assignment else None))
        messages = event.messages if assignment else [event.message]

        def is_text(message):
            return (event.event != 'message_updated' and message.kind == 'visitor'
                    and message.text and message.text.strip())

        records = []
        with connect(self.settings.db_path, timeout=.25) as db, db:
            db.execute('BEGIN IMMEDIATE')
            reserved = db.execute('SELECT 1 FROM demo_chats WHERE chat_id=?', (chat_id,)).fetchone()
            if bool(reserved) != demo or (self.settings.demo_enabled and self.webim.mock and not demo):
                raise DemoScopeError
            first_empty_assignment = False
            if assignment:
                db.execute('INSERT INTO chats(chat_id,page_url) VALUES (?,?) ON CONFLICT(chat_id) DO NOTHING',
                           (chat_id, url))
                key = self.settings.webim_webhook_token.get_secret_value().encode()
                fingerprint = hmac.new(key, event.visitor.id.encode(), hashlib.sha256).hexdigest()
                db.execute('INSERT INTO webim_visitors VALUES (?,?) ON CONFLICT(chat_id) DO NOTHING',
                           (chat_id, fingerprint))
                visitor = db.execute('SELECT visitor_fingerprint FROM webim_visitors WHERE chat_id=?',
                                     (chat_id,)).fetchone()[0]
                if not hmac.compare_digest(fingerprint, visitor):
                    return None
                if not messages:
                    # v2 has no assignment ID or timestamp. A later identical empty callback is ambiguous.
                    first_empty_assignment = bool(db.execute(
                        'INSERT INTO webim_empty_assignments VALUES (?) ON CONFLICT(chat_id) DO NOTHING',
                        (chat_id,)).rowcount)
            queued = False
            wake = False
            for message in messages:
                record = {'kind': message.kind if message.kind in (
                    'visitor', 'operator', 'file_visitor', 'file_operator', 'keyboard_response') else 'other'}
                if is_text(message):
                    previous = db.execute('SELECT chat_id,pending_text IS NOT NULL FROM webim_inbox WHERE message_id=?',
                                          (message.id,)).fetchone()
                    record.update(text_length=len(message.text), duplicate=previous is not None)
                    if previous is None:
                        db.execute('INSERT INTO chats(chat_id,page_url) VALUES (?,?) ON CONFLICT(chat_id) DO NOTHING',
                                   (chat_id, url))
                        saved_page = db.execute('SELECT page_url FROM chats WHERE chat_id=?', (chat_id,)).fetchone()[0]
                        db.execute('INSERT INTO webim_inbox(chat_id,message_id,pending_text,pending_page_url) '
                                   'VALUES (?,?,?,?)', (chat_id, message.id, message.text, url or saved_page))
                        queued = wake = True
                    elif previous[0] == chat_id and previous[1]:
                        wake = True
                records.append(record)
            if assignment and (first_empty_assignment or queued):
                changed = db.execute("UPDATE chats SET status='bot' WHERE chat_id=? AND status IN ('manager','escalated')",
                                     (chat_id,))
                if changed.rowcount:
                    db.execute('DELETE FROM escalation_redirects WHERE chat_id=?', (chat_id,))
            if url is not None and (first_empty_assignment or queued):
                db.execute("UPDATE chats SET page_url=? WHERE chat_id=? AND status='bot'", (url, chat_id))
            payload = {'event': event.event, 'has_page_url': url is not None}
            if assignment:
                payload.update(chat={'id': int(chat_id)}, messages=records)
            else:
                payload.update(chat_id=int(chat_id), message=records[0])
            db.execute("INSERT INTO audit_log(chat_id,event_type,payload_json) VALUES (?,'webim_in',?)",
                       (chat_id, json.dumps(payload)))
        return chat_id if wake else None

    def pending_chats(self):
        with connect(self.settings.db_path) as db:
            return [row[0] for row in db.execute('''SELECT DISTINCT chat_id FROM webim_inbox
                LEFT JOIN demo_chats USING(chat_id) WHERE pending_text IS NOT NULL
                AND (demo_chats.chat_id IS NOT NULL) = ?''', (self.settings.demo_enabled and self.webim.mock,))]

    def _usage_cursor(self, chat_id):
        with connect(self.settings.db_path, timeout=.25) as db:
            if db.execute('SELECT 1 FROM demo_chats WHERE chat_id=?', (chat_id,)).fetchone():
                return db.execute("SELECT COALESCE(MAX(id),0) FROM audit_log WHERE chat_id=? AND event_type='llm_call'",
                                  (chat_id,)).fetchone()[0]
        return None

    def _audit_turn(self, chat_id, cursor):
        with connect(self.settings.db_path, timeout=.25) as db, db:
            db.execute('''INSERT INTO audit_log(chat_id,event_type,prompt_tokens,completion_tokens,cost_usd)
                SELECT ?, 'agent_turn', CASE WHEN COUNT(*)=COUNT(prompt_tokens) THEN SUM(prompt_tokens) END,
                CASE WHEN COUNT(*)=COUNT(completion_tokens) THEN SUM(completion_tokens) END,
                CASE WHEN COUNT(*)=COUNT(cost_usd) THEN SUM(cost_usd) END FROM audit_log
                WHERE chat_id=? AND event_type='llm_call' AND id>?''', (chat_id, chat_id, cursor))

    def kick(self, chat_id):
        if chat_id in self.workers:
            self.workers[chat_id][1].set()
        else:
            wake = asyncio.Event()
            self.workers[chat_id] = asyncio.create_task(self._drain(chat_id, wake)), wake

    def _next(self, chat_id):
        with connect(self.settings.db_path, timeout=2) as db, db:
            db.execute('BEGIN IMMEDIATE')
            item = db.execute('SELECT seq,pending_text,pending_page_url FROM webim_inbox '
                              'WHERE chat_id=? AND pending_text IS NOT NULL '
                              'ORDER BY seq LIMIT 1', (chat_id,)).fetchone()
            if item is None:
                return None
            db.execute("INSERT INTO messages(chat_id,sender,kind,text) VALUES (?,'client','visitor',?)",
                       (chat_id, item['pending_text']))
            db.execute('UPDATE webim_inbox SET pending_text=NULL,pending_page_url=NULL WHERE seq=?', (item['seq'],))
            chat = db.execute('''SELECT chats.status,escalation_redirects.status AS redirect
                                 FROM chats LEFT JOIN escalation_redirects USING(chat_id) WHERE chat_id=?''',
                              (chat_id,)).fetchone()
            if chat['status'] != 'bot' and not (chat['status'] == 'escalated' and chat['redirect'] == 'pending'):
                return [], ''
            rows = db.execute('''SELECT sender,text FROM messages WHERE chat_id=? AND text IS NOT NULL
                              AND sender IN ('client','bot') ORDER BY id DESC LIMIT 32''', (chat_id,)).fetchall()
            history = [{'role': 'user' if row['sender'] == 'client' else 'assistant', 'content': row['text']}
                       for row in reversed(rows)]
            return history, item['pending_page_url'] or ''

    async def _drain(self, chat_id, wake):
        try:
            while True:
                wake.clear()
                try:
                    turn = await asyncio.to_thread(self._next, chat_id)
                except sqlite3.Error as exc:
                    logger.warning('Webim inbox read failed (%s)', type(exc).__name__)
                    await asyncio.sleep(.25)
                    continue
                if turn is None:
                    if wake.is_set():
                        continue
                    return
                history, page_url = turn
                if not history:
                    continue
                cursor = None
                try:
                    if self.settings.demo_enabled and self.webim.mock:
                        try:
                            cursor = await asyncio.to_thread(self._usage_cursor, chat_id)
                        except sqlite3.Error:
                            pass
                    reply = await self.agent.run(chat_id, history, page_url=page_url)
                    if reply:
                        await self.webim.send_text(chat_id, reply)
                except IneligibleChat:
                    continue  # Assignment/state changed before the send.
                except Exception as exc:
                    self.agent.tools.audit(chat_id, 'error', {'stage': 'webim_turn', 'exception': type(exc).__name__})
                finally:
                    if cursor is not None:
                        try:
                            await asyncio.to_thread(self._audit_turn, chat_id, cursor)
                        except sqlite3.Error as exc:
                            logger.warning('Demo turn audit failed (%s)', type(exc).__name__)
        finally:
            self.workers.pop(chat_id, None)

    async def aclose(self):
        tasks = [task for task, _ in self.workers.values()]
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=5)
            for task in pending:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
