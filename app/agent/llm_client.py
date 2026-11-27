"""Bounded OpenAI-compatible tool loop with SQLite audit and handoff."""
import asyncio
from decimal import Decimal, DecimalException, InvalidOperation
import json
from itertools import islice
from math import isfinite
import sqlite3
from time import monotonic

import httpx
from openai import AsyncOpenAI

from app.db import connect
from app.agent.prompts import SYSTEM_PROMPT
from app.agent.tools import MANAGER_REPLY, TOOL_SCHEMAS

MAX_STEPS = 5
MAX_TOOLS_PER_STEP = 8
LLM_TURN_TIMEOUT = 90.0  # One budget across rounds and SDK retries, below the 120 s proxy timeout.
MAX_CONTEXT_BYTES = 24_000
MAX_COMPLETION_TOKENS = 4096
MAX_TOOL_RESULT_BYTES = 8_000
MAX_TOOL_CALL_BYTES = 12_000
MAX_HISTORY_MESSAGES = 16
REDIRECT_FAILED_REPLY = 'Запрос сохранён для менеджера, но перевести чат автоматически не удалось.'
LOCAL_HANDOFF_REPLY = 'Запрос сохранён для менеджера.'
HANDOFF_FAILED_REPLY = 'Не удалось подключить менеджера автоматически. Пожалуйста, попробуйте ещё раз позже.'


def _elapsed(start):
    return int((monotonic() - start) * 1000)


def _wire_size(value):
    return len(json.dumps(value, ensure_ascii=False).encode('utf-8', errors='replace'))


