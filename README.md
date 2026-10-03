# «Деньги» — подушка безопасности и хотелки (Telegram Mini App)

Принцип «плати сначала себе»: с каждого поступления процент автоматически
уходит в две копилки — подушку безопасности и фонд хотелок. Всё управление
в Mini App внутри Telegram; бот только открывает приложение.

## Файлы

```
cushion_bot.py     бот + веб-сервер + API + встроенная страница Mini App
requirements.txt   зависимости
Procfile           команда запуска для Railway
```

## Railway

Variables:
- `BOT_TOKEN` — токен от BotFather
- `DB_PATH` — `/data/cushion.db` (и Volume с mount path `/data`)
- `PORT` — `8080`
- `WEBAPP_URL` — `https://<домен сервиса>.up.railway.app`

Проверка: `https://<домен>/health` показывает версию бота. обновление
