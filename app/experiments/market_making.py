from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_DOWN, localcontext
from statistics import median

from app.collector.models import MarketMetadata, OrderBookLevel, OrderBookState
from app.common.settings import Settings


HUNDRED = Decimal("100")
ZERO = Decimal("0")
FOUR_DP = Decimal("0.0001")
EIGHTEEN_DP = Decimal("0.000000000000000001")
PRICE_HORIZONS_MS = (100, 500, 1_000, 5_000, 30_000)


@dataclass(slots=True)
class MarketMakingSignal:
    market: str
    base: str
    quote: str
    snapshot_at: datetime
    spread_pct: Decimal
    bid_depth_quote: Decimal
    ask_depth_quote: Decimal
    depth_score: Decimal
    balance_score: Decimal
    spread_score: Decimal
    overall_score: Decimal
    bias: str
    status: str
    rationale: str
    liquidity_level: str
    volatility_pct: Decimal
    volatility_bucket: str
    imbalance_pct: Decimal
    imbalance_bucket: str
    hour_of_day: int


@dataclass(slots=True)
class SimulatedFill:
    quantity: Decimal = ZERO
    wait_ms: int | None = None
    queue_ahead_start: Decimal = ZERO
    queue_ahead_remaining: Decimal = ZERO


@dataclass(slots=True)
class MarketMakingSimulation:
    simulation_key: str
    signal_at: datetime
    market: str
    base: str
    quote: str
    capital: Decimal
    spread_pct: Decimal
    score: Decimal
    liquidity_level: str
    volatility_bucket: str
    volatility_pct: Decimal
    imbalance_bucket: str
    imbalance_pct: Decimal
    hour_of_day: int
    execution: str
    executable: bool
    discarded: bool
    discarded_reason: str
    queue_position_buy: Decimal
    queue_position_sell: Decimal
    buy_fill_ratio: Decimal
    sell_fill_ratio: Decimal
    wait_buy_ms: int | None
    wait_sell_ms: int | None
    maker_fee: Decimal
    taker_fee: Decimal
    average_entry_price: Decimal | None
    average_exit_price: Decimal | None
    slippage_pct: Decimal
    adverse_selection_pct: Decimal
    price_path: dict[str, Decimal]
    realized_pnl: Decimal
    exposure_time_ms: int
    max_capital_exposed: Decimal
    partial_fill: bool
    only_one_side: bool
    close_reason: str


@dataclass(slots=True)
class MarketMakingSummary:
    total_simulations: int = 0
    executable_signals: int = 0
    discarded_signals: int = 0
    profitable_count: int = 0
    negative_count: int = 0
    pnl_total: Decimal = ZERO
    best_trade: Decimal = ZERO
    worst_trade: Decimal = ZERO
    avg_profit: Decimal = ZERO
    avg_loss: Decimal = ZERO
    mean_pnl: Decimal = ZERO
    median_pnl: Decimal = ZERO
    profitable_ratio: Decimal = ZERO
    max_capital_exposed: Decimal = ZERO
    average_exposure_ms: Decimal = ZERO
    evaluation: str = "NEUTRO"
    by_capital: list[dict] = field(default_factory=list)
    by_market: list[dict] = field(default_factory=list)
    by_signal_type: list[dict] = field(default_factory=list)


