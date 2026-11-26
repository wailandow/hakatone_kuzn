# Стенд пресейл-агента Softline

Хакатонный прототип. Каталог и база знаний хранятся в SQLite с FTS5.
Не добавляйте исходные выгрузки, базу, КП и `.env` в Git.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
python -m unittest discover -s tests -v
python -c 'from app.config import Settings; from app.db import initialize; initialize(Settings().db_path)'
```

При старте без ключей не следует обращаться к боевым сервисам. Значения секретов вводятся локально.

## Индекс каталога и базы знаний

Проверка на ограниченной выборке (XML или gzip, формат определяется по содержимому):

```sh
python -m scripts.build_db --feed ../FeedStore.yml.gz.xml --limit 50 --db data/sample.db --fetch-kb
```

`--fetch-kb` загружает только отсутствующие страницы; повторную сборку можно выполнить
с `--kb-cache raw_data/kb_cache` без доступа к сайтам. Записи менеджеров не удаляются.
Эмбеддинги строятся через FastEmbed на CPU. При первом запуске E5 скачивает ONNX и токенизатор;
для проверки без ONNX добавьте `--fts-only` и используйте отдельную БД: этот флаг
записывает выбранные строки **без** эмбеддингов. `--limit` ограничивает чтение фида,
но не удаляет строки, уже существующие в БД.

`app.db.search_catalog(conn, query, embedder=E5Embedding(), synonyms=...)` и
`app.db.search_kb(conn, query, embedder=E5Embedding())` объединяют FTS5/BM25 и
косинусное сходство через RRF. Переиспользуйте один экземпляр `E5Embedding`.
`search_catalog` сначала выводит точные совпадения артикула с исходным запросом,
даже если синоним изменил текст для FTS5. Без `embedder` или готового `query_vector`
семантическое ранжирование отключено. Пустой запрос в `search_catalog` с `group_id`
возвращает варианты этой группы.
