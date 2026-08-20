from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from math import sqrt
from statistics import median

from app.collector.models import MarketMetadata, OrderBookLevel, OrderBookState
from app.common.settings import Settings


PRICE_HORIZONS_MS = (100, 250, 500, 1000, 2000, 5000, 10000, 30000)
SIGNAL_LABELS = {
    "IMBALANCE_STRONG": "Desequilibrio fuerte",
    "LIQUIDITY_VANISH": "Desaparicion brusca de liquidez",
    "SPREAD_WIDENING": "Ampliacion anormal del spread",
    "JUMP_HIGH_VOLUME": "Movimiento brusco con mucho volumen",
    "JUMP_LOW_VOLUME": "Movimiento brusco con poco volumen",
    "JUMP_REVERSION": "Reversion despues de un salto",
    "JUMP_CONTINUATION": "Continuacion despues de un salto",
    "LARGE_ORDER_CLUSTER": "Concentracion de ordenes grandes",
    "DEPTH_SHIFT": "Cambios repentinos de profundidad",
}
MIN_TOTAL_SIGNALS = 2000
MIN_VALIDATION_SIGNALS = 500
MIN_PATTERN_CASES = 100
COOLDOWN_MS = 1500


@dataclass(slots=True)
class MicrostructureOutcome:
    horizon_ms: int
    direction: str
    move_pct: Decimal
    spread_pct: Decimal
    depth_quote: Decimal
    liquidity_level: str
    volatility_pct: Decimal
    volume_quote: Decimal


@dataclass(slots=True)
class MicrostructureSignal:
    signal_key: str
    detected_at: datetime
    market: str
    base: str
    quote: str
    signal_type: str
    signal_label: str
    predicted_direction: str
    score: Decimal
    spread_pct: Decimal
    depth_quote: Decimal
    liquidity_level: str
    volatility_pct: Decimal
    volatility_bucket: str
    volume_quote: Decimal
    imbalance_ratio: Decimal
    imbalance_pct: Decimal
    depth_change_pct: Decimal
    price_jump_pct: Decimal
    large_order_share_pct: Decimal
    spread_bucket: str
    signal_strength: str
    hour_of_day: int
    outcomes: list[MicrostructureOutcome] = field(default_factory=list)
    details: dict = field(default_factory=dict)


@dataclass(slots=True)
class MicrostructureSummary:
    total_signals: int
    sample_size: int
    state: str
    promising_signal: str | None
    best_horizon_ms: int | None
    worst_horizon_ms: int | None
    movement_mean_pct: Decimal
    movement_median_pct: Decimal
    hit_rate_pct: Decimal
    by_type: list[dict]
    by_market: list[dict]
    by_direction: list[dict]
    by_horizon: list[dict]
    candidates: list[dict]
    ready_for_evaluation: bool


