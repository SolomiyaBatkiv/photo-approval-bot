# Photo Approval Bot

Виконавець генерує фото на Higgsfield → надсилає в бот → апрувер тисне ✅ або ❌ з коментарем → відповідь повертається виконавцю.
Після ✅ фото зберігається на Google Drive, а в таблицю додається рядок: **Manager · Notes · Added Date · Link · Gender**.

## Запуск

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # заповнити змінні
python bot.py
```

1. Створіть бота в @BotFather, візьміть токен → `BOT_TOKEN`.
2. Апрувер пише боту `/id` → `APPROVER_ID`, перезапустити.

## Google Таблиця + Drive

1. У таблиці додайте заголовок **Gender** у клітинку `E1`.
2. Створіть папку на Google Drive для фото, скопіюйте її ID з адреси.
3. Таблиця → **Розширення → Apps Script**, вставте код з `apps_script.gs`, заповніть `SECRET` і `FOLDER_ID`, збережіть.
4. **Розгорнути → Нове розгортання → Вебзастосунок**: «Виконувати як: Я», «Доступ: Усі». Дайте дозволи.
5. URL розгортання → `SHEETS_WEBHOOK_URL`, секрет → `SHEETS_SECRET`.

Після змін у скрипті: **Розгорнути → Керувати розгортаннями → Редагувати → Нова версія** (URL лишається той самий).

## Стать

Після надсилання фото бот питає менеджера: **Male / Female**. Фото йде на апрув лише після вибору.
Після апруву файл потрапляє в підпапку `male` або `female` всередині папки `FOLDER_ID`, а в колонку Gender пишеться Male / Female.

## Деплой на сервер (Ubuntu, systemd)

```bash
sudo apt update && sudo apt install -y python3-venv git
git clone <URL цього репозиторію> ~/photobot && cd ~/photobot
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env && nano .env   # значення передаються окремо, не через git
```

Сервіс:

```bash
sudo tee /etc/systemd/system/photobot.service > /dev/null <<EOF
[Unit]
Description=Photo approval bot
After=network-online.target

[Service]
User=$USER
WorkingDirectory=$HOME/photobot
ExecStart=$HOME/photobot/.venv/bin/python bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload && sudo systemctl enable --now photobot
journalctl -u photobot -f   # логи
```

Оновлення: `git pull && sudo systemctl restart photobot`.

> Бот на одному токені може працювати лише в одному місці — перед запуском на сервері зупиніть локальну копію.
> `bot.db` (менеджери й заявки) не в git: перенесіть файл окремо (`scp`) або менеджери зареєструються заново.

## Команди
- `/start` — інструкція + кнопка «Згенерувати фото»
- `/id` — показати свій Telegram ID
- `/cancel` — апрувер скасовує написання коментаря
- `/managers` — апрувер: список менеджерів
- `/approver` — апрувер: передати роль іншому зареєстрованому менеджеру (заявки в очікуванні переходять до нього; зберігається в `bot.db` і має пріоритет над `APPROVER_ID` з `.env`)
- `/resync` — апрувер: повторно записати в таблицю фото, які не записались

Заявки й статуси — у `bot.db` (SQLite, таблиця `submissions`).
