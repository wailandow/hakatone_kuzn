# Стенд пресейл-агента Softline

Хакатонный прототип. Начальный этап: настройки и единый SQLite с FTS5.
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
