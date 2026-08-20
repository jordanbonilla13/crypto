import json
import resource
from datetime import datetime, timezone
from decimal import Decimal

import asyncpg

from app.collector.models import MarketMetadata, OrderBookState
from app.common.logging import get_logger
from app.common.settings import Settings
from app.experiments.market_making import MarketMakingSimulation
from app.experiments.microstructure import MicrostructureSignal
from app.experiments.models import OpportunitySnapshot


logger = get_logger(__name__)


class Database:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        self.pool = await asyncpg.create_pool(dsn=self.settings.database_dsn, min_size=1, max_size=5)

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.close()

    async def ping(self) -> bool:
        if self.pool is None:
            return False
        try:
            async with self.pool.acquire() as connection:
                await connection.fetchval("SELECT 1")
            return True
        except Exception:  # noqa: BLE001
            return False

    async def upsert_markets(self, markets: list[MarketMetadata]) -> None:
        if self.pool is None or not markets:
            return
        query = """
        INSERT INTO markets (
            market, base_asset, quote_asset, status, price_decimals, quantity_decimals,
            min_order_in_quote, min_order_in_base, tick_size, updated_at
        )
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
        ON CONFLICT (market) DO UPDATE SET
            base_asset = EXCLUDED.base_asset,
            quote_asset = EXCLUDED.quote_asset,
            status = EXCLUDED.status,
            price_decimals = EXCLUDED.price_decimals,
            quantity_decimals = EXCLUDED.quantity_decimals,
            min_order_in_quote = EXCLUDED.min_order_in_quote,
            min_order_in_base = EXCLUDED.min_order_in_base,
            tick_size = EXCLUDED.tick_size,
            updated_at = EXCLUDED.updated_at
        """
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                await connection.executemany(
                    query,
                    [
                        (
                            item.market,
                            item.base,
                            item.quote,
                            item.status,
                            item.price_decimals,
                            item.quantity_decimals,
                            item.min_order_in_quote,
                            item.min_order_in_base,
                            item.tick_size,
                            datetime.now(timezone.utc),
                        )
                        for item in markets
                    ],
                )

    async def upsert_books(self, books: list[OrderBookState]) -> None:
        if self.pool is None or not books:
            return
        market_snapshot_query = """
        INSERT INTO market_snapshots (
            market, snapshot_at, best_bid, best_bid_size, best_ask, best_ask_size, spread, source
        )
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
        """
        orderbook_query = """
        INSERT INTO orderbook_snapshots (
            market, snapshot_at, nonce, bids, asks
        )
        VALUES ($1,$2,$3,$4::jsonb,$5::jsonb)
        """
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                for book in books:
                    if not book.best_bid or not book.best_ask:
                        continue
                    snapshot_at = book.updated_at or datetime.now(timezone.utc)
                    await connection.execute(
                        market_snapshot_query,
                        book.market,
                        snapshot_at,
                        book.best_bid.price,
                        book.best_bid.size,
                        book.best_ask.price,
                        book.best_ask.size,
                        book.best_ask.price - book.best_bid.price,
                        "bitvavo_public",
                    )
                    await connection.execute(
                        orderbook_query,
                        book.market,
                        snapshot_at,
                        book.nonce,
                        json.dumps([[str(level.price), str(level.size)] for level in book.bids[:20]]),
                        json.dumps([[str(level.price), str(level.size)] for level in book.asks[:20]]),
                    )

    async def insert_event(self, event_type: str, payload: dict) -> None:
        if self.pool is None:
            return
        async with self.pool.acquire() as connection:
            await connection.execute(
                """
                INSERT INTO system_events (event_type, payload, created_at)
                VALUES ($1, $2::jsonb, $3)
                """,
                event_type,
                json.dumps(payload),
                datetime.now(timezone.utc),
            )

    async def insert_opportunities(self, opportunities: list[OpportunitySnapshot]) -> None:
        if self.pool is None or not opportunities:
            return
        query = """
        INSERT INTO opportunities (
            experiment_type, route, profitability_pct, executable, detected_at,
            capital, gross_profit_pct, net_profit_pct, total_fee_amount, slippage_pct,
            duration_ms, classification, market_path, details
        )
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14::jsonb)
        """
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                await connection.executemany(
                    query,
                    [
                        (
                            item.strategy,
                            item.route,
                            item.net_profit_pct,
                            item.executable,
                            item.detected_at,
                            item.capital,
                            item.gross_profit_pct,
                            item.net_profit_pct,
                            item.total_fee_amount,
                            item.slippage_pct,
                            item.duration_ms,
                            item.classification,
                            item.markets,
                            json.dumps(
                                {
                                    "route_assets": item.route_assets,
                                    "final_amount": str(item.final_amount),
                                    "gross_profit": str(item.gross_profit),
                                    "net_profit": str(item.net_profit),
                                    "latency_adjusted_profit": str(item.latency_adjusted_profit),
                                    "latency_adjusted_profit_pct": str(item.latency_adjusted_profit_pct),
                                    "worst_case_profit": str(item.worst_case_profit),
                                    "worst_case_profit_pct": str(item.worst_case_profit_pct),
                                    "worst_case_latency_ms": item.worst_case_latency_ms,
                                    "safety_margin_pct": str(item.safety_margin_pct),
                                    "safety_buffer_pct": str(item.safety_buffer_pct),
                                    "profitability_score": str(item.profitability_score),
                                    "liquidity_score": str(item.liquidity_score),
                                    "latency_score": str(item.latency_score),
                                    "confidence_score": str(item.confidence_score),
                                    "ranking_score": str(item.ranking_score),
                                    "executable_reason": item.executable_reason,
                                    "latency_scenarios_ms": item.latency_scenarios_ms,
                                    "legs": [
                                        {
                                            "from_asset": leg.from_asset,
                                            "to_asset": leg.to_asset,
                                            "market": leg.market,
                                            "side": leg.side,
                                            "input_amount": str(leg.input_amount),
                                            "output_amount": str(leg.output_amount),
                                            "fee_amount": str(leg.fee_amount),
                                            "average_price": str(leg.average_price),
                                            "liquidity_used_quote": str(leg.liquidity_used_quote),
                                            "fully_filled": leg.fully_filled,
                                        }
                                        for leg in item.legs
                                    ],
                                }
                            ),
                        )
                        for item in opportunities
                    ],
                )

    async def insert_market_making_simulations(self, simulations: list[MarketMakingSimulation]) -> None:
        if self.pool is None or not simulations:
            return
        query = """
        INSERT INTO market_making_simulations (
            simulation_key, signal_at, market, base_asset, quote_asset, capital,
            spread_pct, score, liquidity_level, volatility_bucket, volatility_pct,
            imbalance_bucket, imbalance_pct, hour_of_day, execution, executable,
            discarded, realized_pnl, exposure_time_ms, max_capital_exposed, details
        )
        VALUES (
            $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,
            $12,$13,$14,$15,$16,$17,$18,$19,$20,$21::jsonb
        )
        ON CONFLICT (simulation_key) DO NOTHING
        """
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                await connection.executemany(
                    query,
                    [
                        (
                            item.simulation_key,
                            item.signal_at,
                            item.market,
                            item.base,
                            item.quote,
                            item.capital,
                            item.spread_pct,
                            item.score,
                            item.liquidity_level,
                            item.volatility_bucket,
                            item.volatility_pct,
                            item.imbalance_bucket,
                            item.imbalance_pct,
                            item.hour_of_day,
                            item.execution,
                            item.executable,
                            item.discarded,
                            item.realized_pnl,
                            item.exposure_time_ms,
                            item.max_capital_exposed,
                            json.dumps(
                                {
                                    "discarded_reason": item.discarded_reason,
                                    "queue_position_buy": str(item.queue_position_buy),
                                    "queue_position_sell": str(item.queue_position_sell),
                                    "buy_fill_ratio": str(item.buy_fill_ratio),
                                    "sell_fill_ratio": str(item.sell_fill_ratio),
                                    "wait_buy_ms": item.wait_buy_ms,
                                    "wait_sell_ms": item.wait_sell_ms,
                                    "maker_fee": str(item.maker_fee),
                                    "taker_fee": str(item.taker_fee),
                                    "average_entry_price": str(item.average_entry_price) if item.average_entry_price is not None else None,
                                    "average_exit_price": str(item.average_exit_price) if item.average_exit_price is not None else None,
                                    "slippage_pct": str(item.slippage_pct),
                                    "adverse_selection_pct": str(item.adverse_selection_pct),
                                    "price_path": {key: str(value) for key, value in item.price_path.items()},
                                    "partial_fill": item.partial_fill,
                                    "only_one_side": item.only_one_side,
                                    "close_reason": item.close_reason,
                                    "score_bucket": self._score_bucket(item.score),
                                    "spread_bucket": self._spread_bucket(item.spread_pct),
                                }
                            ),
                        )
                        for item in simulations
                    ],
                )

    async def freeze_experiment_baseline(
        self,
        experiment_type: str,
        status: str,
        summary: dict,
        configuration: dict,
        notes: str,
    ) -> None:
        if self.pool is None:
            return
        async with self.pool.acquire() as connection:
            await connection.execute(
                """
                INSERT INTO experiment_baselines (
                    experiment_type, status, frozen_at, summary, configuration, notes
                )
                VALUES ($1, $2, $3, $4::jsonb, $5::jsonb, $6)
                ON CONFLICT (experiment_type) DO UPDATE SET
                    status = EXCLUDED.status,
                    frozen_at = EXCLUDED.frozen_at,
                    summary = EXCLUDED.summary,
                    configuration = EXCLUDED.configuration,
                    notes = EXCLUDED.notes
                """,
                experiment_type,
                status,
                datetime.now(timezone.utc),
                json.dumps(summary),
                json.dumps(configuration),
                notes,
            )

    async def get_experiment_baseline(self, experiment_type: str) -> dict | None:
        if self.pool is None:
            return None
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT experiment_type, status, frozen_at, summary, configuration, notes
                FROM experiment_baselines
                WHERE experiment_type = $1
                """,
                experiment_type,
            )
        if row is None:
            return None
        summary = row["summary"] or {}
        configuration = row["configuration"] or {}
        if isinstance(summary, str):
            summary = json.loads(summary)
        if isinstance(configuration, str):
            configuration = json.loads(configuration)
        return {
            "experiment_type": row["experiment_type"],
            "status": row["status"],
            "frozen_at": row["frozen_at"].isoformat(),
            "summary": summary,
            "configuration": configuration,
            "notes": row["notes"],
        }

    async def insert_microstructure_signals(self, signals: list[MicrostructureSignal]) -> None:
        if self.pool is None or not signals:
            return
        signal_types = sorted({item.signal_type for item in signals})
        query = """
        INSERT INTO microstructure_signals (
            signal_key, detected_at, market, base_asset, quote_asset, signal_type, partition,
            predicted_direction, score, spread_pct, depth_quote, liquidity_level, volatility_pct,
            volume_quote, imbalance_ratio, imbalance_pct, depth_change_pct, price_jump_pct,
            large_order_share_pct, hour_of_day, details
        )
        VALUES (
            $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21::jsonb
        )
        ON CONFLICT (signal_key) DO NOTHING
        """
        async with self.pool.acquire() as connection:
            counts = await connection.fetch(
                """
                SELECT signal_type, COUNT(*) AS count
                FROM microstructure_signals
                WHERE signal_type = ANY($1::text[])
                GROUP BY signal_type
                """,
                signal_types,
            )
            counts_by_type = {row["signal_type"]: row["count"] for row in counts}
            rows = []
            for item in sorted(signals, key=lambda signal: (signal.detected_at, signal.signal_type, signal.market)):
                current_count = counts_by_type.get(item.signal_type, 0)
                partition = (
                    "DISCOVERY"
                    if current_count < self.settings.microstructure_discovery_partition_size
                    else "VALIDATION"
                )
                counts_by_type[item.signal_type] = current_count + 1
                rows.append(
                    (
                        item.signal_key,
                        item.detected_at,
                        item.market,
                        item.base,
                        item.quote,
                        item.signal_type,
                        partition,
                        item.predicted_direction,
                        item.score,
                        item.spread_pct,
                        item.depth_quote,
                        item.liquidity_level,
                        item.volatility_pct,
                        item.volume_quote,
                        item.imbalance_ratio,
                        item.imbalance_pct,
                        item.depth_change_pct,
                        item.price_jump_pct,
                        item.large_order_share_pct,
                        item.hour_of_day,
                        json.dumps(
                            {
                                "signal_label": item.signal_label,
                                "volatility_bucket": item.volatility_bucket,
                                "spread_bucket": item.spread_bucket,
                                "signal_strength": item.signal_strength,
                                "details": item.details,
                                "outcomes": [
                                    {
                                        "horizon_ms": outcome.horizon_ms,
                                        "direction": outcome.direction,
                                        "move_pct": str(outcome.move_pct),
                                        "spread_pct": str(outcome.spread_pct),
                                        "depth_quote": str(outcome.depth_quote),
                                        "liquidity_level": outcome.liquidity_level,
                                        "volatility_pct": str(outcome.volatility_pct),
                                        "volume_quote": str(outcome.volume_quote),
                                    }
                                    for outcome in item.outcomes
                                ],
                            }
                        ),
                    )
                )
            async with connection.transaction():
                await connection.executemany(query, rows)

    async def list_recent_events(self, limit: int = 50) -> list[dict]:
        if self.pool is None:
            return []
        async with self.pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT event_type, payload, created_at
                FROM system_events
                ORDER BY created_at DESC
                LIMIT $1
                """,
                limit,
            )
        return [
            {
                "event_type": row["event_type"],
                "payload": row["payload"],
                "created_at": row["created_at"].isoformat(),
            }
            for row in rows
        ]

    async def list_recent_market_making_simulations(self, limit: int = 5000) -> list[dict]:
        if self.pool is None:
            return []
        async with self.pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT
                    simulation_key, signal_at, market, base_asset, quote_asset, capital,
                    spread_pct, score, liquidity_level, volatility_bucket, volatility_pct,
                    imbalance_bucket, imbalance_pct, hour_of_day, execution, executable,
                    discarded, realized_pnl, exposure_time_ms, max_capital_exposed, details
                FROM market_making_simulations
                ORDER BY signal_at DESC
                LIMIT $1
                """,
                limit,
            )
        payload = []
        for row in rows:
            details = row["details"] or {}
            if isinstance(details, str):
                details = json.loads(details)
            payload.append(
                {
                    "simulation_key": row["simulation_key"],
                    "signal_at": row["signal_at"].isoformat(),
                    "market": row["market"],
                    "base_asset": row["base_asset"],
                    "quote_asset": row["quote_asset"],
                    "capital": float(row["capital"]),
                    "spread_pct": float(row["spread_pct"]),
                    "score": float(row["score"]),
                    "score_bucket": details.get("score_bucket"),
                    "spread_bucket": details.get("spread_bucket"),
                    "liquidity_level": row["liquidity_level"],
                    "volatility_bucket": row["volatility_bucket"],
                    "volatility_pct": float(row["volatility_pct"]),
                    "imbalance_bucket": row["imbalance_bucket"],
                    "imbalance_pct": float(row["imbalance_pct"]),
                    "hour_of_day": row["hour_of_day"],
                    "execution": row["execution"],
                    "executable": row["executable"],
                    "discarded": row["discarded"],
                    "discarded_reason": details.get("discarded_reason"),
                    "realized_pnl": float(row["realized_pnl"]),
                    "exposure_time_ms": row["exposure_time_ms"],
                    "max_capital_exposed": float(row["max_capital_exposed"]),
                    "queue_position_buy": float(details.get("queue_position_buy", 0)),
                    "queue_position_sell": float(details.get("queue_position_sell", 0)),
                    "buy_fill_ratio": float(details.get("buy_fill_ratio", 0)),
                    "sell_fill_ratio": float(details.get("sell_fill_ratio", 0)),
                    "wait_buy_ms": details.get("wait_buy_ms"),
                    "wait_sell_ms": details.get("wait_sell_ms"),
                    "maker_fee": float(details.get("maker_fee", 0)),
                    "taker_fee": float(details.get("taker_fee", 0)),
                    "average_entry_price": float(details["average_entry_price"]) if details.get("average_entry_price") is not None else None,
                    "average_exit_price": float(details["average_exit_price"]) if details.get("average_exit_price") is not None else None,
                    "slippage_pct": float(details.get("slippage_pct", 0)),
                    "adverse_selection_pct": float(details.get("adverse_selection_pct", 0)),
                    "price_path": {key: float(value) for key, value in (details.get("price_path") or {}).items()},
                    "partial_fill": details.get("partial_fill", False),
                    "only_one_side": details.get("only_one_side", False),
                    "close_reason": details.get("close_reason"),
                }
            )
        return payload

    async def list_recent_microstructure_signals(self, limit: int = 20000) -> list[dict]:
        if self.pool is None:
            return []
        async with self.pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT
                    signal_key, detected_at, market, base_asset, quote_asset, signal_type, partition,
                    predicted_direction, score, spread_pct, depth_quote, liquidity_level, volatility_pct,
                    volume_quote, imbalance_ratio, imbalance_pct, depth_change_pct, price_jump_pct,
                    large_order_share_pct, hour_of_day, details
                FROM microstructure_signals
                ORDER BY detected_at DESC
                LIMIT $1
                """,
                limit,
            )
        payload = []
        for row in rows:
            details = row["details"] or {}
            if isinstance(details, str):
                details = json.loads(details)
            outcomes = details.get("outcomes") or []
            payload.append(
                {
                    "signal_key": row["signal_key"],
                    "detected_at": row["detected_at"].isoformat(),
                    "market": row["market"],
                    "base_asset": row["base_asset"],
                    "quote_asset": row["quote_asset"],
                    "signal_type": row["signal_type"],
                    "signal_label": details.get("signal_label"),
                    "partition": row["partition"],
                    "predicted_direction": row["predicted_direction"],
                    "score": float(row["score"]),
                    "spread_pct": float(row["spread_pct"]),
                    "depth_quote": float(row["depth_quote"]),
                    "liquidity_level": row["liquidity_level"],
                    "volatility_pct": float(row["volatility_pct"]),
                    "volatility_bucket": details.get("volatility_bucket", "BAJA"),
                    "volume_quote": float(row["volume_quote"]),
                    "imbalance_ratio": float(row["imbalance_ratio"]),
                    "imbalance_pct": float(row["imbalance_pct"]),
                    "depth_change_pct": float(row["depth_change_pct"]),
                    "price_jump_pct": float(row["price_jump_pct"]),
                    "large_order_share_pct": float(row["large_order_share_pct"]),
                    "spread_bucket": details.get("spread_bucket", "ESTRECHO"),
                    "signal_strength": details.get("signal_strength", "BAJA"),
                    "hour_of_day": row["hour_of_day"],
                    "details": details.get("details") or {},
                    "outcomes": [
                        {
                            "horizon_ms": item["horizon_ms"],
                            "direction": item["direction"],
                            "move_pct": float(item["move_pct"]),
                            "spread_pct": float(item["spread_pct"]),
                            "depth_quote": float(item["depth_quote"]),
                            "liquidity_level": item["liquidity_level"],
                            "volatility_pct": float(item["volatility_pct"]),
                            "volume_quote": float(item["volume_quote"]),
                        }
                        for item in outcomes
                    ],
                }
            )
        return payload

    async def get_stats_windows(self) -> dict:
        if self.pool is None:
            return {}
        query = """
        WITH windows AS (
            SELECT '24h' AS label, NOW() - INTERVAL '24 hours' AS since
            UNION ALL
            SELECT '7d', NOW() - INTERVAL '7 days'
            UNION ALL
            SELECT '30d', NOW() - INTERVAL '30 days'
            UNION ALL
            SELECT 'total', TIMESTAMPTZ '1970-01-01 00:00:00+00'
        )
        SELECT
            windows.label,
            COUNT(o.*) AS opportunity_count,
            COUNT(*) FILTER (WHERE o.executable) AS executable_count,
            COALESCE(MAX(o.net_profit_pct), 0) AS best_net_pct,
            COALESCE(SUM((o.details->>'latency_adjusted_profit')::numeric), 0) AS simulated_profit
        FROM windows
        LEFT JOIN opportunities o
            ON o.detected_at >= windows.since
        GROUP BY windows.label
        """
        async with self.pool.acquire() as connection:
            rows = await connection.fetch(query)
        return {
            row["label"]: {
                "opportunity_count": row["opportunity_count"],
                "executable_count": row["executable_count"],
                "best_net_pct": float(row["best_net_pct"]),
                "simulated_profit": float(row["simulated_profit"]),
            }
            for row in rows
        }

    async def upsert_experiment_status(self, experiment_type: str, status: str) -> None:
        if self.pool is None:
            return
        async with self.pool.acquire() as connection:
            await connection.execute(
                """
                INSERT INTO experiments (experiment_type, status, updated_at)
                VALUES ($1, $2, $3)
                ON CONFLICT (experiment_type) DO UPDATE SET
                    status = EXCLUDED.status,
                    updated_at = EXCLUDED.updated_at
                """,
                experiment_type,
                status,
                datetime.now(timezone.utc),
            )

    async def list_experiments(self) -> list[dict]:
        if self.pool is None:
            return []
        async with self.pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT experiment_type, status, created_at, updated_at
                FROM experiments
                ORDER BY experiment_type
                """
            )
        return [
            {
                "experiment_type": row["experiment_type"],
                "status": row["status"],
                "created_at": row["created_at"].isoformat(),
                "updated_at": row["updated_at"].isoformat(),
            }
            for row in rows
        ]

    async def get_storage_metrics(self) -> dict:
        if self.pool is None:
            return {}
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT
                    (SELECT COUNT(*) FROM markets) AS markets,
                    (SELECT COUNT(*) FROM market_snapshots) AS market_snapshots,
                    (SELECT COUNT(*) FROM orderbook_snapshots) AS orderbook_snapshots,
                    (SELECT COUNT(*) FROM opportunities) AS opportunities,
                    (SELECT COUNT(*) FROM market_making_simulations) AS market_making_simulations,
                    (SELECT COUNT(*) FROM microstructure_signals) AS microstructure_signals,
                    (SELECT COUNT(*) FROM system_events) AS system_events
                """
            )
        usage = resource.getrusage(resource.RUSAGE_SELF)
        return {
            "markets": row["markets"],
            "market_snapshots": row["market_snapshots"],
            "orderbook_snapshots": row["orderbook_snapshots"],
            "opportunities": row["opportunities"],
            "market_making_simulations": row["market_making_simulations"],
            "microstructure_signals": row["microstructure_signals"],
            "system_events": row["system_events"],
            "process_memory_mb": round(usage.ru_maxrss / 1024, 2),
        }

    @staticmethod
    def _score_bucket(score: Decimal) -> str:
        if score >= Decimal("85"):
            return "ALTO"
        if score >= Decimal("70"):
            return "MEDIO"
        return "BAJO"

    @staticmethod
    def _spread_bucket(spread_pct: Decimal) -> str:
        if spread_pct >= Decimal("0.40"):
            return "AMPLIO"
        if spread_pct >= Decimal("0.15"):
            return "MEDIO"
        return "ESTRECHO"
