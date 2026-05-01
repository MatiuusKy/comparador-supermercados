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

from fastapi import FastAPI, Request
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
