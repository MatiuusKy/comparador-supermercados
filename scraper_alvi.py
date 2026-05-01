# scraper_alvi.py
"""
Scraper para Alvi.cl usando Playwright.
Intercepta respuestas XHR de IntelliSearch (VTEX) y,
como fallback, extrae datos de __NEXT_DATA__.
"""
import json
from typing import Iterator, List
from playwright.sync_api import sync_playwright

ALVI_STORE_ID = 10
BASE_URL = "https://www.alvi.cl"
LOGO_URL = "https://www.alvi.cl/arquivos/logo-alvi.png"


def _parse_image(img) -> str:
    if isinstance(img, str):
        return img
    if isinstance(img, dict):
        return img.get("imageUrl", "") or img.get("src", "") or ""
    return ""


def _to_grouped(product: dict) -> dict:
    price = float(product.get("price", 0) or 0)
    list_price = float(product.get("listPrice", price) or price)
    promo_price = price if price < list_price else None

    images = product.get("images", [])
    image_url = _parse_image(images[0]) if images else ""

    link_text = product.get("linkText", "")
    product_url = f"{BASE_URL}/{link_text}/p" if link_text else ""

    return {
        "product_id": f"alvi_{product.get('productId', '')}",
        "slug": link_text,
        "brand": product.get("brand", ""),
        "unit": product.get("format", ""),
        "stores": [
            {
                "store_id": ALVI_STORE_ID,
                "product_name": product.get("nameComplete", product.get("name", "")),
                "price": price,
                "price_per_unit": product.get("ppum", ""),
                "price_per_unit_scale": "",
                "best_promo_price": promo_price,
                "available": 1,
                "url": product_url,
                "image": image_url,
            }
        ],
    }


def iter_products(query: str) -> Iterator[List[dict]]:
    """Genera un batch con los productos de Alvi para el query dado."""
    captured: list[dict] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()

            def on_response(response):
                url = response.url
                if "intelligent-search" in url or "product_search" in url:
                    try:
                        data = response.json()
                        products = (
                            data.get("availableProducts")
                            or data.get("products")
                            or []
                        )
                        if products:
                            captured.extend(products)
                    except Exception:
                        pass

            page.on("response", on_response)
            page.goto(
                f"{BASE_URL}/search?q={query}",
                wait_until="networkidle",
                timeout=30_000,
            )

            # Fallback: extraer de __NEXT_DATA__ si XHR no capturó nada
            if not captured:
                try:
                    raw = page.evaluate(
                        "() => document.getElementById('__NEXT_DATA__')?.textContent"
                    )
                    if raw:
                        data = json.loads(raw)
                        search = (
                            data.get("props", {})
                            .get("pageProps", {})
                            .get("intelliSearchData", {})
                        )
                        products = search.get("availableProducts", [])
                        captured.extend(products)
                except Exception:
                    pass
        finally:
            browser.close()

    seen: set = set()
    batch = []
    for product in captured:
        pid = product.get("productId", "")
        if pid and pid not in seen:
            seen.add(pid)
            batch.append(_to_grouped(product))

    if batch:
        yield batch
