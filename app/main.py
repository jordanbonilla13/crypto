from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import api_router
from app.collector.service import CollectorService
from app.common.logging import configure_logging, get_logger
from app.common.settings import get_settings
from app.database.core import Database


settings = get_settings()
configure_logging(settings.log_level)
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    database = Database(settings)
    await database.connect()
    collector = CollectorService(settings=settings, database=database)
    await collector.start()

    app.state.database = database
    app.state.collector = collector

    logger.info("application_started")
    try:
        yield
    finally:
        await collector.stop()
        await database.close()
        logger.info("application_stopped")


app = FastAPI(title="Bitvavo Lab", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(api_router)

