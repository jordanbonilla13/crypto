from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal


@dataclass(slots=True)
class MarketMetadata:
    market: str
    base: str
    quote: str
    status: str
    price_decimals: int | None
    quantity_decimals: int | None
    min_order_in_quote: Decimal | None
    min_order_in_base: Decimal | None
    tick_size: Decimal | None


@dataclass(slots=True)
class OrderBookLevel:
    price: Decimal
    size: Decimal


@dataclass(slots=True)
class OrderBookState:
    market: str
    bids: list[OrderBookLevel] = field(default_factory=list)
    asks: list[OrderBookLevel] = field(default_factory=list)
    nonce: int | None = None
    updated_at: datetime | None = None

    @property
    def best_bid(self) -> OrderBookLevel | None:
        return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> OrderBookLevel | None:
        return self.asks[0] if self.asks else None

    def clone(self) -> "OrderBookState":
        return OrderBookState(
            market=self.market,
            bids=[OrderBookLevel(price=level.price, size=level.size) for level in self.bids],
            asks=[OrderBookLevel(price=level.price, size=level.size) for level in self.asks],
            nonce=self.nonce,
            updated_at=self.updated_at,
        )


@dataclass(slots=True)
class CollectorState:
    connected: bool = False
    discovered_markets: int = 0
    tracked_markets: int = 0
    last_market_update_at: datetime | None = None
    last_error: str | None = None
    reconnect_count: int = 0
    total_updates: int = 0
    average_update_interval_ms: float | None = None
