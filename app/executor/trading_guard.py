from dataclasses import dataclass

from app.common.settings import Settings


@dataclass(slots=True)
class StrategyApproval:
    strategy_name: str
    approved_for_real: bool = False


class TradingGuard:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def assert_real_order_allowed(self, strategy: StrategyApproval) -> None:
        if self.settings.operation_mode.lower() != "real":
            raise PermissionError("Real trading blocked: MODO_OPERACION no es real.")
        if not self.settings.real_operation_enabled:
            raise PermissionError("Real trading blocked: OPERACION_REAL_HABILITADA=false.")
        if self.settings.emergency_stop:
            raise PermissionError("Real trading blocked: PARADA_EMERGENCIA=true.")
        if not strategy.approved_for_real:
            raise PermissionError("Real trading blocked: estrategia no aprobada para prueba real.")

