from app.collector.orderbook import OrderBookStore


def test_orderbook_updates_keep_best_prices_sorted():
    store = OrderBookStore()
    store.snapshot("BTC-EUR", [["10", "1"], ["9", "2"]], [["11", "1"], ["12", "2"]], 1)
    store.apply_update("BTC-EUR", [["10", "0"], ["10.5", "3"]], [["11", "0"], ["10.8", "4"]], 2)

    book = store.get("BTC-EUR")

    assert book is not None
    assert str(book.best_bid.price) == "10.5"
    assert str(book.best_ask.price) == "10.8"

