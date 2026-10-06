# Отчётный бот «Судебный приказ»

Отдельный read-only Telegram-бот для ежедневной статистики за предыдущий календарный день. Он читает таблицы основного бота и amoCRM через GET API; его собственная SQLite-БД содержит только снимки отчёта и журнал отправок.

## Запуск

1. Создайте отдельного Telegram-бота и задайте `REPORT_TG_BOT_TOKEN` и `REPORT_CHAT_ID` в `.env`.
2. Укажите `SOURCE_DATABASE_URL` на SQLite-файл `prikaz-cancel-bot`, `AMOCRM_BASE_URL`, `AMOCRM_ACCESS_TOKEN`, `TIMEZONE` и `DAILY_REPORT_TIME`.
3. Установите зависимости и запустите:

```powershell
python -m pip install -r requirements.txt
python -m app
```

Для тестов установите `python -m pip install -r requirements-dev.txt`.

Команды `/report` и `/yesterday` показывают отчёт за предыдущий календарный день; `/report YYYY-MM-DD` открывает конкретный сохранённый снимок или создаёт его. Автоматическая доставка происходит в `DAILY_REPORT_TIME` по часовому поясу `TIMEZONE`. После успешной отправки дата и chat ID сохраняются, поэтому повторный запуск scheduler не доставляет отчёт повторно.

## Источник метрик

- Подписка: успешное событие `crm_sync_logs.event_type = user_started_bot`, с платформой из связанного кейса.
- Контакт: успешное `phone_provided`; для кампании консультаций дополнительно используется `mailing_actions.event_type = mailing_phone_received` и исходное время `payload_json.mailing_event_at`.
- Оплата: только `payments.status = paid` и `payments.paid_at`.

В каждом показателе люди дедуплицируются по записи пользователя, а drill-down — по уникальным amoCRM lead ID в воронке `Судебный приказ`. Все границы дня рассчитываются как локальная полночь IANA timezone, затем переводятся в UTC для фильтрации SQLite timestamp.

Проверки: `python -m pytest`.
