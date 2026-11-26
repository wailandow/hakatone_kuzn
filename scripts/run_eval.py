"""Run mined eval_set.json cases against the real Agent and write a JSON report."""
import argparse
import asyncio
from datetime import datetime, timezone
import json
from math import isfinite
from pathlib import Path
import sys
from time import monotonic
from types import SimpleNamespace as NS
from uuid import uuid4

from app.agent.llm_client import Agent
from app.agent.tools import AgentTools
from app.config import Settings
from app.db import connect, initialize

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EVAL_FILE = ROOT / 'data' / 'mined' / 'eval_set.json'
DEFAULT_OUT_FILE = ROOT / 'data' / 'eval_report.json'
ESCALATION_INTENTS = frozenset({'order_status'})
SEARCH_INTENTS = frozenset({'price', 'configuration', 'vendor_availability', 'foreign_availability', 'quote_document'})


class _OfflineDryRunClient:
    """Deterministic local test-double for --offline dry-runs.

    Never fabricates LLM token counts or costs (usage is always None).
    Exercises the real Agent loop, AgentTools, SQLite catalog/KB, and escalation path.
    """

    def __init__(self, case):
        self._case = case
        self._step = 0
        self.chat = NS(completions=NS(create=self._create))

    async def _create(self, **kwargs):
        self._step += 1
        intents = set(self._case.get('intents') or ())
        products = list(self._case.get('products') or ())
        if self._step == 1:
            if intents & ESCALATION_INTENTS:
                call = NS(
                    id='offline_call_1',
                    type='function',
                    function=NS(
                        name='escalate_to_manager',
                        arguments=json.dumps({
                            'reason': 'order_status',
                            'summary': '[OFFLINE DRY-RUN] Запрос статуса заказа передан менеджеру.',
                            'collected_slots': {},
                        }, ensure_ascii=False),
                    ),
                )
                return NS(choices=[NS(message=NS(content=None, tool_calls=[call]), finish_reason='tool_calls')],
                          usage=None)
            query = products[0] if products else self._case.get('text', '')[:80]
            call = NS(
                id='offline_call_1',
                type='function',
                function=NS(
                    name='search_catalog',
                    arguments=json.dumps({
                        'query': query,
                        'russian_only': 'foreign_availability' in intents,
                        'limit': 3,
                    }, ensure_ascii=False),
                ),
            )
            return NS(choices=[NS(message=NS(content=None, tool_calls=[call]), finish_reason='tool_calls')],
                      usage=None)
        return NS(
            choices=[NS(message=NS(
                content='[OFFLINE DRY-RUN] Локальный прогон без LLM завершён; метрики токенов и стоимости не измерялись.',
                tool_calls=(),
            ), finish_reason='stop')],
            usage=None,
        )


def _mean(values):
    return float(sum(values) / len(values)) if values else None


def _percentile(values, pct):
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    pos = (pct / 100.0) * (len(ordered) - 1)
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    return float(ordered[low] + (ordered[high] - ordered[low]) * (pos - low))


def _evaluate_checks(case, *, reply, escalated, tools_used, errors):
    intents = set(case.get('intents') or ())
    products = list(case.get('products') or ())
    expect_escalation = bool(intents & ESCALATION_INTENTS)
    expect_search = (not expect_escalation) and bool(products or (intents & SEARCH_INTENTS))

    has_reply = isinstance(reply, str) and bool(reply.strip())
    no_error = not errors
    escalation_match = escalated if expect_escalation else True
    used_search = any(name in ('search_catalog', 'search_kb') for name in tools_used)
    tool_check = used_search if expect_search else True
    return {
        'expect_escalation': expect_escalation,
        'expect_search': expect_search,
        'has_reply': has_reply,
        'no_error': no_error,
        'escalation_match': escalation_match,
        'used_search': used_search,
        'tool_check': tool_check,
        'passed': bool(has_reply and no_error and escalation_match),
    }


