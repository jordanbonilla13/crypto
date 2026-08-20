from datetime import datetime, timezone

from fastapi import APIRouter, Request


api_router = APIRouter()


@api_router.get("/health")
async def health(request: Request) -> dict:
    database = request.app.state.database
    collector = request.app.state.collector
    collector_state = collector.current_state()
    database_ok = await database.ping()

    last_update_seconds = None
    if collector_state.last_market_update_at is not None:
        last_update_seconds = round(
            (datetime.now(timezone.utc) - collector_state.last_market_update_at).total_seconds(),
            3,
        )

    return {
        "database": "ok" if database_ok else "error",
        "collector": "ok" if collector_state.connected else "degraded",
        "last_market_update_seconds": last_update_seconds,
        "mode": request.app.state.collector.settings.operation_mode,
    }


@api_router.get("/api/v1/dashboard/summary")
async def dashboard_summary(request: Request) -> dict:
    collector = request.app.state.collector
    return collector.build_dashboard_summary()


@api_router.get("/api/v1/overview")
async def overview(request: Request) -> dict:
    return await request.app.state.collector.build_overview_payload()


@api_router.get("/api/v1/markets")
async def markets(request: Request) -> dict:
    return await request.app.state.collector.build_markets_payload()


@api_router.get("/api/v1/opportunities")
async def opportunities(request: Request) -> dict:
    return request.app.state.collector.build_opportunities_payload()


@api_router.get("/api/v1/experiments")
async def experiments(request: Request) -> dict:
    return await request.app.state.collector.build_experiments_payload()


@api_router.get("/api/v1/statistics")
async def statistics(request: Request) -> dict:
    return await request.app.state.collector.build_statistics_payload()


@api_router.get("/api/v1/system")
async def system(request: Request) -> dict:
    return await request.app.state.collector.build_system_payload()


@api_router.get("/api/v1/configuration")
async def configuration(request: Request) -> dict:
    return request.app.state.collector.build_configuration_payload()
