from app.common.settings import Settings


def test_settings_parse_quote_currencies():
    settings = Settings(TRACK_QUOTE_CURRENCIES="EUR,USDC,BTC")
    assert settings.track_quote_currencies == ["EUR", "USDC", "BTC"]

