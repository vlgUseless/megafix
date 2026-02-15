# Megafix

Megafix — GitHub App, которая реагирует на webhooks, прогоняет LLM-агента для
решения задач из Issue, открывает/обновляет PR и оставляет ревью по результатам CI.
Сервис состоит из:

- **webhook-server** (FastAPI) — принимает webhooks от GitHub.
- **worker** (RQ) — выполняет задачи агента и ревьюера.
- **redis** — очередь задач и дедупликация delivery.
- **review_state.db** — хранит состояние ревью/перезапусков.

## Быстрый старт (Docker)

### Требования

- Docker + Docker Compose
- Созданная GitHub App (см. ниже)
- App установлена на нужные репозитории (иначе webhooks/installation_id не появятся)
- LLM-сервис с OpenAI-compatible API (`/v1/chat/completions`)

### Запуск

1. Скопируйте `.env.example` в `.env` и заполните значения.
2. Если используете ключ из файла, смонтируйте его в контейнеры и укажите путь в
   `GITHUB_PRIVATE_KEY_PATH`.
3. Запустите:

```bash
docker compose up --build
```

Webhook будет слушать `http://localhost:8000/webhook`.

Для локальной разработки с Smee:

```bash
docker compose --profile dev up --build
```

## Управление зависимостями (uv)

Проект использует `uv` и lock-файл `uv.lock`.

- Установка зависимостей: `uv sync`
- Установка с dev-инструментами: `uv sync --dev`
- Обновление lock-файла после изменения зависимостей: `uv lock`

## Настройка GitHub App

### 1) Создайте GitHub App

GitHub → **Settings** → **Developer settings** → **GitHub Apps** → **New GitHub App**.

### 2) Webhook

- **Payload URL**
  - Прод: публичный URL на `https://<host>/webhook`
  - Локально: URL Smee (см. раздел ниже)
- **Webhook secret**: значение из `.env` (`WEBHOOK_SECRET`)
- **Active**: включить

### 3) Permissions (минимальные)

Repository permissions:
- **Contents**: Read & write (клон/пуш веток)
- **Issues**: Read & write (чтение Issue, комментарии)
- **Pull requests**: Read & write (создание/обновление PR, ревью)
- **Actions**: Read (чтение workflow runs и логов)
- **Metadata**: Read (обязательное)

### 4) Subscribe to events

- **Issues**
- **Workflow run**

### 5) Сгенерируйте ключ

В настройках App нажмите **Generate a private key**, скачайте `.pem`.

### 6) Установите App на нужные репозитории

Убедитесь, что App установлена на репозитории, где будут создаваться PR/комментарии.
Как правило: GitHub → **Settings** → **Developer settings** → **GitHub Apps** →
ваша App → **Install App** → выбрать аккаунт/организацию → выбрать репозитории.

## Получение webhook URL через Smee (локальная разработка)

1. Откройте `https://smee.io` и создайте новый канал.
2. Скопируйте URL вида `https://smee.io/<id>`.
3. Укажите этот URL в GitHub App (Webhook URL).
4. В `.env` задайте `SMEE_URL=...`.
5. Запустите `docker compose --profile dev up --build`.

Контейнер `smee` будет форвардить события на `http://web:8000/webhook`.

## Приватный ключ GitHub App

Можно хранить ключ прямо в `.env` (переводы строк через `\n`):

```bash
GITHUB_PRIVATE_KEY="-----BEGIN RSA PRIVATE KEY-----\n...\n-----END RSA PRIVATE KEY-----"
```

Или смонтировать файл в контейнеры и указать путь внутри контейнера:

```yaml
# docker-compose.yml
# - ./path/to/private-key.pem:/run/secrets/github_private_key.pem:ro
```

```bash
GITHUB_PRIVATE_KEY_PATH=/run/secrets/github_private_key.pem
```

## Как работает сервис

- **Issues**: при `opened`, `reopened` или `edited` (если менялись title/body)
  задача ставится в очередь.
- **workflow_run**: при `completed` запускается review-job. Перед публикацией
  вердикта агент агрегирует состояние всех runs для текущего `head_sha` PR и
  ждёт завершения CI (чтобы не публиковать ранние/противоречивые ревью).

Пайплайн issue:
1. Получаем installation token для GitHub App.
2. Клонируем репозиторий, создаем ветку `agent/issue-<id>`.
3. LLM-агент генерирует правки по issue.
4. Опционально запускается `AGENT_APPLY_CMD` (форматтер/линтер).
5. Коммит → push → create/update PR.
6. Опционально публикуются прогресс-комментарии (`COMMENT_PROGRESS=1`).

Пайплайн ревью:
1. Считываем workflow runs и логи упавших jobs.
2. Генерируем ревью (LLM, с fallback).
3. Публикуем PR review comment.
4. Если вердикт "request changes", возможно автоматическое повторное выполнение
   code-agent только для agent-PR (ветка `agent/issue-*` и ссылка `Closes #...`
   в PR body), с лимитом попыток `REVIEW_RERUN_MAX_ATTEMPTS`.

## Переменные окружения

### Обязательные

- `GITHUB_APP_ID`
- `GITHUB_PRIVATE_KEY` **или** `GITHUB_PRIVATE_KEY_PATH`
- `WEBHOOK_SECRET`
- `LLM_SERVICE_URL`
- `LLM_SERVICE_MODEL`
- `LLM_SERVICE_API_KEY` **или** `OPENAI_API_KEY`

### GitHub / Webhook

