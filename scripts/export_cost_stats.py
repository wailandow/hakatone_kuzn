"""Export unit-economics statistics from SQLite audit_log as Markdown, JSON, and optional CSV."""
import argparse
import csv
import json
from math import isfinite
from pathlib import Path
import sys

from app.config import Settings
from app.db import connect

CSV_FIELDS = (
    'id', 'chat_id', 'event_type', 'prompt_tokens', 'completion_tokens',
    'total_tokens', 'cost_usd', 'cost_rub', 'latency_ms', 'created_at',
)


def _percentile(values, pct):
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    pos = (pct / 100.0) * (len(ordered) - 1)
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    frac = pos - low
    return float(ordered[low] + (ordered[high] - ordered[low]) * frac)


def _mean(values):
    return float(sum(values) / len(values)) if values else None


def _select_event_type(db, event_type):
    if event_type != 'auto':
        return event_type
    has_turns = db.execute("SELECT 1 FROM audit_log WHERE event_type='agent_turn' LIMIT 1").fetchone()
    if has_turns is not None:
        return 'agent_turn'
    has_calls = db.execute("SELECT 1 FROM audit_log WHERE event_type='llm_call' LIMIT 1").fetchone()
    return 'llm_call' if has_calls is not None else 'agent_turn'


def _fetch_rows(db_path, event_type='auto'):
    with connect(db_path, timeout=2) as db:
        selected = _select_event_type(db, event_type)
        rows = db.execute(
            '''SELECT id, chat_id, event_type, prompt_tokens, completion_tokens, cost_usd, latency_ms, created_at
               FROM audit_log WHERE event_type=? ORDER BY id''',
            (selected,),
        ).fetchall()
    return selected, [dict(row) for row in rows]


