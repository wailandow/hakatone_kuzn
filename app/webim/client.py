"""Send text, a registered PDF, or a handoff to Webim Smart Bot v2."""
import asyncio
import json
import logging
import re
import sqlite3
from urllib.parse import urlsplit

import httpx

from app.db import connect
from app.webim.mock_server import save_bot_message

PDF_NAME = re.compile(r'kp_([0-9a-f]{32})\.pdf\Z')
logger = logging.getLogger(__name__)


class DeliveryError(RuntimeError):
    pass


class IneligibleChat(DeliveryError):
    pass


class WebimClient:
    def __init__(self, settings, *, transport=None):
        self.settings = settings
        self.base = settings.webim_base_url.rstrip('/')
        url = urlsplit(self.base)
        self.mock = (url.scheme in ('http', 'https') and url.hostname in ('127.0.0.1', 'localhost', '::1')
                     and url.path == '/mock-webim')
        if ((not self.mock and (url.scheme != 'https' or url.path)) or not url.hostname
                or url.username is not None or url.password is not None or url.query or url.fragment):
            raise ValueError('WEBIM_BASE_URL must be an HTTPS Webim origin or a loopback /mock-webim URL')
        self.http = httpx.AsyncClient(transport=transport, timeout=5, follow_redirects=False, trust_env=False)

    async def aclose(self):
        await self.http.aclose()

    def pdf_path(self, name):
        return self.settings.db_path.parent / 'pdfs' / name

    def owns_pdf(self, db, chat_id, url):
        upstream = f'{self.settings.public_base_url.rstrip("/")}/pdfs/'
        if not url.startswith(upstream) or not (match := PDF_NAME.fullmatch(url[len(upstream):])):
            return False
        path = self.pdf_path(match[0])
        return (not path.is_symlink() and path.is_file() and db.execute(
            'SELECT 1 FROM quotes WHERE id=? AND chat_id=? AND pdf_url=?',
            (match[1], str(chat_id), url)).fetchone() is not None)

    def _allowed(self, command, chat_id, message):
        with connect(self.settings.db_path, timeout=.25) as db:
            row = db.execute('SELECT status FROM chats WHERE chat_id=?', (str(chat_id),)).fetchone()
            if command == 'redirect_chat':
                return row is not None and row['status'] in ('escalated', 'manager')
            return (row is not None and row['status'] == 'bot' and
                    (message['kind'] == 'operator' or self.owns_pdf(db, chat_id, message['data']['url'])))

    def _audit(self, chat_id, command, message, outcome):
        # Retain wire shape, never free text, URLs, credentials or upstream error bodies.
        payload = {'command': command, 'chat_id': int(chat_id), 'outcome': outcome}
        if message:
            payload['message'] = {'kind': message['kind']}
            if message['kind'] == 'file_operator':
                payload['message']['data'] = {'name': 'KP_Softline.pdf', 'media_type': 'application/pdf'}
            else:
                payload['message']['text_length'] = len(message['text'])
        with connect(self.settings.db_path, timeout=.25) as db, db:
            db.execute("INSERT INTO audit_log(chat_id,event_type,payload_json) VALUES (?,'webim_out',?)",
                       (str(chat_id), json.dumps(payload)))

    def _record_remote(self, command, chat_id, message):
        with connect(self.settings.db_path, timeout=.25) as db, db:
            if command == 'redirect_chat':
                db.execute("UPDATE chats SET status='manager' WHERE chat_id=? AND status='escalated'", (str(chat_id),))
            else:
                save_bot_message(db, chat_id, message['kind'], message.get('text'), message.get('data', {}).get('url'))

    async def _post(self, command, chat_id, message=None):
        outcome = None
        try:
            if not await asyncio.to_thread(self._allowed, command, chat_id, message):
                raise IneligibleChat('chat or attachment is not eligible')
            token = self.settings.webim_bot_token
            outcome = 'not-configured'
            if token is None or not token.get_secret_value():
                raise DeliveryError('Webim credentials are not configured')
            payload = {'chat_id': int(chat_id)}
            if message is not None:
                payload['message'] = message
            outcome = 'transport-error'
            response = await self.http.post(f'{self.base}/api/bot/v2/{command}', json=payload,
                                            headers={'Authorization': f'Token {token.get_secret_value()}'})
            outcome = 'rejected'
            body = response.json() if response.content else {}
            if response.status_code != 200 or not isinstance(body, dict) or 'error' in body:
                raise DeliveryError('Webim rejected the command')
            outcome = 'delivered'
            if not self.mock:
                await asyncio.to_thread(self._record_remote, command, chat_id, message)
        except (httpx.HTTPError, ValueError):
            raise DeliveryError('Webim delivery failed') from None
        finally:
            if outcome is not None:
                try:
                    await asyncio.to_thread(self._audit, chat_id, command, message, outcome)
                except sqlite3.Error as exc:
                    logger.warning('Webim outbound audit write failed (%s)', type(exc).__name__)

    async def send_text(self, chat_id, text):
        await self._post('send_message', chat_id, {'kind': 'operator', 'text': text})

    async def send_file(self, chat_id, url):
        await self._post('send_message', chat_id, {'kind': 'file_operator', 'data': {
            'url': url, 'name': 'KP_Softline.pdf', 'media_type': 'application/pdf'}})

    async def redirect_chat(self, chat_id):
        await self._post('redirect_chat', chat_id)
