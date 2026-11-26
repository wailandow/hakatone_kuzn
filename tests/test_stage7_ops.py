import asyncio
import configparser
import csv
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock

from app.config import Settings
from app.db import connect, initialize

ROOT = Path(__file__).resolve().parents[1]


def _call(name, arguments, id_='call_1'):
    return NS(id=id_, type='function', function=NS(name=name, arguments=arguments))


def _completion(content=None, calls=(), prompt_tokens=500, completion_tokens=100):
    usage = (NS(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
             if prompt_tokens is not None and completion_tokens is not None else None)
    return NS(choices=[NS(message=NS(content=content, tool_calls=calls),
                          finish_reason='tool_calls' if calls else 'stop')],
              usage=usage)


class ExportCostStatsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / 'stats.db'
        initialize(self.db_path)

    def test_empty_db_handles_zero_requests_without_division_by_zero(self):
        from scripts.export_cost_stats import collect_cost_stats, format_markdown

        stats = collect_cost_stats(self.db_path, usd_to_rub=95.0)
        self.assertEqual(stats['total_requests'], 0)
        self.assertEqual(stats['metered_requests'], 0)
        self.assertEqual(stats['priced_requests'], 0)
        self.assertIsNone(stats['avg_tokens'])
        self.assertIsNone(stats['p50_tokens'])
        self.assertIsNone(stats['p95_tokens'])
        self.assertIsNone(stats['avg_latency_ms'])
        self.assertIsNone(stats['avg_cost_usd'])
        self.assertIsNone(stats['avg_cost_rub'])
        self.assertIsNone(stats['total_cost_usd'])
        self.assertIsNone(stats['total_cost_rub'])

        md = format_markdown(stats)
        self.assertIn('0', md)
        self.assertIn('n/a', md)

    def test_non_empty_agent_turn_stats_percentiles_and_rub_override(self):
        from scripts.export_cost_stats import collect_cost_stats, main

        rows = [
            ('c1', 'agent_turn', 100, 50, 0.001, 200),
            ('c2', 'agent_turn', 200, 100, 0.002, 400),
            ('c3', 'agent_turn', 300, 150, 0.003, 600),
            ('c4', 'agent_turn', 400, 200, 0.004, 800),
            ('c5', 'agent_turn', None, None, None, None),  # unmetered row must not become zero
            ('c1', 'kb_llm_call', 9999, 9999, 9.99, 9999),  # separate audit type must be excluded
        ]
        with connect(self.db_path) as db, db:
            db.executemany(
                'INSERT INTO audit_log(chat_id,event_type,prompt_tokens,completion_tokens,cost_usd,latency_ms) '
                'VALUES (?,?,?,?,?,?)',
                rows,
            )

        stats = collect_cost_stats(self.db_path, usd_to_rub=100.0)
        self.assertEqual(stats['event_type'], 'agent_turn')
        self.assertEqual(stats['total_requests'], 5)
        self.assertEqual(stats['metered_requests'], 4)
        self.assertEqual(stats['priced_requests'], 4)
        self.assertEqual(stats['latency_samples'], 4)
        # Totals: tokens = 150, 300, 450, 600 -> total=1500, avg=375.0, p50=375.0
        self.assertEqual(stats['total_tokens'], 1500)
        self.assertAlmostEqual(stats['avg_tokens'], 375.0)
        self.assertAlmostEqual(stats['p50_tokens'], 375.0)
        self.assertGreaterEqual(stats['p95_tokens'], 550.0)
        self.assertAlmostEqual(stats['avg_latency_ms'], 500.0)
        self.assertAlmostEqual(stats['p50_latency_ms'], 500.0)
        self.assertAlmostEqual(stats['total_cost_usd'], 0.01)
        self.assertAlmostEqual(stats['avg_cost_usd'], 0.0025)
        self.assertAlmostEqual(stats['total_cost_rub'], 1.0)
        self.assertAlmostEqual(stats['avg_cost_rub'], 0.25)

        csv_out = Path(self.tmp.name) / 'stats.csv'
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(['--db', str(self.db_path), '--usd-to-rub', '100', '--format', 'json', '--csv', str(csv_out)])
        self.assertEqual(code, 0)
        parsed = json.loads(buf.getvalue())
        self.assertEqual(parsed['total_requests'], 5)
        self.assertAlmostEqual(parsed['avg_cost_rub'], 0.25)
        self.assertTrue(csv_out.is_file())
        with csv_out.open(encoding='utf-8', newline='') as fh:
            csv_rows = list(csv.DictReader(fh))
        self.assertEqual(len(csv_rows), 5)

    def test_auto_falls_back_to_llm_call_when_no_agent_turn_rows(self):
        from scripts.export_cost_stats import collect_cost_stats

        with connect(self.db_path) as db, db:
            db.execute(
                'INSERT INTO audit_log(chat_id,event_type,prompt_tokens,completion_tokens,cost_usd,latency_ms) '
                "VALUES ('c1','llm_call',1000,200,0.005,350)"
            )
        stats = collect_cost_stats(self.db_path, usd_to_rub=90.0)
        self.assertEqual(stats['event_type'], 'llm_call')
        self.assertEqual(stats['total_requests'], 1)
        self.assertEqual(stats['total_tokens'], 1200)
        self.assertAlmostEqual(stats['avg_cost_rub'], 0.45)


class RunEvalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / 'eval.db'
        initialize(self.db_path)
        with connect(self.db_path) as db, db:
            db.execute(
                'INSERT INTO offers(id,group_id,name,vendor,vendor_code,url,price,currency,available,'
                'description,is_russian_registry,search_text) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                ('off-1', 'grp-1', 'Astra Linux Special Edition', 'Astra', 'ASTRA-SE-1',
                 'https://store.softline.ru/astra/', 15000.0, 'RUB', 1,
                 'Российская ОС из реестра ПО', 1, 'Astra Linux Special Edition ASTRA-SE-1'),
            )
        self.eval_file = Path(self.tmp.name) / 'eval_set.json'
        self.eval_cases = [
            {
                'category': 'follow_up',
                'intents': ['follow_up', 'order_status'],
                'products': [],
                'slots': ['payment'],
                'text': 'На каком этапе исполнение уже оформленного заказа?',
            },
            {
                'category': 'price',
                'intents': ['price'],
                'products': ['Astra Linux'],
                'slots': ['quantity'],
                'text': 'Сколько стоит лицензия Astra Linux?',
            },
        ]
        self.eval_file.write_text(json.dumps(self.eval_cases, ensure_ascii=False), encoding='utf-8')
        self.out_file = Path(self.tmp.name) / 'eval_report.json'

    async def test_offline_mode_runs_cleanly_without_fabricating_llm_metrics(self):
        from scripts.run_eval import run_eval

        settings = Settings(_env_file=None, db_path=self.db_path)
        report = await run_eval(
            settings,
            eval_file=self.eval_file,
            out_file=self.out_file,
            limit=2,
            offline=True,
        )
        self.assertTrue(self.out_file.is_file())
        self.assertEqual(report['mode'], 'offline_dry_run')
        self.assertTrue(report['dry_run'])
        self.assertEqual(report['total_cases'], 2)
        self.assertIsNone(report['summary']['total_tokens'])
        self.assertIsNone(report['summary']['total_cost_usd'])
        self.assertIsNone(report['summary']['avg_cost_usd'])
        for case in report['cases']:
            self.assertIsNone(case['prompt_tokens'])
            self.assertIsNone(case['completion_tokens'])
            self.assertIsNone(case['cost_usd'])
            self.assertTrue(case['reply'])
            self.assertGreaterEqual(case['latency_ms'], 0)

    async def test_live_mode_requires_llm_config_when_no_client_injected(self):
        from unittest.mock import patch
        from scripts.run_eval import main

        empty_settings = Settings(_env_file=None, db_path=self.db_path)
        with patch('scripts.run_eval.Settings', return_value=empty_settings):
            with self.assertRaises(SystemExit) as ctx:
                main(['--db', str(self.db_path), '--eval-file', str(self.eval_file), '--out', str(self.out_file)])
        self.assertNotEqual(ctx.exception.code, 0)

    def test_offline_cli_entrypoint_writes_report(self):
        from unittest.mock import patch
        from scripts.run_eval import main

        empty_settings = Settings(_env_file=None, db_path=self.db_path)
        buf = io.StringIO()
        with patch('scripts.run_eval.Settings', return_value=empty_settings), redirect_stdout(buf):
            code = main([
                '--offline', '--limit', '1',
                '--db', str(self.db_path),
                '--eval-file', str(self.eval_file),
                '--out', str(self.out_file),
            ])
        self.assertEqual(code, 0)
        self.assertIn('[offline_dry_run]', buf.getvalue())
        report = json.loads(self.out_file.read_text(encoding='utf-8'))
        self.assertEqual(report['total_cases'], 1)
        self.assertTrue(report['dry_run'])

    async def test_live_mode_with_test_double_measures_tokens_cost_tools_and_escalation(self):
        from scripts.run_eval import run_eval

        settings = Settings(
            _env_file=None,
            db_path=self.db_path,
            llm_model='test-model',
            llm_price_in_per_1m=2.0,
            llm_price_out_per_1m=8.0,
            usd_to_rub=100.0,
        )
        create = AsyncMock(side_effect=[
            # Case 1 (order_status): escalate_to_manager
            _completion(calls=[_call('escalate_to_manager', json.dumps({
                'reason': 'order_status',
                'summary': 'Клиент уточняет статус заказа.',
                'collected_slots': {},
            }), 'c1')], prompt_tokens=600, completion_tokens=100),
            # Case 2 (price): search_catalog then final answer
            _completion(calls=[_call('search_catalog', json.dumps({
                'query': 'Astra Linux',
            }), 'c2')], prompt_tokens=500, completion_tokens=50),
            _completion(content='Astra Linux Special Edition (ASTRA-SE-1) — 15 000 руб.',
                        prompt_tokens=700, completion_tokens=150),
        ])
        client = NS(chat=NS(completions=NS(create=create)))

        report = await run_eval(
            settings,
            eval_file=self.eval_file,
            out_file=self.out_file,
            offline=False,
            client=client,
        )
        self.assertEqual(report['mode'], 'live')
        self.assertFalse(report['dry_run'])
        self.assertEqual(report['total_cases'], 2)
        self.assertEqual(report['summary']['passed'], 2)

        case1, case2 = report['cases']
        self.assertTrue(case1['escalated'])
        self.assertTrue(case1['checks']['escalation_match'])
        self.assertIn('escalate_to_manager', case1['tools_used'])
        self.assertEqual(case1['prompt_tokens'], 600)
        self.assertEqual(case1['completion_tokens'], 100)

        self.assertFalse(case2['escalated'])
        self.assertIn('search_catalog', case2['tools_used'])
        self.assertEqual(case2['prompt_tokens'], 1200)
        self.assertEqual(case2['completion_tokens'], 200)
        self.assertAlmostEqual(report['summary']['total_cost_usd'],
                               (1800 * 2.0 + 300 * 8.0) / 1_000_000)
        self.assertAlmostEqual(report['summary']['total_cost_rub'],
                               report['summary']['total_cost_usd'] * 100.0)

        # Verify agent_turn rows were persisted in audit_log for export_cost_stats.py
        with connect(self.db_path) as db:
            turns = db.execute("SELECT COUNT(*) FROM audit_log WHERE event_type='agent_turn'").fetchone()[0]
        self.assertEqual(turns, 2)


