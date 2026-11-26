"""Webim Smart Bot v2 wire types and privacy-safe audit projections."""
import hmac
import json
from typing import Annotated, Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, StringConstraints, TypeAdapter, field_validator
from starlette.requests import ClientDisconnect

ChatID = Annotated[int, Field(strict=True, gt=0, le=2**63 - 1)]
OpaqueID = Annotated[str, StringConstraints(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9_-]+$')]
Text = Annotated[str, StringConstraints(min_length=1, max_length=16000)]


class IncomingMessage(BaseModel):
    id: OpaqueID
    kind: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    text: Annotated[str, StringConstraints(max_length=16000)] | None = None


class Chat(BaseModel):
    id: ChatID
    page_url: str | None = None


class Visitor(BaseModel):
    id: OpaqueID


class NewChat(BaseModel):
    event: Literal['new_chat']
    chat: Chat
    visitor: Visitor
    messages: Annotated[list[IncomingMessage], Field(max_length=100)] = Field(default_factory=list)
    page_url: str | None = None


class NewMessage(BaseModel):
    event: Literal['new_message', 'message_updated']
    chat_id: ChatID
    message: IncomingMessage
    page_url: str | None = None


incoming = TypeAdapter(Annotated[NewChat | NewMessage, Field(discriminator='event')])


class CommandModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Operator(CommandModel):
    kind: Literal['operator']
    text: Text


class FileData(CommandModel):
    url: str = Field(strict=True, max_length=2083)
    name: Literal['KP_Softline.pdf']
    media_type: Literal['application/pdf']

    @field_validator('url')
    @classmethod
    def validate_url(cls, value: str) -> str:
        HttpUrl(value)
        # Keep the validated spelling: registration is for the exact URL, not a canonical alias.
        return value


class FileOperator(CommandModel):
    kind: Literal['file_operator']
    data: FileData


class SendMessage(CommandModel):
    chat_id: ChatID
    message: Annotated[Operator | FileOperator, Field(discriminator='kind')]


class RedirectChat(CommandModel):
    chat_id: ChatID


def page_context(value):
    if not value or len(value) > 2048 or any(c.isspace() or ord(c) < 32 for c in value):
        return None
    try:
        url = urlsplit(value)
        if url.scheme not in ('http', 'https') or not url.hostname or '@' in url.netloc or '\\' in value:
            return None
        return urlunsplit((url.scheme, url.netloc, url.path, '', ''))
    except ValueError:
        return None


def matches(secret, value):
    return (secret is not None and bool(secret.get_secret_value()) and value is not None
            and hmac.compare_digest(secret.get_secret_value().encode(), value.encode()))


def authorized(secret, header):
    return bool(header and header.startswith('Token ') and matches(secret, header[6:]))


async def parse_body(request, validate):
    if request.headers.get('content-type', '').partition(';')[0].strip().lower() != 'application/json':
        return None
    raw = bytearray()
    try:
        async for chunk in request.stream():
            if len(raw) + len(chunk) > 128 * 1024:
                return None
            raw.extend(chunk)
        data = json.loads(raw)
        # Reject lone surrogate escapes before any SQLite write or outbound encoding.
        json.dumps(data, ensure_ascii=False).encode('utf-8')
        return validate(data)
    except (ValueError, RecursionError, ClientDisconnect):
        return None
