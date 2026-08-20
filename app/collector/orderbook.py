from collections import deque
from datetime import datetime, timezone
from decimal import Decimal

from app.collector.models import OrderBookLevel, OrderBookState


class OrderBookStore:
    def __init__(self, history_limit: int = 32) -> None:
        self._books: dict[str, OrderBookState] = {}
        self._history: dict[str, deque[OrderBookState]] = {}
        self._history_limit = history_limit

    def snapshot(
        self,
        market: str,
        bids: list[list[str]],
        asks: list[list[str]],
        nonce: int | None,
        updated_at: datetime | None = None,
    ) -> None:
        self._books[market] = OrderBookState(
            market=market,
            bids=self._parse_levels(bids, reverse=True),
            asks=self._parse_levels(asks, reverse=False),
            nonce=nonce,
            updated_at=updated_at or datetime.now(timezone.utc),
        )
        self._record_history(market)

    def apply_update(
        self,
        market: str,
        bids: list[list[str]],
        asks: list[list[str]],
        nonce: int | None,
        updated_at: datetime | None = None,
    ) -> None:
        if market not in self._books:
            self.snapshot(market, bids, asks, nonce, updated_at=updated_at)
            return

        book = self._books[market]
        self._apply_side(book.bids, bids, reverse=True)
        self._apply_side(book.asks, asks, reverse=False)
        book.nonce = nonce
        book.updated_at = updated_at or datetime.now(timezone.utc)
        self._record_history(market)

    def get(self, market: str) -> OrderBookState | None:
        return self._books.get(market)

    def all(self) -> list[OrderBookState]:
        return list(self._books.values())

    def history(self, market: str) -> list[OrderBookState]:
        return [item.clone() for item in self._history.get(market, [])]

    def histories(self) -> dict[str, list[OrderBookState]]:
        return {market: [item.clone() for item in history] for market, history in self._history.items()}

    def build_latency_views(
        self,
        latencies_ms: list[int],
        reference_time: datetime | None = None,
    ) -> dict[int, dict[str, OrderBookState]]:
        now = reference_time or datetime.now(timezone.utc)
        normalized = sorted({max(0, int(item)) for item in latencies_ms})
        views: dict[int, dict[str, OrderBookState]] = {}
        for latency_ms in normalized:
            cutoff = now.timestamp() - (latency_ms / 1000)
            scenario_books: dict[str, OrderBookState] = {}
            for market, history in self._history.items():
                selected = None
                for snapshot in reversed(history):
                    if snapshot.updated_at is None:
                        continue
                    if snapshot.updated_at.timestamp() <= cutoff:
                        selected = snapshot
                        break
                if selected is None and history:
                    selected = history[0]
                if selected is not None:
                    scenario_books[market] = selected.clone()
            views[latency_ms] = scenario_books
        return views

    def _record_history(self, market: str) -> None:
        book = self._books.get(market)
        if book is None:
            return
        history = self._history.setdefault(market, deque(maxlen=self._history_limit))
        history.append(book.clone())

    @staticmethod
    def _parse_levels(levels: list[list[str]], reverse: bool) -> list[OrderBookLevel]:
        parsed = [OrderBookLevel(price=Decimal(price), size=Decimal(size)) for price, size in levels]
        return sorted(parsed, key=lambda item: item.price, reverse=reverse)

    @staticmethod
    def _apply_side(side: list[OrderBookLevel], updates: list[list[str]], reverse: bool) -> None:
        by_price = {level.price: level for level in side}
        for raw_price, raw_size in updates:
            price = Decimal(raw_price)
            size = Decimal(raw_size)
            if size == 0:
                by_price.pop(price, None)
                continue
            by_price[price] = OrderBookLevel(price=price, size=size)
        side[:] = sorted(by_price.values(), key=lambda item: item.price, reverse=reverse)
