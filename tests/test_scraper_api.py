# tests/test_scraper_api.py
import json
import pytest
from unittest.mock import patch, MagicMock
import scraper_api


MOCK_PRODUCT = {
    "id": 123,
    "brand": "TestBrand",
    "category_id": 5,
    "slug": "test-product-abc",
    "total": 1.0,
    "unit": "kg",
    "stores": [
        {
            "name": "Test Product 1 kg",
            "available": 9999,
            "images": {"catalog": "https://img.example.com/cat.jpg"},
            "price": 1000.0,
            "price_per_unit": 1000.0,
            "price_per_unit_scale": 1,
            "promos": [],
            "store_id": 1,
            "sku": "abc123",
            "url": "https://jumbo.cl/product/test",
            "last_updated": "2026-04-30T09:00:00",
        },
        {
            "name": "Test Product 1 kg",
            "available": 9999,
            "images": {"catalog": "https://img.example.com/cat2.jpg"},
            "price": 900.0,
            "price_per_unit": 900.0,
            "price_per_unit_scale": 1,
            "promos": [{"price": 800.0, "restrictions": ["Tarjeta Unimarc"]}],
            "store_id": 2,
            "sku": "def456",
            "url": "https://unimarc.cl/product/test",
            "last_updated": "2026-04-30T09:00:00",
        },
    ],
}

MOCK_PAGE_1 = {"products": [MOCK_PRODUCT], "next_page": None}


def test_group_product_structure():
    result = scraper_api.group_product(MOCK_PRODUCT)
    assert result["product_id"] == 123
    assert result["brand"] == "TestBrand"
    assert result["unit"] == "kg"
    assert len(result["stores"]) == 2


def test_group_product_store_fields():
    result = scraper_api.group_product(MOCK_PRODUCT)
    store = result["stores"][0]
    assert store["store_id"] == 1
    assert store["product_name"] == "Test Product 1 kg"
    assert store["price"] == 1000.0
    assert store["url"] == "https://jumbo.cl/product/test"
    assert store["best_promo_price"] is None


def test_group_product_promo_price():
    result = scraper_api.group_product(MOCK_PRODUCT)
    unimarc = next(s for s in result["stores"] if s["store_id"] == 2)
    assert unimarc["best_promo_price"] == 800.0


def test_iter_products_yields_grouped(monkeypatch):
    monkeypatch.setattr(scraper_api, "get", lambda url: MOCK_PAGE_1)
    batches = list(scraper_api.iter_products("test"))
    assert len(batches) == 1
    assert batches[0][0]["product_id"] == 123


def test_iter_products_deduplicates(monkeypatch):
    # Simulate two pages returning the same product
    call_count = {"n": 0}
    def mock_get(url):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return {"products": [MOCK_PRODUCT], "next_page": "token1"}
        return {"products": [MOCK_PRODUCT], "next_page": None}  # duplicate

    monkeypatch.setattr(scraper_api, "get", mock_get)
    batches = list(scraper_api.iter_products("test"))
    total_products = sum(len(b) for b in batches)
    assert total_products == 1  # deduplicado


def test_get_stores_returns_mapping(monkeypatch):
    mock_stores = [
        {"id": 1, "name": "Jumbo", "store_url": "https://jumbo.cl", "logo_url": ""},
        {"id": 2, "name": "Unimarc", "store_url": "https://unimarc.cl", "logo_url": ""},
    ]
    monkeypatch.setattr(scraper_api, "get", lambda url: mock_stores)
    result = scraper_api.get_stores()
    assert result == {1: {"name": "Jumbo", "url": "https://jumbo.cl", "logo": ""},
                      2: {"name": "Unimarc", "url": "https://unimarc.cl", "logo": ""}}


def test_get_categories_returns_list(monkeypatch):
    mock_sections = [
        {"id": 1, "name": "Despensa", "slug": "despensa",
         "sub_menu": [{"id": 10, "name": "Arroz y Legumbres", "slug": "arroz_legumbres"}]},
        {"id": 2, "name": "Lácteos", "slug": "lacteos", "sub_menu": []},
    ]
    monkeypatch.setattr(scraper_api, "get", lambda url: mock_sections)
    result = scraper_api.get_categories()
    assert {"slug": "despensa", "name": "Despensa", "parent": None} in result
    assert {"slug": "arroz_legumbres", "name": "Arroz y Legumbres", "parent": "despensa"} in result


def test_iter_products_encodes_query(monkeypatch):
    called_urls = []
    def mock_get(url):
        called_urls.append(url)
        return {"products": [], "next_page": None}
    monkeypatch.setattr(scraper_api, "get", mock_get)
    list(scraper_api.iter_products("papel higiénico"))
    assert called_urls, "get() should have been called"
    assert " " not in called_urls[0], "URL must not contain spaces"
    assert "é" not in called_urls[0], "URL must not contain raw accented chars"
