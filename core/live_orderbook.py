"""Real-time order book client using Polymarket's public CLOB WebSocket.

Replaces REST polling (core/clob_client.get_order_book, called once every
60s in the old market_maker_bot) with push-based updates -- confirmed via
direct testing to fire many times per second on an active market. This is
the actual fix for the adverse-selection problem diagnosed in
BUILD_INTELLIGENCE_REPORT.md Session 13: a quote that's up to 60 seconds
stale gets picked off regardless of how cleverly it's priced, because the
problem is the *information*, not the math built on top of it.

No authentication required (same as the REST CLOB reads). Confirmed via
direct testing:
  URL: wss://ws-subscriptions-clob.polymarket.com/ws/market
  Subscribe: {"assets_ids": [...], "type": "market"}
  "book" events arrive as a JSON ARRAY of book objects (bids/asks snapshot)
  "price_change" events arrive as a single JSON object (incremental delta,
  includes best_bid/best_ask directly -- we use these for the live state
  rather than reconstructing the book from individual price levels, since
  best bid/ask is all market_maker_bot's quoting logic actually needs)
"""
from __future__ import annotations

import json
import logging
import threading
import time

import websocket

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
RECONNECT_DELAY_SECONDS = 5

log = logging.getLogger(__name__)


class LiveOrderBook:
    """Maintains best-bid/best-ask for a set of token_ids via a background
    WebSocket connection. Thread-safe; the connection runs on its own
    thread so the rest of a bot's scan loop is unaffected by it.

    Usage:
        book = LiveOrderBook()
        book.start()
        book.subscribe(["1234...", "5678..."])
        bid, ask = book.best_bid_ask("1234...")
        book.stop()
    """

    def __init__(self, ws_url: str = WS_URL):
        self.ws_url = ws_url
        self._lock = threading.Lock()
        self._best: dict[str, dict] = {}  # token_id -> {"bid": float, "ask": float, "updated_at": float}
        self._subscribed: set[str] = set()
        self._ws: websocket.WebSocketApp | None = None
        self._thread: threading.Thread | None = None
        self._stop_requested = False

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop_requested = False
        self._thread = threading.Thread(target=self._run_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_requested = True
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass

    def subscribe(self, token_ids: list[str]) -> None:
        new_ids = [t for t in token_ids if t not in self._subscribed]
        if not new_ids:
            return
        self._subscribed.update(new_ids)
        if self._ws is not None:
            try:
                self._ws.send(json.dumps({"assets_ids": new_ids, "type": "market"}))
            except Exception as exc:
                log.warning("subscribe send failed (will resubscribe on reconnect): %s", exc)

    def unsubscribe(self, token_ids: list[str]) -> None:
        ids = [t for t in token_ids if t in self._subscribed]
        if not ids:
            return
        self._subscribed.difference_update(ids)
        with self._lock:
            for tid in ids:
                self._best.pop(tid, None)
        if self._ws is not None:
            try:
                self._ws.send(json.dumps({"operation": "unsubscribe", "assets_ids": ids}))
            except Exception as exc:
                log.warning("unsubscribe send failed: %s", exc)

    def best_bid_ask(self, token_id: str, max_age_seconds: float = 30.0) -> tuple[float | None, float | None]:
        """Returns (bid, ask) from the live feed, or (None, None) if we
        have no data yet or it's gone stale (e.g. the connection dropped
        silently) -- max_age_seconds is a safety net, not the normal path."""
        with self._lock:
            entry = self._best.get(token_id)
        if entry is None:
            return None, None
        if time.time() - entry["updated_at"] > max_age_seconds:
            return None, None
        return entry["bid"], entry["ask"]

    def _run_forever(self) -> None:
        while not self._stop_requested:
            try:
                self._ws = websocket.WebSocketApp(
                    self.ws_url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                )
                self._ws.run_forever(ping_interval=10, ping_payload="PING")
            except Exception as exc:
                log.warning("WebSocket connection error: %s", exc)
            if not self._stop_requested:
                time.sleep(RECONNECT_DELAY_SECONDS)

    def _on_open(self, ws) -> None:
        log.info("WebSocket connected")
        if self._subscribed:
            ws.send(json.dumps({"assets_ids": sorted(self._subscribed), "type": "market"}))

    def _on_error(self, ws, error) -> None:
        log.warning("WebSocket error: %s", error)

    def _on_message(self, ws, message: str) -> None:
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            return

        events = data if isinstance(data, list) else [data]
        now = time.time()

        for event in events:
            event_type = event.get("event_type")

            if event_type == "book":
                asset_id = event.get("asset_id")
                bids = event.get("bids") or []
                asks = event.get("asks") or []
                if not asset_id or not bids or not asks:
                    continue
                best_bid = max(float(level["price"]) for level in bids)
                best_ask = min(float(level["price"]) for level in asks)
                self._update_best(asset_id, best_bid, best_ask, now)

            elif event_type == "price_change":
                for change in event.get("price_changes", []):
                    asset_id = change.get("asset_id")
                    best_bid = change.get("best_bid")
                    best_ask = change.get("best_ask")
                    if not asset_id or best_bid is None or best_ask is None:
                        continue
                    self._update_best(asset_id, float(best_bid), float(best_ask), now)

    def _update_best(self, token_id: str, bid: float, ask: float, timestamp: float) -> None:
        with self._lock:
            self._best[token_id] = {"bid": bid, "ask": ask, "updated_at": timestamp}
