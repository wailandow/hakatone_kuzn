"""Generate privacy-projected artifacts from local, one-sided chat exports."""
import argparse
from collections import Counter
from dataclasses import dataclass
from itertools import chain
import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
RECORD = re.compile(r'#\S+\s+\[([^\]]+)\]\s+\[([^\]]*)\]\s*(.+)')
CATEGORIES = {
    'Просто цена конкретного товара': 'price',
    'Прочее (покупка)': 'other_purchase',
    'Счет / КП как документ (с реквизитами)': 'quote_document',
    'Конфигурация / спецификация': 'configuration',
    'Доступность вендора (можно ли купить)': 'vendor_availability',
    'Физлицо / самозанятый / срок': 'individual_purchase',
    'Скидки / льготы / спецусловия': 'discount',
    'Госзакупки / процедуры': 'public_procurement',
    'Follow-up / не дозвонился': 'follow_up',
}
QUESTIONS = {
    'price': 'Сколько стоит лицензия?',
    'other_purchase': 'Как оформить покупку?',
    'quote_document': 'Можно получить счёт или коммерческое предложение?',
    'configuration': 'Помогите подобрать конфигурацию и лицензию.',
    'vendor_availability': 'Можно ли купить продукт этого вендора?',
    'individual_purchase': 'На каких условиях можно оформить покупку как физлицо или самозанятый?',
    'discount': 'Есть ли скидки, льготы или специальные условия?',
    'public_procurement': 'Как оформить закупку по регламенту организации?',
    'follow_up': 'Можно ли уточнить, что с моим обращением?',
    'order_status': 'На каком этапе исполнение уже оформленного заказа?',
    'foreign_availability': 'Можно ли сейчас купить продукт зарубежного вендора?',
    'other': 'Помогите разобраться с покупкой.',
}
PRODUCT_PATTERNS = {
    'РЕД ОС': r'ред[ -]+ос|red[ -]+os',
    'RedCheck': r'red[ -]*check|ред[ -]*чек',
    'Power BI': r'power[ -]+bi|пауэр[ -]+би',
    'Astra Linux': r'astra(?:[ -]+linux)?|астра(?:[ -]+линукс)?',
    'MDaemon': r'mdaemon|мдаемон',
    'Kaspersky': r'kaspersky|каспер(?:ский|ского)?',
    'Microsoft 365': r'(?:microsoft|office|офис|ms)[ -]+365|m365',
    'Microsoft Office': r'(?:microsoft|ms)[ -]+office|офис[ -]+майкрософт',
    'Windows Server': r'windows[ -]+server|виндовс[ -]+сервер',
    'Windows': r'windows(?![ -]+server)|виндовс(?![ -]+сервер)|виндоус',
    'Adobe': r'adobe|адоб[еэ]?',
    'AutoCAD': r'autocad|автокад',
    'AutoDesk': r'autodesk',
    'VMware': r'vmware|вмваре',
    'Corel': r'corel(?:draw)?|корел',
    'JetBrains': r'jetbrains',
    'Oracle': r'oracle',
    'ESET': r'eset|нод[ -]*32',
    'Dr.Web': r'dr[. ]?web|доктор[ -]+веб',
    '1С': r'1[сc]',
    'WinRAR': r'winrar|винрар',
}
SLOT_PATTERNS = {
    'server': (r'сервер\w*|server', 'серверное развёртывание'),
    'virtualization': (r'виртуальн\w*|гипервизор\w*|vmware', 'виртуальная среда'),
    'quantity': (r'количеств\w*|\d+\s*(?:шт\.?|лицензи\w*)', 'количество лицензий'),
    'users': (r'пользовател\w*|сотрудник\w*|рабочи\w* мест\w*', 'число пользователей'),
    'devices': (r'компьютер\w*|устройств\w*|пк', 'число устройств'),
    'term': (r'срок\w*|продл\w*|подписк\w*|бессрочн\w*', 'срок лицензии или подписки'),
    'edition': (r'редакци\w*|верси\w*|edition', 'версия или редакция'),
    'registry': (r'реестр\w*|отечественн\w* по', 'реестр российского ПО'),
    'education': (r'образовательн\w*|учебн\w*|университет\w*|студент\w*', 'образовательная лицензия'),
    'payment': (r'оплат\w*|оплачен\w*|безнал\w*|ндс', 'условия оплаты'),
    'activation': (r'активац\w*|активир\w*|ключ\w*', 'активация или ключ'),
}
FOREIGN_PRODUCTS = {'Microsoft 365', 'Microsoft Office', 'Windows Server', 'Windows', 'Adobe',
                    'AutoCAD', 'AutoDesk', 'VMware', 'Corel', 'JetBrains', 'Oracle', 'ESET', 'Power BI'}
