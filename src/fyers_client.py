"""
Fyers v3 historical data client, using the official fyers_apiv3 SDK.

Auth: reads FYERS_APP_ID / FYERS_ACCESS_TOKEN from environment (set these as
GitHub Actions secrets - see .github/workflows/backtest.yml).

Caches every symbol/date/resolution combo to a local JSON file so repeated
backtest runs don't re-hit the API (also keeps you under rate limits).

NOTE: Fyers typically only retains intraday option-chain historical data for
a limited lookback window (commonly ~60-90 days for options, longer for the
index/equity itself). If a request for an old expiry comes back empty, that's
likely why - not a bug in this client.
"""

import json
import os
import time

import config

try:
    from fyers_apiv3 import fyersModel
except ImportError:
    fyersModel = None


class FyersHistoryClient:
    def __init__(self):
        self.app_id = os.environ.get(config.FYERS_APP_ID_ENV)
        self.access_token = os.environ.get(config.FYERS_ACCESS_TOKEN_ENV)
        self._fyers = None
        os.makedirs(config.CACHE_DIR, exist_ok=True)

    def _client(self):
        if self._fyers is None:
            if fyersModel is None:
                raise RuntimeError(
                    "fyers_apiv3 not installed. Run: pip install fyers-apiv3"
                )
            if not self.app_id or not self.access_token:
                raise RuntimeError(
                    f"Missing {config.FYERS_APP_ID_ENV} / {config.FYERS_ACCESS_TOKEN_ENV} "
                    "environment variables."
                )
            self._fyers = fyersModel.FyersModel(
                client_id=self.app_id,
                is_async=False,
                token=self.access_token,
                log_path="",
            )
        return self._fyers

    def _cache_path(self, symbol, date_str, resolution):
        safe_symbol = symbol.replace(":", "_").replace("/", "_")
        return os.path.join(
            config.CACHE_DIR, f"{safe_symbol}_{date_str}_{resolution}.json"
        )

    def get_day_candles(self, symbol, date_str, resolution=None, use_cache=True,
                         max_retries=3):
        """
        Fetch 1-minute (or given resolution) candles for `symbol` on `date_str`
        (YYYY-MM-DD). Returns a list of dicts:
            [{"epoch": int, "open": f, "high": f, "low": f, "close": f, "volume": int}, ...]
        sorted ascending by epoch. Returns [] if no data (e.g. holiday, or
        history not available for that far back).
        """
        resolution = resolution or config.CANDLE_RESOLUTION
        cache_path = self._cache_path(symbol, date_str, resolution)

        if use_cache and os.path.exists(cache_path):
            with open(cache_path, "r") as f:
                return json.load(f)

        data = {
            "symbol": symbol,
            "resolution": resolution,
            "date_format": "1",       # 1 = yyyy-mm-dd strings for range_from/to
            "range_from": date_str,
            "range_to": date_str,
            "cont_flag": "1",
        }

        candles = []
        last_err = None
        for attempt in range(max_retries):
            try:
                resp = self._client().history(data=data)
                if resp.get("s") == "ok":
                    for c in resp.get("candles", []):
                        candles.append({
                            "epoch": int(c[0]),
                            "open": float(c[1]),
                            "high": float(c[2]),
                            "low": float(c[3]),
                            "close": float(c[4]),
                            "volume": int(c[5]),
                        })
                    break
                elif resp.get("s") == "no_data":
                    break
                else:
                    last_err = resp
                    time.sleep(1.5 * (attempt + 1))
            except Exception as e:  # noqa: BLE001 - surface & retry
                last_err = e
                time.sleep(1.5 * (attempt + 1))
        else:
            if last_err is not None:
                print(f"WARNING: history fetch failed for {symbol} {date_str}: {last_err}")

        candles.sort(key=lambda c: c["epoch"])

        with open(cache_path, "w") as f:
            json.dump(candles, f)

        return candles
