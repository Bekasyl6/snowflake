# Snowflake SQL Proxy для JanitorAI

Готовый код второго способа: JanitorAI → отдельный Render Web Service → Snowflake SQL API → AI_COMPLETE.
Старый LiteLLM с Gemini продолжает работать отдельно. Настоящая проверка на твоём аккаунте требует deployment и двух тестов ниже; локальные тесты используют имитацию Snowflake.

## 1. Snowflake

В SQL worksheet выполни весь setup.sql. Он выдаёт ролью JANITOR_CORTEX_ROLE доступ к AI_COMPLETE и к небольшому warehouse JANITOR_SQL_WH. Последний запрос проверяет именно роль прокси.

Если уже существует JANITOR_SQL_WH, проверь в интерфейсе его размер и AUTO_SUSPEND: IF NOT EXISTS не изменит существующий warehouse.

Warehouse и AI расходуют кредиты. AUTO_SUSPEND=60 уменьшает простой, но не является ограничением общего бюджета. SQL API не делает запросы бесплатными. Подтверждение, что расход покрывается trial и когда начнутся списания с карты, запроси у поддержки. Не делай Upgrade для этого способа.

## 2. Отдельный Web Service на Render

1. Распакуй архив. Создай новый GitHub-репозиторий и загрузи в его корень файлы из папки snowflake-sql-proxy. Не загружай реальные токены или файл .env.
2. Render → New → Web Service → подключи репозиторий.
3. Runtime: Docker. Dockerfile уже включён. Можно использовать Python runtime: Build Command `pip install -r requirements.txt`, Start Command `uvicorn app:app --host 0.0.0.0 --port $PORT`.
4. Для проверки можно выбрать доступный бесплатный план, если он предлагается в твоём аккаунте. Засыпание сервиса увеличивает время первого ответа. Платный Render план — отдельная оплата, её Snowflake-кредиты не покрывают.
5. Environment: добавь переменные из таблицы. Нажми Deploy.

| Переменная | Значение |
|---|---|
| SNOWFLAKE_API_BASE | Твой существующий account URL `https://…snowflakecomputing.com`, без пути API |
| SNOWFLAKE_API_KEY | Тот же действующий Snowflake PAT; сюда можно вставить с `pat/` или без, адаптер удалит префикс |
| SNOWFLAKE_ROLE | JANITOR_CORTEX_ROLE |
| SNOWFLAKE_WAREHOUSE | JANITOR_SQL_WH |
| SNOWFLAKE_MODELS | claude-opus-4-6,claude-sonnet-4-5 |
| PROXY_API_KEY | Новый длинный случайный секрет для этого прокси; не PAT |

Секрет для PROXY_API_KEY можно создать на своём компьютере:
`python -c "import secrets; print(secrets.token_urlsafe(32))"`

## 3. Разреши IP нового Render сервиса

У нового сервиса открой Connect → Outbound. Сравни все CIDR диапазоны с текущим network rule JANITOR_PROXY.PUBLIC.RENDER_OUTBOUND. Если они отличаются, дополни разрешённый список диапазонами нового сервиса, сохраняя старые.

Пример (замени ВСЕ placeholders до выполнения):
```sql
USE ROLE ACCOUNTADMIN;
ALTER NETWORK RULE JANITOR_PROXY.PUBLIC.RENDER_OUTBOUND
  SET VALUE_LIST = ('OLD_CIDR_1', 'OLD_CIDR_2', 'NEW_CIDR_1');
```
SET VALUE_LIST заменяет список целиком. Не вставляй этот пример как есть, не удаляй старые диапазоны LiteLLM. Меняем существующий rule пользователя JANITOR_CORTEX_USER, не policy администратора.

## 4. Две проверки с компьютера