INTENT_PATTERNS = {
    'price': r'цен[ауые]|стоимост\w*|сколько стоит',
    'configuration': r'конфигурац\w*|спецификац\w*|совместим\w*|какая редакция',
    'discount': r'скид\w*|льгот\w*|спецуслови\w*|акци[яию]',
    'vendor_availability': r'доступн\w*|в наличии|можно.{0,18}(?:купить|приобрести)|санкци\w*|поставля\w*',
    'quote_document': r'коммерческ\w+ предложени\w+|кп|выстав\w* сч[её]т\w*',
    'order_status': (r'статус\w*\s+(?:моего\s+)?заказ\w*|где (?:мой |наш )?заказ\w*'
                     r'|заказ\w*.{0,30}(?:оплачен\w*|не приш[её]л|не получ\w*)'
                     r'|(?:оплатил\w*|оплачен\w*|после оплат\w*).{0,55}'
                     r'(?:не приш[её]л|не получ\w*|когда|жд[её]м|ждать\W+(?:ключ\w*|лицензи\w*|заказ\w*))'
                     r'|когда.{0,24}(?:отгруз\w*|пришл\w*)'),
}


def boundary(pattern):
    return re.compile(rf'(?<!\w)(?:{pattern})(?!\w)', re.I)


PRODUCTS = {name: boundary(pattern) for name, pattern in PRODUCT_PATTERNS.items()}
SLOTS = {key: boundary(pattern) for key, (pattern, _) in SLOT_PATTERNS.items()}
INTENTS = {key: boundary(pattern) for key, pattern in INTENT_PATTERNS.items()}
# Remove contact/URI tokens before matching even allowlisted words inside them.
LINK_OR_CONTACT = re.compile(r'(?:\w+://|www\.|\S*@|\b[\w.-]+\.[a-z]{2,}(?=[/?#]))[^\s<>]*', re.I)


@dataclass(frozen=True)
class Record:
    category: str
    body: str


def read_records(paths):
    for path in paths:
        with path.open(encoding='utf-8-sig') as source:
            for line_no, line in enumerate(source, 1):
                if not line.strip():
                    continue
                match = RECORD.fullmatch(line.strip())
                if not match:
                    raise ValueError(f'invalid chat record at line {line_no}')
                tag, _, body = match.groups()
                yield Record(CATEGORIES.get(tag.casefold(), 'other'), LINK_OR_CONTACT.sub(' ', body.casefold()))


# Use source categories case-insensitively; only fixed slugs can leave the process.
CATEGORIES = {tag.casefold(): slug for tag, slug in CATEGORIES.items()}


def project(record):
    """Project a real record to controlled cues; never serialize raw text or redacted fragments."""
    products = [name for name, pattern in PRODUCTS.items() if pattern.search(record.body)]
    slots = [key for key, pattern in SLOTS.items() if pattern.search(record.body)]
    intents = {key for key, pattern in INTENTS.items() if pattern.search(record.body)}
    intents.add(record.category)
    if 'vendor_availability' in intents and FOREIGN_PRODUCTS.intersection(products):
        intents.add('foreign_availability')
    primary = next((key for key in ('order_status', 'foreign_availability') if key in intents), record.category)
    # Include secondary evidence in the prompt, without treating it as ground-truth routing.
    topics = [QUESTIONS[key] for key in ('price', 'configuration', 'discount', 'vendor_availability', 'quote_document')
              if key in intents and key != primary and not (primary == 'foreign_availability' and key == 'vendor_availability')]
    text = ' '.join([QUESTIONS[primary], *topics])
    if products:
        text += ' Продукты: ' + ', '.join(products) + '.'
    if slots:
        text += ' Детали: ' + ', '.join(SLOT_PATTERNS[key][1] for key in slots) + '.'
    return dict(category=record.category, intents=sorted(intents), products=products, slots=slots, text=text)


