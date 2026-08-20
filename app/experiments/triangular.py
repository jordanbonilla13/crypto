from __future__ import annotations

from dataclasses import dataclass
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, localcontext
from itertools import pairwise

from app.collector.models import MarketMetadata, OrderBookLevel, OrderBookState
from app.common.settings import Settings
from app.experiments.models import DailyCounters, OpportunitySnapshot, RouteDefinition, SimulatedLeg


EIGHTEEN_DP = Decimal("0.000000000000000001")


@dataclass(slots=True)
class RouteSimulation:
    final_amount: Decimal
    gross_profit: Decimal
    gross_profit_pct: Decimal
    total_fee_amount: Decimal
    slippage_pct: Decimal
    legs: list[SimulatedLeg]


class TriangularArbitrageEngine:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.routes: list[RouteDefinition] = []
        self.active_opportunities: dict[str, OpportunitySnapshot] = {}
        self.completed_opportunities: list[OpportunitySnapshot] = []
        self.daily_counters = DailyCounters(day=datetime.now(timezone.utc).date())
        self.last_analysis_at: datetime | None = None
        self.last_cycle_opportunities: list[OpportunitySnapshot] = []

    def rebuild_routes(self, markets: dict[str, MarketMetadata]) -> None:
        start = self.settings.initial_currency.upper()
        neighbors: dict[str, set[str]] = {}

        for market in markets.values():
            if market.status.lower() != "trading":
                continue
            neighbors.setdefault(market.base.upper(), set()).add(market.quote.upper())
            neighbors.setdefault(market.quote.upper(), set()).add(market.base.upper())

        route_keys: set[tuple[str, str, str, str]] = set()
        for asset_a in neighbors.get(start, set()):
            if asset_a == start:
                continue
            for asset_b in neighbors.get(asset_a, set()):
                if asset_b in {start, asset_a}:
                    continue
                if start in neighbors.get(asset_b, set()):
                    route_keys.add((start, asset_a, asset_b, start))

        self.routes = [RouteDefinition(route) for route in sorted(route_keys)]

    def analyze(
        self,
        markets: dict[str, MarketMetadata],
        books: dict[str, OrderBookState],
        scenario_books: dict[int, dict[str, OrderBookState]] | None = None,
    ) -> list[OpportunitySnapshot]:
        self._roll_day_if_needed()
        if not self.routes:
            self.rebuild_routes(markets)

        now = datetime.now(timezone.utc)
        latency_views = self._normalize_latency_views(books, scenario_books)
        detected_now: dict[str, OpportunitySnapshot] = {}

        for route in self.routes:
            for capital in self.settings.simulated_capitals:
                opportunity = self._simulate_route(route, capital, markets, books, latency_views, now)
                if opportunity is None:
                    continue
                if opportunity.net_profit_pct > Decimal("0"):
                    detected_now[opportunity.key] = opportunity

        self._reconcile_active_opportunities(detected_now, now)
        self.last_analysis_at = now
        self.last_cycle_opportunities = sorted(
            detected_now.values(),
            key=lambda item: (item.ranking_score, item.worst_case_profit_pct, item.net_profit),
            reverse=True,
        )
        return self.last_cycle_opportunities

    def _simulate_route(
        self,
        route: RouteDefinition,
        capital: Decimal,
        markets: dict[str, MarketMetadata],
        books: dict[str, OrderBookState],
        latency_views: dict[int, dict[str, OrderBookState]],
        now: datetime,
    ) -> OpportunitySnapshot | None:
        baseline = self._run_route_simulation(route, capital, markets, books)
        if baseline is None:
            return None

        markets_used = [leg.market for leg in baseline.legs]
        scenario_outcomes: list[tuple[int, RouteSimulation | None]] = []
        for latency_ms, scenario_view in latency_views.items():
            scenario_outcomes.append(
                (latency_ms, self._run_route_simulation(route, capital, markets, scenario_view))
            )

        executable = True
        executable_reason = "Rentable en todos los escenarios configurados."
        worst_case_latency_ms = 0
        worst_case_profit = baseline.gross_profit
        worst_case_profit_pct = baseline.gross_profit_pct

        for latency_ms, scenario in scenario_outcomes:
            if scenario is None:
                executable = False
                executable_reason = f"Sin profundidad suficiente en escenario de {latency_ms} ms."
                worst_case_latency_ms = latency_ms
                worst_case_profit = Decimal("-1")
                worst_case_profit_pct = Decimal("-100")
                break
            if scenario.gross_profit_pct < worst_case_profit_pct:
                worst_case_latency_ms = latency_ms
                worst_case_profit = scenario.gross_profit
                worst_case_profit_pct = scenario.gross_profit_pct

        if executable and worst_case_profit_pct < self.settings.safety_margin_pct:
            executable = False
            executable_reason = (
                f"Margen neto insuficiente frente al umbral de seguridad de "
                f"{self.settings.safety_margin_pct}%."
            )

        safety_buffer_pct = worst_case_profit_pct - self.settings.safety_margin_pct
        profitability_score = self._profitability_score(worst_case_profit_pct)
        liquidity_score = self._liquidity_score(baseline.slippage_pct)
        latency_score = self._latency_score(worst_case_latency_ms)
        confidence_score = self._confidence_score(executable, worst_case_profit_pct, safety_buffer_pct)
        ranking_score = self._ranking_score(profitability_score, liquidity_score, latency_score, confidence_score)
        classification = self._classify(worst_case_profit_pct)

        return OpportunitySnapshot(
            route=route.key,
            route_assets=list(route.assets),
            capital=capital,
            final_amount=baseline.final_amount,
            gross_profit=baseline.gross_profit,
            gross_profit_pct=baseline.gross_profit_pct,
            net_profit=baseline.gross_profit,
            net_profit_pct=baseline.gross_profit_pct,
            total_fee_amount=baseline.total_fee_amount,
            slippage_pct=baseline.slippage_pct,
            latency_adjusted_profit=worst_case_profit,
            latency_adjusted_profit_pct=worst_case_profit_pct,
            worst_case_profit=worst_case_profit,
            worst_case_profit_pct=worst_case_profit_pct,
            worst_case_latency_ms=worst_case_latency_ms,
            safety_margin_pct=self.settings.safety_margin_pct,
            safety_buffer_pct=safety_buffer_pct,
            profitability_score=profitability_score,
            liquidity_score=liquidity_score,
            latency_score=latency_score,
            confidence_score=confidence_score,
            ranking_score=ranking_score,
            executable=executable,
            executable_reason=executable_reason,
            latency_scenarios_ms=sorted(latency_views),
            detected_at=now,
            first_detected_at=now,
            last_detected_at=now,
            duration_ms=0,
            classification=classification,
            strategy="TRIANGULAR_ARBITRAGE",
            markets=markets_used,
            legs=baseline.legs,
        )

    def _run_route_simulation(
        self,
        route: RouteDefinition,
        capital: Decimal,
        markets: dict[str, MarketMetadata],
        books: dict[str, OrderBookState],
    ) -> RouteSimulation | None:
        current_amount = capital
        theoretical_amount = capital
        legs: list[SimulatedLeg] = []
        fee_total = Decimal("0")

        for from_asset, to_asset in pairwise(route.assets):
            leg = self._simulate_conversion(from_asset, to_asset, current_amount, markets, books)
            if leg is None or not leg.fully_filled:
                return None
            theoretical = self._theoretical_conversion(from_asset, to_asset, theoretical_amount, markets, books)
            if theoretical is None:
                return None
            theoretical_amount = theoretical
            current_amount = leg.output_amount
            fee_total += leg.fee_amount
            legs.append(leg)

        gross_profit = current_amount - capital
        gross_pct = self._pct(gross_profit, capital)
        theoretical_diff = theoretical_amount - current_amount
        slippage_pct = self._pct(theoretical_diff if theoretical_diff > 0 else Decimal("0"), capital)
        return RouteSimulation(
            final_amount=current_amount,
            gross_profit=gross_profit,
            gross_profit_pct=gross_pct,
            total_fee_amount=fee_total,
            slippage_pct=slippage_pct,
            legs=legs,
        )

    def _simulate_conversion(
        self,
        from_asset: str,
        to_asset: str,
        amount_in: Decimal,
        markets: dict[str, MarketMetadata],
        books: dict[str, OrderBookState],
    ) -> SimulatedLeg | None:
        direct_market = f"{from_asset}-{to_asset}"
        inverse_market = f"{to_asset}-{from_asset}"

        if direct_market in markets and direct_market in books:
            metadata = markets[direct_market]
            book = books[direct_market]
            return self._sell_base_for_quote(from_asset, to_asset, direct_market, amount_in, metadata, book)

        if inverse_market in markets and inverse_market in books:
            metadata = markets[inverse_market]
            book = books[inverse_market]
            return self._buy_base_with_quote(from_asset, to_asset, inverse_market, amount_in, metadata, book)

        return None

    def _sell_base_for_quote(
        self,
        from_asset: str,
        to_asset: str,
        market: str,
        amount_in: Decimal,
        metadata: MarketMetadata,
        book: OrderBookState,
    ) -> SimulatedLeg | None:
        amount_to_sell = self._quantize_amount(amount_in, metadata.quantity_decimals)
        if metadata.min_order_in_base is not None and amount_to_sell < metadata.min_order_in_base:
            return None

        remaining = amount_to_sell
        quote_out = Decimal("0")
        quote_used = Decimal("0")

        for level in book.bids:
            consumed = min(remaining, level.size)
            quote_piece = consumed * level.price
            quote_out += quote_piece
            quote_used += quote_piece
            remaining -= consumed
            if remaining <= EIGHTEEN_DP:
                remaining = Decimal("0")
                break

        if remaining > EIGHTEEN_DP or quote_out <= 0:
            return None
        if metadata.min_order_in_quote is not None and quote_out < metadata.min_order_in_quote:
            return None

        fee_amount = self._quantize_freeform(quote_out * self.settings.simulated_taker_fee)
        output_amount = self._quantize_freeform(quote_out - fee_amount)
        average_price = self._quantize_freeform(quote_used / amount_to_sell)

        return SimulatedLeg(
            from_asset=from_asset,
            to_asset=to_asset,
            market=market,
            side="sell",
            input_amount=amount_to_sell,
            output_amount=output_amount,
            fee_amount=fee_amount,
            average_price=average_price,
            liquidity_used_quote=quote_used,
            fully_filled=True,
        )

    def _buy_base_with_quote(
        self,
        from_asset: str,
        to_asset: str,
        market: str,
        amount_in: Decimal,
        metadata: MarketMetadata,
        book: OrderBookState,
    ) -> SimulatedLeg | None:
        if metadata.min_order_in_quote is not None and amount_in < metadata.min_order_in_quote:
            return None

        remaining_quote = self._quantize_freeform(amount_in)
        base_out = Decimal("0")
        quote_used = Decimal("0")

        for level in book.asks:
            level_quote_capacity = level.price * level.size
            quote_to_spend = min(remaining_quote, level_quote_capacity)
            if quote_to_spend <= 0:
                continue
            base_piece = quote_to_spend / level.price
            base_out += base_piece
            quote_used += quote_to_spend
            remaining_quote -= quote_to_spend
            if remaining_quote <= EIGHTEEN_DP:
                remaining_quote = Decimal("0")
                break

        base_out = self._quantize_amount(base_out, metadata.quantity_decimals)
        if remaining_quote > EIGHTEEN_DP or base_out <= 0:
            return None
        if metadata.min_order_in_base is not None and base_out < metadata.min_order_in_base:
            return None

        fee_amount = self._quantize_amount(base_out * self.settings.simulated_taker_fee, metadata.quantity_decimals)
        output_amount = self._quantize_amount(base_out - fee_amount, metadata.quantity_decimals)
        if output_amount <= 0:
            return None
        average_price = self._quantize_freeform(quote_used / base_out)

        return SimulatedLeg(
            from_asset=from_asset,
            to_asset=to_asset,
            market=market,
            side="buy",
            input_amount=self._quantize_freeform(amount_in),
            output_amount=output_amount,
            fee_amount=fee_amount,
            average_price=average_price,
            liquidity_used_quote=quote_used,
            fully_filled=True,
        )

    def _theoretical_conversion(
        self,
        from_asset: str,
        to_asset: str,
        amount_in: Decimal,
        markets: dict[str, MarketMetadata],
        books: dict[str, OrderBookState],
    ) -> Decimal | None:
        direct_market = f"{from_asset}-{to_asset}"
        inverse_market = f"{to_asset}-{from_asset}"

        if direct_market in markets and direct_market in books:
            best_bid = books[direct_market].best_bid
            if best_bid is None:
                return None
            gross = amount_in * best_bid.price
            return self._quantize_freeform(gross * (Decimal("1") - self.settings.simulated_taker_fee))

        if inverse_market in markets and inverse_market in books:
            best_ask = books[inverse_market].best_ask
            metadata = markets[inverse_market]
            if best_ask is None:
                return None
            base_out = self._quantize_amount(amount_in / best_ask.price, metadata.quantity_decimals)
            return self._quantize_amount(base_out * (Decimal("1") - self.settings.simulated_taker_fee), metadata.quantity_decimals)

        return None

    def _reconcile_active_opportunities(
        self,
        detected_now: dict[str, OpportunitySnapshot],
        now: datetime,
    ) -> None:
        previous_keys = set(self.active_opportunities)
        current_keys = set(detected_now)

        for key, current in detected_now.items():
            if key in self.active_opportunities:
                existing = self.active_opportunities[key]
                current.first_detected_at = existing.first_detected_at
                current.duration_ms = int((now - existing.first_detected_at).total_seconds() * 1000)
            else:
                self.daily_counters.detected += 1
                self.daily_counters.profitable += 1
                self.daily_counters.executable += 1 if current.executable else 0
                if current.executable:
                    self.daily_counters.simulated_profit += current.latency_adjusted_profit
            current.last_detected_at = now
            self.active_opportunities[key] = current

        expired_keys = previous_keys - current_keys
        for key in expired_keys:
            self.completed_opportunities.append(replace(self.active_opportunities[key]))
            self.active_opportunities.pop(key, None)

    def snapshot_for_persistence(self) -> list[OpportunitySnapshot]:
        snapshots = list(self.last_cycle_opportunities[:30])
        for item in self.completed_opportunities[:]:
            snapshots.append(item)
            self.completed_opportunities.remove(item)
            if len(snapshots) >= 60:
                break
        return snapshots

    def _roll_day_if_needed(self) -> None:
        today = datetime.now(timezone.utc).date()
        if today != self.daily_counters.day:
            self.daily_counters = DailyCounters(day=today)

    def _normalize_latency_views(
        self,
        books: dict[str, OrderBookState],
        scenario_books: dict[int, dict[str, OrderBookState]] | None,
    ) -> dict[int, dict[str, OrderBookState]]:
        views = {0: books}
        if scenario_books:
            for latency_ms, snapshot in scenario_books.items():
                views[max(0, int(latency_ms))] = snapshot
        for latency_ms in self.settings.latency_scenarios_ms:
            views.setdefault(max(0, int(latency_ms)), books)
        return dict(sorted(views.items()))

    def _profitability_score(self, worst_case_profit_pct: Decimal) -> Decimal:
        threshold = max(self.settings.safety_margin_pct * Decimal("4"), Decimal("0.10"))
        return self._clamp_score((worst_case_profit_pct / threshold) * Decimal("100"))

    def _liquidity_score(self, slippage_pct: Decimal) -> Decimal:
        if slippage_pct <= 0:
            return Decimal("100.00")
        penalty = (slippage_pct / Decimal("0.25")) * Decimal("100")
        return self._clamp_score(Decimal("100") - penalty)

    def _latency_score(self, worst_case_latency_ms: int) -> Decimal:
        primary = max(self.settings.primary_latency_ms, 1)
        penalty = (Decimal(worst_case_latency_ms) / Decimal(primary)) * Decimal("35")
        return self._clamp_score(Decimal("100") - penalty)

    def _confidence_score(
        self,
        executable: bool,
        worst_case_profit_pct: Decimal,
        safety_buffer_pct: Decimal,
    ) -> Decimal:
        base = Decimal("75") if executable else Decimal("40")
        if safety_buffer_pct > 0:
            base += min(Decimal("20"), safety_buffer_pct * Decimal("40"))
        if worst_case_profit_pct <= 0:
            base -= Decimal("25")
        return self._clamp_score(base)

    def _ranking_score(
        self,
        profitability_score: Decimal,
        liquidity_score: Decimal,
        latency_score: Decimal,
        confidence_score: Decimal,
    ) -> Decimal:
        value = (
            profitability_score * Decimal("0.35")
            + liquidity_score * Decimal("0.20")
            + latency_score * Decimal("0.20")
            + confidence_score * Decimal("0.25")
        )
        return value.quantize(Decimal("0.01"))

    @staticmethod
    def _clamp_score(value: Decimal) -> Decimal:
        return min(Decimal("100.00"), max(Decimal("0"), value)).quantize(Decimal("0.01"))

    @staticmethod
    def _pct(value: Decimal, base: Decimal) -> Decimal:
        if base == 0:
            return Decimal("0")
        return (value / base) * Decimal("100")

    @staticmethod
    def _quantize_freeform(value: Decimal) -> Decimal:
        return value.quantize(EIGHTEEN_DP, rounding=ROUND_DOWN)

    @staticmethod
    def _quantize_amount(value: Decimal, decimals: int | None) -> Decimal:
        if decimals is None:
            return value.quantize(EIGHTEEN_DP, rounding=ROUND_DOWN)
        places = Decimal("1").scaleb(-decimals)
        with localcontext() as ctx:
            ctx.rounding = ROUND_DOWN
            return value.quantize(places)

    @staticmethod
    def _classify(net_pct: Decimal) -> str:
        if net_pct < Decimal("0"):
            return "NEGATIVA"
        if net_pct < Decimal("0.10"):
            return "INSIGNIFICANTE"
        if net_pct < Decimal("0.25"):
            return "INTERESANTE"
        if net_pct < Decimal("0.50"):
            return "MUY_INTERESANTE"
        return "EXCEPCIONAL"
