import asyncio
import contextlib
import json
import os
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from statistics import mean

from app.collector.bitvavo_client import BitvavoPublicClient
from app.collector.models import CollectorState, MarketMetadata
from app.collector.orderbook import OrderBookStore
from app.common.logging import get_logger
from app.common.settings import Settings
from app.database.core import Database
from app.experiments.market_making import MarketMakingBacktester, MarketMakingEngine
from app.experiments.microstructure import MicrostructureResearchEngine
from app.experiments.models import OpportunitySnapshot
from app.experiments.triangular import TriangularArbitrageEngine


logger = get_logger(__name__)


class CollectorService:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        client: BitvavoPublicClient | None = None,
    ) -> None:
        self.settings = settings
        self.database = database
        self.client = client or BitvavoPublicClient(settings.bitvavo_rest_url, settings.bitvavo_ws_url)
        self.state = CollectorState()
        self.books = OrderBookStore(history_limit=2048)
        self.markets: dict[str, MarketMetadata] = {}
        self.triangular_engine = TriangularArbitrageEngine(settings)
        self.market_making_engine = MarketMakingEngine(settings)
        self.market_making_backtester = MarketMakingBacktester(settings, self.market_making_engine)
        self.microstructure_engine = MicrostructureResearchEngine(settings)
        self._runner_task: asyncio.Task | None = None
        self._snapshot_task: asyncio.Task | None = None
        self._analysis_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._started_at = datetime.now(timezone.utc)

    async def start(self) -> None:
        self._stop_event.clear()
        await self._bootstrap_markets()
        self._runner_task = asyncio.create_task(self._run_forever(), name="bitvavo-collector")
        self._snapshot_task = asyncio.create_task(self._persist_periodic_snapshots(), name="snapshot-persistor")
        self._analysis_task = asyncio.create_task(self._run_analysis_loop(), name="triangular-analyzer")
        await self.database.upsert_experiment_status("TRIANGULAR_ARBITRAGE", "SIMULANDO")
        await self._freeze_market_making_baseline()
        await self.database.upsert_experiment_status("MARKET_MAKING", "NO RENTABLE")
        await self.database.upsert_experiment_status("CROSS_EXCHANGE_ARBITRAGE", "PENDIENTE")
        await self.database.upsert_experiment_status("ORDERBOOK_MICROSTRUCTURE", "SIMULANDO")

    async def stop(self) -> None:
        self._stop_event.set()
        for task in (self._runner_task, self._snapshot_task, self._analysis_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    async def _bootstrap_markets(self) -> None:
        market_payloads = await self.client.get_markets()
        self.markets = {item["market"]: self._to_market_metadata(item) for item in market_payloads}
        self.state.discovered_markets = len(self.markets)
        tracked = self._select_tracked_markets(self.markets.values())
        self.state.tracked_markets = len(tracked)
        await self.database.upsert_markets(list(self.markets.values()))
        await self.database.insert_event("markets_discovered", {"discovered": len(self.markets), "tracked": len(tracked)})

        snapshots = await asyncio.gather(
            *[self.client.get_book(market.market, self.settings.orderbook_depth) for market in tracked]
        )
        for market, snapshot in zip(tracked, snapshots, strict=False):
            self.books.snapshot(market.market, snapshot.get("bids", []), snapshot.get("asks", []), snapshot.get("nonce"))
        await self.database.upsert_books(self.books.all())

    async def _run_forever(self) -> None:
        retry_seconds = 3
        tracked_markets = [item.market for item in self._select_tracked_markets(self.markets.values())]
        while not self._stop_event.is_set():
            try:
                async with self.client.websocket() as websocket:
                    self.state.connected = True
                    self.state.last_error = None
                    await self.database.insert_event("collector_connected", {"tracked_markets": tracked_markets})
                    await self.client.subscribe_books(websocket, tracked_markets)

                    async for raw_message in websocket:
                        if self._stop_event.is_set():
                            break
                        await self._handle_ws_message(raw_message)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.state.connected = False
                self.state.last_error = str(exc)
                self.state.reconnect_count += 1
                await self.database.insert_event("collector_disconnected", {"error": str(exc)})
                logger.exception("collector_loop_failed")
                await asyncio.sleep(retry_seconds)

    async def _handle_ws_message(self, raw_message: str) -> None:
        payload = json.loads(raw_message)
        if payload.get("event") != "book" or "market" not in payload:
            return

        market = payload["market"]
        self.books.apply_update(
            market=market,
            bids=payload.get("bids", []),
            asks=payload.get("asks", []),
            nonce=payload.get("nonce"),
        )
        self.state.last_market_update_at = datetime.now(timezone.utc)
        self.state.total_updates += 1
        intervals = []
        if self.state.average_update_interval_ms is not None:
            intervals.append(self.state.average_update_interval_ms)
        if self.state.total_updates > 1:
            intervals.append(self._last_update_seconds() * 1000 if self._last_update_seconds() is not None else 0)
        if intervals:
            self.state.average_update_interval_ms = round(mean(intervals), 3)
        book = self.books.get(market)
        if book and book.best_bid and book.best_ask and book.best_ask.price < book.best_bid.price:
            await self._resync_market_book(market)

    async def _persist_periodic_snapshots(self) -> None:
        while not self._stop_event.is_set():
            await asyncio.sleep(self.settings.snapshot_interval_seconds)
            books = self.books.all()
            if not books:
                continue
            await self.database.upsert_books(books)
            snapshots = self.triangular_engine.snapshot_for_persistence()
            await self.database.insert_opportunities(snapshots)

    async def _resync_market_book(self, market: str) -> None:
        snapshot = await self.client.get_book(market, self.settings.orderbook_depth)
        self.books.snapshot(market, snapshot.get("bids", []), snapshot.get("asks", []), snapshot.get("nonce"))
        await self.database.insert_event("orderbook_resynced", {"market": market})

    async def _run_analysis_loop(self) -> None:
        while not self._stop_event.is_set():
            await asyncio.sleep(2)
            all_books = self.books.all()
            if not all_books:
                continue
            current_books = {book.market: book for book in all_books}
            latency_views = self.books.build_latency_views(self.settings.latency_scenarios_ms)
            self.triangular_engine.analyze(self.markets, current_books, latency_views)
            micro_signals = self.microstructure_engine.analyze_from_histories(self.markets, self.books.histories())
            if micro_signals:
                await self.database.insert_microstructure_signals(micro_signals)

    def current_state(self) -> CollectorState:
        return self.state

    def build_dashboard_summary(self) -> dict:
        visible_markets = []
        for market_name in ("BTC-EUR", "ETH-EUR", "USDC-EUR"):
            book = self.books.get(market_name)
            if book is None:
                continue
            visible_markets.append(self._market_to_card(book))

        if not visible_markets:
            visible_markets = [self._market_to_card(book) for book in self.books.all()[:5]]

        return {
            "title": "BITVAVO LAB",
            "bitvavo_status": "CONECTADO" if self.state.connected else "RECONECTANDO",
            "mode": self.settings.operation_mode.upper(),
            "postgresql_status": "OK",
            "collector_status": "OK" if self.state.connected else "DEGRADED",
            "markets_found": self.state.discovered_markets,
            "markets_tracked": self.state.tracked_markets,
            "last_update_seconds": self._last_update_seconds(),
            "current_latency_ms": self._current_latency_ms(),
            "opportunities_today": self.triangular_engine.daily_counters.detected,
            "profitable_today": self.triangular_engine.daily_counters.profitable,
            "executable_today": self.triangular_engine.daily_counters.executable,
            "latency_scenarios_ms": self.settings.latency_scenarios_ms,
            "primary_latency_ms": self.settings.primary_latency_ms,
            "best_opportunity": self._serialize_opportunity(self._best_opportunity()),
            "simulated_profit_today": str(self.triangular_engine.daily_counters.simulated_profit),
            "best_strategy": self._best_strategy_label(),
            "markets": visible_markets,
        }

    async def build_overview_payload(self) -> dict:
        summary = self.build_dashboard_summary()
        stats = await self.database.get_stats_windows()
        experiments = await self.database.list_experiments()
        active = self.get_active_opportunities(limit=8)
        warnings = self._build_warnings()
        chart_points = self._build_profitability_chart()
        market_making = await self._build_market_making_payload()
        microstructure = await self._build_microstructure_payload()
        return {
            **summary,
            "system_status": "ESTABLE" if not warnings else "ATENCION",
            "warnings": warnings,
            "charts": chart_points,
            "recent_opportunities": active,
            "experiments": self._decorate_experiments(experiments),
            "stats_windows": stats,
            "market_making_pnl": market_making,
            "microstructure": microstructure,
        }

    async def build_markets_payload(self) -> dict:
        rows = []
        for book in self.books.all():
            metadata = self.markets.get(book.market)
            if metadata is None or book.best_bid is None or book.best_ask is None:
                continue
            spread = book.best_ask.price - book.best_bid.price
            mid = (book.best_ask.price + book.best_bid.price) / Decimal("2")
            spread_pct = (spread / mid * Decimal("100")) if mid else Decimal("0")
            rows.append(
                {
                    "market": book.market,
                    "base": metadata.base,
                    "quote": metadata.quote,
                    "status": metadata.status.upper(),
                    "bid": str(book.best_bid.price),
                    "ask": str(book.best_ask.price),
                    "spread": str(spread),
                    "spread_pct": float(spread_pct),
                    "bid_liquidity": str(sum(level.size for level in book.bids[:5])),
                    "ask_liquidity": str(sum(level.size for level in book.asks[:5])),
                    "updated_at": book.updated_at.isoformat() if book.updated_at else None,
                }
            )
        rows.sort(key=lambda item: item["market"])
        return {"markets": rows, "last_update_seconds": self._last_update_seconds()}

    def build_opportunities_payload(self) -> dict:
        current = self.get_active_opportunities(limit=100)
        return {
            "opportunities": current,
            "summary": {
                "detected_today": self.triangular_engine.daily_counters.detected,
                "profitable_today": self.triangular_engine.daily_counters.profitable,
                "executable_today": self.triangular_engine.daily_counters.executable,
                "top_score": float(current[0]["ranking_score"]) if current else None,
            },
        }

    async def build_experiments_payload(self) -> dict:
        experiments = await self.database.list_experiments()
        return {
            "experiments": self._decorate_experiments(experiments),
            "triangular_route_count": len(self.triangular_engine.routes),
            "active_opportunity_count": len(self.triangular_engine.active_opportunities),
            "market_making_signal_count": 0,
            "market_making_signals": [],
            "market_making_pnl": await self._build_market_making_payload(),
            "microstructure": await self._build_microstructure_payload(),
        }

    async def build_statistics_payload(self) -> dict:
        windows = await self.database.get_stats_windows()
        return {
            "windows": windows,
            "profitability_chart": self._build_profitability_chart(),
            "duration_chart": self._build_duration_chart(),
        }

    async def build_system_payload(self) -> dict:
        events = await self.database.list_recent_events(limit=50)
        storage = await self.database.get_storage_metrics()
        return {
            "services": {
                "database": "OK",
                "collector": "OK" if self.state.connected else "DEGRADED",
                "bitvavo": "CONECTADO" if self.state.connected else "DESCONECTADO",
            },
            "runtime": {
                "uptime_seconds": int((datetime.now(timezone.utc) - self._started_at).total_seconds()),
                "pid": os.getpid(),
                "reconnect_count": self.state.reconnect_count,
                "total_updates": self.state.total_updates,
                "average_update_interval_ms": self.state.average_update_interval_ms,
                "last_error": self.state.last_error,
                "current_latency_ms": self._current_latency_ms(),
            },
            "storage": storage,
            "events": events,
        }

    def build_configuration_payload(self) -> dict:
        return {
            "mode": self.settings.operation_mode.upper(),
            "initial_currency": self.settings.initial_currency,
            "simulated_capitals": [str(item) for item in self.settings.simulated_capitals],
            "simulated_taker_fee": str(self.settings.simulated_taker_fee),
            "simulated_maker_fee": str(self.settings.simulated_maker_fee),
            "safety_margin_pct": str(self.settings.safety_margin_pct),
            "latency_scenarios_ms": self.settings.latency_scenarios_ms,
            "primary_latency_ms": self.settings.primary_latency_ms,
            "track_quote_currencies": self.settings.track_quote_currencies,
            "book_markets_limit": self.settings.book_markets_limit,
            "orderbook_depth": self.settings.orderbook_depth,
            "snapshot_interval_seconds": self.settings.snapshot_interval_seconds,
            "market_making_min_spread_pct": str(self.settings.market_making_min_spread_pct),
            "market_making_max_spread_pct": str(self.settings.market_making_max_spread_pct),
            "market_making_min_depth_quote": str(self.settings.market_making_min_depth_quote),
            "market_making_signal_limit": self.settings.market_making_signal_limit,
            "microstructure_signal_limit": self.settings.microstructure_signal_limit,
            "microstructure_discovery_partition_size": self.settings.microstructure_discovery_partition_size,
            "microstructure_min_depth_quote": str(self.settings.microstructure_min_depth_quote),
            "microstructure_imbalance_ratio_threshold": str(self.settings.microstructure_imbalance_ratio_threshold),
            "microstructure_liquidity_vanish_threshold_pct": str(self.settings.microstructure_liquidity_vanish_threshold_pct),
            "microstructure_jump_threshold_pct": str(self.settings.microstructure_jump_threshold_pct),
            "microstructure_large_order_share_threshold_pct": str(self.settings.microstructure_large_order_share_threshold_pct),
            "microstructure_depth_change_threshold_pct": str(self.settings.microstructure_depth_change_threshold_pct),
            "real_operation_enabled": self.settings.real_operation_enabled,
            "emergency_stop": self.settings.emergency_stop,
            "max_operation_capital_eur": str(self.settings.max_operation_capital_eur),
            "max_total_exposure_eur": str(self.settings.max_total_exposure_eur),
            "max_daily_loss_eur": str(self.settings.max_daily_loss_eur),
            "max_operations_per_day": self.settings.max_operations_per_day,
        }

    def _last_update_seconds(self) -> float | None:
        if self.state.last_market_update_at is None:
            return None
        return round((datetime.now(timezone.utc) - self.state.last_market_update_at).total_seconds(), 3)

    def _current_latency_ms(self) -> int | None:
        seconds = self._last_update_seconds()
        return int(seconds * 1000) if seconds is not None else None

    def _best_opportunity(self) -> OpportunitySnapshot | None:
        if not self.triangular_engine.last_cycle_opportunities:
            return None
        return max(
            self.triangular_engine.last_cycle_opportunities,
            key=lambda item: (item.ranking_score, item.worst_case_profit_pct, item.net_profit),
        )

    def get_active_opportunities(self, limit: int = 50) -> list[dict]:
        items = self.triangular_engine.last_cycle_opportunities[:limit]
        return [self._serialize_opportunity(item) for item in items]

    def _serialize_opportunity(self, item: OpportunitySnapshot | None) -> dict | None:
        if item is None:
            return None
        return {
            "route": item.route,
            "route_assets": item.route_assets,
            "capital": str(item.capital),
            "final_amount": str(item.final_amount),
            "gross_profit": str(item.gross_profit),
            "gross_profit_pct": float(item.gross_profit_pct),
            "net_profit": str(item.net_profit),
            "net_profit_pct": float(item.net_profit_pct),
            "total_fee_amount": str(item.total_fee_amount),
            "slippage_pct": float(item.slippage_pct),
            "latency_adjusted_profit": str(item.latency_adjusted_profit),
            "latency_adjusted_profit_pct": float(item.latency_adjusted_profit_pct),
            "worst_case_profit": str(item.worst_case_profit),
            "worst_case_profit_pct": float(item.worst_case_profit_pct),
            "worst_case_latency_ms": item.worst_case_latency_ms,
            "safety_margin_pct": float(item.safety_margin_pct),
            "safety_buffer_pct": float(item.safety_buffer_pct),
            "profitability_score": float(item.profitability_score),
            "liquidity_score": float(item.liquidity_score),
            "latency_score": float(item.latency_score),
            "confidence_score": float(item.confidence_score),
            "ranking_score": float(item.ranking_score),
            "duration_ms": item.duration_ms,
            "classification": item.classification,
            "strategy": item.strategy,
            "markets": item.markets,
            "executable": item.executable,
            "executable_reason": item.executable_reason,
            "latency_scenarios_ms": item.latency_scenarios_ms,
            "detected_at": item.detected_at.isoformat(),
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

    async def _build_market_making_payload(self) -> dict:
        rows = await self.database.list_recent_market_making_simulations(limit=5000)
        baseline = await self.database.get_experiment_baseline("MARKET_MAKING")
        now = datetime.now(timezone.utc)
        today_rows = [row for row in rows if datetime.fromisoformat(row["signal_at"]).date() == now.date()]
        rows_24h = [row for row in rows if datetime.fromisoformat(row["signal_at"]) >= now - timedelta(hours=24)]
        rows_7d = [row for row in rows if datetime.fromisoformat(row["signal_at"]) >= now - timedelta(days=7)]
        summary_all = self.market_making_backtester.build_summary(rows)
        return {
            "today": self._summary_dict(self.market_making_backtester.build_summary(today_rows)),
            "window_24h": self._summary_dict(self.market_making_backtester.build_summary(rows_24h)),
            "window_7d": self._summary_dict(self.market_making_backtester.build_summary(rows_7d)),
            "overall": self._summary_dict(summary_all),
            "latest_simulations": rows[:20],
            "equity_curve": self._build_market_making_equity_curve(rows),
            "baseline": baseline,
            "breakdowns": {
                "by_capital": summary_all.by_capital,
                "by_market": summary_all.by_market,
                "by_signal_type": summary_all.by_signal_type,
                "by_score": self._aggregate_market_making(rows, "score_bucket"),
                "by_liquidity": self._aggregate_market_making(rows, "liquidity_level"),
                "by_volatility": self._aggregate_market_making(rows, "volatility_bucket"),
                "by_imbalance": self._aggregate_market_making(rows, "imbalance_bucket"),
                "by_hour": self._aggregate_market_making(rows, "hour_of_day"),
                "by_spread": self._aggregate_market_making(rows, "spread_bucket"),
            },
        }

    async def _build_microstructure_payload(self) -> dict:
        rows = await self.database.list_recent_microstructure_signals(limit=20000)
        overall = self.microstructure_engine.build_summary(rows)
        discovery = self.microstructure_engine.build_summary(rows, partition="DISCOVERY")
        validation = self.microstructure_engine.build_summary(rows, partition="VALIDATION")
        pattern_analysis = self.microstructure_engine.build_pattern_analysis(rows)
        latest = rows[:20]
        direction_counts: dict[str, int] = {}
        for row in rows:
            direction_counts[row["predicted_direction"]] = direction_counts.get(row["predicted_direction"], 0) + 1
        return {
            "overall": self._micro_summary_dict(overall),
            "discovery": self._micro_summary_dict(discovery),
            "validation": self._micro_summary_dict(validation),
            "pattern_analysis": pattern_analysis,
            "latest_signals": latest,
            "signal_count": len(rows),
            "current_signal_count": len(self.microstructure_engine.last_signals),
            "current_signals": [self._serialize_micro_signal(item) for item in self.microstructure_engine.last_signals[:12]],
            "direction_breakdown": [{"label": key, "count": value} for key, value in sorted(direction_counts.items())],
        }

    @staticmethod
    def _summary_dict(summary) -> dict:
        return {
            "total_simulations": summary.total_simulations,
            "executable_signals": summary.executable_signals,
            "discarded_signals": summary.discarded_signals,
            "profitable_count": summary.profitable_count,
            "negative_count": summary.negative_count,
            "pnl_total": float(summary.pnl_total),
            "best_trade": float(summary.best_trade),
            "worst_trade": float(summary.worst_trade),
            "avg_profit": float(summary.avg_profit),
            "avg_loss": float(summary.avg_loss),
            "mean_pnl": float(summary.mean_pnl),
            "median_pnl": float(summary.median_pnl),
            "profitable_ratio": float(summary.profitable_ratio),
            "max_capital_exposed": float(summary.max_capital_exposed),
            "average_exposure_ms": float(summary.average_exposure_ms),
            "evaluation": summary.evaluation,
            "by_capital": summary.by_capital,
            "by_market": summary.by_market,
            "by_signal_type": summary.by_signal_type,
        }

    @staticmethod
    def _micro_summary_dict(summary) -> dict:
        return {
            "total_signals": summary.total_signals,
            "sample_size": summary.sample_size,
            "state": summary.state,
            "promising_signal": summary.promising_signal,
            "best_horizon_ms": summary.best_horizon_ms,
            "worst_horizon_ms": summary.worst_horizon_ms,
            "movement_mean_pct": float(summary.movement_mean_pct),
            "movement_median_pct": float(summary.movement_median_pct),
            "hit_rate_pct": float(summary.hit_rate_pct),
            "by_type": summary.by_type,
            "by_market": summary.by_market,
            "by_direction": summary.by_direction,
            "by_horizon": summary.by_horizon,
            "candidates": summary.candidates,
            "ready_for_evaluation": summary.ready_for_evaluation,
        }

    @staticmethod
    def _build_market_making_equity_curve(rows: list[dict]) -> list[dict]:
        running = Decimal("0")
        points = []
        for row in sorted(rows, key=lambda item: item["signal_at"])[-80:]:
            running += Decimal(str(row["realized_pnl"]))
            points.append({"label": row["market"], "value": float(running.quantize(Decimal("0.01")))})
        return points

    @staticmethod
    def _aggregate_market_making(rows: list[dict], field: str) -> list[dict]:
        grouped: dict[str, list[Decimal]] = {}
        for row in rows:
            grouped.setdefault(str(row.get(field)), []).append(Decimal(str(row["realized_pnl"])))
        output = []
        for label, pnls in grouped.items():
            output.append(
                {
                    "label": label,
                    "count": len(pnls),
                    "pnl": float(sum(pnls, start=Decimal("0")).quantize(Decimal("0.01"))),
                    "profitable_ratio": float((Decimal(sum(1 for pnl in pnls if pnl > 0)) / Decimal(len(pnls)) * Decimal("100")).quantize(Decimal("0.01"))),
                }
            )
        return sorted(output, key=lambda item: (item["pnl"], item["count"]), reverse=True)[:12]

    def _build_warnings(self) -> list[dict]:
        warnings = []
        if not self.state.connected:
            warnings.append({"level": "error", "message": "Bitvavo no esta conectado."})
        if self.settings.operation_mode.lower() != "simulacion" and not self.settings.real_operation_enabled:
            warnings.append({"level": "warning", "message": "Modo real configurado pero operaciones reales bloqueadas."})
        if self.settings.emergency_stop:
            warnings.append({"level": "error", "message": "Parada de emergencia activa."})
        if self.state.last_error:
            warnings.append({"level": "warning", "message": self.state.last_error})
        return warnings

    async def _freeze_market_making_baseline(self) -> None:
        rows = await self.database.list_recent_market_making_simulations(limit=5000)
        summary = self._summary_dict(self.market_making_backtester.build_summary(rows))
        configuration = {
            "mode": self.settings.operation_mode.upper(),
            "simulated_capitals": [str(item) for item in self.settings.simulated_capitals],
            "simulated_taker_fee": str(self.settings.simulated_taker_fee),
            "simulated_maker_fee": str(self.settings.simulated_maker_fee),
            "market_making_min_spread_pct": str(self.settings.market_making_min_spread_pct),
            "market_making_max_spread_pct": str(self.settings.market_making_max_spread_pct),
            "market_making_min_depth_quote": str(self.settings.market_making_min_depth_quote),
            "market_making_signal_limit": self.settings.market_making_signal_limit,
        }
        await self.database.freeze_experiment_baseline(
            experiment_type="MARKET_MAKING",
            status="NO RENTABLE",
            summary=summary,
            configuration=configuration,
            notes="Baseline congelado el 2026-08-20 tras 5000 simulaciones netamente no rentables. No reoptimizar sobre la misma muestra.",
        )

    def _build_profitability_chart(self) -> list[dict]:
        top = self.triangular_engine.last_cycle_opportunities[:12]
        return [{"label": item.route.replace(" -> ", " / "), "value": float(item.worst_case_profit_pct)} for item in top]

    def _build_duration_chart(self) -> list[dict]:
        top = self.triangular_engine.last_cycle_opportunities[:12]
        return [{"label": item.route.replace(" -> ", " / "), "value": item.duration_ms} for item in top]

    @staticmethod
    def _decorate_experiments(experiments: list[dict]) -> list[dict]:
        descriptions = {
            "TRIANGULAR_ARBITRAGE": "Busca rutas EUR -> A -> B -> EUR dentro de Bitvavo.",
            "MARKET_MAKING": "Experimento congelado como NO RENTABLE tras simulacion realista con costes y cierres forzados.",
            "CROSS_EXCHANGE_ARBITRAGE": "Pendiente para comparar Bitvavo con otras plataformas.",
            "ORDERBOOK_MICROSTRUCTURE": "Observa patrones estadisticos repetibles en el libro sin activar ninguna estrategia real.",
        }
        labels = {
            "TRIANGULAR_ARBITRAGE": "Arbitraje triangular",
            "MARKET_MAKING": "Creacion de liquidez",
            "CROSS_EXCHANGE_ARBITRAGE": "Arbitraje entre plataformas",
            "ORDERBOOK_MICROSTRUCTURE": "Microineficiencias estadisticas",
        }
        return [
            {
                **item,
                "label": labels.get(item["experiment_type"], item["experiment_type"]),
                "description": descriptions.get(item["experiment_type"], ""),
            }
            for item in experiments
        ]

    @staticmethod
    def _serialize_market_making_signal(item) -> dict:
        return {
            "market": item.market,
            "base": item.base,
            "quote": item.quote,
            "snapshot_at": item.snapshot_at.isoformat(),
            "spread_pct": float(item.spread_pct),
            "bid_depth_quote": str(item.bid_depth_quote),
            "ask_depth_quote": str(item.ask_depth_quote),
            "depth_score": float(item.depth_score),
            "balance_score": float(item.balance_score),
            "spread_score": float(item.spread_score),
            "overall_score": float(item.overall_score),
            "bias": item.bias,
            "status": item.status,
            "rationale": item.rationale,
            "liquidity_level": item.liquidity_level,
            "volatility_pct": float(item.volatility_pct),
            "volatility_bucket": item.volatility_bucket,
            "imbalance_pct": float(item.imbalance_pct),
            "imbalance_bucket": item.imbalance_bucket,
            "hour_of_day": item.hour_of_day,
        }

    def _best_strategy_label(self) -> str:
        if self.microstructure_engine.last_signals and not self.triangular_engine.last_cycle_opportunities:
            return "Microineficiencias estadisticas"
        if self.market_making_engine.last_signals and not self.triangular_engine.last_cycle_opportunities:
            return "Creacion de liquidez"
        return "Arbitraje triangular"

    @staticmethod
    def _serialize_micro_signal(item) -> dict:
        return {
            "signal_key": item.signal_key,
            "detected_at": item.detected_at.isoformat(),
            "market": item.market,
            "signal_type": item.signal_type,
            "signal_label": item.signal_label,
            "predicted_direction": item.predicted_direction,
            "score": float(item.score),
            "spread_pct": float(item.spread_pct),
            "depth_quote": float(item.depth_quote),
            "liquidity_level": item.liquidity_level,
            "volatility_pct": float(item.volatility_pct),
            "volume_quote": float(item.volume_quote),
            "imbalance_ratio": float(item.imbalance_ratio),
            "imbalance_pct": float(item.imbalance_pct),
            "depth_change_pct": float(item.depth_change_pct),
            "price_jump_pct": float(item.price_jump_pct),
            "large_order_share_pct": float(item.large_order_share_pct),
            "hour_of_day": item.hour_of_day,
            "outcomes": [
                {
                    "horizon_ms": outcome.horizon_ms,
                    "direction": outcome.direction,
                    "move_pct": float(outcome.move_pct),
                }
                for outcome in item.outcomes
            ],
        }

    @staticmethod
    def _market_to_card(book) -> dict:
        return {
            "market": book.market,
            "bid": str(book.best_bid.price) if book.best_bid else None,
            "ask": str(book.best_ask.price) if book.best_ask else None,
            "bid_size": str(book.best_bid.size) if book.best_bid else None,
            "ask_size": str(book.best_ask.size) if book.best_ask else None,
            "spread": str(book.best_ask.price - book.best_bid.price) if book.best_bid and book.best_ask else None,
        }

    def _select_tracked_markets(self, markets: Iterable[MarketMetadata]) -> list[MarketMetadata]:
        market_list = [market for market in markets if market.status.lower() == "trading"]
        market_map = {market.market: market for market in market_list}
        self.triangular_engine.rebuild_routes(market_map)

        preferred = {"BTC-EUR": 0, "ETH-EUR": 1, "USDC-EUR": 2}
        selected: list[MarketMetadata] = []
        seen: set[str] = set()

        def add_market(name: str) -> None:
            if name in market_map and name not in seen and len(selected) < self.settings.book_markets_limit:
                selected.append(market_map[name])
                seen.add(name)

        for name in ("BTC-EUR", "ETH-EUR", "USDC-EUR", "BTC-USDC", "ETH-BTC", "ETH-USDC", "USDT-EUR", "BTC-USDT"):
            add_market(name)

        preferred_assets = {self.settings.initial_currency.upper(), "BTC", "ETH", "USDC", "USDT"}
        scored_routes = sorted(
            self.triangular_engine.routes,
            key=lambda route: (-sum(1 for asset in route.assets if asset in preferred_assets), route.key),
        )
        for route in scored_routes:
            for from_asset, to_asset in zip(route.assets, route.assets[1:], strict=False):
                direct = f"{from_asset}-{to_asset}"
                inverse = f"{to_asset}-{from_asset}"
                add_market(direct if direct in market_map else inverse)

        quotes = {item.strip().upper() for item in self.settings.track_quote_currencies if item.strip()}
        fallback = [market for market in market_list if market.quote.upper() in quotes]
        fallback.sort(key=lambda item: (preferred.get(item.market, 99), item.quote, item.base))
        for market in fallback:
            add_market(market.market)

        return selected[: self.settings.book_markets_limit]

    @staticmethod
    def _to_market_metadata(payload: dict) -> MarketMetadata:
        min_order_in_quote = payload.get("minOrderInQuoteAsset")
        min_order_in_base = payload.get("minOrderInBaseAsset")
        tick_size = payload.get("tickSize")
        return MarketMetadata(
            market=payload["market"],
            base=payload["base"],
            quote=payload["quote"],
            status=payload.get("status", "unknown"),
            price_decimals=payload.get("priceDecimals"),
            quantity_decimals=payload.get("quantityDecimals"),
            min_order_in_quote=Decimal(min_order_in_quote) if min_order_in_quote else None,
            min_order_in_base=Decimal(min_order_in_base) if min_order_in_base else None,
            tick_size=Decimal(tick_size) if tick_size else None,
        )
