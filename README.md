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
| `requirements.txt` | залежності |
| `.env.example` | шаблон налаштувань; справжній `.env` передається окремо, не через git |
| `apps_script.gs` | код Google Apps Script, що кладе фото на Drive і пише рядок у таблицю (вже розгорнутий, чіпати не треба) |
| `bot.db` | створюється автоматично при першому запуску (менеджери, заявки); не в git |

## Запуск на сервері (Ubuntu)

```bash
sudo apt update && sudo apt install -y python3-venv git
git clone https://github.com/SolomiyaBatkiv/photo-approval-bot.git ~/photobot
cd ~/photobot
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
# покласти отриманий .env у ~/photobot/
```

Автозапуск:

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
```

Після запуску апрувер пише боту `/start` і вводить ім'я, менеджери — так само `/start`.

## Корисне

- Логи: `journalctl -u photobot -f`
- Оновлення: `git pull && sudo systemctl restart photobot`
- Бот на одному токені працює лише в одному місці — перед запуском на сервері зупинити локальну копію.
- Не змінювати `APPROVER_ID` у `.env` без узгодження з власницею.