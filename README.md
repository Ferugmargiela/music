# CARTEL MUSICBOT — версия для слабого VPS

Аудиобот на Python, aiogram 3 и yt-dlp. Настройки рассчитаны как стартовая конфигурация для VPS 2 ГБ RAM, 1 vCPU и диска 15 ГБ.

> **Важно:** глобального ограничения одновременных загрузок в коде нет. Поэтому при массовом наплыве запросов VPS с 1 vCPU может перегрузиться. Есть только персональная защита: один активный download на Telegram-пользователя и максимум 3 запроса на загрузку за 60 секунд на пользователя. Параллельный поиск отдельно ограничен двумя операциями — это не лимит загрузок.

## Возможности

- Поиск аудио на YouTube и варианты поиска (remix, sped up, slowed и т. д.).
- MP3 192 кбит/с с помощью FFmpeg; максимальный размер файла для отправки — 49 MiB.
- Уникальная временная папка для каждой загрузки; удаление после отправки, ошибки или отмены.
- Очистка старых временных файлов/папок старше 6 часов при старте и каждые 30 минут; активные задачи защищены от очистки.
- Кэш поиска на 10 минут, максимум 500 запросов; устаревшие результаты кнопок очищаются.
- SQLite-статистика в WAL-режиме: пользователи, поиски, запросы, успехи и ошибки.
- `/stats` — общая статистика и системные показатели, `/user TELEGRAM_ID` — статистика пользователя; доступны только ID из `ADMIN_IDS`.
- Ротация логов; уведомления администратору о нехватке места и высокой загрузке RAM.
- Вызовы yt-dlp и очистки вынесены в `asyncio.to_thread`, чтобы не блокировать event loop.

## Установка на Ubuntu/Debian VPS

1. Установи Python 3.11+, FFmpeg и venv:

   ```bash
   sudo apt update
   sudo apt install -y python3 python3-venv python3-pip ffmpeg
   ```

2. Распакуй проект, например в `/opt/CartelMusicBot`, и перейди в папку проекта.

3. Создай окружение и поставь зависимости:

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install --upgrade pip
   pip install -r requirements.txt
   ```

4. Создай конфигурацию:

   ```bash
   cp .env.example .env
   nano .env
   ```

   Укажи токен от BotFather и свой числовой Telegram ID в `ADMIN_IDS`. Не отправляй `.env` в чаты и не публикуй его.

5. Запусти для проверки:

   ```bash
   .venv/bin/python bot.py
   ```

   Открой Telegram, отправь `/start`, затем выполни поиск. Для админ-проверки используй `/stats` и `/user TELEGRAM_ID`.

## Запуск через systemd

Файл службы: `deploy/cartelmusicbot.service`. Он предполагает, что проект лежит в `/opt/CartelMusicBot`, а Linux-пользователь службы называется `cartelbot`.

```bash
sudo useradd --system --home /opt/CartelMusicBot --shell /usr/sbin/nologin cartelbot
sudo chown -R cartelbot:cartelbot /opt/CartelMusicBot
sudo cp deploy/cartelmusicbot.service /etc/systemd/system/cartelmusicbot.service
sudo systemctl daemon-reload
sudo systemctl enable --now cartelmusicbot
sudo systemctl status cartelmusicbot
```

Если `.env` создавался после `chown`, проверь владельца и права. Для просмотра логов:

```bash
sudo journalctl -u cartelmusicbot -f
```

Файловые логи находятся в `logs/cartelmusic.log` с ротацией.

## Конфигурация `.env`

- `BOT_TOKEN` — токен Telegram-бота.
- `ADMIN_IDS` — ID администраторов через запятую, например `123456789,987654321`.
- `MAX_CONCURRENT_SEARCHES=2` — максимум параллельных поисков, **не загрузок**.
- `MAX_DOWNLOADS_PER_MINUTE=3` — персональная защита от спама для каждого пользователя.
- `BANNER_FILE=assets/banner.png` — необязательный баннер.
- `LOG_LEVEL=INFO` — уровень логирования.

Не добавляй `MAX_CONCURRENT_DOWNLOADS`: глобального лимита загрузок в этом проекте нет.

## Мониторинг

```bash
free -h
df -h
htop
vmstat 5
du -sh downloads logs
```

Рекомендуемые стартовые пороги: предупреждение при свободном месте менее 2 ГБ; критическое — менее 1 ГБ; предупреждение при использовании RAM выше 85%. Для диска 15 ГБ важно проверить свободное место перед запуском и не хранить готовые файлы дольше необходимого.

## Примечания

- Требуется подключение к интернету и рабочий FFmpeg в `PATH`.
- Доступность загрузки зависит от источника, региона, ограничений платформы и версии yt-dlp. Не каждый результат поиска гарантированно доступен.
- Проверь соблюдение авторских прав и правил источника: скачивай и распространяй только материалы, на которые у тебя есть права.
- В этом окружении проверены синтаксис Python и целостность архива. Полный end-to-end тест с Telegram API требует запуска с действительным токеном на VPS.
