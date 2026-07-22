"""
Fyers v3 WebSocket client - builds 1-minute OHLCV candles LOCALLY from a live
tick stream, for live_notifier_local.py running continuously on your own
machine. No REST polling, no GitHub Actions.

Based on Fyers' official sample pattern (fyers_apiv3.FyersWebsocket.data_ws,
"SymbolUpdate" mode): https://github.com/FyersDev/fyers-api-sample-code

IMPORTANT - things worth double-checking on your machine before relying on
this for real trading:
  - Fyers access tokens are valid for that trading day only. You need to
    regenerate FYERS_ACCESS_TOKEN each morning before running this, same as
    your other Fyers scripts.
  - The exact field names in a "SymbolUpdate" tick (ltp / vol_traded_today
    etc.) have varied slightly across fyers-apiv3 versions in reports online.
    _parse_tick() below tries a couple of common key-name variants
    defensively, but the first time you run this, watch the console for a
    few seconds - if you see "WARNING: ws tick missing ltp/volume keys",
    print the raw `message` dict once to see the exact keys your installed
    version sends, and adjust _parse_tick() accordingly.
  - Per-minute volume is derived by diffing Fyers' cumulative "volume traded
    today" figure across the minute boundary - if a tick is missed right at
    a minute boundary, that minute's volume can be slightly off. This only
    affects VWAP's volume weighting, not the price itself.
"""

import os
import threading
import time

import config

try:
    from fyers_apiv3.FyersWebsocket import data_ws
except ImportError:
    data_ws = None


class FyersWSClient:
    def __init__(self):
        self.app_id = os.environ.get(config.FYERS_APP_ID_ENV)
        self.access_token = os.environ.get(config.FYERS_ACCESS_TOKEN_ENV)
        if not self.app_id or not self.access_token:
            raise RuntimeError(
                f"Missing {config.FYERS_APP_ID_ENV} / {config.FYERS_ACCESS_TOKEN_ENV} "
                "environment variables."
            )
        if data_ws is None:
            raise RuntimeError("fyers_apiv3 not installed. Run: pip install fyers-apiv3")

        self._ws = None
        self._initial_symbols = []
        self._lock = threading.Lock()
        self._connected = threading.Event()

        # in-progress (not yet finalized) minute bar per symbol
        self._current_bar = {}
        # finalized 1-min candles today, per symbol: [{"epoch","open","high","low","close","volume"}, ...]
        self._candles = {}
        # last-seen cumulative "volume traded today" per symbol, for diffing into per-minute volume
        self._last_day_vol = {}

    # --- tick parsing --------------------------------------------------------

    @staticmethod
    def _parse_tick(message):
        """Best-effort extraction of (symbol, ltp, cumulative_day_volume) from
        a SymbolUpdate message. See the module docstring - verify against
        your actual payload if this starts warning."""
        symbol = message.get("symbol")
        ltp = message.get("ltp", message.get("last_traded_price"))
        cum_vol = message.get("vol_traded_today", message.get("tot_traded_qty"))
        return symbol, ltp, cum_vol

    def _on_message(self, message):
        try:
            symbol, ltp, cum_vol = self._parse_tick(message)
            if not symbol or ltp is None:
                print(f"WARNING: ws tick missing symbol/ltp keys: {message}")
                return
            ltp = float(ltp)
            cum_vol = float(cum_vol) if cum_vol is not None else None
            minute_epoch = (int(time.time()) // 60) * 60

            with self._lock:
                cur = self._current_bar.get(symbol)
                if cur is None or cur["minute"] != minute_epoch:
                    if cur is not None:
                        self._finalize_bar_locked(symbol, cur)
                    vol_at_open = self._last_day_vol.get(
                        symbol, cum_vol if cum_vol is not None else 0.0)
                    self._current_bar[symbol] = {
                        "minute": minute_epoch,
                        "open": ltp, "high": ltp, "low": ltp, "close": ltp,
                        "vol_at_open": vol_at_open,
                    }
                else:
                    if ltp > cur["high"]:
                        cur["high"] = ltp
                    if ltp < cur["low"]:
                        cur["low"] = ltp
                    cur["close"] = ltp

                if cum_vol is not None:
                    self._last_day_vol[symbol] = cum_vol
        except Exception as e:  # noqa: BLE001 - never let a bad tick kill the socket
            print(f"WARNING: ws on_message error: {e} (raw: {message})")

    def _finalize_bar_locked(self, symbol, bar):
        """Caller must hold self._lock."""
        vol_at_open = bar["vol_at_open"] or 0.0
        vol_at_close = self._last_day_vol.get(symbol, vol_at_open)
        minute_volume = max(0.0, vol_at_close - vol_at_open)
        self._candles.setdefault(symbol, []).append({
            "epoch": bar["minute"],
            "open": bar["open"], "high": bar["high"], "low": bar["low"],
            "close": bar["close"], "volume": minute_volume,
        })

    # --- connection lifecycle -------------------------------------------------

    def _on_connect(self):
        print("Fyers WebSocket connected.")
        self._connected.set()
        if self._initial_symbols:
            print(f"Subscribing to: {self._initial_symbols}")
            self._ws.subscribe(symbols=self._initial_symbols, data_type="SymbolUpdate")
        self._ws.keep_running()

    def _on_error(self, message):
        print(f"WS ERROR: {message}")

    def _on_close(self, message):
        print(f"WS CLOSED: {message}")
        self._connected.clear()

    def connect(self, initial_symbols=None, timeout=15):
        self._initial_symbols = initial_symbols or []
        token = f"{self.app_id}:{self.access_token}"
        self._ws = data_ws.FyersDataSocket(
            access_token=token,
            log_path="",
            litemode=False,
            write_to_file=False,
            reconnect=True,
            on_connect=self._on_connect,
            on_close=self._on_close,
            on_error=self._on_error,
            on_message=self._on_message,
        )
        thread = threading.Thread(target=self._ws.connect, daemon=True)
        thread.start()
        self._connected.wait(timeout=timeout)

    def is_connected(self):
        return self._connected.is_set()

    def subscribe(self, symbols):
        if symbols:
            self._ws.subscribe(symbols=symbols, data_type="SymbolUpdate")

    def unsubscribe(self, symbols):
        if not symbols:
            return
        try:
            self._ws.unsubscribe(symbols=symbols, data_type="SymbolUpdate")
        except Exception as e:  # noqa: BLE001 - harmless if we're about to resubscribe elsewhere
            print(f"WARNING: unsubscribe failed (usually harmless): {e}")

    # --- reading accumulated data ----------------------------------------------

    def flush_current_bar(self, symbol):
        """Force-finalize symbol's in-progress bar right now, even if its
        minute hasn't technically closed yet. Used at the strike-fix moment
        and at square-off, where we want 'price right now', not 'price as of
        the last fully-closed minute'."""
        with self._lock:
            cur = self._current_bar.get(symbol)
            if cur:
                self._finalize_bar_locked(symbol, dict(cur))
                # keep the (still-live) current bar in place too - don't drop it,
                # future ticks in the same minute should keep updating it normally.

    def get_candles(self, symbol):
        with self._lock:
            return list(self._candles.get(symbol, []))

    def get_latest_ltp(self, symbol):
        with self._lock:
            cur = self._current_bar.get(symbol)
            if cur:
                return cur["close"]
            candles = self._candles.get(symbol)
            return candles[-1]["close"] if candles else None