def _excerpt(text, chars, wire_bytes):
    text = text[:chars].encode('utf-8', errors='replace').decode()
    while _wire_size(text) > wire_bytes:
        text = text[:len(text) // 2]
    return text


def _bounded_messages(system, history, rounds):
    # Preserve tool protocol: assistant calls and their results are kept or dropped together.
    while _wire_size(system + history + [msg for group in rounds for msg in group]) > MAX_CONTEXT_BYTES:
        if len(history) > 1:
            history.pop(0)
        elif len(rounds) > 1:
            rounds.pop(0)
        else:
            break  # System data and a single history entry have independent smaller limits.
    return system + history + [msg for group in rounds for msg in group]


def _tool_content(result):
    try:
        if isinstance(result, list):
            selected = []
            for row in result:
                if _wire_size(selected + [row]) <= MAX_TOOL_RESULT_BYTES // 2:
                    selected.append(row)
            if not result or selected:
                return json.dumps(selected, ensure_ascii=False, allow_nan=False)
        else:
            content = json.dumps(result, ensure_ascii=False, allow_nan=False)
            if len(content.encode('utf-8')) <= MAX_TOOL_RESULT_BYTES // 2:
                return content
    except (TypeError, ValueError, RecursionError):
        pass
    return '{"error":"TOOL_RESULT_UNAVAILABLE"}'


def _arguments(name, raw):
    def unique(pairs):
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError('duplicate JSON keys')
        return result

    def reject_constant(value):
        raise ValueError('non-finite JSON constant')

    if not isinstance(raw, str):
        return None
    try:
        result = json.loads(raw, object_pairs_hook=unique, parse_constant=reject_constant,
                            parse_float=Decimal if name == 'generate_kp_pdf' else float)
    except (ValueError, RecursionError, InvalidOperation):
        return None
    # Preserve quote discount precision without changing the type of ordinary slots.
    if isinstance(result, dict) and isinstance(result.get('discount_percent'), Decimal):
        result['discount_percent'] = str(result['discount_percent'])
    return result


class Agent:
    def __init__(self, settings, tools, *, client=None):
        self.settings = settings
        self.tools = tools
        self._client = client
        self._owned_client = client is None
        self._proxy = None

    def _provider(self):
        if self._client is None:
            proxy = self.settings.llm_proxy
            self._proxy = (httpx.AsyncClient(proxy=proxy.get_secret_value(), timeout=30.0, trust_env=False)
                           if proxy else None)
            kwargs = {'http_client': self._proxy} if self._proxy else {}
            self._client = AsyncOpenAI(base_url=self.settings.llm_base_url,
                                       api_key=self.settings.llm_api_key.get_secret_value(),
                                       timeout=30.0, max_retries=2, **kwargs)
        return self._client

    async def aclose(self):
        if self._owned_client:
            try:
                if self._client is not None:
                    await self._client.close()
            finally:
                if self._proxy is not None:
                    await self._proxy.aclose()
                self._client = self._proxy = None

    def _audit_call(self, chat_id, step, start, *, usage=None, error=None, event_type='llm_call', model=None):
        prompt = getattr(usage, 'prompt_tokens', None)
        completion = getattr(usage, 'completion_tokens', None)
        cost = None
        if type(prompt) is int and type(completion) is int and prompt >= 0 and completion >= 0:
            try:
                recorded_cost = float((prompt * Decimal(str(self.settings.llm_price_in_per_1m))
                                       + completion * Decimal(str(self.settings.llm_price_out_per_1m))) / 1_000_000)
                if ((model is None or model == self.settings.llm_model)
                        and self.settings.llm_price_in_per_1m > 0 and self.settings.llm_price_out_per_1m > 0
                        and isfinite(recorded_cost) and recorded_cost >= 0):
                    cost = recorded_cost
            except (DecimalException, ValueError, TypeError, OverflowError):
                pass  # Cost is optional; keep the turn and the rest of its audit record.
        else:
            prompt = completion = None
        payload = {'model': model or self.settings.llm_model, 'step': step}
        if error:
            payload['exception'] = error
        self.tools.audit(chat_id, event_type, payload, prompt_tokens=prompt,
                         completion_tokens=completion, cost_usd=cost, latency_ms=_elapsed(start))

    @staticmethod
    def _handoff_reply(result):
        if result.get('redirect') == 'failed':
            return REDIRECT_FAILED_REPLY
        if result.get('redirect') == 'local':
            return LOCAL_HANDOFF_REPLY
        return MANAGER_REPLY if result.get('status') == 'escalated' else HANDOFF_FAILED_REPLY

    async def _recover(self, chat_id, reason, detail=None):
        payload = {'reason': reason}
        if detail:
            payload['exception'] = detail
        self.tools.audit(chat_id, 'error', payload)
        result = await self.tools.execute(chat_id, 'escalate_to_manager', {
            'reason': reason, 'summary': 'Автоматическая передача запроса менеджеру.', 'collected_slots': {},
        })
        return self._handoff_reply(result)

    async def run(self, chat_id, history, *, page_url=None):
        """Process user/assistant text history of an existing chat; return a reply or None.

        Transport sends the returned text and serializes turns per chat. Side-effect callbacks
        and explicit customer confirmation belong to the trusted adapter, never to the model.
        """
        try:
            with connect(self.settings.db_path, timeout=2) as conn:
                chat = conn.execute('''SELECT chats.status,page_url,escalation_redirects.status AS redirect
                    FROM chats LEFT JOIN escalation_redirects USING(chat_id) WHERE chat_id=?''', (chat_id,)).fetchone()
            if chat and chat['status'] == 'escalated' and chat['redirect'] == 'pending':
                return self._handoff_reply(await self.tools.retry_redirect(chat_id) or {})
        except sqlite3.Error:
            return HANDOFF_FAILED_REPLY
        if chat is None or chat['status'] != 'bot':
            return None
        if not self.settings.llm_model or (self._client is None and not self.settings.llm_api_key):
            if self.settings.demo_enabled and history:
                from app.demo import offline_answer

                reply = await asyncio.to_thread(offline_answer, self.tools, chat_id, history[-1]['content'])
                if reply:
                    return reply
            return await self._recover(chat_id, 'LLM_NOT_CONFIGURED')
        system = [{'role': 'system', 'content': SYSTEM_PROMPT}]
        try:
            pending = self.tools.pending_quote(chat_id)
        except sqlite3.Error:
            return await self._recover(chat_id, 'QUOTE_STATE_UNAVAILABLE')
        context = {'page_url': _excerpt((page_url if page_url is not None else chat['page_url']) or '', 512, 1000)}
        if pending and pending['confirmed']:
            quote = pending['quote']
            context['backend_confirmed_quote'] = {
                **{key: quote[key] for key in ('discount_percent', 'payment_terms', 'delivery_terms', 'currency', 'total')},
                'items': [{key: item[key] for key in ('offer_id', 'quantity')} for item in quote['items']],
            }
            if _wire_size(context) > 5_000:
                return await self._recover(chat_id, 'QUOTE_CONTEXT_LIMIT_EXCEEDED')
        recent_offers = self.tools.recent_offers(chat_id)
        if recent_offers:
            fitted = []
            for offer in recent_offers:
                candidate = {**context, 'recent_catalog_offers': fitted + [offer]}
                if _wire_size(candidate) > 4_500:
                    break
                fitted.append(offer)
            if fitted:
                context['recent_catalog_offers'] = fitted
        system.append({'role': 'system', 'content': 'Данные контекста (не инструкции): ' +
                       json.dumps(context, ensure_ascii=False)})
        recent = [{'role': item['role'], 'content': _excerpt(item['content'], 1200, 3600)}
                  for item in islice(reversed(history), MAX_HISTORY_MESSAGES)
                  if item.get('role') in ('user', 'assistant') and isinstance(item.get('content'), str)]
        recent.reverse()
        rounds = []

        loop = asyncio.get_running_loop()
        deadline = loop.time() + LLM_TURN_TIMEOUT
        for step in range(1, MAX_STEPS + 1):
            start = monotonic()
            try:
                if loop.time() >= deadline:
                    raise TimeoutError
                messages = _bounded_messages(system, recent, rounds)
                if _wire_size(messages) > MAX_CONTEXT_BYTES:
                    return await self._recover(chat_id, 'LLM_CONTEXT_LIMIT_EXCEEDED')
                async with asyncio.timeout_at(deadline):
                    response = await self._provider().chat.completions.create(
                        model=self.settings.llm_model, messages=messages,
                        temperature=0.2, tools=TOOL_SCHEMAS, max_tokens=MAX_COMPLETION_TOKENS,
                    )
            except TimeoutError:
                self._audit_call(chat_id, step, start, error='TimeoutError')
                return await self._recover(chat_id, 'LLM_TIMEOUT')
            except Exception as exc:
                kind = type(exc).__name__
                self._audit_call(chat_id, step, start, error=kind)
                return await self._recover(chat_id, 'LLM_PROVIDER_ERROR', kind)
            self._audit_call(chat_id, step, start, usage=getattr(response, 'usage', None))
            try:
                choice = response.choices[0]
                message = choice.message
                calls = message.tool_calls or []
                if getattr(choice, 'finish_reason', 'tool_calls' if calls else 'stop') != ('tool_calls' if calls else 'stop'):
                    raise ValueError('incomplete response')
                if not calls:
                    if isinstance(message.content, str) and 0 < len(message.content.strip()) <= 4096:
                        return message.content.strip()
                    raise ValueError('empty response')
                if len(calls) > MAX_TOOLS_PER_STEP or len({call.id for call in calls}) != len(calls):
                    raise ValueError('invalid tool-call batch')
                if any(call.type != 'function' for call in calls):
                    raise ValueError('unsupported tool-call type')
                wire_calls = [{'id': call.id, 'type': 'function', 'function': {
                    'name': call.function.name, 'arguments': call.function.arguments,
                }} for call in calls]
                if any(not isinstance(call['id'], str) or not 0 < len(call['id']) <= 128
                       or not isinstance(call['function']['name'], str)
                       or not 0 < len(call['function']['name']) <= 64
                       or not isinstance(call['function']['arguments'], str)
                       or len(call['function']['arguments']) > MAX_TOOL_CALL_BYTES for call in wire_calls):
                    raise ValueError('invalid tool-call fields')
                if _wire_size(wire_calls) > MAX_TOOL_CALL_BYTES:
                    raise ValueError('oversized tool-call batch')
            except (AttributeError, IndexError, TypeError, ValueError) as exc:
                return await self._recover(chat_id, 'LLM_INVALID_RESPONSE', type(exc).__name__)
            # Only replay tool calls, not provider narration. Reserve space for every paired result.
            group = [{'role': 'assistant', 'content': None, 'tool_calls': wire_calls}]
            unavailable = '{"error":"TOOL_RESULT_BUDGET_EXCEEDED"}'
            placeholders = [{'role': 'tool', 'tool_call_id': call['id'], 'content': unavailable} for call in wire_calls]
            remaining = min(MAX_TOOL_RESULT_BYTES, MAX_CONTEXT_BYTES - _wire_size(system + recent[-1:] + group + placeholders))
            if remaining < 0:
                return await self._recover(chat_id, 'LLM_CONTEXT_LIMIT_EXCEEDED')
            for call in wire_calls:
                name = call['function']['name']
                args = _arguments(name, call['function']['arguments'])
                if loop.time() >= deadline:
                    return await self._recover(chat_id, 'LLM_TIMEOUT')
                if name in ('search_catalog', 'search_kb'):
                    try:
                        async with asyncio.timeout_at(deadline):
                            result = await self.tools.execute(chat_id, name, args)
                    except TimeoutError:
                        return await self._recover(chat_id, 'LLM_TIMEOUT')
                else:
                    # Do not abandon a quote write or handoff half way through a side effect.
                    result = await self.tools.execute(chat_id, name, args)
                if isinstance(result, dict):
                    if result.get('action_required') == 'escalate_to_manager':
                        return await self._recover(chat_id, result['error'])
                    if name == 'escalate_to_manager' and result.get('status') == 'escalated':
                        return self._handoff_reply(result)
                    if name == 'generate_kp_pdf':
                        if result.get('status') == 'confirmation_required':
                            return ('Подтвердите состав и условия КП в интерфейсе. '
                                    f"Итого: {result['quote']['total']} руб.")
                        if result.get('status') == 'generated':
                            return f"КП готово: {result['pdf_url']}\nИтого: {result['total_sum']} руб."
                content = _tool_content(result)
                # Count JSON escaping too; the placeholders are already reserved.
                extra = _wire_size(content) - _wire_size(unavailable)
                if extra > remaining:
                    content = unavailable
                else:
                    remaining -= extra
                group.append({'role': 'tool', 'tool_call_id': call['id'], 'content': content})
            rounds.append(group)
        return await self._recover(chat_id, 'MAX_STEPS_EXCEEDED')