def _collect_case_audit(db_path, chat_id):
    with connect(db_path, timeout=2) as db:
        llm_rows = db.execute(
            "SELECT prompt_tokens, completion_tokens, cost_usd FROM audit_log WHERE chat_id=? AND event_type='llm_call'",
            (chat_id,),
        ).fetchall()
        tool_rows = db.execute(
            "SELECT payload_json FROM audit_log WHERE chat_id=? AND event_type='tool_call' ORDER BY id",
            (chat_id,),
        ).fetchall()
        error_rows = db.execute(
            "SELECT payload_json FROM audit_log WHERE chat_id=? AND event_type='error' ORDER BY id",
            (chat_id,),
        ).fetchall()
        chat_row = db.execute('SELECT status FROM chats WHERE chat_id=?', (chat_id,)).fetchone()
        esc_row = db.execute(
            'SELECT reason FROM escalations WHERE chat_id=? ORDER BY id DESC LIMIT 1',
            (chat_id,),
        ).fetchone()

    prompt_vals = [r['prompt_tokens'] for r in llm_rows if isinstance(r['prompt_tokens'], int)]
    completion_vals = [r['completion_tokens'] for r in llm_rows if isinstance(r['completion_tokens'], int)]
    cost_vals = [float(r['cost_usd']) for r in llm_rows if isinstance(r['cost_usd'], (int, float)) and isfinite(r['cost_usd'])]

    prompt_tokens = sum(prompt_vals) if llm_rows and len(prompt_vals) == len(llm_rows) else None
    completion_tokens = sum(completion_vals) if llm_rows and len(completion_vals) == len(llm_rows) else None
    total_tokens = (prompt_tokens + completion_tokens) if (prompt_tokens is not None and completion_tokens is not None) else None
    cost_usd = float(sum(cost_vals)) if llm_rows and len(cost_vals) == len(llm_rows) else None

    tools_used = []
    for row in tool_rows:
        try:
            name = json.loads(row['payload_json']).get('tool')
            if name:
                tools_used.append(name)
        except ValueError:
            pass

    errors = []
    for row in error_rows:
        try:
            errors.append(json.loads(row['payload_json']))
        except ValueError:
            errors.append({'raw': row['payload_json']})

    escalated = bool((chat_row and chat_row['status'] == 'escalated') or esc_row is not None)
    escalation_reason = esc_row['reason'] if esc_row is not None else None
    return {
        'llm_calls': len(llm_rows),
        'prompt_tokens': prompt_tokens,
        'completion_tokens': completion_tokens,
        'total_tokens': total_tokens,
        'cost_usd': cost_usd,
        'tools_used': tools_used,
        'errors': errors,
        'escalated': escalated,
        'escalation_reason': escalation_reason,
    }


def _record_agent_turn(db_path, chat_id, *, prompt_tokens, completion_tokens, cost_usd, latency_ms):
    with connect(db_path, timeout=2) as db, db:
        db.execute(
            '''INSERT INTO audit_log(chat_id,event_type,payload_json,prompt_tokens,completion_tokens,cost_usd,latency_ms)
               VALUES (?,'agent_turn','{"source":"eval"}',?,?,?,?)''',
            (chat_id, prompt_tokens, completion_tokens, cost_usd, latency_ms),
        )


