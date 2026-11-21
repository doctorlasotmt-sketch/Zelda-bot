# 🗡️ Zelda Restock Bot

Vigila la **Nintendo Switch 2 – The Legend of Zelda – 40th Anniversary Edition** (US$519.99) en
Nintendo Store, Best Buy, Target y GameStop, y te manda una notificación **urgente al celular**
en cuanto aparece stock. Funciona parecido a TrackaLacker, pero es tuyo y gratis.

## Cómo detecta el stock
1. Lee el dato `availability` (schema.org) que muchas tiendas incluyen en la página: `InStock`, `OutOfStock`, `PreOrder`.
2. Si ese dato no está, busca en el texto palabras como *Sold out / Agotado* o *Add to cart / Agregar al carro*.
3. Ignora ofertas sobre `max_price` (US$560 por defecto), así no te avisa por revendedores.
4. Al tocar la notificación push se abre directo la página del producto en la tienda que tiene stock.
5. Avisa cuando algo **pasa a tener stock**, te lo recuerda cada 10 min mientras siga disponible y te avisa cuando se agota.

## 1) Notificaciones al celular (lo más fácil: ntfy)
1. Instala la app **ntfy** (iOS / Android).
2. Suscríbete a un tema con un nombre difícil de adivinar, por ejemplo `zelda-restock-k8x92q`.
3. Ese nombre es tu `NTFY_TOPIC`.

*Alternativas:* Telegram (`TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`, creados con @BotFather) o
Discord (`DISCORD_WEBHOOK_URL`). Puedes activar varias a la vez.

## 2a) Correrlo gratis en la nube con GitHub Actions (no necesitas computador)
1. Crea un repositorio **privado** en GitHub y sube todos estos archivos (incluida la carpeta `.github`).
2. Ve a *Settings → Secrets and variables → Actions → New repository secret* y crea `NTFY_TOPIC`.
3. Ve a *Actions → Zelda restock check → Run workflow* para probarlo.
4. Desde ahí corre solo cada ~5 minutos (GitHub a veces lo atrasa unos minutos).

## 2b) Correrlo en tu PC o en una Raspberry Pi (más rápido: cada 90 s)
```bash
pip install -r requirements.txt
python -m playwright install chromium     # para tiendas con JavaScript
export NTFY_TOPIC=zelda-restock-k8x92q
python bot.py --test-notify               # debería llegarte una notificación
python bot.py                             # queda vigilando
```

## Agregar o quitar tiendas
Edita `config.json`. Cada producto tiene:
- `url`: la página del producto
- `render_js: true`: abre la página con un navegador real (más lento, pero ve el botón cargado por JavaScript)
- `enabled: false`: la desactiva
- `max_price`, `in_stock_patterns`, `out_of_stock_patterns`: opcionales, sirven para ajustar una tienda

Para tiendas en Chile (Paris, Falabella, Ripley, Weplay, MicroPlay…), copia el bloque de ejemplo, pega la
URL del producto, pon `enabled: true` y ajusta `max_price` (en CLP). El código de barras de la consola es
**045496886592**; te sirve para encontrarla en el buscador de cada tienda.

## Límites que conviene saber
- Best Buy, Target y Walmart tienen protección antibots. Si una tienda bloquea muchas revisiones seguidas,
  el bot te avisa (`alert_after_consecutive_blocks`).
- Las IP de GitHub Actions se bloquean más seguido que la de tu casa. Si eso pasa, correrlo en tu PC funciona mejor.
- No revises más seguido que cada ~60 s: te bloquean antes y no ganas mucho.
- `python test_detect.py` prueba el detector sin internet.
