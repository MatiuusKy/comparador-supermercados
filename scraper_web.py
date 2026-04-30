# scraper_web.py
"""
Fallback scraper usando Playwright.
Abre carriapp.cl, intercepta las respuestas XHR a la API interna y
extrae los productos con el mismo formato que scraper_api.group_product.
"""
from typing import Iterator
from urllib.parse import quote
from playwright.sync_api import sync_playwright
from scraper_api import group_product

CARRIAPP_SEARCH_PATH = "/api/search/"


def iter_products(query: str) -> Iterator[list[dict]]:
    """
    Genera un único batch con todos los productos visibles en carriapp.cl/search/{query}.
    No pagina (limitación del fallback) — retorna lo que carga en la primera página.
    """
    captured: list[dict] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()

            def on_response(response):
                if CARRIAPP_SEARCH_PATH in response.url:
                    try:
                        data = response.json()
                        products = data.get("products", [])
                        if products:
                            captured.extend(products)
                    except Exception as exc:
                        print(f"[scraper_web] skipping response {response.url}: {exc}")

            page.on("response", on_response)
            page.goto(
                f"https://carriapp.cl/search/{quote(query)}",
                wait_until="networkidle",
                timeout=30_000,
            )
        finally:
            browser.close()

    seen_ids: set = set()
    batch = []
    for product in captured:
        if product["id"] not in seen_ids:
            seen_ids.add(product["id"])
            batch.append(group_product(product))

    if batch:
        yield batch
