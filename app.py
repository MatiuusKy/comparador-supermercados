# app.py
"""
FastAPI server para el comparador de supermercados.
Endpoints:
  GET /                         → sirve index.html
  GET /api/categories           → lista de categorías carriapp
  GET /api/search/stream?q=...  → SSE stream de productos
"""
import json
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
    sku_qty: list[tuple[str, int]] = []

    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
        for item in items_data:
            product_url = item.get("url", "")
            qty = int(item.get("qty", 1))
            if not product_url:
                continue

            # Extraer slug de URLs tipo https://domain/producto-nombre/p
            path = urlparse(product_url).path.rstrip("/")
            if path.endswith("/p"):
                path = path[:-2]
            slug = path.rsplit("/", 1)[-1]
            if not slug:
                continue

            try:
                resp = await client.get(
                    f"https://{domain}/api/catalog_system/pub/products/search/{slug}",
                    headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"},
                )
                resp.raise_for_status()
                products = resp.json()
                if isinstance(products, list) and products and products[0].get("items"):
                    sku_id = str(products[0]["items"][0]["itemId"])
                    sku_qty.append((sku_id, qty))
            except Exception as exc:
                print(f"[WARN] VTEX skuId lookup falló para {product_url}: {exc}")

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