class MarketMakingEngine:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.last_signals: list[MarketMakingSignal] = []

    def analyze(
        self,
        markets: dict[str, MarketMetadata],
        books: dict[str, OrderBookState],
    ) -> list[MarketMakingSignal]:
        signals: list[MarketMakingSignal] = []
        for market_name, book in books.items():
            metadata = markets.get(market_name)
            signal = self.evaluate_market(metadata, book, previous_books=[])
            if signal is not None:
                signals.append(signal)

        self.last_signals = sorted(
            signals,
            key=lambda item: (item.overall_score, item.spread_pct, item.bid_depth_quote + item.ask_depth_quote),
            reverse=True,
        )[: self.settings.market_making_signal_limit]
        return self.last_signals

    def evaluate_market(
        self,
        metadata: MarketMetadata | None,
        book: OrderBookState | None,
        previous_books: list[OrderBookState],
    ) -> MarketMakingSignal | None:
        if metadata is None or book is None:
            return None
        if metadata.quote.upper() not in {item.upper() for item in self.settings.track_quote_currencies}:
            return None
        if book.best_bid is None or book.best_ask is None or book.best_ask.price <= 0:
            return None

        spread = book.best_ask.price - book.best_bid.price
        mid = self._mid(book)
        if mid is None or mid <= 0:
            return None

        spread_pct = (spread / mid) * HUNDRED
        bid_depth_quote = sum(level.price * level.size for level in book.bids[:5])
        ask_depth_quote = sum(level.price * level.size for level in book.asks[:5])
        depth_floor = min(bid_depth_quote, ask_depth_quote)

        if spread_pct < self.settings.market_making_min_spread_pct:
            return None
        if spread_pct > self.settings.market_making_max_spread_pct:
            return None
        if depth_floor < self.settings.market_making_min_depth_quote:
            return None

        depth_score = self._bounded_score(depth_floor / self.settings.market_making_min_depth_quote)
        balance_score = self._balance_score(bid_depth_quote, ask_depth_quote)
        spread_score = self._spread_score(spread_pct)
        overall_score = (
            spread_score * Decimal("0.45")
            + depth_score * Decimal("0.35")
            + balance_score * Decimal("0.20")
        ).quantize(Decimal("0.01"))
        bias = self._bias(bid_depth_quote, ask_depth_quote)
        volatility_pct = self._volatility(previous_books)
        imbalance_pct = self._imbalance_pct(bid_depth_quote, ask_depth_quote)
        liquidity_level = self._liquidity_level(depth_floor)
        volatility_bucket = self._volatility_bucket(volatility_pct)
        imbalance_bucket = self._imbalance_bucket(imbalance_pct)
        status = "VIGILAR" if overall_score < Decimal("70") else "CANDIDATA"
        rationale = (
            f"Spread {spread_pct.quantize(Decimal('0.001'))}% con profundidad {liquidity_level.lower()} "
            f"y sesgo {bias.lower()}."
        )

        return MarketMakingSignal(
            market=metadata.market,
            base=metadata.base,
            quote=metadata.quote,
            snapshot_at=book.updated_at or datetime.utcnow(),
            spread_pct=spread_pct.quantize(FOUR_DP),
            bid_depth_quote=bid_depth_quote.quantize(Decimal("0.01")),
            ask_depth_quote=ask_depth_quote.quantize(Decimal("0.01")),
            depth_score=depth_score,
            balance_score=balance_score,
            spread_score=spread_score,
            overall_score=overall_score,
            bias=bias,
            status=status,
            rationale=rationale,
            liquidity_level=liquidity_level,
            volatility_pct=volatility_pct,
            volatility_bucket=volatility_bucket,
            imbalance_pct=imbalance_pct,
            imbalance_bucket=imbalance_bucket,
            hour_of_day=(book.updated_at or datetime.utcnow()).hour,
        )

    @staticmethod
    def _mid(book: OrderBookState) -> Decimal | None:
        if book.best_bid is None or book.best_ask is None:
            return None
        return (book.best_bid.price + book.best_ask.price) / Decimal("2")

    @staticmethod
    def _bounded_score(value: Decimal, max_score: Decimal = Decimal("100")) -> Decimal:
        scaled = value * Decimal("100")
        return min(max_score, max(ZERO, scaled)).quantize(Decimal("0.01"))

    def _spread_score(self, spread_pct: Decimal) -> Decimal:
        target = self.settings.market_making_min_spread_pct * Decimal("4")
        if target <= 0:
            return ZERO
        return self._bounded_score(spread_pct / target)

    @staticmethod
    def _balance_score(bid_depth_quote: Decimal, ask_depth_quote: Decimal) -> Decimal:
        bigger = max(bid_depth_quote, ask_depth_quote)
        smaller = min(bid_depth_quote, ask_depth_quote)
        if bigger <= 0:
            return ZERO
        return ((smaller / bigger) * Decimal("100")).quantize(Decimal("0.01"))

    @staticmethod
    def _bias(bid_depth_quote: Decimal, ask_depth_quote: Decimal) -> str:
        if bid_depth_quote > ask_depth_quote * Decimal("1.15"):
            return "COMPRADOR"
        if ask_depth_quote > bid_depth_quote * Decimal("1.15"):
            return "VENDEDOR"
        return "NEUTRO"

    def _liquidity_level(self, depth_floor: Decimal) -> str:
        if depth_floor >= self.settings.market_making_min_depth_quote * Decimal("4"):
            return "ALTA"
        if depth_floor >= self.settings.market_making_min_depth_quote * Decimal("2"):
            return "MEDIA"
        return "BAJA"

    @staticmethod
    def _imbalance_pct(bid_depth_quote: Decimal, ask_depth_quote: Decimal) -> Decimal:
        total = bid_depth_quote + ask_depth_quote
        if total <= 0:
            return ZERO
        return (((bid_depth_quote - ask_depth_quote) / total) * HUNDRED).quantize(FOUR_DP)

    @staticmethod
    def _imbalance_bucket(imbalance_pct: Decimal) -> str:
        value = abs(imbalance_pct)
        if value < Decimal("5"):
            return "EQUILIBRADO"
        if value < Decimal("15"):
            return "MODERADO"
        return "FUERTE"

    @staticmethod
    def _volatility(previous_books: list[OrderBookState]) -> Decimal:
        mids: list[Decimal] = []
        for book in previous_books[-8:]:
            if book.best_bid is None or book.best_ask is None:
                continue
            mids.append((book.best_bid.price + book.best_ask.price) / Decimal("2"))
        if len(mids) < 2:
            return ZERO
        moves = [abs((current - prev) / prev) * HUNDRED for prev, current in zip(mids, mids[1:], strict=False) if prev > 0]
        if not moves:
            return ZERO
        return (sum(moves, start=ZERO) / Decimal(len(moves))).quantize(FOUR_DP)

    @staticmethod
    def _volatility_bucket(volatility_pct: Decimal) -> str:
        if volatility_pct < Decimal("0.03"):
            return "BAJA"
        if volatility_pct < Decimal("0.10"):
            return "MEDIA"
        return "ALTA"


