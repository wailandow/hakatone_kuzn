"""FastAPI entrypoint for the Webim adapter and quote downloads."""
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse

from app import demo
from app.agent.llm_client import Agent
from app.agent.tools import AgentTools
from app.config import Settings
from app.db import connect, initialize
from app.embeddings import E5Embedding
from app.manager.routes import router as manager_router
from app.webim import mock_server, webhook
from app.webim.client import PDF_NAME, WebimClient
from app.webim.schemas import matches

FRONTEND = Path(__file__).resolve().parents[1] / 'frontend'


def create_app(settings=None):
    @asynccontextmanager
    async def lifespan(app):
        config = settings if settings is not None else Settings()
        if config.webim_bot_token and matches(config.webim_webhook_token, config.webim_bot_token.get_secret_value()):
            raise ValueError('Webim bot and webhook credentials must be distinct')
        if config.demo_enabled and config.manager_token and any(matches(token, config.manager_token.get_secret_value())
                for token in (config.webim_bot_token, config.webim_webhook_token)):
            raise ValueError('Manager and Webim credentials must be distinct')
        initialize(config.db_path)
        service = WebimClient(config, transport=mock_transport(app, config))
        embedder = E5Embedding() if config.demo_enabled and service.mock and config.demo_embeddings else None
        tools = AgentTools(config, embedder=embedder, send_file=service.send_file, redirect_chat=service.redirect_chat)
        agent = Agent(config, tools)
        inbound = webhook.Webhook(config, agent, service)
        app.state.webim, app.state.agent, app.state.webhook = service, agent, inbound
        try:
            for chat_id in inbound.pending_chats():
                inbound.kick(chat_id)
            yield
        finally:
            await inbound.aclose()
            await agent.aclose()
            await service.aclose()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(webhook.router)
    app.include_router(mock_server.router)
    app.include_router(demo.router)
    app.include_router(manager_router)

    @app.get('/')
    @app.get('/demo.js')
    @app.get('/demo.css')
    def demo_ui(request: Request):
        demo.check_enabled(request)
        name = request.url.path[1:] or 'index.html'
        return FileResponse(FRONTEND / name, headers={
            'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
            'Content-Security-Policy': "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
                                       "base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
            'Referrer-Policy': 'no-referrer',
        })

    @app.get('/pdfs/{name}', include_in_schema=False)
    def quote_pdf(request: Request, name: str):
        match = PDF_NAME.fullmatch(name)
        if match is None:
            raise HTTPException(404)
        service = request.app.state.webim
        path = service.pdf_path(name)
        with connect(service.settings.db_path) as db:
            row = db.execute('SELECT chat_id,pdf_url FROM quotes WHERE id=?', (match[1],)).fetchone()
            if not row or not service.owns_pdf(db, row['chat_id'], row['pdf_url']) or not path.is_file():
                raise HTTPException(404)
        return FileResponse(path, media_type='application/pdf', filename='KP_Softline.pdf',
                            headers={'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff'})

    return app


def mock_transport(app, settings):
    # No socket or proxy is needed to exercise the same authenticated Mock-Webim HTTP routes.
    url = urlsplit(settings.webim_base_url.rstrip('/'))
    if url.hostname in ('127.0.0.1', 'localhost', '::1') and url.path == '/mock-webim':
        return httpx.ASGITransport(app=app)
    return None


app = create_app()
