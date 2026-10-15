1) pip install aiogram aiohttp
2) Впиши OWNER_ID и OWNER_USERNAME (в miniapp.py или через переменные окружения).
3) В start.py (где создаётся bot и dp):
     from miniapp import router as miniapp_router, start_web
     dp.include_router(miniapp_router)            # до запуска polling
     await start_web(bot, port=8080)              # перед await dp.start_polling(bot)
4) Выставь порт 8080 наружу по HTTPS (nginx/Caddy + домен). Нужен именно https:// и wss://.
5) @BotFather -> /newapp (или /setmenubutton) -> адрес https://твой-домен/
6) Оплата и игры работают только внутри StaticGram (initData проверяется по токену бота).
