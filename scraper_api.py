"""
Scraper primario via API de carriapp.cl.
Refactor de carriapp_scraper.py con formato agrupado por producto.
"""
import json
import time
import uuid
from typing import Iterator, Union, Dict, List
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

BASE = "https://api.carriapp.cl/api"
VISITOR_ID = str(uuid.uuid4())

HEADERS = {
    "x-visitor-id": VISITOR_ID,
    "content-type": "application/json",
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/147.0.0.0 Safari/537.36"
    ),
    "referer": "https://carriapp.cl/",
}


def get(url: str) -> Union[dict, list]:
    req = Request(url, headers=HEADERS)
    with urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def get_stores() -> Dict[int, dict]:
    """Retorna {store_id: {name, url, logo}}."""
    data = get(f"{BASE}/stores")
    return {
        s["id"]: {"name": s["name"], "url": s["store_url"], "logo": s["logo_url"]}
        for s in data
    }


def get_categories() -> List[dict]:
    """Retorna lista plana de {slug, name, parent}."""
    data = get(f"{BASE}/sections")
    result = []
    for section in data:
        result.append({
            "slug": section["slug"],
            "name": section["name"],
            "parent": None,
        })
        for sub in section.get("sub_menu", []):
            result.append({
                "slug": sub["slug"],
                "name": sub["name"],
                "parent": section["slug"],
            })
    return result


def group_product(product: dict) -> dict:
    """Convierte un producto raw en formato agrupado con stores como lista."""
    return {
        "product_id": product["id"],
        "slug": product["slug"],
        "brand": product["brand"],
        "unit": product["unit"],
        "stores": [
            {
                "store_id": store["store_id"],
                "product_name": store["name"],
                "price": store["price"],
                "price_per_unit": store["price_per_unit"],
                "price_per_unit_scale": store["price_per_unit_scale"],
                "best_promo_price": min(
                    (p["price"] for p in store.get("promos", [])), default=None
                ),
                "available": store["available"],
                "url": store.get("url", ""),
                "image": (store.get("images") or {}).get("catalog", ""),
            }
            for store in product.get("stores", [])
        ],
    }


def iter_products(query: str, location: int = 1) -> Iterator[List[dict]]:
    """
    Genera batches de productos agrupados, página a página.
    Raises HTTPError / URLError si la API falla (para activar fallback).
    """
    url = f"{BASE}/search/{query}?sort=relevance&location={location}"
    seen_ids: set = set()

    while url:
        data = get(url)  # puede lanzar excepción — el caller hace fallback

        products = data.get("products", [])
        new_products = [p for p in products if p["id"] not in seen_ids]
        if not new_products:
            break
        for p in new_products:
            seen_ids.add(p["id"])

        yield [group_product(p) for p in new_products]

        next_page = data.get("next_page")
        if next_page:
            url = f"{BASE}/search/{query}?sort=relevance&location={location}&next_page={next_page}"
            time.sleep(0.3)
        else:
            url = None
