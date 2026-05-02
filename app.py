# app.py
"""
FastAPI server para el comparador de supermercados.
Endpoints:
  GET /                         → sirve index.html
  GET /api/categories           → lista de categorías carriapp
  GET /api/search/stream?q=...  → SSE stream de productos
"""
import asyncio
import json
import re
from contextlib import asynccontextmanager
from urllib.error import HTTPError, URLError

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

import scraper_api
import scraper_alvi
import scraper_web

_stores: dict = {}
_categories: list = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _stores, _categories
    try:
        _stores = scraper_api.get_stores()
    except Exception as e:
        print(f"[WARN] No se pudieron cargar stores: {e}")
        _stores = {
            1: {"name": "Jumbo", "url": "https://jumbo.cl", "logo": ""},
            2: {"name": "Unimarc", "url": "https://unimarc.cl", "logo": ""},
            3: {"name": "Lider", "url": "https://lider.cl", "logo": ""},
            4: {"name": "Tottus", "url": "https://tottus.cl", "logo": ""},
        }
    try:
        _categories = scraper_api.get_categories()
    except Exception as e:
        print(f"[WARN] No se pudieron cargar categorías: {e}")
        _categories = []
    # Alvi no está en carriapp — registrar manualmente
    _stores[scraper_alvi.ALVI_STORE_ID] = {
        "name": "Alvi",
        "url": scraper_alvi.BASE_URL,
        "logo": scraper_alvi.LOGO_URL,
    }
    yield


app = FastAPI(lifespan=lifespan)
templates = Jinja2Templates(directory="templates")


@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return templates.TemplateResponse(request, "index.html")


@app.get("/api/categories")
async def categories():
    return _categories


@app.get("/api/search/stream")
async def search_stream(q: str):
    def generate():
        try:
            for batch in scraper_api.iter_products(q):
                enriched = _enrich_batch(batch)
                data = json.dumps(enriched, ensure_ascii=False)
                yield f"event: products\ndata: {data}\n\n"
        except (HTTPError, URLError, OSError):
            # Fallback a Playwright carriapp
            try:
                for batch in scraper_web.iter_products(q):
                    enriched = _enrich_batch(batch)
                    data = json.dumps(enriched, ensure_ascii=False)
                    yield f"event: products\ndata: {data}\n\n"
            except Exception as e:
                yield f"event: error\ndata: {json.dumps({'message': str(e)})}\n\n"
                return

        # Fuente adicional: Alvi (scraper independiente)
        try:
            for batch in scraper_alvi.iter_products(q):
                enriched = _enrich_batch(batch)
                data = json.dumps(enriched, ensure_ascii=False)
                yield f"event: products\ndata: {data}\n\n"
        except Exception as e:
            print(f"[WARN] Alvi scraper falló: {e}")

        yield "event: done\ndata: {}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# Tiendas con backend VTEX → auto-carga de carrito
_VTEX_DOMAINS = {
    1: "www.jumbo.cl",
    2: "www.unimarc.cl",
    10: "www.alvi.cl",
}

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-CL,es;q=0.9",
}


def _scrape_skus_playwright(urls: list[str]) -> dict[str, str]:
    """Extrae {url: skuId} usando Playwright (para tiendas con WAF)."""
    from playwright.sync_api import sync_playwright

    results: dict[str, str] = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            for url in urls:
                try:
                    page = browser.new_page()
                    page.goto(url, wait_until="domcontentloaded", timeout=15_000)
                    m = re.search(r'"sku":"(\d+)"', page.content())
                    if m:
                        results[url] = m.group(1)
                    page.close()
                except Exception as exc:
                    print(f"[WARN] Playwright sku lookup falló para {url}: {exc}")
        finally:
            browser.close()
    return results


@app.get("/api/cart/build")
async def cart_build(store_id: int = Query(...), items: str = Query(...)):
    """
    Dado store_id y lista JSON de {url, qty}, devuelve una checkout_url que,
    al abrirla en el browser del usuario, agrega todos los productos a su
    carrito VTEX en esa tienda.
    """
    domain = _VTEX_DOMAINS.get(store_id)
    if not domain:
        raise HTTPException(status_code=400, detail="not_vtex")

    items_data: list[dict] = json.loads(items)
    valid = [(item["url"], int(item.get("qty", 1))) for item in items_data if item.get("url")]
    sku_qty: list[tuple[str, int]] = []

    if store_id == 1:
        # Jumbo: httpx puede descargar la página HTML del producto
        async def _fetch_sku(client: httpx.AsyncClient, url: str, qty: int):
            try:
                resp = await client.get(url, headers=_BROWSER_HEADERS)
                m = re.search(r'"sku":"(\d+)"', resp.text)
                return (m.group(1), qty) if m else None
            except Exception as exc:
                print(f"[WARN] httpx sku lookup falló para {url}: {exc}")
                return None

        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            results = await asyncio.gather(*[_fetch_sku(client, u, q) for u, q in valid])
        sku_qty = [r for r in results if r]

    else:
        # Unimarc, Alvi: WAF bloquea httpx → usar Playwright en hilo
        urls = [u for u, _ in valid]
        qty_map = {u: q for u, q in valid}
        sku_map = await asyncio.to_thread(_scrape_skus_playwright, urls)
        sku_qty = [(sku, qty_map[url]) for url, sku in sku_map.items()]

    if not sku_qty:
        raise HTTPException(status_code=422, detail="no_items_resolved")

    params = "&".join(f"sku={sku}&qty={qty}&seller=1" for sku, qty in sku_qty)
    return {"checkout_url": f"https://{domain}/checkout/cart/add?{params}&redirect=true"}


def _enrich_batch(batch: list[dict]) -> list[dict]:
    """Agrega store_name, store_url, store_logo a cada store usando el caché _stores.
    Returns a new list — does not mutate the input."""
    result = []
    for product in batch:
        enriched_stores = []
        for store in product["stores"]:
            sid = store["store_id"]
            enriched_stores.append({
                **store,
                "store_name": _stores.get(sid, {}).get("name", f"Store {sid}"),
                "store_url": _stores.get(sid, {}).get("url", ""),
                "store_logo": _stores.get(sid, {}).get("logo", ""),
            })
        result.append({**product, "stores": enriched_stores})
    return result
