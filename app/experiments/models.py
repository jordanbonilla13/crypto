from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal


@dataclass(slots=True)
class SimulatedLeg:
    from_asset: str
    to_asset: str
    market: str
    side: str
    input_amount: Decimal
    output_amount: Decimal
    fee_amount: Decimal
    average_price: Decimal
    liquidity_used_quote: Decimal
    fully_filled: bool


@dataclass(slots=True)
class RouteDefinition:
    assets: tuple[str, str, str, str]

    @property
    def key(self) -> str:
        return " -> ".join(self.assets)


@dataclass(slots=True)
class OpportunitySnapshot:
    route: str
    route_assets: list[str]
    capital: Decimal
    final_amount: Decimal
    gross_profit: Decimal
    gross_profit_pct: Decimal
    net_profit: Decimal
    net_profit_pct: Decimal
    total_fee_amount: Decimal
    slippage_pct: Decimal
    latency_adjusted_profit: Decimal
    latency_adjusted_profit_pct: Decimal
    worst_case_profit: Decimal
    worst_case_profit_pct: Decimal
    worst_case_latency_ms: int
    safety_margin_pct: Decimal
    safety_buffer_pct: Decimal
    profitability_score: Decimal
    liquidity_score: Decimal
    latency_score: Decimal
    confidence_score: Decimal
    ranking_score: Decimal
    executable: bool
    executable_reason: str
    latency_scenarios_ms: list[int]
    detected_at: datetime
    first_detected_at: datetime
    last_detected_at: datetime
    duration_ms: int
    classification: str
    strategy: str
    markets: list[str]
    legs: list[SimulatedLeg] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.route}|{self.capital}"


@dataclass(slots=True)
class DailyCounters:
    day: date
    detected: int = 0
    profitable: int = 0
    executable: int = 0
    simulated_profit: Decimal = Decimal("0")