class DeployConfigTests(unittest.TestCase):
    def test_systemd_unit_is_valid_and_hardened(self):
        unit_path = ROOT / 'deploy' / 'softline-agent.service'
        self.assertTrue(unit_path.is_file(), f'Missing {unit_path}')
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        parser.read(unit_path, encoding='utf-8')
        for section in ('Unit', 'Service', 'Install'):
            self.assertIn(section, parser)
        svc = parser['Service']
        self.assertEqual(svc['User'], 'softline')
        self.assertEqual(svc['Group'], 'softline')
        self.assertEqual(svc['WorkingDirectory'], '/opt/softline-agent')
        self.assertEqual(svc['EnvironmentFile'], '/opt/softline-agent/.env')
        self.assertIn('/opt/softline-agent/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000',
                      svc['ExecStart'])
        self.assertEqual(svc['Restart'], 'always')
        self.assertEqual(svc['RestartSec'], '3')
        self.assertEqual(svc['NoNewPrivileges'], 'true')
        self.assertEqual(svc['ProtectSystem'], 'strict')
        self.assertIn('/opt/softline-agent/data', svc['ReadWritePaths'])
        self.assertEqual(parser['Install']['WantedBy'], 'multi-user.target')

    def test_nginx_conf_has_proxy_headers_timeouts_and_security_guards(self):
        conf_path = ROOT / 'deploy' / 'nginx-softline-agent.conf'
        self.assertTrue(conf_path.is_file(), f'Missing {conf_path}')
        text = conf_path.read_text(encoding='utf-8')
        self.assertEqual(text.count('{'), text.count('}'))
        for expected in (
            'listen 80;',
            'http://127.0.0.1:8000',
            'proxy_set_header Host $host;',
            'proxy_set_header X-Real-IP $remote_addr;',
            'proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;',
            'proxy_set_header X-Forwarded-Proto $scheme;',
            'proxy_read_timeout 120s;',
            'autoindex off;',
            'client_max_body_size',
            '/mock-webim/',
            '/webhook/webim/',
            'access_log off;',
        ):
            self.assertIn(expected, text)


if __name__ == '__main__':
    unittest.main()