Открой PowerShell в распакованной папке и запусти:
```powershell
powershell -ExecutionPolicy Bypass -File .\check.ps1
```
Введи URL нового Render сервиса и PROXY_API_KEY. Скрипт скрывает ввод ключа; отправляет SELECT 1, затем короткий запрос к Opus 4.6. Ожидаемый результат первого: `sql_api: ok`, второго: `choices[0].message.content` с ответом модели.

`/health` проверяет только запуск сервера, не Snowflake. Первый запуск бесплатного Render сервиса может быть долгим — сначала открой его /health и дождись ответа.

Ошибки:
- 401 от адаптера: PROXY_API_KEY не совпал.
- Snowflake SQL API 401: PAT истёк/неверен.
- Snowflake SQL API 403: проверь диапазоны IP, ограничения пользователя/токена; SQL API тоже может быть недоступен для аккаунта, его работу нельзя предполагать.
- Ошибка SQL: в Snowflake Query History найди запрос с tag `janitor_sql_proxy`. Там полный текст ошибки; сообщения и токены в поддержку не копируй.
- Warehouse permission: проверь setup.sql и SNOWFLAKE_ROLE.
- NULL: попробуй ту же модель через SQL под JANITOR_CORTEX_ROLE.
- Timeout: не нажимай повторную генерацию много раз — предыдущий запрос мог расходовать кредиты. Адаптер не повторяет генерацию автоматически.

## 5. JanitorAI после успешных тестов

Proxy URL: `https://ТВОЙ-НОВЫЙ-СЕРВИС.onrender.com/v1/chat/completions`
API Key: значение PROXY_API_KEY.
Custom Model: `claude-opus-4-6` (или `claude-sonnet-4-5`).

Если поле клиента просит именно Base URL, укажи `https://ТВОЙ-НОВЫЙ-СЕРВИС.onrender.com/v1`; если полный endpoint — полный адрес выше. Не удваивай `/v1/chat/completions`.
Первый тест: новый короткий чат, Streaming OFF, max tokens 512–1024, temperature 0.8.
После успешного ответа можно проверить Streaming ON. SSE отправляет комментарии ожидания и затем готовый ответ одним блоком: это совместимость с форматом streaming, а не получение токенов во время генерации.

## Ограничения

- AI_COMPLETE принимает историю как единый текст: все сообщения и роли сохранены внутри JSON, но не становятся отдельными системными/пользовательскими сообщениями API. Это может менять поведение RP. При включении Cortex REST API предпочтительнее вернуться к LiteLLM.
- Изображения, tools, structured output не поддерживаются. Дополнительные параметры JanitorAI (penalties, stop, top_k и т.д.) не применяются.
- Не заявляем доступ к Opus 5.5: используем модели, которые уже прошли твои SQL-проверки.
- max_tokens 1..8192; temperature 0..1. Если JanitorAI выставлен выше 1, снизь до 0.8.
- Нет автоматического удаления истории. При превышении 1 MB запрос отклоняется.
- Полная история снова оплачивается при каждом запросе. Проверь расход после первых сообщений; не обещаем тот же расход или caching, что у прямого API.
- Сообщения не записываются адаптером в файлы; bind variables не подставляются в SQL-строку. Сам Snowflake может хранить сведения о запросах по собственным правилам.
- PROXY_API_KEY и PAT — разные ключи. Не публикуй их.
- Возвращается finish_reason=stop: точную причину остановки AI_COMPLETE не предоставляет в использованном формате. Ответ может обрезаться лимитом.

## Локальный запуск (необязательно)

Установи Python 3.12, `pip install -r requirements.txt`, задай переменные окружения из .env.example и запусти `uvicorn app:app --host 0.0.0.0 --port 10000`. Файл .env автоматически не читается. IP локального компьютера потребуется отдельно разрешить; проще выполнять проверку через Render.

## Источники

https://docs.snowflake.com/en/developer-guide/sql-api/reference
https://docs.snowflake.com/en/user-guide/programmatic-access-tokens
https://docs.snowflake.com/en/sql-reference/functions/ai_complete-single-string
