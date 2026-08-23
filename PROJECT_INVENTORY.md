# VPN-проект с Telegram-ботом

Копия снята с сервера `144.31.104.143`. Исходные файлы на сервере не изменялись.

## Состав

- `root/telegram-vpn-bot/` — Telegram-бот на Python. Управляет пользователями VPN через Marzban API; содержит Docker Compose, env-файлы, сертификаты, клиент Marzban и обработчики Telegram.
- `opt/speedy-vpn-backend/` — FastAPI backend facade проекта Speedy VPN, SQLite-база `speedy_vpn_data.sqlite3`, APK и графические assets. На сервере запускается через Uvicorn на loopback-порту 8081.
- `opt/speedy-vpn-admin/` — статическая админ-панель, которую сервер отдаёт через Python HTTP server на порту 3737.
- `opt/marzban/` — Docker Compose и env для Marzban; постоянные данные Marzban на сервере находятся в Docker volume и не входят в файловую копию каталога.
- `root/awg/` — AmneziaWG: серверные ключи, клиентские конфигурации `my_phone`/`my_laptop`, QR/URI-файлы и скрипт управления.
- `server-config/` — deployment-конфигурация: systemd units, nginx-конфиги и env backend.

## Запуск на сервере

- `speedy-vpn-backend.service` — `/opt/speedy-vpn-backend/.venv/bin/uvicorn app.main:app`, локальный порт 8081.
- `speedy-vpn-admin.service` — `python3 -m http.server 3737` из `/opt/speedy-vpn-admin`.
- Telegram-бот и Marzban — Docker Compose из `root/telegram-vpn-bot`.
- На сервере также работает Docker-контейнер Marzban.

## Важно

В копии присутствуют секретные env-файлы, приватные ключи и VPN-конфигурации. Не публиковать эту папку и не коммитить секреты в Git.
