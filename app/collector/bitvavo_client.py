import json
from typing import Any

import httpx
import websockets


class BitvavoPublicClient:
    def __init__(self, rest_url: str, ws_url: str, timeout_seconds: float = 10.0) -> None:
        self.rest_url = rest_url.rstrip("/")
        self.ws_url = ws_url
        self.timeout_seconds = timeout_seconds

    async def get_markets(self) -> list[dict[str, Any]]:
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.get(f"{self.rest_url}/markets")
            response.raise_for_status()
            return response.json()

    async def get_book(self, market: str, depth: int) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.get(
                f"{self.rest_url}/{market}/book",
                params={"depth": depth},
            )
            response.raise_for_status()
            return response.json()

    def websocket(self):
        return websockets.connect(self.ws_url, ping_interval=20, ping_timeout=20)

    @staticmethod
    async def subscribe_books(websocket: Any, markets: list[str]) -> None:
        message = {
            "action": "subscribe",
            "channels": [{"name": "book", "markets": markets}],
        }
        await websocket.send(json.dumps(message))
