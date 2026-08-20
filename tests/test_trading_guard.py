import pytest

from app.common.settings import Settings
from app.executor.trading_guard import StrategyApproval, TradingGuard


def test_simulation_mode_blocks_real_orders():
    settings = Settings(MODO_OPERACION="simulacion", OPERACION_REAL_HABILITADA=True, PARADA_EMERGENCIA=False)
    guard = TradingGuard(settings)

    with pytest.raises(PermissionError):
        guard.assert_real_order_allowed(StrategyApproval(strategy_name="triangular", approved_for_real=True))


def test_strategy_must_be_explicitly_approved():
    settings = Settings(MODO_OPERACION="real", OPERACION_REAL_HABILITADA=True, PARADA_EMERGENCIA=False)
    guard = TradingGuard(settings)

    with pytest.raises(PermissionError):
        guard.assert_real_order_allowed(StrategyApproval(strategy_name="triangular", approved_for_real=False))