def write_json(out_dir, name, data):
    (out_dir / name).write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def select_eval(projected, limit):
    """Cover as many source tags as possible with distinct prompts, then fill the set."""
    rare_intents = ('order_status', 'foreign_availability')
    candidates = {}
    for case in projected:
        candidates.setdefault(case['text'], {}).setdefault(case['category'], case)
    selected = []
    tag_counts = Counter()
    seen = set()

    def cues(case):
        return set(chain(case['products'], case['slots'], case['intents']))

    def add(case):
        selected.append(case)
        del candidates[case['text']]
        tag_counts[case['category']] += 1
        seen.update(cues(case))

    # Keep every observed (tag, text) pair until the distinct representatives are assigned.
    by_tag = {}
    for group in candidates.values():
        for tag, case in group.items():
            by_tag.setdefault(tag, []).append(case)
    for cases in by_tag.values():
        cases.sort(key=lambda case: (tuple(intent not in case['intents'] for intent in rare_intents),
                                     -len(cues(case)), case['text']))
    assigned = {}

    def assign(tag, visited):
        for case in by_tag[tag]:
            text = case['text']
            if text in visited:
                continue
            visited.add(text)
            # Move the previous owner to another prompt if that lets both tags be covered.
            if text not in assigned or assign(assigned[text]['category'], visited):
                assigned[text] = case
                return True
        return False

    for tag in sorted(by_tag, key=lambda tag: (len(by_tag[tag]), tag)):
        if len(assigned) >= limit:
            break
        assign(tag, set())
    for case in assigned.values():
        add(case)

    def remaining():
        return chain.from_iterable(group.values() for group in candidates.values())

    def take(eligible):
        best = min(eligible, key=lambda case: (tag_counts[case['category']],
                   -len(cues(case) - seen), case['text'], case['category']), default=None)
        if best is not None and len(selected) < limit:
            add(best)

    for intent in rare_intents:
        for _ in range(2 - sum(intent in case['intents'] for case in selected)):
            take(case for case in remaining() if intent in case['intents'])
    while candidates and len(selected) < limit:
        take(remaining())
    return selected


# Curated spellings from the task are unconditional; other safe candidates need evidence.
SEED_ALIASES = {'ред ос': 'РЕД ОС', 'red check': 'RedCheck',
                'ms power bi': 'Power BI', 'астра': 'Astra Linux'}
ALIAS_CANDIDATES = {
    'ред-чек': 'RedCheck', 'редчек': 'RedCheck', 'red os': 'РЕД ОС',
    'astra': 'Astra Linux', 'астра линукс': 'Astra Linux', 'каспер': 'Kaspersky',
    'касперский': 'Kaspersky', 'касперского': 'Kaspersky', 'мдаемон': 'MDaemon',
    'майкрософт офис': 'Microsoft Office', 'офис майкрософт': 'Microsoft Office',
    'офис 365': 'Microsoft 365', 'm365': 'Microsoft 365',
    'виндовс': 'Windows', 'виндоус': 'Windows', 'виндовс сервер': 'Windows Server',
    'адоб': 'Adobe', 'адобе': 'Adobe', 'автокад': 'AutoCAD', 'корел': 'Corel',
    'нод32': 'ESET', 'доктор веб': 'Dr.Web', 'винрар': 'WinRAR',
}
# Category and fixed question candidates. Corpus frequency orders questions, not policy.
PLAYBOOKS = {
    'os': (
        {'Astra Linux', 'РЕД ОС', 'Windows', 'Windows Server'},
        {'deployment': (r'физическ\w*|виртуальн\w*|сервер\w*|server', 'Установка на физический сервер, ВМ или рабочую станцию?'),
         'term': (r'срок\w*|бессрочн\w*|подписк\w*', 'На какой срок нужна лицензия?'),
         'support': (r'техподдержк\w*|техническ\w+ поддержк\w*|тп', 'Нужна ли техподдержка и какого уровня?'),
         'organization': (r'организаци\w*|государственн\w*|коммерческ\w*', 'Какой у вас тип организации?')},
    ),
    'scanner': (
        {'RedCheck'},
        {'assets': (r'актив\w*|ip|узл\w*|адрес\w*', 'Сколько активов или IP-адресов нужно сканировать?'),
         'edition': (r'редакци\w*|верси\w*|edition', 'Какая редакция и функции сканирования нужны?'),
         'term': (r'срок\w*|продл\w*|подписк\w*', 'Это новая лицензия или продление, на какой срок?')},
    ),
    'office': (
        {'Microsoft Office', 'Microsoft 365'},
        {'users': (r'пользовател\w*|устройств\w*|рабочи\w* мест\w*', 'На сколько пользователей или устройств нужна лицензия?'),
         'term': (r'срок\w*|бессрочн\w*|подписк\w*', 'Нужна подписка на определённый срок или бессрочная лицензия?'),
         'registry': (r'реестр\w*|отечественн\w* по', 'Обязательно ли наличие продукта в реестре российского ПО?')},
    ),
    'mail': (
        {'MDaemon'},
        {'users': (r'пользовател\w*|ящик\w*', 'Сколько пользователей или почтовых ящиков нужно обслуживать?'),
         'term': (r'срок\w*|продл\w*|подписк\w*', 'Нужна новая лицензия или продление, на какой срок?'),
         'registry': (r'реестр\w*|отечественн\w* по', 'Обязательно ли наличие продукта в реестре российского ПО?'),
         'deployment': (r'облак\w*|сервер\w*|on-premise', 'Почта будет на собственном сервере или в облаке?')},
    ),
}


