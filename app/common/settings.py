from functools import lru_cache
from decimal import Decimal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        enable_decoding=False,
        extra="ignore",
    )

    operation_mode: str = Field(default="simulacion", alias="MODO_OPERACION")
    real_operation_enabled: bool = Field(default=False, alias="OPERACION_REAL_HABILITADA")
    emergency_stop: bool = Field(default=False, alias="PARADA_EMERGENCIA")
    initial_currency: str = Field(default="EUR", alias="MONEDA_INICIAL")
    simulated_capitals: list[Decimal] = Field(default=[Decimal("5"), Decimal("10"), Decimal("20")], alias="CAPITALES_SIMULADOS")
    simulated_taker_fee: Decimal = Field(default=Decimal("0.0025"), alias="COMISION_SIMULADA_TOMADOR")
    simulated_maker_fee: Decimal = Field(default=Decimal("0.0015"), alias="COMISION_SIMULADA_CREADOR")
    safety_margin_pct: Decimal = Field(default=Decimal("0.10"), alias="MARGEN_SEGURIDAD_PCT")
    latency_scenarios_ms: list[int] = Field(default=[0, 250, 750, 1500], alias="LATENCY_SCENARIOS_MS")
    max_operation_capital_eur: Decimal = Field(default=Decimal("5"), alias="CAPITAL_MAXIMO_OPERACION_EUR")
    max_total_exposure_eur: Decimal = Field(default=Decimal("5"), alias="EXPOSICION_MAXIMA_TOTAL_EUR")
    max_daily_loss_eur: Decimal = Field(default=Decimal("1"), alias="PERDIDA_MAXIMA_DIARIA_EUR")
    max_operations_per_day: int = Field(default=5, alias="NUMERO_MAXIMO_OPERACIONES_DIA")
    telegram_active: bool = Field(default=False, alias="TELEGRAM_ACTIVO")

    postgres_db: str = Field(default="bitvavo_lab", alias="POSTGRES_DB")
    postgres_user: str = Field(default="bitvavo", alias="POSTGRES_USER")
    postgres_password: str = Field(default="cambiar", alias="POSTGRES_PASSWORD")
    postgres_host: str = Field(default="db", alias="POSTGRES_HOST")
    postgres_port: int = Field(default=5432, alias="POSTGRES_PORT")

    bitvavo_rest_url: str = Field(default="https://api.bitvavo.com/v2", alias="BITVAVO_REST_URL")
    bitvavo_ws_url: str = Field(default="wss://ws.bitvavo.com/v2/", alias="BITVAVO_WS_URL")
    track_quote_currencies: list[str] = Field(default=["EUR", "USDC"], alias="TRACK_QUOTE_CURRENCIES")
    book_markets_limit: int = Field(default=25, alias="BOOK_MARKETS_LIMIT")
    orderbook_depth: int = Field(default=10, alias="ORDERBOOK_DEPTH")
    snapshot_interval_seconds: int = Field(default=15, alias="SNAPSHOT_INTERVAL_SECONDS")
    market_making_min_spread_pct: Decimal = Field(default=Decimal("0.08"), alias="MARKET_MAKING_MIN_SPREAD_PCT")
    market_making_max_spread_pct: Decimal = Field(default=Decimal("0.80"), alias="MARKET_MAKING_MAX_SPREAD_PCT")
    market_making_min_depth_quote: Decimal = Field(default=Decimal("5000"), alias="MARKET_MAKING_MIN_DEPTH_QUOTE")
    market_making_signal_limit: int = Field(default=12, alias="MARKET_MAKING_SIGNAL_LIMIT")
    microstructure_signal_limit: int = Field(default=24, alias="MICROSTRUCTURE_SIGNAL_LIMIT")
    microstructure_discovery_partition_size: int = Field(default=1000, alias="MICROSTRUCTURE_DISCOVERY_PARTITION_SIZE")
    microstructure_min_depth_quote: Decimal = Field(default=Decimal("2500"), alias="MICROSTRUCTURE_MIN_DEPTH_QUOTE")
    microstructure_imbalance_ratio_threshold: Decimal = Field(default=Decimal("5.0"), alias="MICROSTRUCTURE_IMBALANCE_RATIO_THRESHOLD")
    microstructure_liquidity_vanish_threshold_pct: Decimal = Field(default=Decimal("70"), alias="MICROSTRUCTURE_LIQUIDITY_VANISH_THRESHOLD_PCT")
    microstructure_jump_threshold_pct: Decimal = Field(default=Decimal("0.18"), alias="MICROSTRUCTURE_JUMP_THRESHOLD_PCT")
    microstructure_large_order_share_threshold_pct: Decimal = Field(default=Decimal("55"), alias="MICROSTRUCTURE_LARGE_ORDER_SHARE_THRESHOLD_PCT")
    microstructure_depth_change_threshold_pct: Decimal = Field(default=Decimal("45"), alias="MICROSTRUCTURE_DEPTH_CHANGE_THRESHOLD_PCT")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    @field_validator("track_quote_currencies", mode="before")
    @classmethod
    def split_quotes(cls, value):
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("simulated_capitals", mode="before")
    @classmethod
    def split_capitals(cls, value):
        if isinstance(value, str):
            return [Decimal(item.strip()) for item in value.split(",") if item.strip()]
        return value

    @field_validator("latency_scenarios_ms", mode="before")
    @classmethod
    def split_latency_scenarios(cls, value):
        if isinstance(value, str):
            return [int(item.strip()) for item in value.split(",") if item.strip()]
        return value

    @property
    def primary_latency_ms(self) -> int:
        return max(self.latency_scenarios_ms) if self.latency_scenarios_ms else 0

    @property
    def database_dsn(self) -> str:
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
