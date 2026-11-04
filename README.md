# Photo Approval Bot

Telegram-бот для апруву фото від менеджерів.

## Що робить

1. Менеджер пише `/start`, вводить ім'я → апрувер підтверджує доступ.
2. Менеджер надсилає фото в бот і обирає стать: **Male / Female**.
3. Апрувер отримує фото з кнопками **✅ Approve / ❌ Reject**.
   - **Reject** → апрувер пише коментар → він приходить менеджеру.
   - **Approve** → фото зберігається на Google Drive (`photo/male` або `photo/female`),
     у Google Таблицю додається рядок: Manager · Notes · Added Date · Link · Gender.

Апрувер — людина з `APPROVER_ID` у `.env`.

## Файли

| Файл | За що відповідає |
|---|---|
| `bot.py` | увесь код бота (Python, aiogram 3) |
| `pyproject.toml` / `uv.lock` | залежності (пакетний менеджер [uv](https://docs.astral.sh/uv/)) |
| `Dockerfile` | образ для продакшну (python:3.13-alpine, ~22 МБ стиснутий) |
| `fly.toml` | конфіг Fly.io (застосунок `photo-approval-bot`, регіон `fra`, том `photobot_data` → `/data`) |
| `package.json` | лише скрипти: `npm run dev`, `npm run deploy`, `npm run logs` |
| `.github/workflows/deploy.yml` | CI: деплой при пуші в `main` з ручним підтвердженням |
| `.env.example` | шаблон налаштувань (dev); прод-значення живуть у `fly secrets` |
| `apps_script.gs` | код Google Apps Script, що кладе фото на Drive і пише рядок у таблицю (розгортається в Google, не в образі) |

## Локальний запуск

```bash
uv sync
cp .env.example .env   # вписати свої значення
npm run dev            # = uv run bot.py; bot.db створиться поруч
```

## Деплой (Fly.io)

Застосунок і том уже створені. Секрети задаються один раз (прод-значення, не dev):

```bash
fly secrets set -a photo-approval-bot \
  BOT_TOKEN=... APPROVER_ID=... SHEETS_WEBHOOK_URL=... SHEETS_SECRET=...
```

Деплой з локальної машини: `npm run deploy`.

### CI

При кожному пуші в `main` GitHub Actions створює джобу **Deploy to Fly.io**, яка чекає ручного підтвердження.
Одноразове налаштування:

1. `fly tokens create deploy -a photo-approval-bot` → додати як секрет репозиторію `FLY_API_TOKEN`
   (Settings → Secrets and variables → Actions).
2. Settings → Environments → `production` → **Required reviewers** → додати себе.
   Без цього правила деплой піде автоматично.

Також джобу можна запустити вручну: Actions → Deploy → Run workflow.

## Корисне

- Логи: `npm run logs`
- SSH у машину: `fly ssh console -a photo-approval-bot` (база — `/data/bot.db`)
- Бот на одному токені працює лише в одному місці, і на Fly має бути **рівно одна машина** (том це й так гарантує).
- Не змінювати `APPROVER_ID` без узгодження з власницею. Якщо апрувера передали командою `/approver`, значення з бази має пріоритет над змінною.
