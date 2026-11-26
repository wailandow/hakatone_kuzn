import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest


class ChatMiningTests(unittest.TestCase):
    def test_astra_alias_frequency_excludes_every_exact_canonical_span(self):
        from scripts.mine_chats import Record, build_synonyms

        canonical_only = Record('other_purchase', 'ASTRA Linux, затем Astra Linux.')
        standalone = Record('price', 'Astra Linux или astra? Astra Linux, astra.')
        self.assertNotIn('astra', build_synonyms([canonical_only, standalone]))
        second_standalone = Record('configuration', 'astra для сервера; Astra Linux.')
        synonyms = build_synonyms([canonical_only, standalone, second_standalone])
        self.assertEqual(synonyms['astra'], 'Astra Linux')

    def test_cli_writes_frequency_supported_aliases_and_ranked_playbooks(self):
        from scripts.mine_chats import main
        from contextlib import redirect_stdout
        from io import StringIO

        with TemporaryDirectory() as directory:
            root = Path(directory)
            for part in range(1, 4):
                (root / f'buy_part_{part}_masked.txt').write_text(
                    '#A [Конфигурация / спецификация] [] Каспер: лицензия, редакция, сервер?\n'
                    '#B [Прочее (покупка)] [] Каспер на компьютеры, какой срок?\n'
                    '#C [Конфигурация / спецификация] [] Astra Linux: версия, редакция?\n',
                    encoding='utf-8',
                )
            (root / 'other.txt').write_text('not a masked chat', encoding='utf-8')
            out = root / 'mined'
            stdout = StringIO()
            with redirect_stdout(stdout):
                main(['--chat-dir', str(root), '--out-dir', str(out), '--eval-size', '1'])
            self.assertIn('9 records', stdout.getvalue())
            synonyms = json.loads((out / 'synonyms.json').read_text(encoding='utf-8'))
            self.assertEqual(synonyms['каспер'], 'Kaspersky')
            self.assertNotIn('виндоус', synonyms)
            self.assertEqual(synonyms['ms power bi'], 'Power BI')
            playbooks = json.loads((out / 'slot_playbooks.json').read_text(encoding='utf-8'))
            self.assertTrue({'os', 'scanner', 'office', 'mail'} <= set(playbooks))
            self.assertTrue(all(3 <= len(entries) <= 4 for entries in playbooks.values()))
            self.assertTrue(all(entry['question'].endswith('?') for entries in playbooks.values() for entry in entries))

    def test_eval_covers_categories_and_rare_intents_without_duplicate_prompts(self):
        from scripts.mine_chats import mine

        with TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / 'buy_part_1_masked.txt'
            second = root / 'buy_part_2_masked.txt'
            first.write_text(
                '#A [Просто цена конкретного товара] [] Какая цена Astra Linux?\n'
                '#B [Просто цена конкретного товара] [] Какая цена Astra Linux?\n'
                '#C [Просто цена конкретного товара] [] Какая цена RedCheck?\n', encoding='utf-8',
            )
            second.write_text(
                '#D [Прочее (покупка)] [] Подскажите статус моего заказа.\n'
                '#E [Доступность вендора (можно ли купить)] [] Можно купить Adobe?\n'
                '#F [Конфигурация / спецификация] [] Какая редакция для сервера?\n', encoding='utf-8',
            )
            out = root / 'mined'
            mine([first, second], out, eval_size=4)
            path = out / 'eval_set.json'
            original = path.read_bytes()
            cases = json.loads(original)
            self.assertEqual(len({case['text'] for case in cases}), 4)
            self.assertEqual({case['category'] for case in cases},
                             {'price', 'other_purchase', 'vendor_availability', 'configuration'})
            self.assertTrue({'order_status', 'foreign_availability'} <=
                            {intent for case in cases for intent in case['intents']})
            mine([second, first], out, eval_size=4)
            self.assertEqual(path.read_bytes(), original)

    def test_eval_covers_tags_even_when_a_rare_prompt_is_shared(self):
        from scripts.mine_chats import mine

        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'input_masked.txt'
            source.write_text(
                '#A [Прочее (покупка)] [] Где мой заказ?\n'
                '#B [Прочее (покупка)] [] Где мой заказ Astra Linux?\n'
                '#C [Follow-up / не дозвонился] [] Где мой заказ?\n',
                encoding='utf-8',
            )
            out = root / 'mined'
            mine([source], out, eval_size=2)
            cases = json.loads((out / 'eval_set.json').read_text(encoding='utf-8'))
            self.assertEqual(len({case['text'] for case in cases}), 2)
            self.assertEqual({case['category'] for case in cases}, {'other_purchase', 'follow_up'})
            self.assertEqual(next(case for case in cases if case['category'] == 'follow_up')['products'], [])

    def test_eval_reassigns_a_shared_prompt_to_cover_another_tag(self):
        from scripts.mine_chats import Record, project, select_eval

        products_by_tag = {
            'follow_up': ('astra linux', 'redcheck'),
            'individual_purchase': ('astra linux', 'windows'),
            'other_purchase': ('astra linux', 'windows'),
        }
        cases = [project(Record(tag, f'где мой заказ {product}?'))
                 for tag, products in products_by_tag.items() for product in products]
        selected = select_eval(cases, limit=3)
        self.assertEqual(len({case['text'] for case in selected}), 3)
        self.assertEqual({case['category'] for case in selected}, products_by_tag.keys())
        self.assertEqual(next(case for case in selected if case['category'] == 'follow_up')['products'], ['RedCheck'])

    def test_eval_does_not_invent_prompts_when_tags_cannot_all_be_covered(self):
        from scripts.mine_chats import Record, project, select_eval

        cases = [project(Record(tag, 'где мой заказ?'))
                 for tag in ('follow_up', 'other_purchase', 'individual_purchase')]
        cases.extend(project(Record('price', f'какая цена {product}?'))
                     for product in ('astra linux', 'redcheck', 'windows'))
        selected = select_eval(cases, limit=4)
        self.assertEqual(len({case['text'] for case in selected}), 4)
        self.assertEqual(len({case['category'] for case in selected}), 2)
        self.assertTrue(all(case in cases for case in selected))

    def test_eval_recognizes_post_payment_key_follow_up_as_order_status(self):
        from scripts.mine_chats import mine

        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'input_masked.txt'
            source.write_text(
                '#A [Follow-up / не дозвонился] [] Оплатили вчера. Сколько ждать ключ?\n'
                '#B [Прочее (покупка)] [] Сколько ждать ключ до покупки?\n', encoding='utf-8',
            )
            out = root / 'mined'
            mine([source], out, eval_size=2)
            paid, unpaid = json.loads((out / 'eval_set.json').read_text(encoding='utf-8'))
            self.assertIn('order_status', paid['intents'])
            self.assertNotIn('order_status', unpaid['intents'])

    def test_mine_projects_real_case_without_copying_private_fields(self):
        from scripts.mine_chats import mine

        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'input_masked.txt'
            source.write_text(
                '#CHATPRIVATE [Просто цена конкретного товара] '
                '[store.example/path?token=URLPRIVATE] Сколько стоит Astra Linux на сервере? '
                'Иван Иванов, ООО Секрет, user@example.com, +7 (999) 123-45-67, артикул SKU-PRIVATE.\n',
                encoding='utf-8-sig',
            )
            out = root / 'mined'
            mine([source], out, eval_size=1)
            serialized = (out / 'eval_set.json').read_text(encoding='utf-8')
            case, = json.loads(serialized)
            self.assertEqual(case['category'], 'price')
            self.assertIn('price', case['intents'])
            self.assertIn('Astra Linux', case['products'])
            self.assertIn('server', case['slots'])
            for private in ('CHATPRIVATE', 'URLPRIVATE', 'example', 'Иван', 'Секрет', '999', 'SKU-PRIVATE'):
                self.assertFalse(private in serialized, 'retained unexpected private fixture value')
