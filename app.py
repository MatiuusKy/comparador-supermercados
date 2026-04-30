# app.py
"""
FastAPI server para el comparador de supermercados.
Endpoints:
  GET /                         → sirve index.html
  GET /api/categories           → lista de categorías carriapp
  GET /api/search/stream?q=...  → SSE stream de productos
"""
import json
from urllib.error import HTTPError, URLError

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

import scraper_api
import scraper_web

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# Cache de datos estáticos (se cargan una vez al arrancar)
_stores: dict = {}
_categories: list = []


@app.on_event("startup")
async def load_static_data():
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


@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


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
        except (HTTPError, URLError):
            # Fallback a Playwright
            try:
                for batch in scraper_web.iter_products(q):
                    enriched = _enrich_batch(batch)
                    data = json.dumps(enriched, ensure_ascii=False)
                    yield f"event: products\ndata: {data}\n\n"
            except Exception as e:
                yield f"event: error\ndata: {json.dumps({'message': str(e)})}\n\n"
                return

        yield "event: done\ndata: {}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


def _enrich_batch(batch: list) -> list:
    """Agrega store_name, store_url, store_logo a cada store usando el caché _stores."""
    for product in batch:
        for store in product["stores"]:
            sid = store["store_id"]
            store["store_name"] = _stores.get(sid, {}).get("name", f"Store {sid}")
            store["store_url"] = _stores.get(sid, {}).get("url", "")
            store["store_logo"] = _stores.get(sid, {}).get("logo", "")
    return batch
