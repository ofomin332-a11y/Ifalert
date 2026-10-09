# Тривога ІФ | Моніторинг — Railway v3

## Що підключено
- `alerts.in.ua` API для активних повітряних тривог і змін статусу по Івано-Франківській області (UID 13).
- Публічні Telegram-перегляди: `@martsinkiv_online`, `@mrada_if_ua`, `@onyshchuksvitlana`, `@totallzrada`.
- Пересилання нових дописів із посиланням на оригінал, автором-джерелом і часом публікації, якщо публічна сторінка Telegram доступна.

## Railway Variables
Required:
- `TELEGRAM_BOT_TOKEN` — токен Telegram-бота (зберігати лише в Railway Variables).
- `TELEGRAM_CHAT_ID` — ID групи, наприклад `-1003696297758`.
- `ALERTS_API_TOKEN` — токен API alerts.in.ua.

Optional:
- `POLL_SECONDS=10` — частота перевірки API тривог (мінімум 10 сек).
- `CHANNEL_POLL_SECONDS=60` — інтервал перевірки Telegram-каналів (мінімум 30 сек).
- `TELEGRAM_CHANNELS=handle1,handle2` — додаткові публічні Telegram-канали без @.
- `OFFICIAL_FEEDS=url1,url2` — зарезервовано для додаткових RSS/сайтів; у цій версії автоматичний парсер RSS ще не реалізований.

## Важливі обмеження
- Перевірка публічних `t.me/s/...` сторінок — best-effort: Telegram може змінити HTML, закрити перегляд або тимчасово обмежити доступ. Перевіряйте логи `Public Telegram source poll failed`.
- Перший успішний перегляд каналу лише створює baseline, щоб не пересилати старі дописи. Нові публікації надсилаються після цього.
- Ключові слова для неофіційних каналів — лише фільтр релевантності, не перевірка правдивості. Повідомлення з неофіційних джерел позначаються назвою каналу й посиланням.
- Канал «Західний» не включено, бо назва неоднозначна. Після отримання точного `@handle` додайте його до `TELEGRAM_CHANNELS` у Railway.
- Обласна рада як окремий офіційний канал/сайт ще потребує перевіреного URL; не підставляйте неперевірені адреси.
- Бот не може гарантувати отримання публікації раніше за першоджерело; точність залежить від джерела і доступності мережі.
- У разі тривоги дотримуйтеся офіційних вказівок і прямуйте в укриття. Не публікуйте чутливі фото/відео місць ударів чи точні траєкторії.

## Запуск
Railway використовує `Procfile` (`python main.py`). Перевірте, що deployment активний і в логах немає помилок.


## Source coverage status
- Alerts.in.ua API is implemented and filtered to oblast UID 13.
- Public Telegram preview polling is best-effort for the configured handles.
- `Dron Alert` is **not connected** in this build: the exact official product/channel/API endpoint could not be verified from the supplied information. Do not treat it as a live source yet.
- The name `Західний` is ambiguous; add its verified public handle to `TELEGRAM_CHANNELS` in Railway once confirmed. The `OFFICIAL_FEEDS` variable is reserved and RSS fetching is not implemented.
- This package has syntax checks only; it has not been deployed or tested against your Railway secrets/Telegram group.