def collect_cost_stats(db_path, *, usd_to_rub=None, event_type='auto', csv_path=None):
    rate = None
    if usd_to_rub is not None and isfinite(usd_to_rub) and usd_to_rub > 0:
        rate = float(usd_to_rub)

    selected, rows = _fetch_rows(Path(db_path), event_type=event_type)

    prompt_vals = []
    completion_vals = []
    total_token_vals = []
    cost_usd_vals = []
    latency_vals = []

    for row in rows:
        p = row['prompt_tokens']
        c = row['completion_tokens']
        if isinstance(p, int) and isinstance(c, int) and p >= 0 and c >= 0:
            prompt_vals.append(p)
            completion_vals.append(c)
            total_token_vals.append(p + c)
        cost = row['cost_usd']
        if isinstance(cost, (int, float)) and isfinite(cost) and cost >= 0:
            cost_usd_vals.append(float(cost))
        lat = row['latency_ms']
        if isinstance(lat, (int, float)) and isfinite(lat) and lat >= 0:
            latency_vals.append(float(lat))

    total_cost_usd = float(sum(cost_usd_vals)) if cost_usd_vals else None
    avg_cost_usd = _mean(cost_usd_vals)
    total_cost_rub = (total_cost_usd * rate) if (total_cost_usd is not None and rate is not None) else None
    avg_cost_rub = (avg_cost_usd * rate) if (avg_cost_usd is not None and rate is not None) else None

    if csv_path is not None:
        out = Path(csv_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open('w', encoding='utf-8', newline='') as fh:
            writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
            writer.writeheader()
            for row in rows:
                p, c = row['prompt_tokens'], row['completion_tokens']
                total_tok = (p + c) if (isinstance(p, int) and isinstance(c, int)) else None
                cost = row['cost_usd']
                cost_rub = (float(cost) * rate) if (isinstance(cost, (int, float)) and isfinite(cost) and rate) else None
                writer.writerow({
                    'id': row['id'],
                    'chat_id': row['chat_id'],
                    'event_type': row['event_type'],
                    'prompt_tokens': p,
                    'completion_tokens': c,
                    'total_tokens': total_tok,
                    'cost_usd': cost,
                    'cost_rub': cost_rub,
                    'latency_ms': row['latency_ms'],
                    'created_at': row['created_at'],
                })

    return {
        'db_path': str(db_path),
        'event_type': selected,
        'total_requests': len(rows),
        'metered_requests': len(total_token_vals),
        'priced_requests': len(cost_usd_vals),
        'latency_samples': len(latency_vals),
        'total_prompt_tokens': sum(prompt_vals) if prompt_vals else None,
        'total_completion_tokens': sum(completion_vals) if completion_vals else None,
        'total_tokens': sum(total_token_vals) if total_token_vals else None,
        'avg_tokens': _mean(total_token_vals),
        'p50_tokens': _percentile(total_token_vals, 50),
        'p95_tokens': _percentile(total_token_vals, 95),
        'avg_latency_ms': _mean(latency_vals),
        'p50_latency_ms': _percentile(latency_vals, 50),
        'p95_latency_ms': _percentile(latency_vals, 95),
        'total_cost_usd': total_cost_usd,
        'avg_cost_usd': avg_cost_usd,
        'usd_to_rub': rate,
        'total_cost_rub': total_cost_rub,
        'avg_cost_rub': avg_cost_rub,
    }


def _fmt(value, fmt_spec='.2f', suffix=''):
    if value is None:
        return 'n/a'
    return f'{value:{fmt_spec}}{suffix}'


def format_markdown(stats):
    lines = [
        f"## Unit Economics (`{stats['event_type']}`)",
        '',
        '| Metric | Value |',
        '| --- | ---: |',
        f"| Total requests | {stats['total_requests']} |",
        f"| Metered requests (tokens) | {stats['metered_requests']} |",
        f"| Priced requests (cost) | {stats['priced_requests']} |",
        f"| Latency samples | {stats['latency_samples']} |",
        f"| Total tokens | {stats['total_tokens'] if stats['total_tokens'] is not None else 'n/a'} |",
        f"| Avg / p50 / p95 tokens | {_fmt(stats['avg_tokens'], '.1f')} / {_fmt(stats['p50_tokens'], '.1f')} / {_fmt(stats['p95_tokens'], '.1f')} |",
        f"| Avg / p50 / p95 latency (ms) | {_fmt(stats['avg_latency_ms'], '.1f')} / {_fmt(stats['p50_latency_ms'], '.1f')} / {_fmt(stats['p95_latency_ms'], '.1f')} |",
        f"| Avg cost per turn (USD) | {_fmt(stats['avg_cost_usd'], '.6f', ' USD')} |",
        f"| Total cost (USD) | {_fmt(stats['total_cost_usd'], '.6f', ' USD')} |",
        f"| USD/RUB rate | {_fmt(stats['usd_to_rub'], '.2f')} |",
        f"| Avg cost per turn (RUB) | {_fmt(stats['avg_cost_rub'], '.4f', ' RUB')} |",
        f"| Total cost (RUB) | {_fmt(stats['total_cost_rub'], '.4f', ' RUB')} |",
    ]
    return '\n'.join(lines)


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(description='Export unit-economics stats from SQLite audit_log.')
    parser.add_argument('--db', type=Path, default=None, help='Path to SQLite database (default: Settings().db_path)')
    parser.add_argument('--usd-to-rub', type=float, default=None, help='Override USD_TO_RUB rate from Settings')
    parser.add_argument('--event-type', choices=('auto', 'agent_turn', 'llm_call'), default='auto',
                        help='Audit event type to aggregate (default: auto -> agent_turn else llm_call)')
    parser.add_argument('--format', choices=('markdown', 'json'), default='markdown', dest='output_format',
                        help='Output format (default: markdown)')
    parser.add_argument('--csv', type=Path, default=None, help='Optional CSV export path for raw rows')
    return parser.parse_args(argv)


def main(argv=None):
    args = _parse_args(argv)
    settings = Settings()
    db_path = args.db or settings.db_path
    if not db_path.is_file():
        print(f'Database not found: {db_path}', file=sys.stderr)
        return 1
    rate = settings.usd_to_rub if args.usd_to_rub is None else args.usd_to_rub
    if not isfinite(rate) or rate < 0:
        print('Invalid --usd-to-rub value: must be a finite non-negative number.', file=sys.stderr)
        return 2
    stats = collect_cost_stats(db_path, usd_to_rub=rate, event_type=args.event_type, csv_path=args.csv)
    if args.output_format == 'json':
        print(json.dumps(stats, ensure_ascii=False, indent=2))
    else:
        print(format_markdown(stats))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
