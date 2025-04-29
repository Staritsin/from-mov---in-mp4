
Инструкция по деплою:
1. Залей на Render или Railway.
2. Не забудь включить сборку пакетов: Flask, requests.
3. Пример запроса:
   POST /convert
   {
     "url": "https://api.telegram.org/file/bot<токен>/videos/file_0.MOV"
   }
4. На выходе ты получишь JSON со ссылкой на готовое .mp4 видео.