def build_synonyms(records):
    mined = {}
    for alias, value in ALIAS_CANDIDATES.items():
        pattern = boundary(re.escape(alias).replace(r'\ ', r'[\s-]+'))
        canonical = boundary(re.escape(value))

        def has_alias(body):
            canonical_spans = [match.span() for match in canonical.finditer(body)]
            return any(not any(start <= match.start() and match.end() <= end
                               for start, end in canonical_spans)
                       for match in pattern.finditer(body))

        if sum(has_alias(record.body) for record in records) >= 2:
            mined[alias] = value
    return SEED_ALIASES | mined


def build_playbooks(training):
    playbooks = {}
    for family, (products, questions) in PLAYBOOKS.items():
        relevant = [record for record, case in training if products.intersection(case['products'])]
        entries = []
        for slot, (cue, question) in questions.items():
            pattern = boundary(cue)
            mentions = sum(bool(pattern.search(record.body)) for record in relevant)
            entries.append(dict(slot=slot, question=question, mentions=mentions))
        playbooks[family] = sorted(entries, key=lambda entry: -entry['mentions'])
    return playbooks


def mine(paths, out_dir, *, eval_size=28):
    records = list(read_records(sorted(set(paths))))
    projections = [project(record) for record in records]
    selected = select_eval(projections, eval_size)
    if len(selected) != eval_size or eval_size < 1:
        raise ValueError(f'need {eval_size} distinct eval cases; found {len(selected)}')
    # Hold out every row that projects to a selected prompt, including duplicates.
    eval_texts = {case['text'] for case in selected}
    training = [(record, case) for record, case in zip(records, projections) if case['text'] not in eval_texts]
    synonyms = build_synonyms([record for record, _ in training])
    playbooks = build_playbooks(training)
    out_dir.mkdir(parents=True, exist_ok=True)
    write_json(out_dir, 'eval_set.json', selected)
    write_json(out_dir, 'synonyms.json', synonyms)
    write_json(out_dir, 'slot_playbooks.json', playbooks)
    return len(records), len(training), len(synonyms)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--chat-dir', type=Path, default=ROOT.parent / 'support_chat')
    parser.add_argument('--out-dir', type=Path, default=ROOT / 'data' / 'mined')
    parser.add_argument('--eval-size', type=int, default=28)
    args = parser.parse_args(argv)
    paths = [args.chat_dir / f'buy_part_{part}_masked.txt' for part in range(1, 4)]
    try:
        total, training, aliases = mine(paths, args.out_dir, eval_size=args.eval_size)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(f'{total} records; {training} mining records; {args.eval_size} unique eval cases; {aliases} aliases')


if __name__ == '__main__':
    main()
