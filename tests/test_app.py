# tests/test_app.py
import json
import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch


MOCK_STORES = {
    1: {"name": "Jumbo", "url": "https://jumbo.cl", "logo": ""},
    2: {"name": "Unimarc", "url": "https://unimarc.cl", "logo": ""},
}

MOCK_CATEGORIES = [
    {"slug": "despensa", "name": "Despensa", "parent": None},
    {"slug": "arroz_legumbres", "name": "Arroz y Legumbres", "parent": "despensa"},
]

MOCK_BATCH = [
    {
        "product_id": 123,
        "slug": "arroz-test-abc",
        "brand": "TestBrand",
        "unit": "kg",
        "stores": [
            {
                "store_id": 1,
                "product_name": "Arroz Test 1 kg",
                "price": 1000.0,
                "price_per_unit": 1000.0,
                "price_per_unit_scale": 1,
                "best_promo_price": None,
                "available": 9999,
                "url": "https://jumbo.cl/product/arroz-test",
                "image": "",
            }
        ],
    }
]


@pytest.fixture
def client():
    # Import app inside patch context so lifespan startup uses mocked functions
    with patch("scraper_api.get_stores", return_value=MOCK_STORES), \
         patch("scraper_api.get_categories", return_value=MOCK_CATEGORIES):
        from app import app
        with TestClient(app) as c:
            yield c


def test_root_returns_html(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_categories_endpoint(client):
    response = client.get("/api/categories")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    assert any(c["slug"] == "despensa" for c in data)


def test_search_stream_returns_sse(client):
    def mock_iter_products(query, **kwargs):
        yield MOCK_BATCH

    with patch("scraper_api.iter_products", side_effect=mock_iter_products):
        with client.stream("GET", "/api/search/stream?q=arroz") as response:
            assert response.status_code == 200
            assert "text/event-stream" in response.headers["content-type"]
            body = response.read().decode()
            assert "event: products" in body
            assert "event: done" in body
            # Verify enrichment: store_name must be present in SSE payload
            assert "store_name" in body


def test_search_stream_fallback_on_api_error(client):
    from urllib.error import URLError

    def failing_api(query, **kwargs):
        raise URLError("connection refused")
        yield  # noqa: unreachable — makes this a generator for mock compatibility

    def mock_fallback(query):
        yield MOCK_BATCH

    with patch("scraper_api.iter_products", side_effect=failing_api), \
         patch("scraper_web.iter_products", side_effect=mock_fallback):
        with client.stream("GET", "/api/search/stream?q=arroz") as response:
            body = response.read().decode()
            assert "event: products" in body
            assert "event: done" in body


def test_search_stream_error_event_when_both_fail(client):
    from urllib.error import URLError

    def failing(query, **kwargs):
        raise URLError("fail")
        yield  # noqa: unreachable — makes this a generator for mock compatibility

    with patch("scraper_api.iter_products", side_effect=failing), \
         patch("scraper_web.iter_products", side_effect=failing):
        with client.stream("GET", "/api/search/stream?q=arroz") as response:
            body = response.read().decode()
            assert "event: error" in body