- `GITHUB_USER_AGENT` — User-Agent для GitHub API.
- `GITHUB_APP_NAME` — имя приложения (fallback для User-Agent).
- `COMMENT_PROGRESS` — 1/0, комментировать прогресс в issue.
- `LOG_LEVEL` — уровень логирования (default `INFO`).

### Redis / RQ

- `REDIS_URL` (default `redis://localhost:6379/0`)
- `RQ_QUEUE` (default `default`)
- `RQ_JOB_TIMEOUT` (default `20m`)
- `RQ_RESULT_TTL` (default `3600`)
- `RQ_FAILURE_TTL` (default `86400`)
- `DELIVERY_TTL_SEC` (default `86400`) — дедуп delivery webhook.

### Workspace / git

- `AGENT_WORKDIR` (default `.agent_workdir`)
- `KEEP_WORKDIR` (1/0) — сохранять рабочие директории после выполнения задач
- `GIT_AUTHOR_NAME`, `GIT_AUTHOR_EMAIL` — optional override автора коммита для `git`.
- `GIT_COMMITTER_NAME`, `GIT_COMMITTER_EMAIL` — optional override коммитера для `git`.
- `AGENT_APPLY_CMD` — команда, запускаемая после правок (например, `ruff`/`black`)

### Checks по умолчанию

- `run_checks` автоматически выбирает дефолтные команды под репозиторий:
  - `python -m pytest -q` — только если найдены pytest targets/config.
  - `python -m ruff check .` — только если найдена Ruff-конфигурация.
- Для non-Python или минимальных репозиториев это может означать пустой набор
  проверок по умолчанию (без ложных падений).

### LLM

- `LLM_SERVICE_URL` — базовый URL провайдера; если нет `/v1`, он будет добавлен.
- `LLM_SERVICE_MODEL` — идентификатор модели (меняйте здесь).
- `LLM_SERVICE_API_KEY` или `OPENAI_API_KEY`
- `LLM_MAX_TOKENS`
- `AGENT_MAX_PATCH_ATTEMPTS` (default `4`) — сколько подряд неуспешных попыток
  `repo_propose_edits`/`repo_propose_patches` допускается до принудительного
  завершения цикла.
- `REVIEW_LLM_SERVICE_URL` (optional) — отдельный URL для review-агента;
  если не задан, используется `LLM_SERVICE_URL`.
- `REVIEW_LLM_SERVICE_API_KEY` (optional) — отдельный API key для review-агента;
  если не задан, используется `LLM_SERVICE_API_KEY` / `OPENAI_API_KEY`.
- `REVIEW_LLM_SERVICE_MODEL` (optional) — отдельная модель для review-агента;
  если не задана, используется `LLM_SERVICE_MODEL`.
- `REVIEW_LLM_MAX_TOKENS` (optional) — отдельный `max_tokens` для review-агента;
  если не задан, используется `LLM_MAX_TOKENS`.
- `EDIT_ALLOW_CREATE_FILES` (default `0`) — разрешает op `create_file` в
  `repo_propose_edits` / `repo_apply_edits`.

### Review

- `REVIEW_STATE_DB` (default `review_state.db`)
- `REVIEW_MAX_DIFF_CHARS` (default `120000`)
- `REVIEW_MAX_PATCH_CHARS` (default `6000`)
- `REVIEW_MAX_LOG_CHARS` (default `4000`)
- `REVIEW_MAX_LOG_DOWNLOAD_BYTES` (default `2000000`) — ограничение на загрузку raw-логов failed jobs.
- `REVIEW_RERUN_MAX_ATTEMPTS` (default `5`)

## Guardrails для create_file

- По умолчанию создание файлов выключено (`EDIT_ALLOW_CREATE_FILES=0`).
- Разрешены только безопасные относительные пути внутри репозитория.
- На создание файла распространяются deny-правила:
  `PATCH_DENY_PREFIXES` и `PATCH_DENY_GLOBS`.
- `create_file` запрещён для уже существующих файлов.
- Для `create_file` требуется:
  `start_line=null`, `end_line=null`, `line=null`, `expected_old_text=""`.
- Размер `new_text` ограничен (`200000` символов на один файл).
- За один вызов edit tool можно создать не более `10` новых файлов.

## Как менять модель

1. В `.env` измените `LLM_SERVICE_MODEL` на нужную.
2. При необходимости обновите `LLM_SERVICE_URL` и ключ.
3. Если хотите отдельную модель для review-агента, задайте `REVIEW_LLM_SERVICE_MODEL`
   (и при необходимости `REVIEW_LLM_SERVICE_URL`/`REVIEW_LLM_SERVICE_API_KEY`).
4. Перезапустите контейнеры.

Сервис работает с OpenAI-compatible API и ожидает `POST /v1/chat/completions`.

## Локальный запуск без webhook (CLI)

```bash
python -m agent_cli run-issue --issue-url https://github.com/<owner>/<repo>/issues/<id>
python -m agent_cli review-pr --pr-url https://github.com/<owner>/<repo>/pull/<id>
```

Требуются те же переменные окружения (GitHub App + LLM).

## Troubleshooting

- `WEBHOOK_SECRET not set` или 401 → проверьте `WEBHOOK_SECRET` и подпись.
- `Missing GITHUB_APP_ID` / `Missing GitHub App private key` → проверьте `.env`
  и путь к ключу в контейнере.
- `LLM_SERVICE_URL is not configured` → задайте URL/модель/API key.
- Webhook не приходит → проверьте подписанные события и установку App;
  для локалки — что Smee запущен и URL совпадает.
