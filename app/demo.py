"""Opt-in synthetic chats: server-generated identity and per-chat capability."""
import asyncio
import hashlib
import hmac
import json
import re
import secrets
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import StringConstraints

from app.db import connect
from app.webim.client import PDF_NAME
from app.webim.schemas import CommandModel, OpaqueID, incoming, parse_body

router = APIRouter(prefix='/api/demo', include_in_schema=False)
PAGES = {key: 'https://store.softline.ru/' + path for key, path in (
    ('home', ''), ('redcheck', 'alteks-soft/redcheck/'), ('red-os', 'redsoft/red-os-1/'),
    ('power-bi', 'microsoft/microsoft-power-bi/'))}
Question = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=240)]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1200)]
CONTACT = re.compile(r'@|(?:\+?\d[\d() .-]{7,}\d)', re.I)


def synthetic_text(text):
    if CONTACT.search(text):
        raise HTTPException(422, 'Не вводите контактные данные и длинные номера в демо')
    return text


class NewDemo(CommandModel):
    page: Literal['home', 'redcheck', 'red-os', 'power-bi']


class Message(CommandModel):
    text: Text


class ConfirmQuote(CommandModel):
    confirmation_id: OpaqueID


def check_enabled(request):
    service = request.app.state.webim
    if not (service.settings.demo_enabled and service.mock and service.settings.webim_webhook_token
            and service.settings.webim_bot_token):
        raise HTTPException(404)
    return service.settings


async def body(request, model):
    parsed = await parse_body(request, model.model_validate)
    if parsed is None:
        raise HTTPException(400, 'Некорректные данные')
    return parsed


def response(data, status=200):
    return JSONResponse(data, status_code=status, headers={'Cache-Control': 'no-store'})


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def client_chat(request, chat_id):
    settings = check_enabled(request)
    auth = request.headers.get('authorization', '')
    with connect(settings.db_path, timeout=2) as db:
        row = db.execute('SELECT capability_hash FROM demo_chats WHERE chat_id=?', (chat_id,)).fetchone()
    if not (row and auth.startswith('Token ') and hmac.compare_digest(row[0], digest(auth[6:]))):
        raise HTTPException(404)
    return settings


@router.post('/chats')
async def create_chat(request: Request):
    settings = check_enabled(request)
    data = await body(request, NewDemo)
    token = secrets.token_urlsafe(32)
    with connect(settings.db_path, timeout=2) as db, db:
        db.execute('BEGIN IMMEDIATE')
        while True:
            # Positive Webim integer; JSON clients receive its exact decimal string.
            chat_id = str((1 << 62) + secrets.randbits(62))
            if db.execute('INSERT OR IGNORE INTO chats(chat_id,page_url) VALUES (?,?)',
                          (chat_id, PAGES[data.page])).rowcount:
                break
        db.execute('INSERT INTO demo_chats VALUES (?,?)', (chat_id, digest(token)))
    event = incoming.validate_python({'event': 'new_chat', 'chat': {'id': int(chat_id), 'page_url': PAGES[data.page]},
                                      'visitor': {'id': uuid4().hex}})
    await asyncio.to_thread(request.app.state.webhook.accept, event, demo=True)
    return response({'chat_id': chat_id, 'token': token}, 201)


@router.post('/chats/{chat_id}/messages')
async def post_message(request: Request, chat_id: str):
    client_chat(request, chat_id)
    data = await body(request, Message)
    event = incoming.validate_python({'event': 'new_message', 'chat_id': int(chat_id),
                                      'message': {'id': uuid4().hex, 'kind': 'visitor', 'text': synthetic_text(data.text)}})
    webhook = request.app.state.webhook
    if await asyncio.to_thread(webhook.accept, event, demo=True):
        webhook.kick(chat_id)
    return response({'accepted': True}, 202)


def chat_view(request, db, chat_id):
    service = request.app.state.webim
    row = db.execute('SELECT status,page_url FROM chats JOIN demo_chats USING(chat_id) WHERE chat_id=?',
                     (chat_id,)).fetchone()
    if row is None:
        raise HTTPException(404)
    messages = []
    for item in reversed(db.execute('''SELECT id,sender,kind,text,file_url FROM messages WHERE chat_id=?
                                      ORDER BY id DESC LIMIT 100''', (chat_id,)).fetchall()):
        message = dict(item)
        url = message.pop('file_url')
        if url and service.owns_pdf(db, chat_id, url):
            name = url.rsplit('/', 1)[-1]
            if PDF_NAME.fullmatch(name):
                message['pdf'] = f'/pdfs/{name}'
        messages.append(message)
    busy = chat_id in request.app.state.webhook.workers or db.execute(
        'SELECT 1 FROM webim_inbox WHERE chat_id=? AND pending_text IS NOT NULL LIMIT 1', (chat_id,)).fetchone()
    return dict(row) | {'messages': messages, 'busy': bool(busy),
                        'mode': 'llm' if llm_enabled(service.settings) else 'offline'}


@router.get('/chats/{chat_id}')
def get_chat(request: Request, chat_id: str):
    settings = client_chat(request, chat_id)
    with connect(settings.db_path, timeout=2) as db:
        view = chat_view(request, db, chat_id)
    pending = request.app.state.agent.tools.pending_quote(chat_id)
    view['quote'] = pending if pending and not pending['confirmed'] else None
    return response(view)


@router.post('/chats/{chat_id}/confirm-quote')
async def confirm_quote(request: Request, chat_id: str):
    client_chat(request, chat_id)
    data = await body(request, ConfirmQuote)
    if not await asyncio.to_thread(request.app.state.agent.tools.confirm_quote, chat_id, data.confirmation_id):
        raise HTTPException(409, 'Это предложение уже изменилось или подтверждено')
    return response({'confirmed': True})


def llm_enabled(settings):
    return bool(settings.llm_model and settings.llm_api_key)


def offline_answer(tools, chat_id, question):
    if len(question) > 240:
        return None
    # Exact approved demo Q&A only; never guess from the top fuzzy match or expose other sources.
    with connect(tools.settings.db_path, timeout=2) as db:
        if not db.execute('SELECT 1 FROM demo_chats WHERE chat_id=?', (chat_id,)).fetchone():
            return None
        rows = tools.search_kb(question)
        for row in rows:
            if row['source'] == 'manager_added' and row['title'].casefold() == question.casefold() and db.execute(
                'SELECT 1 FROM demo_kb_drafts WHERE article_id=?', (row['id'],)
            ).fetchone():
                return 'Офлайн · подтверждённая база знаний\n\n' + row['content']
    return None
