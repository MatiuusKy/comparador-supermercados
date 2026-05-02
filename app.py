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
from urllib.parse import urlparse

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


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response


@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return templates.TemplateResponse(request, "index.html")


@app.get("/api/categories")
async def categories():
    return _categories


@app.get("/api/search/stream")
async def search_stream(q: str = Query(..., min_length=1, max_length=200)):
    async def generate():
        try:
            batches = await asyncio.to_thread(lambda: list(scraper_api.iter_products(q)))
            for batch in batches:
                enriched = _enrich_batch(batch)
                data = json.dumps(enriched, ensure_ascii=False)
                yield f"event: products\ndata: {data}\n\n"
        except (HTTPError, URLError, OSError):
            try:
                batches = await asyncio.to_thread(lambda: list(scraper_web.iter_products(q)))
                for batch in batches:
                    enriched = _enrich_batch(batch)
                    data = json.dumps(enriched, ensure_ascii=False)
                    yield f"event: products\ndata: {data}\n\n"
            except Exception as e:
                yield f"event: error\ndata: {json.dumps({'message': str(e)})}\n\n"
                return

        try:
            alvi_batches = await asyncio.to_thread(lambda: list(scraper_alvi.iter_products(q)))
            for batch in alvi_batches:
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


def _is_valid_vtex_url(url: str, store_id: int) -> bool:
    """Verifica que la URL pertenezca al dominio esperado (previene SSRF)."""
    try:
        parsed = urlparse(url)
        expected = _VTEX_DOMAINS.get(store_id, "")
        return parsed.scheme == "https" and parsed.netloc == expected
    except Exception:
        return False


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

    try:
        items_data = json.loads(items)
        if not isinstance(items_data, list):
            raise HTTPException(status_code=400, detail="items debe ser un array JSON")
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="JSON inválido en items")
    valid: list[tuple[str, int]] = []
    for item in items_data:
        if not isinstance(item, dict):
            continue
        url = item.get("url", "")
        if not isinstance(url, str) or not url:
            continue
        if not _is_valid_vtex_url(url, store_id):
            print(f"[WARN] URL rechazada por validación de dominio: {url}")
            continue
        try:
            qty = max(1, int(item.get("qty", 1)))
        except (ValueError, TypeError):
            qty = 1
        valid.append((url, qty))
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
        urls = urls[:20]  # cap razonable para evitar Playwright descontrolado
        try:
            sku_map = await asyncio.wait_for(
                asyncio.to_thread(_scrape_skus_playwright, urls),
                timeout=90.0,
            )
        except asyncio.TimeoutError:
            print("[WARN] Playwright sku lookup excedió timeout global")
            sku_map = {}
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
