from decimal import Decimal
from datetime import datetime, timedelta, timezone

from app.collector.orderbook import OrderBookStore
from app.collector.models import MarketMetadata, OrderBookLevel, OrderBookState
from app.common.settings import Settings
from app.experiments.microstructure import MicrostructureResearchEngine
from app.experiments.triangular import TriangularArbitrageEngine


def build_market(market: str, base: str, quote: str) -> MarketMetadata:
    return MarketMetadata(
        market=market,
        base=base,
        quote=quote,
        status="trading",
        price_decimals=2,
        quantity_decimals=6,
        min_order_in_quote=Decimal("1"),
        min_order_in_base=Decimal("0.000001"),
        tick_size=Decimal("0.01"),
    )


def build_book(market: str, bid: str, ask: str, size: str = "1000") -> OrderBookState:
    return OrderBookState(
        market=market,
        bids=[OrderBookLevel(price=Decimal(bid), size=Decimal(size))],
        asks=[OrderBookLevel(price=Decimal(ask), size=Decimal(size))],
    )


def test_engine_discovers_triangular_routes():
    settings = Settings(MONEDA_INICIAL="EUR", CAPITALES_SIMULADOS="10")
    engine = TriangularArbitrageEngine(settings)
    markets = {
        "BTC-EUR": build_market("BTC-EUR", "BTC", "EUR"),
        "BTC-USDC": build_market("BTC-USDC", "BTC", "USDC"),
        "USDC-EUR": build_market("USDC-EUR", "USDC", "EUR"),
    }

    engine.rebuild_routes(markets)

    assert any(route.assets == ("EUR", "BTC", "USDC", "EUR") for route in engine.routes)


def test_engine_simulates_profitable_route_when_books_allow_it():
    settings = Settings(MONEDA_INICIAL="EUR", CAPITALES_SIMULADOS="10", COMISION_SIMULADA_TOMADOR="0", LATENCY_SCENARIOS_MS="0")
    engine = TriangularArbitrageEngine(settings)
    markets = {
        "BTC-EUR": build_market("BTC-EUR", "BTC", "EUR"),
        "BTC-USDC": build_market("BTC-USDC", "BTC", "USDC"),
        "USDC-EUR": build_market("USDC-EUR", "USDC", "EUR"),
    }
    books = {
        "BTC-EUR": build_book("BTC-EUR", bid="99", ask="100"),
        "BTC-USDC": build_book("BTC-USDC", bid="120", ask="121"),
        "USDC-EUR": build_book("USDC-EUR", bid="0.90", ask="0.91"),
    }

    opportunities = engine.analyze(markets, books)

    assert opportunities
    assert opportunities[0].route == "EUR -> BTC -> USDC -> EUR"
    assert opportunities[0].net_profit > 0
    assert opportunities[0].executable is True
    assert opportunities[0].ranking_score > 0
    assert opportunities[0].confidence_score > 0


def test_engine_marks_route_non_executable_when_latency_breaks_edge():
    settings = Settings(
        MONEDA_INICIAL="EUR",
        CAPITALES_SIMULADOS="10",
        COMISION_SIMULADA_TOMADOR="0",
        MARGEN_SEGURIDAD_PCT="0.10",
        LATENCY_SCENARIOS_MS="0,500",
    )
    engine = TriangularArbitrageEngine(settings)
    markets = {
        "BTC-EUR": build_market("BTC-EUR", "BTC", "EUR"),
        "BTC-USDC": build_market("BTC-USDC", "BTC", "USDC"),
        "USDC-EUR": build_market("USDC-EUR", "USDC", "EUR"),
    }
    current_books = {
        "BTC-EUR": build_book("BTC-EUR", bid="99", ask="100"),
        "BTC-USDC": build_book("BTC-USDC", bid="120", ask="121"),
        "USDC-EUR": build_book("USDC-EUR", bid="0.90", ask="0.91"),
    }
    delayed_books = {
        "BTC-EUR": build_book("BTC-EUR", bid="99", ask="100"),
        "BTC-USDC": build_book("BTC-USDC", bid="110", ask="111"),
        "USDC-EUR": build_book("USDC-EUR", bid="0.87", ask="0.88"),
    }

    opportunities = engine.analyze(markets, current_books, {0: current_books, 500: delayed_books})

    assert opportunities
    assert opportunities[0].net_profit_pct > 0
    assert opportunities[0].executable is False
    assert opportunities[0].worst_case_latency_ms == 500
    assert opportunities[0].latency_adjusted_profit_pct < settings.safety_margin_pct
    assert opportunities[0].ranking_score < Decimal("70")


def test_orderbook_store_builds_latency_views_from_history():
    store = OrderBookStore()
    base_time = datetime(2026, 8, 20, 16, 0, 0, tzinfo=timezone.utc)
    store.snapshot("BTC-EUR", [["99", "10"]], [["100", "10"]], 1, updated_at=base_time)
    first = store.get("BTC-EUR")
    assert first is not None
    first_time = base_time

    second_time = first_time + timedelta(seconds=1)
    store.apply_update("BTC-EUR", [["101", "10"]], [["102", "10"]], 2, updated_at=second_time)
    second = store.get("BTC-EUR")
    assert second is not None

    views = store.build_latency_views([0, 500], reference_time=second_time + timedelta(milliseconds=500))

    assert views[0]["BTC-EUR"].best_bid.price == Decimal("101")
    assert views[500]["BTC-EUR"].best_bid.price == Decimal("101")


def test_microstructure_engine_detects_imbalance_signal_with_future_outcomes():
    settings = Settings(
        MICROSTRUCTURE_MIN_DEPTH_QUOTE="100",
        MICROSTRUCTURE_IMBALANCE_RATIO_THRESHOLD="5",
        MICROSTRUCTURE_SIGNAL_LIMIT=10,
    )
    engine = MicrostructureResearchEngine(settings)
    market = build_market("BTC-EUR", "BTC", "EUR")
    store = OrderBookStore(history_limit=64)
    base_time = datetime(2026, 8, 20, 16, 0, 0, tzinfo=timezone.utc)

    store.snapshot("BTC-EUR", [["100", "4"], ["99.9", "4"]], [["100.1", "4"], ["100.2", "4"]], 1, updated_at=base_time)
    for step in range(1, 7):
        store.apply_update(
            "BTC-EUR",
            [[str(100 + step * 0.02), "4"], [str(99.9 + step * 0.02), "4"]],
            [[str(100.1 + step * 0.02), "4"], [str(100.2 + step * 0.02), "4"]],
            step + 1,
            updated_at=base_time + timedelta(seconds=step),
        )
    store.apply_update("BTC-EUR", [["100.4", "80"], ["100.3", "50"]], [["100.5", "5"], ["100.6", "5"]], 8, updated_at=base_time + timedelta(seconds=7))
    store.apply_update("BTC-EUR", [["100.6", "60"], ["100.5", "40"]], [["100.7", "5"], ["100.8", "5"]], 9, updated_at=base_time + timedelta(seconds=37))

    signals = engine.analyze_from_histories({"BTC-EUR": market}, store.histories())

    assert signals
    assert any(signal.signal_type == "IMBALANCE_STRONG" for signal in signals)
    assert any(outcome.horizon_ms == 30000 for outcome in signals[0].outcomes)