class MicrostructureResearchEngine:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.last_signals: list[MicrostructureSignal] = []

    def analyze_from_histories(
        self,
        markets: dict[str, MarketMetadata],
        histories: dict[str, list[OrderBookState]],
    ) -> list[MicrostructureSignal]:
        emitted: list[MicrostructureSignal] = []
        required_future = PRICE_HORIZONS_MS[-1]
        cooldown_tracker: dict[tuple[str, str], datetime] = {}
        for market, history in histories.items():
            metadata = markets.get(market)
            if metadata is None or len(history) < 8:
                continue
            ordered = [item for item in history if item.updated_at and item.best_bid and item.best_ask]
            if len(ordered) < 8:
                continue
            for index, snapshot in enumerate(ordered):
                if not self._has_future_snapshot(ordered, index, required_future):
                    continue
                context = self._build_context(ordered, index)
                if context is None:
                    continue
                for signal in self._detect_signals(metadata, ordered, index, context):
                    last_seen = cooldown_tracker.get((signal.market, signal.signal_type))
                    if last_seen is not None and (signal.detected_at - last_seen).total_seconds() * 1000 < COOLDOWN_MS:
                        continue
                    cooldown_tracker[(signal.market, signal.signal_type)] = signal.detected_at
                    emitted.append(signal)
        emitted.sort(key=lambda item: (item.detected_at, item.market, item.signal_type), reverse=True)
        self.last_signals = emitted[: self.settings.microstructure_signal_limit]
        return emitted

    def build_summary(self, rows: list[dict], partition: str | None = None) -> MicrostructureSummary:
        selected = [row for row in rows if partition is None or row["partition"] == partition]
        total = len(selected)
        validation_count = sum(1 for row in selected if row["partition"] == "VALIDATION")
        if not selected:
            return MicrostructureSummary(0, 0, "RECOLECTANDO", None, None, None, Decimal("0"), Decimal("0"), Decimal("0"), [], [], [], [], [], False)

        by_type = self._group_rows(selected, "signal_type")
        by_market = self._group_rows(selected, "market")
        by_direction = self._group_rows(selected, "predicted_direction")
        by_horizon = self._build_horizon_rows(selected)
        movement_samples = [Decimal(str(item["move_pct"])) for row in selected for item in row.get("outcomes", [])]
        mean_move = sum(movement_samples, start=Decimal("0")) / Decimal(len(movement_samples)) if movement_samples else Decimal("0")
        median_move = Decimal(str(median([float(item) for item in movement_samples]))) if movement_samples else Decimal("0")
        avg_hit = (
            sum((Decimal(str(item["hit_rate_pct"])) for item in by_horizon), start=Decimal("0")) / Decimal(len(by_horizon))
            if by_horizon
            else Decimal("0")
        )
        best_horizon = max(by_horizon, key=lambda item: (item["hit_rate_pct"], item["movement_mean_pct"]), default=None)
        worst_horizon = min(by_horizon, key=lambda item: (item["hit_rate_pct"], item["movement_mean_pct"]), default=None)
        candidates = self._build_candidates(by_type)
        return MicrostructureSummary(
            total_signals=total,
            sample_size=total,
            state=self._evaluate_state(total, validation_count, candidates),
            promising_signal=candidates[0]["label"] if candidates else (by_type[0]["label"] if by_type else None),
            best_horizon_ms=best_horizon["horizon_ms"] if best_horizon else None,
            worst_horizon_ms=worst_horizon["horizon_ms"] if worst_horizon else None,
            movement_mean_pct=mean_move.quantize(Decimal("0.0001")),
            movement_median_pct=median_move.quantize(Decimal("0.0001")),
            hit_rate_pct=avg_hit.quantize(Decimal("0.01")),
            by_type=by_type,
            by_market=by_market,
            by_direction=by_direction,
            by_horizon=by_horizon,
            candidates=candidates,
            ready_for_evaluation=total >= MIN_TOTAL_SIGNALS and validation_count >= MIN_VALIDATION_SIGNALS,
        )

    def build_pattern_analysis(self, rows: list[dict]) -> list[dict]:
        grouped: dict[str, list[dict]] = {}
        for row in rows:
            grouped.setdefault(row["signal_type"], []).append(row)
        analyses = []
        for signal_type, items in grouped.items():
            analyses.append(self._pattern_analysis_row(signal_type, items))
        analyses.sort(key=lambda item: (self._state_rank(item["state"]), item["validation"]["count"], item["count"]), reverse=True)
        return analyses

    def _detect_signals(
        self,
        metadata: MarketMetadata,
        history: list[OrderBookState],
        index: int,
        context: dict,
    ) -> list[MicrostructureSignal]:
        snapshot = history[index]
        signal_candidates = []

        if context["imbalance_ratio"] >= self.settings.microstructure_imbalance_ratio_threshold and context["depth_quote"] >= self.settings.microstructure_min_depth_quote:
            signal_candidates.append(("IMBALANCE_STRONG", self._score_from_components(context["imbalance_ratio"] / Decimal("5"), context["depth_quote"] / Decimal("10000"), context["spread_score"])))

        liquidity_vanish_pct = min(context["bid_depth_drop_pct"], context["ask_depth_drop_pct"])
        if abs(liquidity_vanish_pct) >= self.settings.microstructure_liquidity_vanish_threshold_pct:
            direction = "DOWN" if context["bid_depth_drop_pct"] <= context["ask_depth_drop_pct"] else "UP"
            signal_candidates.append(("LIQUIDITY_VANISH", self._score_from_components(abs(liquidity_vanish_pct) / Decimal("100"), context["volume_quote"] / Decimal("5000"), Decimal("0.6"), predicted_direction=direction)))

        if abs(context["spread_vs_avg_pct"]) >= Decimal("150") and context["spread_pct"] >= Decimal("0.10"):
            signal_candidates.append(("SPREAD_WIDENING", self._score_from_components(abs(context["spread_vs_avg_pct"]) / Decimal("250"), context["volatility_pct"] / Decimal("0.25"), context["imbalance_ratio"] / Decimal("4"), predicted_direction=context["pressure_direction"])))

        if abs(context["price_jump_pct"]) >= self.settings.microstructure_jump_threshold_pct:
            if context["volume_quote"] >= Decimal("8000"):
                signal_candidates.append(("JUMP_HIGH_VOLUME", self._score_from_components(abs(context["price_jump_pct"]) / Decimal("0.35"), context["volume_quote"] / Decimal("15000"), context["imbalance_ratio"] / Decimal("4"), predicted_direction=context["jump_direction"])))
            if context["volume_quote"] <= Decimal("2500"):
                reverse_direction = "DOWN" if context["jump_direction"] == "UP" else "UP"
                signal_candidates.append(("JUMP_LOW_VOLUME", self._score_from_components(abs(context["price_jump_pct"]) / Decimal("0.35"), Decimal("1") - min(context["volume_quote"] / Decimal("2500"), Decimal("1")), context["spread_score"], predicted_direction=reverse_direction)))
            if abs(context["recent_partial_reversal_pct"]) >= Decimal("0.05"):
                reverse_direction = "DOWN" if context["jump_direction"] == "UP" else "UP"
                signal_candidates.append(("JUMP_REVERSION", self._score_from_components(abs(context["recent_partial_reversal_pct"]) / Decimal("0.15"), context["spread_score"], context["volatility_pct"] / Decimal("0.2"), predicted_direction=reverse_direction)))
            else:
                signal_candidates.append(("JUMP_CONTINUATION", self._score_from_components(abs(context["price_jump_pct"]) / Decimal("0.35"), context["volume_quote"] / Decimal("12000"), context["imbalance_ratio"] / Decimal("4"), predicted_direction=context["jump_direction"])))

        if context["large_order_share_pct"] >= self.settings.microstructure_large_order_share_threshold_pct:
            signal_candidates.append(("LARGE_ORDER_CLUSTER", self._score_from_components(context["large_order_share_pct"] / Decimal("100"), context["depth_quote"] / Decimal("15000"), context["imbalance_ratio"] / Decimal("4"), predicted_direction=context["pressure_direction"])))

        if abs(context["depth_change_pct"]) >= self.settings.microstructure_depth_change_threshold_pct:
            signal_candidates.append(("DEPTH_SHIFT", self._score_from_components(abs(context["depth_change_pct"]) / Decimal("100"), context["volume_quote"] / Decimal("7000"), context["imbalance_ratio"] / Decimal("4"), predicted_direction=context["pressure_direction"])))

        signals: list[MicrostructureSignal] = []
        for signal_type, payload in signal_candidates:
            outcomes = self._build_outcomes(history, index, snapshot)
            if not outcomes:
                continue
            detected_at = snapshot.updated_at
            assert detected_at is not None
            signals.append(
                MicrostructureSignal(
                    signal_key=f"{signal_type}|{metadata.market}|{detected_at.isoformat()}",
                    detected_at=detected_at,
                    market=metadata.market,
                    base=metadata.base,
                    quote=metadata.quote,
                    signal_type=signal_type,
                    signal_label=SIGNAL_LABELS[signal_type],
                    predicted_direction=payload["predicted_direction"],
                    score=payload["score"],
                    spread_pct=context["spread_pct"],
                    depth_quote=context["depth_quote"],
                    liquidity_level=context["liquidity_level"],
                    volatility_pct=context["volatility_pct"],
                    volatility_bucket=context["volatility_bucket"],
                    volume_quote=context["volume_quote"],
                    imbalance_ratio=context["imbalance_ratio"],
                    imbalance_pct=context["imbalance_pct"],
                    depth_change_pct=context["depth_change_pct"],
                    price_jump_pct=context["price_jump_pct"],
                    large_order_share_pct=context["large_order_share_pct"],
                    spread_bucket=context["spread_bucket"],
                    signal_strength=context["signal_strength"],
                    hour_of_day=detected_at.hour,
                    outcomes=outcomes,
                    details={
                        "mid_price": str(context["mid"]),
                        "pressure_direction": context["pressure_direction"],
                        "jump_direction": context["jump_direction"],
                        "bid_depth_quote": str(context["bid_depth_quote"]),
                        "ask_depth_quote": str(context["ask_depth_quote"]),
                        "spread_vs_avg_pct": str(context["spread_vs_avg_pct"]),
                        "recent_partial_reversal_pct": str(context["recent_partial_reversal_pct"]),
                    },
                )
            )
        return signals

    def _build_context(self, history: list[OrderBookState], index: int) -> dict | None:
        current = history[index]
        current_time = current.updated_at
        if current_time is None or current.best_bid is None or current.best_ask is None:
            return None
        baseline = self._nearest_before(history, current_time - timedelta(seconds=1), index)
        recent_window = [item for item in history[max(0, index - 20) : index + 1] if item.updated_at and item.best_bid and item.best_ask]
        if baseline is None or len(recent_window) < 3:
            return None
        mid = self._mid(current)
        baseline_mid = self._mid(baseline)
        bid_depth_quote = self._depth_quote(current.bids)
        ask_depth_quote = self._depth_quote(current.asks)
        depth_quote = bid_depth_quote + ask_depth_quote
        baseline_bid_depth = self._depth_quote(baseline.bids)
        baseline_ask_depth = self._depth_quote(baseline.asks)
        baseline_depth = baseline_bid_depth + baseline_ask_depth
        spread_pct = self._spread_pct(current)
        recent_spreads = [self._spread_pct(item) for item in recent_window]
        avg_spread = sum(recent_spreads, start=Decimal("0")) / Decimal(len(recent_spreads))
        mids = [self._mid(item) for item in recent_window]
        volatility_pct = ((max(mids) - min(mids)) / mid * Decimal("100")) if mid else Decimal("0")
        volume_quote = abs(bid_depth_quote - baseline_bid_depth) + abs(ask_depth_quote - baseline_ask_depth)
        imbalance_pct = ((bid_depth_quote - ask_depth_quote) / depth_quote * Decimal("100")) if depth_quote else Decimal("0")
        small_side = min(bid_depth_quote, ask_depth_quote) or Decimal("1")
        imbalance_ratio = max(bid_depth_quote, ask_depth_quote) / small_side
        depth_change_pct = ((depth_quote - baseline_depth) / baseline_depth * Decimal("100")) if baseline_depth else Decimal("0")
        bid_depth_drop_pct = ((bid_depth_quote - baseline_bid_depth) / baseline_bid_depth * Decimal("100")) if baseline_bid_depth else Decimal("0")
        ask_depth_drop_pct = ((ask_depth_quote - baseline_ask_depth) / baseline_ask_depth * Decimal("100")) if baseline_ask_depth else Decimal("0")
        price_jump_pct = ((mid - baseline_mid) / baseline_mid * Decimal("100")) if baseline_mid else Decimal("0")
        jump_direction = "UP" if price_jump_pct >= 0 else "DOWN"
        pressure_direction = "UP" if imbalance_pct >= 0 else "DOWN"
        prior_500 = self._nearest_before(history, current_time - timedelta(milliseconds=500), index)
        recent_partial_reversal_pct = Decimal("0")
        if prior_500 is not None:
            prior_mid = self._mid(prior_500)
            recent_partial_reversal_pct = ((mid - prior_mid) / prior_mid * Decimal("100")) if prior_mid else Decimal("0")
        return {
            "mid": mid,
            "spread_pct": spread_pct,
            "bid_depth_quote": bid_depth_quote,
            "ask_depth_quote": ask_depth_quote,
            "depth_quote": depth_quote,
            "liquidity_level": self._liquidity_level(depth_quote),
            "volatility_pct": volatility_pct,
            "volatility_bucket": self._volatility_bucket(volatility_pct),
            "volume_quote": volume_quote,
            "imbalance_pct": imbalance_pct,
            "imbalance_ratio": imbalance_ratio,
            "depth_change_pct": depth_change_pct,
            "bid_depth_drop_pct": bid_depth_drop_pct,
            "ask_depth_drop_pct": ask_depth_drop_pct,
            "price_jump_pct": price_jump_pct,
            "spread_vs_avg_pct": ((spread_pct - avg_spread) / avg_spread * Decimal("100")) if avg_spread else Decimal("0"),
            "large_order_share_pct": self._large_order_share_pct(current),
            "pressure_direction": pressure_direction,
            "jump_direction": jump_direction,
            "recent_partial_reversal_pct": recent_partial_reversal_pct,
            "spread_score": max(Decimal("0.2"), Decimal("1") - min(spread_pct / Decimal("1"), Decimal("1"))),
            "spread_bucket": self._spread_bucket(spread_pct),
            "signal_strength": self._signal_strength(max(abs(imbalance_ratio), abs(depth_change_pct), abs(price_jump_pct) * Decimal("25"))),
        }

    def _build_outcomes(self, history: list[OrderBookState], index: int, snapshot: OrderBookState) -> list[MicrostructureOutcome]:
        outcomes = []
        base_mid = self._mid(snapshot)
        current_time = snapshot.updated_at
        if current_time is None:
            return outcomes
        for horizon in PRICE_HORIZONS_MS:
            future = self._nearest_after(history, current_time + timedelta(milliseconds=horizon), index + 1)
            if future is None or future.best_bid is None or future.best_ask is None:
                continue
            future_mid = self._mid(future)
            move_pct = ((future_mid - base_mid) / base_mid * Decimal("100")) if base_mid else Decimal("0")
            future_depth = self._depth_quote(future.bids) + self._depth_quote(future.asks)
            outcomes.append(
                MicrostructureOutcome(
                    horizon_ms=horizon,
                    direction="UP" if move_pct > 0 else "DOWN" if move_pct < 0 else "FLAT",
                    move_pct=move_pct.quantize(Decimal("0.0001")),
                    spread_pct=self._spread_pct(future).quantize(Decimal("0.0001")),
                    depth_quote=future_depth.quantize(Decimal("0.0001")),
                    liquidity_level=self._liquidity_level(future_depth),
                    volatility_pct=Decimal("0"),
                    volume_quote=(abs(self._depth_quote(future.bids) - self._depth_quote(snapshot.bids)) + abs(self._depth_quote(future.asks) - self._depth_quote(snapshot.asks))).quantize(Decimal("0.0001")),
                )
            )
        return outcomes

    def _build_horizon_rows(self, rows: list[dict]) -> list[dict]:
        grouped: dict[int, list[dict]] = {}
        for row in rows:
            for outcome in row.get("outcomes", []):
                grouped.setdefault(outcome["horizon_ms"], []).append({"row": row, "outcome": outcome})
        output = []
        for horizon, items in sorted(grouped.items()):
            moves = [Decimal(str(item["outcome"]["move_pct"])) for item in items]
            hits = [1 if self._matches_direction(item["row"]["predicted_direction"], Decimal(str(item["outcome"]["move_pct"]))) else 0 for item in items]
            output.append(
                {
                    "horizon_ms": horizon,
                    "count": len(items),
                    "movement_mean_pct": float(self._mean(moves)),
                    "movement_median_pct": float(self._median_decimal(moves)),
                    "hit_rate_pct": float((Decimal(sum(hits)) / Decimal(len(hits)) * Decimal("100")).quantize(Decimal("0.01"))) if hits else 0.0,
                }
            )
        return output

    def _group_rows(self, rows: list[dict], field: str) -> list[dict]:
        grouped: dict[str, list[dict]] = {}
        for row in rows:
            grouped.setdefault(str(row[field]), []).append(row)
        output = []
        for label, items in grouped.items():
            discovery = [row for row in items if row["partition"] == "DISCOVERY"]
            validation = [row for row in items if row["partition"] == "VALIDATION"]
            overall_stats = self._evaluate_rows(items)
            discovery_stats = self._evaluate_rows(discovery)
            validation_stats = self._evaluate_rows(validation)
            output.append(
                {
                    "label": label,
                    "count": len(items),
                    "movement_mean_pct": float(overall_stats["movement_mean_pct"]),
                    "movement_median_pct": float(overall_stats["movement_median_pct"]),
                    "hit_rate_pct": float(overall_stats["hit_rate_pct"]),
                    "validation_score": len(validation),
                    "discovery_count": len(discovery),
                    "validation_count": len(validation),
                    "discovery_hit_rate_pct": float(discovery_stats["hit_rate_pct"]),
                    "validation_hit_rate_pct": float(validation_stats["hit_rate_pct"]),
                    "stability_gap_pct": float(abs(discovery_stats["hit_rate_pct"] - validation_stats["hit_rate_pct"]).quantize(Decimal("0.01"))),
                    "best_horizon_ms": overall_stats["best_horizon_ms"],
                    "worst_horizon_ms": overall_stats["worst_horizon_ms"],
                    "state": self._pattern_state(len(items), len(validation), discovery_stats["hit_rate_pct"], validation_stats["hit_rate_pct"]),
                }
            )
        output.sort(key=lambda item: (self._state_rank(item["state"]), item["validation_count"], item["count"], item["hit_rate_pct"]), reverse=True)
        return output[:20]

    def _pattern_analysis_row(self, signal_type: str, rows: list[dict]) -> dict:
        discovery = [row for row in rows if row["partition"] == "DISCOVERY"]
        validation = [row for row in rows if row["partition"] == "VALIDATION"]
        overall = self._evaluate_rows(rows)
        discovery_stats = self._evaluate_rows(discovery)
        validation_stats = self._evaluate_rows(validation)
        return {
            "signal_type": signal_type,
            "label": SIGNAL_LABELS.get(signal_type, signal_type),
            "count": len(rows),
            "favorable_pct": float(overall["hit_rate_pct"]),
            "movement_mean_pct": float(overall["movement_mean_pct"]),
            "movement_median_pct": float(overall["movement_median_pct"]),
            "best_horizon_ms": overall["best_horizon_ms"],
            "worst_horizon_ms": overall["worst_horizon_ms"],
            "deviation_pct": float(overall["deviation_pct"]),
            "percentiles": {key: float(value) for key, value in overall["percentiles"].items()},
            "discovery": self._stats_payload(discovery_stats, len(discovery)),
            "validation": self._stats_payload(validation_stats, len(validation)),
            "difference": {
                "hit_rate_pct": float((validation_stats["hit_rate_pct"] - discovery_stats["hit_rate_pct"]).quantize(Decimal("0.01"))),
                "movement_mean_pct": float((validation_stats["movement_mean_pct"] - discovery_stats["movement_mean_pct"]).quantize(Decimal("0.0001"))),
            },
            "segments": {
                "market": self._group_rows(rows, "market")[:8],
                "hour": self._group_rows(rows, "hour_of_day")[:8],
                "liquidity": self._group_rows(rows, "liquidity_level"),
                "volatility": self._group_rows(rows, "volatility_bucket"),
                "spread": self._group_rows(rows, "spread_bucket"),
                "strength": self._group_rows(rows, "signal_strength"),
            },
            "state": self._pattern_state(len(rows), len(validation), discovery_stats["hit_rate_pct"], validation_stats["hit_rate_pct"]),
        }

    def _evaluate_rows(self, rows: list[dict]) -> dict:
        if not rows:
            return {
                "hit_rate_pct": Decimal("0"),
                "movement_mean_pct": Decimal("0"),
                "movement_median_pct": Decimal("0"),
                "best_horizon_ms": None,
                "worst_horizon_ms": None,
                "deviation_pct": Decimal("0"),
                "percentiles": {"p10": Decimal("0"), "p25": Decimal("0"), "p75": Decimal("0"), "p90": Decimal("0")},
            }
        by_horizon = self._build_horizon_rows(rows)
        best_horizon = max(by_horizon, key=lambda item: (item["hit_rate_pct"], item["movement_mean_pct"]), default=None)
        worst_horizon = min(by_horizon, key=lambda item: (item["hit_rate_pct"], item["movement_mean_pct"]), default=None)
        horizon = best_horizon["horizon_ms"] if best_horizon else 500
        selected = []
        hits = []
        for row in rows:
            outcome = next((item for item in row.get("outcomes", []) if item["horizon_ms"] == horizon), row.get("outcomes", [None])[0])
            if outcome is None:
                continue
            move = Decimal(str(outcome["move_pct"]))
            selected.append(move)
            hits.append(1 if self._matches_direction(row["predicted_direction"], move) else 0)
        if not selected:
            selected = [Decimal("0")]
            hits = [0]
        return {
            "hit_rate_pct": (Decimal(sum(hits)) / Decimal(len(hits)) * Decimal("100")).quantize(Decimal("0.01")),
            "movement_mean_pct": self._mean(selected),
            "movement_median_pct": self._median_decimal(selected),
            "best_horizon_ms": best_horizon["horizon_ms"] if best_horizon else None,
            "worst_horizon_ms": worst_horizon["horizon_ms"] if worst_horizon else None,
            "deviation_pct": self._stddev(selected),
            "percentiles": self._percentiles(selected),
        }

    def _build_candidates(self, by_type: list[dict]) -> list[dict]:
        candidates = []
        for item in by_type:
            if item["count"] < MIN_PATTERN_CASES or item["validation_count"] < MIN_PATTERN_CASES:
                continue
            if item["discovery_hit_rate_pct"] < 55 or item["validation_hit_rate_pct"] < 55:
                continue
            if item["stability_gap_pct"] > 8:
                continue
            state = "PROMETEDORA" if item["validation_count"] >= MIN_VALIDATION_SIGNALS and item["stability_gap_pct"] <= 5 else "CANDIDATA"
            candidates.append({**item, "state": state})
        candidates.sort(key=lambda item: (self._state_rank(item["state"]), item["validation_count"], item["hit_rate_pct"]), reverse=True)
        return candidates[:12]

    @staticmethod
    def _stats_payload(stats: dict, count: int) -> dict:
        return {
            "count": count,
            "favorable_pct": float(stats["hit_rate_pct"]),
            "movement_mean_pct": float(stats["movement_mean_pct"]),
            "movement_median_pct": float(stats["movement_median_pct"]),
            "best_horizon_ms": stats["best_horizon_ms"],
            "worst_horizon_ms": stats["worst_horizon_ms"],
            "deviation_pct": float(stats["deviation_pct"]),
            "percentiles": {key: float(value) for key, value in stats["percentiles"].items()},
        }

    @staticmethod
    def _evaluate_state(total: int, validation_count: int, candidates: list[dict]) -> str:
        if validation_count == 0:
            return "SIN_VALIDACION"
        if total < MIN_TOTAL_SIGNALS or validation_count < MIN_VALIDATION_SIGNALS:
            return "RECOLECTANDO"
        if candidates:
            return "PROMETEDORA" if any(item["state"] == "PROMETEDORA" for item in candidates) else "CANDIDATA"
        return "SIN_EVIDENCIA"

    @staticmethod
    def _pattern_state(total_count: int, validation_count: int, discovery_hit_rate: Decimal, validation_hit_rate: Decimal) -> str:
        if validation_count == 0:
            return "RECOLECTANDO"
        if total_count < MIN_PATTERN_CASES or validation_count < MIN_PATTERN_CASES:
            return "SIN_EVIDENCIA"
        gap = abs(discovery_hit_rate - validation_hit_rate)
        if discovery_hit_rate >= Decimal("58") and validation_hit_rate >= Decimal("58") and gap <= Decimal("5"):
            return "PROMETEDORA"
        if discovery_hit_rate >= Decimal("55") and validation_hit_rate >= Decimal("55") and gap <= Decimal("8"):
            return "CANDIDATA"
        if validation_hit_rate < Decimal("48"):
            return "DESCARTADA"
        return "SIN_EVIDENCIA"

    @staticmethod
    def _state_rank(state: str) -> int:
        return {
            "PROMETEDORA": 5,
            "CANDIDATA": 4,
            "RECOLECTANDO": 3,
            "SIN_VALIDACION": 2,
            "SIN_EVIDENCIA": 1,
            "DESCARTADA": 0,
        }.get(state, 0)

    @staticmethod
    def _has_future_snapshot(history: list[OrderBookState], index: int, horizon_ms: int) -> bool:
        current = history[index]
        if current.updated_at is None:
            return False
        limit = current.updated_at + timedelta(milliseconds=horizon_ms)
        return any(item.updated_at and item.updated_at >= limit for item in history[index + 1 :])

    @staticmethod
    def _nearest_before(history: list[OrderBookState], target: datetime, limit_index: int) -> OrderBookState | None:
        for item in reversed(history[:limit_index]):
            if item.updated_at and item.updated_at <= target:
                return item
        return history[limit_index - 1] if limit_index > 0 else None

    @staticmethod
    def _nearest_after(history: list[OrderBookState], target: datetime, start_index: int) -> OrderBookState | None:
        for item in history[start_index:]:
            if item.updated_at and item.updated_at >= target:
                return item
        return None

    @staticmethod
    def _depth_quote(levels: list[OrderBookLevel]) -> Decimal:
        return sum((level.price * level.size for level in levels[:5]), start=Decimal("0"))

    @staticmethod
    def _mid(book: OrderBookState) -> Decimal:
        if book.best_bid is None or book.best_ask is None:
            return Decimal("0")
        return (book.best_bid.price + book.best_ask.price) / Decimal("2")

    @staticmethod
    def _spread_pct(book: OrderBookState) -> Decimal:
        if book.best_bid is None or book.best_ask is None:
            return Decimal("0")
        mid = (book.best_bid.price + book.best_ask.price) / Decimal("2")
        return ((book.best_ask.price - book.best_bid.price) / mid * Decimal("100")) if mid else Decimal("0")

    @staticmethod
    def _liquidity_level(depth_quote: Decimal) -> str:
        if depth_quote >= Decimal("50000"):
            return "ALTA"
        if depth_quote >= Decimal("15000"):
            return "MEDIA"
        return "BAJA"

    @staticmethod
    def _volatility_bucket(volatility_pct: Decimal) -> str:
        if volatility_pct >= Decimal("0.20"):
            return "ALTA"
        if volatility_pct >= Decimal("0.07"):
            return "MEDIA"
        return "BAJA"

    @staticmethod
    def _spread_bucket(spread_pct: Decimal) -> str:
        if spread_pct >= Decimal("0.18"):
            return "AMPLIO"
        if spread_pct >= Decimal("0.05"):
            return "NORMAL"
        return "ESTRECHO"

    @staticmethod
    def _signal_strength(value: Decimal) -> str:
        if value >= Decimal("60"):
            return "ALTA"
        if value >= Decimal("20"):
            return "MEDIA"
        return "BAJA"

    @staticmethod
    def _large_order_share_pct(book: OrderBookState) -> Decimal:
        bid_total = sum((level.price * level.size for level in book.bids[:5]), start=Decimal("0"))
        ask_total = sum((level.price * level.size for level in book.asks[:5]), start=Decimal("0"))
        top_bid = (book.bids[0].price * book.bids[0].size) if book.bids else Decimal("0")
        top_ask = (book.asks[0].price * book.asks[0].size) if book.asks else Decimal("0")
        total = max(bid_total, ask_total, Decimal("1"))
        return (max(top_bid, top_ask) / total * Decimal("100")).quantize(Decimal("0.01"))

    @staticmethod
    def _score_from_components(first: Decimal, second: Decimal, third: Decimal, predicted_direction: str = "UP") -> dict:
        score = min((first + second + third) / Decimal("3") * Decimal("100"), Decimal("99.9"))
        return {"score": score.quantize(Decimal("0.01")), "predicted_direction": predicted_direction}

    @staticmethod
    def _matches_direction(predicted_direction: str, move_pct: Decimal) -> bool:
        if predicted_direction == "UP":
            return move_pct > 0
        if predicted_direction == "DOWN":
            return move_pct < 0
        return move_pct == 0

    @staticmethod
    def _mean(values: list[Decimal]) -> Decimal:
        return (sum(values, start=Decimal("0")) / Decimal(len(values))).quantize(Decimal("0.0001")) if values else Decimal("0")

    @staticmethod
    def _median_decimal(values: list[Decimal]) -> Decimal:
        return Decimal(str(median([float(item) for item in values]))).quantize(Decimal("0.0001")) if values else Decimal("0")

    @staticmethod
    def _stddev(values: list[Decimal]) -> Decimal:
        if len(values) < 2:
            return Decimal("0")
        float_values = [float(item) for item in values]
        avg = sum(float_values) / len(float_values)
        variance = sum((item - avg) ** 2 for item in float_values) / len(float_values)
        return Decimal(str(sqrt(variance))).quantize(Decimal("0.0001"))

    @staticmethod
    def _percentiles(values: list[Decimal]) -> dict[str, Decimal]:
        if not values:
            zero = Decimal("0")
            return {"p10": zero, "p25": zero, "p75": zero, "p90": zero}
        ordered = sorted(float(item) for item in values)
        return {
            "p10": Decimal(str(MicrostructureResearchEngine._percentile(ordered, 0.10))).quantize(Decimal("0.0001")),
            "p25": Decimal(str(MicrostructureResearchEngine._percentile(ordered, 0.25))).quantize(Decimal("0.0001")),
            "p75": Decimal(str(MicrostructureResearchEngine._percentile(ordered, 0.75))).quantize(Decimal("0.0001")),
            "p90": Decimal(str(MicrostructureResearchEngine._percentile(ordered, 0.90))).quantize(Decimal("0.0001")),
        }

    @staticmethod
    def _percentile(values: list[float], q: float) -> float:
        if not values:
            return 0.0
        if len(values) == 1:
            return values[0]
        position = (len(values) - 1) * q
        lower = int(position)
        upper = min(lower + 1, len(values) - 1)
        weight = position - lower
        return values[lower] * (1 - weight) + values[upper] * weight