class MarketMakingBacktester:
    def __init__(self, settings: Settings, engine: MarketMakingEngine) -> None:
        self.settings = settings
        self.engine = engine
        self._processed_keys: set[str] = set()
        self.last_simulations: list[MarketMakingSimulation] = []

    def simulate_from_histories(
        self,
        markets: dict[str, MarketMetadata],
        histories: dict[str, list[OrderBookState]],
    ) -> list[MarketMakingSimulation]:
        new_results: list[MarketMakingSimulation] = []
        quote_filter = {item.upper() for item in self.settings.track_quote_currencies}

        for market_name, history in histories.items():
            metadata = markets.get(market_name)
            if metadata is None or metadata.quote.upper() not in quote_filter or len(history) < 4:
                continue
            ordered = sorted([item for item in history if item.updated_at is not None], key=lambda item: item.updated_at)
            for index, snapshot in enumerate(ordered[:-1]):
                if snapshot.updated_at is None:
                    continue
                final_future = self._find_snapshot_after(ordered, index, PRICE_HORIZONS_MS[-1])
                if final_future is None:
                    continue
                signal = self.engine.evaluate_market(metadata, snapshot, ordered[: index + 1])
                if signal is None:
                    continue
                future_books = ordered[index + 1 :]
                for capital in self.settings.simulated_capitals:
                    key = f"{market_name}|{snapshot.updated_at.isoformat()}|{capital}"
                    if key in self._processed_keys:
                        continue
                    result = self._simulate_signal(signal, snapshot, future_books, metadata, capital, key)
                    self._processed_keys.add(key)
                    new_results.append(result)

        if new_results:
            self.last_simulations = sorted(
                [*new_results, *self.last_simulations],
                key=lambda item: item.signal_at,
                reverse=True,
            )[:300]
        return new_results

    def build_summary(self, rows: list[dict]) -> MarketMakingSummary:
        summary = MarketMakingSummary(total_simulations=len(rows))
        if not rows:
            return summary

        pnls = [Decimal(str(row["realized_pnl"])) for row in rows]
        positive = [value for value in pnls if value > 0]
        negative = [value for value in pnls if value < 0]
        summary.executable_signals = sum(1 for row in rows if row["executable"])
        summary.discarded_signals = sum(1 for row in rows if row["discarded"])
        summary.profitable_count = len(positive)
        summary.negative_count = len(negative)
        summary.pnl_total = sum(pnls, start=ZERO).quantize(Decimal("0.01"))
        summary.best_trade = max(pnls)
        summary.worst_trade = min(pnls)
        summary.avg_profit = (sum(positive, start=ZERO) / Decimal(len(positive))).quantize(Decimal("0.01")) if positive else ZERO
        summary.avg_loss = (sum(negative, start=ZERO) / Decimal(len(negative))).quantize(Decimal("0.01")) if negative else ZERO
        summary.mean_pnl = (summary.pnl_total / Decimal(len(rows))).quantize(Decimal("0.01"))
        summary.median_pnl = Decimal(str(median([float(item) for item in pnls]))).quantize(Decimal("0.01"))
        summary.profitable_ratio = ((Decimal(summary.profitable_count) / Decimal(len(rows))) * HUNDRED).quantize(Decimal("0.01"))
        summary.max_capital_exposed = max(Decimal(str(row["max_capital_exposed"])) for row in rows)
        summary.average_exposure_ms = (
            sum(Decimal(str(row["exposure_time_ms"])) for row in rows) / Decimal(len(rows))
        ).quantize(Decimal("0.01"))
        summary.evaluation = self._evaluate_experiment(summary)
        summary.by_capital = self._group_metrics(rows, "capital")
        summary.by_market = self._group_metrics(rows, "market")
        summary.by_signal_type = self._group_metrics(rows, "liquidity_level")
        return summary

    def _simulate_signal(
        self,
        signal: MarketMakingSignal,
        snapshot: OrderBookState,
        future_books: list[OrderBookState],
        metadata: MarketMetadata,
        capital: Decimal,
        simulation_key: str,
    ) -> MarketMakingSimulation:
        mid = self.engine._mid(snapshot) or ZERO
        half_capital = capital / Decimal("2")
        starting_cash = half_capital
        buy_target_qty = self._quantize_amount((half_capital / snapshot.best_bid.price), metadata.quantity_decimals) if snapshot.best_bid else ZERO
        sell_target_qty = self._quantize_amount((half_capital / snapshot.best_ask.price), metadata.quantity_decimals) if snapshot.best_ask else ZERO

        buy_fill = SimulatedFill(
            queue_ahead_start=self._estimate_queue(snapshot.best_bid.size if snapshot.best_bid else ZERO, signal.imbalance_pct, "buy"),
            queue_ahead_remaining=self._estimate_queue(snapshot.best_bid.size if snapshot.best_bid else ZERO, signal.imbalance_pct, "buy"),
        )
        sell_fill = SimulatedFill(
            queue_ahead_start=self._estimate_queue(snapshot.best_ask.size if snapshot.best_ask else ZERO, signal.imbalance_pct, "sell"),
            queue_ahead_remaining=self._estimate_queue(snapshot.best_ask.size if snapshot.best_ask else ZERO, signal.imbalance_pct, "sell"),
        )

        buy_filled = ZERO
        sell_filled = ZERO
        maker_fee = ZERO
        taker_fee = ZERO
        cash = starting_cash
        first_fill_time: datetime | None = None
        last_fill_time: datetime | None = None
        max_exposed = ZERO

        previous = snapshot
        for future in future_books:
            if future.updated_at is None or snapshot.updated_at is None:
                continue
            wait_ms = int((future.updated_at - snapshot.updated_at).total_seconds() * 1000)
            if wait_ms > PRICE_HORIZONS_MS[-1]:
                break

            buy_filled_delta = self._maker_fill_delta(
                side="buy",
                target_price=snapshot.best_bid.price if snapshot.best_bid else ZERO,
                target_qty=buy_target_qty,
                already_filled=buy_filled,
                previous=previous,
                current=future,
                fill_state=buy_fill,
            )
            sell_filled_delta = self._maker_fill_delta(
                side="sell",
                target_price=snapshot.best_ask.price if snapshot.best_ask else ZERO,
                target_qty=sell_target_qty,
                already_filled=sell_filled,
                previous=previous,
                current=future,
                fill_state=sell_fill,
            )

            if buy_filled_delta > 0 and snapshot.best_bid is not None:
                buy_filled += buy_filled_delta
                quote_cost = (buy_filled_delta * snapshot.best_bid.price).quantize(EIGHTEEN_DP)
                fee = (quote_cost * self.settings.simulated_maker_fee).quantize(EIGHTEEN_DP)
                maker_fee += fee
                cash -= quote_cost + fee
                buy_fill.wait_ms = buy_fill.wait_ms or wait_ms
                first_fill_time = first_fill_time or future.updated_at
                last_fill_time = future.updated_at

            if sell_filled_delta > 0 and snapshot.best_ask is not None:
                sell_filled += sell_filled_delta
                quote_out = (sell_filled_delta * snapshot.best_ask.price).quantize(EIGHTEEN_DP)
                fee = (quote_out * self.settings.simulated_maker_fee).quantize(EIGHTEEN_DP)
                maker_fee += fee
                cash += quote_out - fee
                sell_fill.wait_ms = sell_fill.wait_ms or wait_ms
                first_fill_time = first_fill_time or future.updated_at
                last_fill_time = future.updated_at

            inventory_delta = buy_filled - sell_filled
            mark_mid = self.engine._mid(future) or mid
            max_exposed = max(max_exposed, abs(inventory_delta) * mark_mid)
            previous = future

        close_book = self._find_snapshot_after(future_books, -1, PRICE_HORIZONS_MS[-1], base_time=snapshot.updated_at) or future_books[-1]
        inventory_delta = buy_filled - sell_filled
        average_exit_price: Decimal | None = None
        slippage_pct = ZERO
        close_reason = "SIN_CIERRE"
        if inventory_delta > 0:
            exit_quote, average_exit_price, slippage_pct = self._execute_taker_sell(inventory_delta, close_book)
            fee = (exit_quote * self.settings.simulated_taker_fee).quantize(EIGHTEEN_DP)
            taker_fee += fee
            cash += exit_quote - fee
            close_reason = "CIERRE_LARGO"
        elif inventory_delta < 0:
            exit_cost, average_exit_price, slippage_pct = self._execute_taker_buy(abs(inventory_delta), close_book)
            fee = (exit_cost * self.settings.simulated_taker_fee).quantize(EIGHTEEN_DP)
            taker_fee += fee
            cash -= exit_cost + fee
            close_reason = "CIERRE_CORTO"

        realized_pnl = (cash - starting_cash).quantize(Decimal("0.01"))
        exposure_time_ms = 0
        if first_fill_time and last_fill_time:
            exposure_time_ms = int((last_fill_time - first_fill_time).total_seconds() * 1000)
        elif first_fill_time and close_book.updated_at:
            exposure_time_ms = int((close_book.updated_at - first_fill_time).total_seconds() * 1000)

        buy_fill_ratio = (buy_filled / buy_target_qty).quantize(FOUR_DP) if buy_target_qty > 0 else ZERO
        sell_fill_ratio = (sell_filled / sell_target_qty).quantize(FOUR_DP) if sell_target_qty > 0 else ZERO
        price_path = self._price_path(snapshot, future_books)
        adverse_selection_pct = self._adverse_selection(snapshot, price_path, buy_filled, sell_filled)
        entry_prices = []
        if buy_filled > 0 and snapshot.best_bid is not None:
            entry_prices.append(snapshot.best_bid.price)
        if sell_filled > 0 and snapshot.best_ask is not None:
            entry_prices.append(snapshot.best_ask.price)
        average_entry_price = (
            (sum(entry_prices, start=ZERO) / Decimal(len(entry_prices))).quantize(EIGHTEEN_DP)
            if entry_prices
            else None
        )

        execution = self._execution_label(buy_fill_ratio, sell_fill_ratio)
        discarded = buy_filled == 0 and sell_filled == 0
        executable = (not discarded) and realized_pnl > 0 and buy_fill_ratio > 0 and sell_fill_ratio > 0
        discarded_reason = self._discarded_reason(discarded, realized_pnl, buy_fill_ratio, sell_fill_ratio)

        return MarketMakingSimulation(
            simulation_key=simulation_key,
            signal_at=signal.snapshot_at,
            market=signal.market,
            base=signal.base,
            quote=signal.quote,
            capital=capital,
            spread_pct=signal.spread_pct,
            score=signal.overall_score,
            liquidity_level=signal.liquidity_level,
            volatility_bucket=signal.volatility_bucket,
            volatility_pct=signal.volatility_pct,
            imbalance_bucket=signal.imbalance_bucket,
            imbalance_pct=signal.imbalance_pct,
            hour_of_day=signal.hour_of_day,
            execution=execution,
            executable=executable,
            discarded=discarded,
            discarded_reason=discarded_reason,
            queue_position_buy=buy_fill.queue_ahead_start,
            queue_position_sell=sell_fill.queue_ahead_start,
            buy_fill_ratio=buy_fill_ratio,
            sell_fill_ratio=sell_fill_ratio,
            wait_buy_ms=buy_fill.wait_ms,
            wait_sell_ms=sell_fill.wait_ms,
            maker_fee=maker_fee.quantize(Decimal("0.01")),
            taker_fee=taker_fee.quantize(Decimal("0.01")),
            average_entry_price=average_entry_price,
            average_exit_price=average_exit_price.quantize(EIGHTEEN_DP) if average_exit_price is not None else None,
            slippage_pct=slippage_pct.quantize(FOUR_DP),
            adverse_selection_pct=adverse_selection_pct.quantize(FOUR_DP),
            price_path=price_path,
            realized_pnl=realized_pnl,
            exposure_time_ms=exposure_time_ms,
            max_capital_exposed=max_exposed.quantize(Decimal("0.01")),
            partial_fill=(buy_fill_ratio not in {ZERO, Decimal("1.0000")} or sell_fill_ratio not in {ZERO, Decimal("1.0000")}),
            only_one_side=((buy_filled > 0) ^ (sell_filled > 0)),
            close_reason=close_reason,
        )

    def _maker_fill_delta(
        self,
        side: str,
        target_price: Decimal,
        target_qty: Decimal,
        already_filled: Decimal,
        previous: OrderBookState,
        current: OrderBookState,
        fill_state: SimulatedFill,
    ) -> Decimal:
        if target_qty <= already_filled or target_price <= 0:
            return ZERO

        remaining = target_qty - already_filled
        if side == "buy":
            if current.best_ask and current.best_ask.price <= target_price:
                fill_state.queue_ahead_remaining = ZERO
                return remaining
            if previous.best_bid and current.best_bid and previous.best_bid.price == target_price and current.best_bid.price == target_price:
                depletion = max(previous.best_bid.size - current.best_bid.size, ZERO)
                return self._consume_queue(remaining, depletion, fill_state)
        else:
            if current.best_bid and current.best_bid.price >= target_price:
                fill_state.queue_ahead_remaining = ZERO
                return remaining
            if previous.best_ask and current.best_ask and previous.best_ask.price == target_price and current.best_ask.price == target_price:
                depletion = max(previous.best_ask.size - current.best_ask.size, ZERO)
                return self._consume_queue(remaining, depletion, fill_state)

        return ZERO

    @staticmethod
    def _consume_queue(remaining: Decimal, depletion: Decimal, fill_state: SimulatedFill) -> Decimal:
        if depletion <= 0:
            return ZERO
        if fill_state.queue_ahead_remaining > 0:
            consumed_queue = min(fill_state.queue_ahead_remaining, depletion)
            fill_state.queue_ahead_remaining -= consumed_queue
            depletion -= consumed_queue
        if depletion <= 0:
            return ZERO
        return min(remaining, depletion).quantize(EIGHTEEN_DP)

    @staticmethod
    def _estimate_queue(displayed_size: Decimal, imbalance_pct: Decimal, side: str) -> Decimal:
        bias_adjustment = Decimal("0.10") if (side == "buy" and imbalance_pct > 0) or (side == "sell" and imbalance_pct < 0) else Decimal("-0.05")
        share = min(Decimal("0.80"), max(Decimal("0.25"), Decimal("0.50") + bias_adjustment))
        return (displayed_size * share).quantize(EIGHTEEN_DP)

    def _execute_taker_sell(self, quantity: Decimal, book: OrderBookState) -> tuple[Decimal, Decimal, Decimal]:
        remaining = quantity
        quote_out = ZERO
        top_price = book.best_bid.price if book.best_bid else ZERO
        for level in book.bids:
            consumed = min(level.size, remaining)
            quote_out += consumed * level.price
            remaining -= consumed
            if remaining <= EIGHTEEN_DP:
                remaining = ZERO
                break
        average_price = (quote_out / quantity).quantize(EIGHTEEN_DP) if quantity > 0 else ZERO
        slippage = (((top_price - average_price) / top_price) * HUNDRED).quantize(FOUR_DP) if top_price > 0 else ZERO
        return quote_out.quantize(EIGHTEEN_DP), average_price, max(ZERO, slippage)

    def _execute_taker_buy(self, quantity: Decimal, book: OrderBookState) -> tuple[Decimal, Decimal, Decimal]:
        remaining = quantity
        quote_cost = ZERO
        top_price = book.best_ask.price if book.best_ask else ZERO
        for level in book.asks:
            consumed = min(level.size, remaining)
            quote_cost += consumed * level.price
            remaining -= consumed
            if remaining <= EIGHTEEN_DP:
                remaining = ZERO
                break
        average_price = (quote_cost / quantity).quantize(EIGHTEEN_DP) if quantity > 0 else ZERO
        slippage = (((average_price - top_price) / top_price) * HUNDRED).quantize(FOUR_DP) if top_price > 0 else ZERO
        return quote_cost.quantize(EIGHTEEN_DP), average_price, max(ZERO, slippage)

    def _price_path(self, snapshot: OrderBookState, future_books: list[OrderBookState]) -> dict[str, Decimal]:
        if snapshot.updated_at is None:
            return {}
        base_mid = self.engine._mid(snapshot) or ZERO
        if base_mid <= 0:
            return {}
        path: dict[str, Decimal] = {}
        for horizon in PRICE_HORIZONS_MS:
            target = self._find_snapshot_after(future_books, -1, horizon, base_time=snapshot.updated_at)
            label = f"{horizon}ms"
            if target is None:
                path[label] = ZERO
                continue
            mid = self.engine._mid(target) or base_mid
            path[label] = (((mid - base_mid) / base_mid) * HUNDRED).quantize(FOUR_DP)
        return path

    @staticmethod
    def _adverse_selection(
        snapshot: OrderBookState,
        price_path: dict[str, Decimal],
        buy_filled: Decimal,
        sell_filled: Decimal,
    ) -> Decimal:
        move_500 = price_path.get("500ms", ZERO)
        if buy_filled > sell_filled:
            return max(ZERO, -move_500)
        if sell_filled > buy_filled:
            return max(ZERO, move_500)
        return abs(move_500) / Decimal("2")

    @staticmethod
    def _execution_label(buy_fill_ratio: Decimal, sell_fill_ratio: Decimal) -> str:
        if buy_fill_ratio >= Decimal("1") and sell_fill_ratio >= Decimal("1"):
            return "AMBOS_LADOS"
        if buy_fill_ratio > 0 and sell_fill_ratio > 0:
            return "PARCIAL_DOBLE"
        if buy_fill_ratio > 0:
            return "SOLO_BID"
        if sell_fill_ratio > 0:
            return "SOLO_ASK"
        return "SIN_EJECUCION"

    @staticmethod
    def _discarded_reason(
        discarded: bool,
        realized_pnl: Decimal,
        buy_fill_ratio: Decimal,
        sell_fill_ratio: Decimal,
    ) -> str:
        if discarded:
            return "La orden no habria llegado a ejecutarse dentro del horizonte."
        if realized_pnl <= 0:
            return "La señal pierde dinero tras comisiones, cierres forzados o seleccion adversa."
        if buy_fill_ratio < 1 or sell_fill_ratio < 1:
            return "La señal depende de ejecucion parcial y requiere prudencia."
        return "Rentable de forma neta tras costes y cierre."

    @staticmethod
    def _find_snapshot_after(
        history: list[OrderBookState],
        start_index: int,
        delta_ms: int,
        base_time: datetime | None = None,
    ) -> OrderBookState | None:
        if base_time is None:
            if start_index < 0 or start_index >= len(history) or history[start_index].updated_at is None:
                return None
            base_time = history[start_index].updated_at
        target_time = base_time + timedelta(milliseconds=delta_ms)
        for item in history[start_index + 1 :] if start_index >= 0 else history:
            if item.updated_at is not None and item.updated_at >= target_time:
                return item
        return None

    @staticmethod
    def _quantize_amount(value: Decimal, decimals: int | None) -> Decimal:
        if decimals is None:
            return value.quantize(EIGHTEEN_DP, rounding=ROUND_DOWN)
        places = Decimal("1").scaleb(-decimals)
        with localcontext() as ctx:
            ctx.rounding = ROUND_DOWN
            return value.quantize(places)

    @staticmethod
    def _evaluate_experiment(summary: MarketMakingSummary) -> str:
        if summary.total_simulations < 50:
            return "NEUTRO"
        if summary.pnl_total > 0 and summary.profitable_ratio >= Decimal("55") and summary.median_pnl > 0:
            return "PROMETEDOR"
        if summary.pnl_total <= 0 or summary.profitable_ratio < Decimal("45"):
            return "NO RENTABLE"
        return "NEUTRO"

    @staticmethod
    def _group_metrics(rows: list[dict], field: str) -> list[dict]:
        groups: dict[str, list[dict]] = {}
        for row in rows:
            key = str(row.get(field))
            groups.setdefault(key, []).append(row)
        result = []
        for key, items in groups.items():
            pnls = [Decimal(str(item["realized_pnl"])) for item in items]
            result.append(
                {
                    "label": key,
                    "count": len(items),
                    "pnl": float(sum(pnls, start=ZERO).quantize(Decimal("0.01"))),
                    "mean_pnl": float((sum(pnls, start=ZERO) / Decimal(len(items))).quantize(Decimal("0.01"))),
                    "profitable_ratio": float((Decimal(sum(1 for item in pnls if item > 0)) / Decimal(len(items)) * HUNDRED).quantize(Decimal("0.01"))),
                }
            )
        return sorted(result, key=lambda item: (item["pnl"], item["count"]), reverse=True)[:10]
