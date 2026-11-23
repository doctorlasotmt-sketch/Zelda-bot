#!/usr/bin/env python3
"""
Zelda Restock Bot
Vigila el stock de la Nintendo Switch 2 – The Legend of Zelda – 40th Anniversary Edition
en varias tiendas y avisa al celular (ntfy / Telegram / Discord) cuando vuelve a haber stock.

Uso:
  python bot.py --once            # revisa una vez (ideal para cron / GitHub Actions)
  python bot.py                   # loop infinito cada N segundos (config.json)
  python bot.py --test-notify     # manda una notificación de prueba
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.getenv("BOT_CONFIG", BASE / "config.json"))
STATE_PATH = Path(os.getenv("BOT_STATE", BASE / "state.json"))

IN_STOCK, OUT_OF_STOCK, UNKNOWN, BLOCKED = "IN_STOCK", "OUT_OF_STOCK", "UNKNOWN", "BLOCKED"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
]

BLOCK_MARKERS = [
    r"captcha", r"access denied", r"are you a human", r"px-captcha",
    r"request unsuccessful", r"pardon our interruption", r"robot check",
]

LD_JSON_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.S | re.I
)


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


# --------------------------------------------------------------------------- #
# Detección de stock
# --------------------------------------------------------------------------- #
@dataclass
class Result:
    status: str
    detail: str = ""
    price: float | None = None


def _walk(obj):
    """Recorre recursivamente un JSON (dicts/listas)."""
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def parse_json_ld(html: str) -> Result | None:
    """Lee schema.org Offer.availability, que muchas tiendas publican en la página."""
    found_out = None
    for raw in LD_JSON_RE.findall(html):
        try:
            data = json.loads(raw.strip())
        except json.JSONDecodeError:
            continue
        for node in _walk(data):
            avail = node.get("availability")
            if not isinstance(avail, str):
                continue
            a = avail.lower()
            price = None
            try:
                price = float(node.get("price") or node.get("lowPrice"))
            except (TypeError, ValueError):
                pass
            if "instock" in a or "preorder" in a or "limitedavailability" in a or "onlineonly" in a:
                return Result(IN_STOCK, f"JSON-LD: {avail}", price)
            if "outofstock" in a or "soldout" in a or "discontinued" in a or "instoreonly" in a:
                found_out = Result(OUT_OF_STOCK, f"JSON-LD: {avail}", price)
    return found_out


def _match_any(patterns: list[str], text: str) -> str | None:
    for p in patterns:
        if re.search(p, text, re.I):
            return p
    return None


def visible_text(html: str) -> str:
    html = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", text)


ONLINE_NO = [
    r"ship(ping)? to home\W{0,30}(not available|unavailable|out of stock|sold out)",
    r"(not available|unavailable) (for|to) (shipping|ship|delivery|home delivery)",
    r"(in[- ]store|pick ?up)[- ]only", r"available (only )?in stores? only",
    r"only available (in[- ]store|for pick ?up)", r"online\W{0,10}(not available|unavailable|sold out)",
    r"shipping\W{0,10}(not available|unavailable|sold out)", r"delivery\W{0,10}(not available|unavailable)",
    r"no disponible para (despacho|env[ií]o)", r"solo (retiro en tienda|en tienda)",
]
ONLINE_YES = [
    r"ship(ping)? to home\W{0,30}(available|in stock|free|arrives|get it)",
    r"shipping\W{0,10}(available|get it|arrives|free)",
    r"despacho a domicilio\W{0,20}disponible", r"(llega|recíbelo|recibelo) (el|entre|a partir)",
    r"\b(arrives|get it|delivered|delivery) by\b", r"estimated delivery", r"free shipping on this item",
]


def detect(html: str, product: dict, defaults: dict) -> Result:
    res = _detect(html, product, defaults)
    online_only = product.get("online_only", defaults.get("online_only", False))
    if not online_only or res.status == BLOCKED:
        return res
    # Solo cuenta si se puede comprar online (envío a domicilio), no solo retiro en tienda.
    text = visible_text(html)
    no = _match_any(product.get("online_unavailable_patterns", ONLINE_NO), text)
    if no:
        return Result(OUT_OF_STOCK, f"sin compra online: /{no}/", res.price)
    yes = _match_any(product.get("online_available_patterns", ONLINE_YES), text)
    if yes:  # envío confirmado: manda aunque "retiro en tienda" diga no disponible
        return Result(IN_STOCK, f"compra online: /{yes}/", res.price)
    if res.status == IN_STOCK:
        return Result(OUT_OF_STOCK, f"{res.detail}, pero sin confirmar envío online (no aviso)", res.price)
    return res


def _detect(html: str, product: dict, defaults: dict) -> Result:
    """Orden: bloqueo -> JSON-LD -> texto 'agotado' -> texto 'en stock'."""
    head = html[:20000].lower()
    if len(html) < 3000 and _match_any(BLOCK_MARKERS, head):
        return Result(BLOCKED, "la tienda respondió con un captcha / bloqueo")

    out_pats = product.get("out_of_stock_patterns", defaults["out_of_stock_patterns"])
    in_pats = product.get("in_stock_patterns", defaults["in_stock_patterns"])

    ld = None if product.get("ignore_json_ld") else parse_json_ld(html)
    text = visible_text(html)

    # JSON-LD describe ESTE producto, así que si dice InStock se le cree.
    if ld and ld.status == IN_STOCK:
        return ld
    # Si no, "agotado" en el texto manda sobre "add to cart" (evita falsos positivos
    # por botones de productos recomendados en la misma página).
    hit_out = _match_any(out_pats, text)
    if hit_out:
        return Result(OUT_OF_STOCK, f"texto: /{hit_out}/", ld.price if ld else None)
    if ld:
        return ld
    hit_in = _match_any(in_pats, text)
    if hit_in:
        return Result(IN_STOCK, f"texto: /{hit_in}/")
    if _match_any(BLOCK_MARKERS, head):
        return Result(BLOCKED, "posible captcha / bloqueo")
    return Result(UNKNOWN, "no se encontró ninguna señal de stock")


# --------------------------------------------------------------------------- #
# Descarga de páginas
# --------------------------------------------------------------------------- #
_session = requests.Session()


def fetch_requests(url: str, timeout: int) -> tuple[int, str]:
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,es;q=0.8",
        "Cache-Control": "no-cache",
    }
    r = _session.get(url, headers=headers, timeout=timeout, allow_redirects=True)
    return r.status_code, r.text


_browser = None


def fetch_browser(url: str, timeout: int) -> tuple[int, str]:
    """Renderiza con Chromium (Playwright) para páginas que cargan el stock con JavaScript."""
    global _browser
    from playwright.sync_api import sync_playwright  # import perezoso: es opcional

    if _browser is None:
        pw = sync_playwright().start()
        _browser = pw.chromium.launch(headless=True)
    ctx = _browser.new_context(user_agent=random.choice(USER_AGENTS), locale="en-US")
    page = ctx.new_page()
    try:
        resp = page.goto(url, timeout=timeout * 1000, wait_until="domcontentloaded")
        page.wait_for_timeout(4000)  # deja que el JS pinte el botón de compra
        return (resp.status if resp else 0), page.content()
    finally:
        ctx.close()


# --------------------------------------------------------------------------- #
# Target: en vez de la página (muy protegida) usa la API pública que usa target.com
# --------------------------------------------------------------------------- #
TARGET_API = "https://redsky.target.com/redsky_aggregations/v1/web/pdp_client_v1"
TARGET_IN = {"IN_STOCK", "LIMITED_STOCK", "PRE_ORDER_SELLABLE", "AVAILABLE"}
TARGET_OUT = {"OUT_OF_STOCK", "UNAVAILABLE", "PRE_ORDER_UNSELLABLE", "NOT_SOLD_IN_STORE",
              "DISCONTINUED", "SOLD_OUT"}


def check_target_api(product: dict, timeout: int) -> Result:
    params = {
        "key": product.get("target_api_key", "9f36aeafbe60771e321a7cc95a78140772ab3e96"),
        "tcin": product["target_tcin"],
        "pricing_store_id": product.get("target_store_id", "3991"),
        "store_id": product.get("target_store_id", "3991"),
        "zip": product.get("target_zip", "55403"),
        "has_pricing_store_id": "true",
        "has_financing_options": "false",
        "visitor_id": "%016X" % random.getrandbits(64),
        "channel": "WEB",
        "page": f"/p/A-{product['target_tcin']}",
    }
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "application/json",
        "Origin": "https://www.target.com",
        "Referer": product["url"],
    }
    r = _session.get(TARGET_API, params=params, headers=headers, timeout=timeout)
    if r.status_code in (403, 429):
        return Result(BLOCKED, f"API Target HTTP {r.status_code}")
    if r.status_code >= 400:
        return Result(UNKNOWN, f"API Target HTTP {r.status_code}")
    data = r.json()

    # Solo cuenta el envío a domicilio (shipping_options), no el retiro en tienda.
    price = None
    statuses = []
    for node in _walk(data):
        ship = node.get("shipping_options")
        if isinstance(ship, dict) and isinstance(ship.get("availability_status"), str):
            statuses.append(ship["availability_status"].upper())
        if price is None and isinstance(node.get("current_retail"), (int, float)):
            price = float(node["current_retail"])

    if any(s in TARGET_IN for s in statuses):
        return Result(IN_STOCK, f"API Target: {', '.join(sorted(set(statuses)))}", price)
    if statuses:
        return Result(OUT_OF_STOCK, f"API Target: {', '.join(sorted(set(statuses)))}", price)
    return Result(UNKNOWN, "API Target sin dato de envío")


def check_product(product: dict, cfg: dict) -> Result:
    timeout = cfg.get("request_timeout_seconds", 25)

    if product.get("target_tcin"):
        try:
            res = check_target_api(product, timeout)
        except Exception as e:
            res = Result(UNKNOWN, f"API Target error: {e.__class__.__name__}")
        if res.status in (IN_STOCK, OUT_OF_STOCK):
            max_price = product.get("max_price", cfg.get("max_price"))
            if res.status == IN_STOCK and max_price and res.price and res.price > max_price:
                return Result(OUT_OF_STOCK, f"stock a ${res.price:.2f} (> ${max_price})", res.price)
            return res
        log(f"  Target API: {res.detail} -> pruebo con la página")
    use_browser = product.get("render_js", False) and cfg.get("enable_browser", True)
    try:
        if use_browser:
            try:
                code, html = fetch_browser(product["url"], timeout)
            except ImportError:
                log("  (Playwright no instalado, uso requests)")
                code, html = fetch_requests(product["url"], timeout)
        else:
            code, html = fetch_requests(product["url"], timeout)
    except Exception as e:  # red caída, timeout, etc.
        return Result(UNKNOWN, f"error de red: {e.__class__.__name__}")

    if code in (403, 429, 503):
        return Result(BLOCKED, f"HTTP {code}")
    if code >= 400:
        return Result(UNKNOWN, f"HTTP {code}")

    res = detect(html, product, cfg["defaults"])

    max_price = product.get("max_price", cfg.get("max_price"))
    if res.status == IN_STOCK and max_price and res.price and res.price > max_price:
        return Result(OUT_OF_STOCK, f"hay stock pero a ${res.price:.2f} (> ${max_price}, revendedor)", res.price)
    return res


# --------------------------------------------------------------------------- #
# Notificaciones
# --------------------------------------------------------------------------- #
def notify(title: str, body: str, url: str | None = None, urgent: bool = True,
           button_label: str = "Comprar ahora") -> None:
    """url = página que se abre al tocar la notificación (y su botón)."""
    sent = False

    topic = os.getenv("NTFY_TOPIC")
    if topic:
        server = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
        headers = {
            "Title": title.encode("utf-8"),
            "Priority": "urgent" if urgent else "default",
            "Tags": "rotating_light,video_game" if urgent else "information_source",
        }
        if url:
            headers["Click"] = url  # tocar la notificación abre esta URL
            headers["Actions"] = f"view, {button_label}, {url}, clear=true"
        try:
            requests.post(f"{server}/{topic}", data=body.encode("utf-8"), headers=headers, timeout=15)
            sent = True
        except Exception as e:
            log(f"  ntfy falló: {e}")

    tg_token, tg_chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if tg_token and tg_chat:
        payload = {"chat_id": tg_chat, "text": f"{title}\n{body}"}
        if url:
            payload["reply_markup"] = {"inline_keyboard": [[{"text": f"🛒 {button_label}", "url": url}]]}
        try:
            requests.post(f"https://api.telegram.org/bot{tg_token}/sendMessage",
                          json=payload, timeout=15)
            sent = True
        except Exception as e:
            log(f"  Telegram falló: {e}")

    hook = os.getenv("DISCORD_WEBHOOK_URL")
    if hook:
        content = f"{'@everyone ' if urgent else ''}**{title}**\n{body}" + (f"\n{url}" if url else "")
        try:
            requests.post(hook, json={"content": content}, timeout=15)
            sent = True
        except Exception as e:
            log(f"  Discord falló: {e}")

    if not sent:
        log(f"  (sin canal de aviso configurado) {title} — {body}")


# --------------------------------------------------------------------------- #
# Estado + ciclo principal
# --------------------------------------------------------------------------- #
def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def run_once(cfg: dict, state: dict) -> dict:
    now = time.time()
    remind_every = cfg.get("remind_every_minutes", 10) * 60
    block_alert_after = cfg.get("alert_after_consecutive_blocks", 0)

    for product in cfg["products"]:
        if not product.get("enabled", True):
            continue
        key = product["id"]
        prev = state.get(key, {})
        res = check_product(product, cfg)
        price = f" ${res.price:.2f}" if res.price else ""
        log(f"{product['store']:<14} {res.status:<12}{price}  ({res.detail})")

        entry = {
            "status": res.status,
            "detail": res.detail,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "last_alert": prev.get("last_alert", 0),
            "blocks": prev.get("blocks", 0) + 1 if res.status == BLOCKED else 0,
            "block_alerted": prev.get("block_alerted", False) if res.status == BLOCKED else False,
        }

        # Tocar la notificación abre la página del producto en la tienda que tiene stock.
        click_url = product.get("click_url") or product["url"]

        if res.status == IN_STOCK:
            was_in = prev.get("status") == IN_STOCK
            if not was_in or now - prev.get("last_alert", 0) >= remind_every:
                notify(
                    f"🟢 ¡STOCK! Switch 2 Zelda en {product['store']}",
                    f"{product['name']}{price}\n{'Sigue disponible' if was_in else 'Acaba de aparecer'}"
                    f" — toca para comprar en {product['store']}.",
                    click_url,
                    button_label=f"Comprar en {product['store']}",
                )
                entry["last_alert"] = now
        elif prev.get("status") == IN_STOCK and cfg.get("notify_when_sold_out", True):
            notify(f"🔴 Se agotó en {product['store']}", product["name"], product["url"], urgent=False)

        if block_alert_after and entry["blocks"] >= block_alert_after and not entry["block_alerted"]:
            notify(
                f"⚠️ {product['store']} está bloqueando al bot",
                f"{entry['blocks']} revisiones seguidas bloqueadas. Prueba con render_js=true o revisa a mano.",
                product["url"],
                urgent=False,
            )
            entry["block_alerted"] = True

        state[key] = entry
        time.sleep(random.uniform(1.5, 4.0))  # no martillar a las tiendas

    return state


def main() -> None:
    ap = argparse.ArgumentParser(description="Bot de restock Switch 2 Zelda Edition")
    ap.add_argument("--once", action="store_true", help="revisa una vez y termina")
    ap.add_argument("--test-notify", action="store_true", help="manda una notificación de prueba")
    ap.add_argument("--duration", type=float, default=0,
                    help="revisa en loop durante N minutos y termina (para GitHub Actions)")
    args = ap.parse_args()
    deadline = time.time() + args.duration * 60 if args.duration else None

    cfg = load_json(CONFIG_PATH, None)
    if not cfg:
        sys.exit(f"No pude leer {CONFIG_PATH}")

    if args.test_notify:
        first = next(p for p in cfg["products"] if p.get("enabled", True))
        notify("🧪 Prueba del bot Zelda",
               f"Si ves esto, las notificaciones funcionan. Tócala: debería abrir {first['store']}.",
               first["url"], urgent=True, button_label=f"Comprar en {first['store']}")
        log("Notificación de prueba enviada.")
        return

    state = load_json(STATE_PATH, {})
    while True:
        state = run_once(cfg, state)
        STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
        if args.once:
            break
        base = cfg.get("interval_seconds", 90)
        wait = base + random.uniform(0, base * 0.3)
        if deadline and time.time() + wait > deadline:
            log("Tiempo de esta ejecución terminado; la siguiente sigue vigilando.")
            break
        log(f"Próxima revisión en {wait:.0f}s")
        time.sleep(wait)


if __name__ == "__main__":
    main()
