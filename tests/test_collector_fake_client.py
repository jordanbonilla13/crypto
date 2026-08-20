import pytest

from app.collector.service import CollectorService
from app.common.settings import Settings


class FakeDatabase:
    def __init__(self):
        self.markets = []
        self.books = []
        self.events = []

    async def upsert_markets(self, markets):
        self.markets = markets

    async def upsert_books(self, books):
        self.books = books

    async def insert_event(self, event_type, payload):
        self.events.append((event_type, payload))

    async def upsert_experiment_status(self, experiment_type, status):
        return None

    async def list_recent_market_making_simulations(self, limit=5000):
        return []

    async def freeze_experiment_baseline(self, experiment_type, status, summary, configuration, notes):
        return None

    async def insert_microstructure_signals(self, signals):
        return None


class FakeClient:
    async def get_markets(self):
        return [
            {
                "market": "BTC-EUR",
                "base": "BTC",
                "quote": "EUR",
                "status": "trading",
                "priceDecimals": 2,
                "quantityDecimals": 6,
                "minOrderInQuoteAsset": "5",
                "minOrderInBaseAsset": "0.0001",
                "tickSize": "0.01",
            },
            {
                "market": "ETH-EUR",
                "base": "ETH",
                "quote": "EUR",
                "status": "trading",
                "priceDecimals": 2,
                "quantityDecimals": 6,
                "minOrderInQuoteAsset": "5",
                "minOrderInBaseAsset": "0.001",
                "tickSize": "0.01",
            },
        ]

    async def get_book(self, market: str, depth: int):
        return {
            "market": market,
            "nonce": 1,
            "bids": [["10", "1"]],
            "asks": [["11", "1"]],
        }


@pytest.mark.asyncio
async def test_collector_bootstraps_markets_and_books():
    settings = Settings(TRACK_QUOTE_CURRENCIES="EUR", BOOK_MARKETS_LIMIT=10)
    database = FakeDatabase()
    service = CollectorService(settings=settings, database=database, client=FakeClient())

    await service._bootstrap_markets()

    summary = service.build_dashboard_summary()
    assert summary["markets_found"] == 2
    assert summary["markets_tracked"] == 2
    assert len(summary["markets"]) == 2


def test_collector_exposes_market_making_signals():
    settings = Settings(
        TRACK_QUOTE_CURRENCIES="EUR",
        BOOK_MARKETS_LIMIT=10,
        MARKET_MAKING_MIN_SPREAD_PCT="5",
        MARKET_MAKING_MAX_SPREAD_PCT="15",
        MARKET_MAKING_MIN_DEPTH_QUOTE="5",
    )
    service = CollectorService(settings=settings, database=FakeDatabase(), client=FakeClient())
    service.markets = {
        "BTC-EUR": service._to_market_metadata(
            {
                "market": "BTC-EUR",
                "base": "BTC",
                "quote": "EUR",
                "status": "trading",
                "priceDecimals": 2,
                "quantityDecimals": 6,
                "minOrderInQuoteAsset": "5",
                "minOrderInBaseAsset": "0.0001",
                "tickSize": "0.01",
            }
        )
    }
    service.books.snapshot("BTC-EUR", [["100", "2"]], [["110", "2"]], 1)
    service.market_making_engine.analyze(service.markets, {book.market: book for book in service.books.all()})

    payload = service.build_dashboard_summary()

    assert payload["best_strategy"] == "Creacion de liquidez"
