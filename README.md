# wb_pereraspr

Telegram-бот для автоматического перераспределения товара по складам Wildberries.

Бот копит заявки (с какого склада → артикул → количество → на какой склад),
а в **9:00 МСК** запускает «штурм» — долбит WB-кабинет, пока не разложит всё
или не истечёт окно. Параллельно фоном обновляет список доступных складов.

## Возможности

- Авторизация через SMS на телефон (как в личном кабинете seller.wildberries.ru)
- Сессия шифруется (Fernet) и хранится локально в SQLite
- Автообновление списка доступных складов каждые N секунд
- Очередь заявок: создание мастером, просмотр, отмена
- Ежедневный планировщик на 9:00 МСК + ручной запуск через `/run`
- При истечении сессии бот пушит в TG «нужен повторный /login» и ждёт

## Команды

| Команда | Действие |
| --- | --- |
| `/login` | Авторизация: телефон → SMS-код |
| `/warehouses` | Обновить и показать список складов |
| `/add` | Создать заявку (FSM-мастер) |
| `/queue` | Все заявки и их статусы |
| `/cancel <id>` | Отменить заявку |
| `/run` | Запустить штурм вручную (вне 9:00) |
| `/status` | Состояние сессии и счётчики |

Доступ — только у `TG_OWNER_ID` из `.env`.

## Запуск

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# заполнить TG_BOT_TOKEN, TG_OWNER_ID, SESSION_ENCRYPTION_KEY
python -m app.main
```

Сгенерировать ключ шифрования:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## Что нужно дописать руками: WB API

У функции «Перераспределение остатков» **нет публичного API** —
это внутренний интерфейс кабинета продавца. В `app/wb/client.py`
эндпоинты и payload-ы помечены `# TODO(WB)`. Их нужно один раз
снять из DevTools браузера:

1. Откройте `https://seller.wildberries.ru` в Chrome, DevTools → Network.
2. Зайдите по SMS — запишите URL и тело запросов:
   - запрос SMS-кода (`URL_SMS_REQUEST`)
   - подтверждение кода (`URL_SMS_VERIFY`)
3. Откройте раздел перераспределения остатков — запишите:
   - GET списка доступных складов (`URL_WAREHOUSES`) и его JSON-схему
   - POST самого перераспределения (`URL_REDISTRIBUTE`) и его payload
4. Подставьте URL/тела/заголовки в `app/wb/client.py` вместо `# TODO(WB)`.
   Часто нужны cookies типа `WBToken`, `x-supplier-id` либо заголовок
   `Authorization: Bearer ...` — посмотрите, что фронт реально шлёт.

Эти эндпоинты ВБ периодически меняет. Если бот вдруг перестал работать —
первое, что проверить, не съехала ли схема в DevTools.

## Архитектура

```
app/
  main.py              запуск
  config.py            настройки из .env
  bot/
    handlers.py        TG-команды и FSM
    keyboards.py
  wb/
    client.py          HTTP-клиент WB (login + warehouses + redistribute)
  db/
    storage.py         SQLite + шифрование сессии
  scheduler/
    jobs.py            APScheduler: 9:00 МСК штурм + обновление складов
```