async def run_eval(
    settings,
    *,
    eval_file=DEFAULT_EVAL_FILE,
    out_file=DEFAULT_OUT_FILE,
    limit=None,
    offline=False,
    client=None,
    embedder=None,
):
    eval_path = Path(eval_file)
    cases = json.loads(eval_path.read_text(encoding='utf-8'))
    if limit is not None:
        cases = cases[:max(0, limit)]

    initialize(settings.db_path)
    run_id = uuid4().hex[:8]
    rate = settings.usd_to_rub if (isfinite(settings.usd_to_rub) and settings.usd_to_rub > 0) else None

    async def _noop_redirect(_chat_id):
        return None

    async def _noop_send_file(_chat_id, _public_url):
        return None

    tools = AgentTools(settings, embedder=embedder, send_file=_noop_send_file, redirect_chat=_noop_redirect)
    live_agent = None
    if not offline:
        live_agent = Agent(settings, tools, client=client)

    results = []
    try:
        for idx, case in enumerate(cases, start=1):
            chat_id = f'eval_{run_id}_{idx:03d}'
            with connect(settings.db_path, timeout=2) as db, db:
                db.execute('INSERT INTO chats(chat_id,page_url) VALUES (?,?)', (chat_id, 'https://store.softline.ru/'))

            if offline:
                eval_settings = settings.model_copy(update={'llm_model': 'offline-dry-run'})
                agent = Agent(eval_settings, tools, client=_OfflineDryRunClient(case))
            else:
                agent = live_agent

            start = monotonic()
            reply = await agent.run(chat_id, [{'role': 'user', 'content': case['text']}])
            latency_ms = int((monotonic() - start) * 1000)

            audit = _collect_case_audit(settings.db_path, chat_id)
            if offline:
                # Never fabricate LLM token or cost metrics in offline/dry-run mode.
                audit['prompt_tokens'] = None
                audit['completion_tokens'] = None
                audit['total_tokens'] = None
                audit['cost_usd'] = None
            else:
                _record_agent_turn(
                    settings.db_path,
                    chat_id,
                    prompt_tokens=audit['prompt_tokens'],
                    completion_tokens=audit['completion_tokens'],
                    cost_usd=audit['cost_usd'],
                    latency_ms=latency_ms,
                )

            cost_rub = (audit['cost_usd'] * rate) if (audit['cost_usd'] is not None and rate is not None) else None
            checks = _evaluate_checks(
                case,
                reply=reply,
                escalated=audit['escalated'],
                tools_used=audit['tools_used'],
                errors=audit['errors'],
            )
            results.append({
                'index': idx,
                'chat_id': chat_id,
                'category': case.get('category'),
                'intents': case.get('intents') or [],
                'products': case.get('products') or [],
                'text': case.get('text'),
                'reply': reply,
                'latency_ms': latency_ms,
                'llm_calls': audit['llm_calls'],
                'prompt_tokens': audit['prompt_tokens'],
                'completion_tokens': audit['completion_tokens'],
                'total_tokens': audit['total_tokens'],
                'cost_usd': audit['cost_usd'],
                'cost_rub': cost_rub,
                'tools_used': audit['tools_used'],
                'escalated': audit['escalated'],
                'escalation_reason': audit['escalation_reason'],
                'errors': audit['errors'],
                'checks': checks,
            })
    finally:
        if live_agent is not None:
            await live_agent.aclose()

    latencies = [item['latency_ms'] for item in results]
    tokens = [item['total_tokens'] for item in results if item['total_tokens'] is not None]
    costs_usd = [item['cost_usd'] for item in results if item['cost_usd'] is not None]
    total_cost_usd = float(sum(costs_usd)) if costs_usd else None
    avg_cost_usd = _mean(costs_usd)

    report = {
        'run_id': run_id,
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'mode': 'offline_dry_run' if offline else 'live',
        'dry_run': bool(offline),
        'disclaimer': (
            'Offline dry-run mode: no real LLM API calls were made; token and cost metrics are null.'
            if offline else None
        ),
        'model': None if offline else settings.llm_model,
        'db_path': str(settings.db_path),
        'eval_file': str(eval_path),
        'total_cases': len(results),
        'summary': {
            'passed': sum(1 for item in results if item['checks']['passed']),
            'with_reply': sum(1 for item in results if item['checks']['has_reply']),
            'escalated': sum(1 for item in results if item['escalated']),
            'escalation_expected': sum(1 for item in results if item['checks']['expect_escalation']),
            'escalation_matched': sum(
                1 for item in results if item['checks']['expect_escalation'] and item['checks']['escalation_match']
            ),
            'with_tools': sum(1 for item in results if item['tools_used']),
            'errors': sum(1 for item in results if item['errors']),
            'avg_latency_ms': _mean(latencies),
            'p50_latency_ms': _percentile(latencies, 50),
            'p95_latency_ms': _percentile(latencies, 95),
            'total_tokens': sum(tokens) if tokens else None,
            'avg_tokens': _mean(tokens),
            'total_cost_usd': total_cost_usd,
            'avg_cost_usd': avg_cost_usd,
            'usd_to_rub': rate,
            'total_cost_rub': (total_cost_usd * rate) if (total_cost_usd is not None and rate is not None) else None,
            'avg_cost_rub': (avg_cost_usd * rate) if (avg_cost_usd is not None and rate is not None) else None,
        },
        'cases': results,
    }

    if out_file is not None:
        out_path = Path(out_file)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(description='Run eval_set.json against the Softline presales Agent.')
    parser.add_argument('--limit', type=int, default=None, help='Optional max number of eval cases to run')
    parser.add_argument('--db', type=Path, default=None, help='Path to SQLite database (default: Settings().db_path)')
    parser.add_argument('--eval-file', type=Path, default=DEFAULT_EVAL_FILE,
                        help='Path to eval_set.json (default: data/mined/eval_set.json)')
    parser.add_argument('--out', type=Path, default=DEFAULT_OUT_FILE,
                        help='Output JSON report path (default: data/eval_report.json)')
    parser.add_argument('--offline', action='store_true',
                        help='Run local offline dry-run without LLM API calls (token/cost metrics remain null)')
    return parser.parse_args(argv)


def main(argv=None):
    args = _parse_args(argv)
    settings = Settings()
    if args.db is not None:
        settings = settings.model_copy(update={'db_path': args.db})
    if not args.eval_file.is_file():
        print(f'Eval file not found: {args.eval_file}', file=sys.stderr)
        raise SystemExit(1)
    if not args.offline and (not settings.llm_model or not settings.llm_api_key):
        print(
            'LLM_API_KEY and LLM_MODEL are required for live eval. '
            'Configure .env or pass --offline for a local dry-run.',
            file=sys.stderr,
        )
        raise SystemExit(2)

    report = asyncio.run(run_eval(
        settings,
        eval_file=args.eval_file,
        out_file=args.out,
        limit=args.limit,
        offline=args.offline,
    ))
    summary = report['summary']
    print(
        f"[{report['mode']}] cases={report['total_cases']} passed={summary['passed']} "
        f"escalated={summary['escalated']} avg_latency_ms={summary['avg_latency_ms']} "
        f"report={args.out}"
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
