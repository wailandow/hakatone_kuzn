"""Loopback implementation of the two Webim commands used by this bot."""
from asyncio import to_thread
from ipaddress import ip_address
import sqlite3

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.db import connect
from app.webim.schemas import RedirectChat, SendMessage, authorized, parse_body

router = APIRouter(prefix='/mock-webim/api/bot/v2', include_in_schema=False)


def mock_error(error, status=200):
    return JSONResponse({'error': error}, status_code=status)


@router.post('/send_message')
async def send_message(request: Request):
    return await dispatch(request, SendMessage, store_message)


@router.post('/redirect_chat')
async def redirect_chat(request: Request):
    return await dispatch(request, RedirectChat, handoff)


async def dispatch(request, schema, mutate):
    service = request.app.state.webim
    if not service.mock:
        return mock_error('method-not-found', 404)
    if not is_loopback(request) or not authorized(service.settings.webim_bot_token, request.headers.get('authorization')):
        return mock_error('unauthorized', 403)
    data = await parse_body(request, schema.model_validate)
    if data is None:
        return mock_error('incorrect-request', 400)
    try:
        return await to_thread(mutate, service, data)
    except sqlite3.Error:
        return mock_error('storage-unavailable', 503)


def is_loopback(request):
    try:
        return request.client is not None and ip_address(request.client.host).is_loopback
    except ValueError:
        return False


def in_mock_scope(db, service, chat_id):
    demo = db.execute('SELECT 1 FROM demo_chats WHERE chat_id=?', (str(chat_id),)).fetchone()
    return bool(demo) == service.settings.demo_enabled


def store_message(service, data):
    with connect(service.settings.db_path, timeout=.25) as db, db:
        db.execute('BEGIN IMMEDIATE')
        if (not in_mock_scope(db, service, data.chat_id) or not db.execute(
                "SELECT 1 FROM chats WHERE chat_id=? AND status='bot'", (str(data.chat_id),)).fetchone()):
            return mock_error('chat-not-found')
        message = data.message
        url = message.data.url if message.kind == 'file_operator' else None
        if url and not service.owns_pdf(db, data.chat_id, url):
            return mock_error('incorrect-request', 400)
        save_bot_message(db, data.chat_id, message.kind, getattr(message, 'text', None), url)
    return {'result': 'ok'}


def handoff(service, data):
    with connect(service.settings.db_path, timeout=.25) as db, db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT status FROM chats WHERE chat_id=?', (str(data.chat_id),)).fetchone()
        if not in_mock_scope(db, service, data.chat_id) or not row or row['status'] not in ('escalated', 'manager'):
            return mock_error('chat-not-found')
        db.execute("UPDATE chats SET status='manager' WHERE chat_id=? AND status='escalated'", (str(data.chat_id),))
    return {'result': 'ok'}


def save_bot_message(db, chat_id, kind, text, url):
    db.execute("INSERT INTO messages(chat_id,sender,kind,text,file_url) VALUES (?,'bot',?,?,?)",
               (str(chat_id), kind, text, url))
