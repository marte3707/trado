from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from importlib import import_module
from typing import Any

import httpx

from app.core.config import Settings
from app.core.enums import MarketType
from app.services._circuit import hyperliquid_info_breaker


@dataclass(frozen=True)
class FundingRate:
    coin: str
    predicted_rate: float  # 8h rate as decimal fraction (e.g. 0.0001 = 0.01%/8h)


@dataclass(frozen=True)
class OrderBookLevel:
    price: float
    size: float


@dataclass(frozen=True)
class L2BookSnapshot:
    bids: list[OrderBookLevel]  # ordinati per prezzo decrescente
    asks: list[OrderBookLevel]  # ordinati per prezzo crescente


def fetch_l2_book(info_url: str, coin: str, n_levels: int = 20) -> L2BookSnapshot | None:
    """Ritorna snapshot L2 dell'order book per `coin`. None in caso di errore."""
    try:
        with httpx.Client(timeout=5) as client:
            response = client.post(
                info_url,
                json={"type": "l2Book", "coin": coin, "nLevels": n_levels},
            )
            if response.is_error:
                return None
            data = response.json()
            raw_levels = data.get("levels", [])
            if len(raw_levels) < 2:
                return None
            bids = [
                OrderBookLevel(price=float(level["px"]), size=float(level["sz"]))
                for level in raw_levels[0]
            ]
            asks = [
                OrderBookLevel(price=float(level["px"]), size=float(level["sz"]))
                for level in raw_levels[1]
            ]
            return L2BookSnapshot(bids=bids, asks=asks)
    except Exception:
        return None


def fetch_predicted_fundings(info_url: str) -> dict[str, float]:
    """Returns coin → predicted 8h funding rate (decimal fraction). Empty dict on failure."""
    try:
        with httpx.Client(timeout=10) as client:
            response = client.post(info_url, json={"type": "predictedFundings"})
            if response.is_error:
                return {}
            data = response.json()
            result: dict[str, float] = {}
            for entry in data:
                if not isinstance(entry, list) or len(entry) < 2:
                    continue
                coin = entry[0]
                venues = entry[1]
                if not isinstance(venues, list):
                    continue
                for venue_entry in venues:
                    if isinstance(venue_entry, list) and len(venue_entry) >= 2:
                        try:
                            result[coin] = float(venue_entry[1])
                        except (ValueError, TypeError):
                            pass
                        break
            return result
    except Exception:
        return {}


def fetch_perp_open_interests(info_url: str) -> dict[str, float]:
    """Returns coin → open interest in USD notional. Empty dict on failure. Perpetuals only."""
    try:
        with httpx.Client(timeout=10) as client:
            response = client.post(info_url, json={"type": "metaAndAssetCtxs"})
            if response.is_error:
                return {}
            data = response.json()
            if not isinstance(data, list) or len(data) < 2:
                return {}
            meta, asset_ctxs = data[0], data[1]
            universe = meta.get("universe", [])
            result: dict[str, float] = {}
            for asset, ctx in zip(universe, asset_ctxs):
                coin = asset.get("name", "")
                oi_str = ctx.get("openInterest")
                if coin and oi_str is not None:
                    try:
                        result[coin] = float(oi_str)
                    except (ValueError, TypeError):
                        pass
            return result
    except Exception:
        return {}


class HyperliquidError(RuntimeError):
    pass


class HyperliquidTransientError(HyperliquidError):
    pass


class HyperliquidClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def _post_info(self, payload: dict[str, Any]) -> Any:
        if not hyperliquid_info_breaker.allow():
            raise HyperliquidTransientError(
                "Hyperliquid info circuit breaker is open"
            )
        try:
            result = await self._post_info_raw(payload)
        except HyperliquidTransientError:
            hyperliquid_info_breaker.record_failure()
            raise
        except HyperliquidError:
            # non-transient (4xx other than rate-limit, etc.) should not trip the breaker
            raise
        else:
            hyperliquid_info_breaker.record_success()
            return result

    async def _post_info_raw(self, payload: dict[str, Any]) -> Any:
        async with httpx.AsyncClient(timeout=20) as client:
            last_status: int | None = None
            last_text = ""
            for attempt in range(4):
                response = await client.post(self.settings.info_url, json=payload)
                if response.status_code in {429, 500, 502, 503, 504}:
                    last_status = response.status_code
                    last_text = response.text[:500]
                    await asyncio.sleep(0.5 * (2**attempt))
                    continue
                if response.is_error:
                    raise HyperliquidError(f"Hyperliquid info error {response.status_code}: {response.text}")
                return response.json()
        raise HyperliquidTransientError(
            f"Hyperliquid info request failed after retries status={last_status} body={last_text}"
        )

    async def meta(self) -> dict[str, Any]:
        return await self._post_info({"type": "meta"})

    async def spot_meta(self) -> dict[str, Any]:
        return await self._post_info({"type": "spotMeta"})

    async def mids(self) -> dict[str, str]:
        return await self._post_info({"type": "allMids"})

    async def candles(self, coin: str, interval: str, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
        return await self._post_info(
            {
                "type": "candleSnapshot",
                "req": {"coin": coin, "interval": interval, "startTime": start_ms, "endTime": end_ms},
            }
        )

    async def user_state(self, address: str) -> dict[str, Any]:
        return await self._post_info({"type": "clearinghouseState", "user": address})

    async def spot_user_state(self, address: str) -> dict[str, Any]:
        return await self._post_info({"type": "spotClearinghouseState", "user": address})

    async def open_orders(self, address: str) -> list[dict[str, Any]]:
        return await self._post_info({"type": "openOrders", "user": address})

    async def frontend_open_orders(self, address: str) -> list[dict[str, Any]]:
        return await self._post_info({"type": "frontendOpenOrders", "user": address})

    async def predicted_fundings(self) -> list[Any]:
        return await self._post_info({"type": "predictedFundings"})

    async def portfolio(self, address: str) -> list[Any]:
        """Returns portfolio history per period (day/week/month/allTime + perp variants).

        Each item is [period_name, {accountValueHistory: [[ts_ms, value], ...], pnlHistory: [...], vlm: ...}].
        """
        return await self._post_info({"type": "portfolio", "user": address})

    async def user_funding(self, address: str, start_ms: int, end_ms: int | None = None) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"type": "userFunding", "user": address, "startTime": start_ms}
        if end_ms is not None:
            payload["endTime"] = end_ms
        return await self._post_info(payload)

    @staticmethod
    def perp_markets(meta: dict[str, Any]) -> list[dict[str, Any]]:
        markets: list[dict[str, Any]] = []
        for index, item in enumerate(meta.get("universe", [])):
            symbol = item["name"]
            markets.append(
                {
                    "symbol": symbol,
                    "base": symbol.split(":")[-1],
                    "quote": "USDC",
                    "market_type": MarketType.PERP.value,
                    "asset_id": index,
                    "hl_coin": symbol,
                    "raw": item,
                }
            )
        return markets

    @staticmethod
    def spot_markets(spot_meta: dict[str, Any]) -> list[dict[str, Any]]:
        tokens = {token["index"]: token for token in spot_meta.get("tokens", [])}
        markets: list[dict[str, Any]] = []
        for item in spot_meta.get("universe", []):
            base_token = tokens.get(item.get("tokens", [None, None])[0], {})
            quote_token = tokens.get(item.get("tokens", [None, None])[1], {})
            hl_coin = item.get("name")
            symbol = hl_coin if "/" in hl_coin else f"{base_token.get('name', hl_coin)}/{quote_token.get('name', 'USDC')}"
            markets.append(
                {
                    "symbol": symbol,
                    "base": base_token.get("name", symbol.split("/")[0]),
                    "quote": quote_token.get("name", "USDC"),
                    "market_type": MarketType.SPOT.value,
                    "asset_id": 10000 + int(item["index"]),
                    "hl_coin": hl_coin,
                    "raw": item,
                }
            )
        return markets


@dataclass(frozen=True)
class HyperliquidOrderResult:
    status: str
    oid: str | None
    avg_price: Decimal | None
    filled_size: Decimal | None
    raw_response: dict[str, Any]
    fee: Decimal | None = None


class HyperliquidTradingClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def account_summary(self) -> tuple[Decimal, Decimal]:
        """Ritorna (total_account_value, available_margin).

        total_account_value = marginSummary.accountValue + free USDC spot.
        available_margin    = total_account_value - totalMarginUsed.

        Usare available_margin per il sizing delle nuove posizioni: con molte
        posizioni isolate aperte, totalMarginUsed può essere = accountValue e
        il margine libero effettivo è quasi zero. Usare il totale causerebbe
        size enormi che HL può solo parzialmente fillare.
        """
        address = self._account_address()
        state = self.user_state(address)
        margin = state.get("marginSummary", {})
        value = margin.get("accountValue")
        if value is None:
            raise HyperliquidError("Hyperliquid user_state did not include accountValue")
        account_val = Decimal(str(value))
        margin_used = Decimal(str(margin.get("totalMarginUsed", "0")))
        # Unified account: add free USDC from spot wallet (total − hold).
        try:
            spot_state = self.spot_user_state(address)
            for balance in spot_state.get("balances", []):
                if balance.get("coin") == "USDC":
                    total = Decimal(str(balance.get("total", "0")))
                    hold = Decimal(str(balance.get("hold", "0")))
                    free_usdc = total - hold
                    if free_usdc > 0:
                        account_val += free_usdc
                    break
        except Exception:
            pass
        available = max(Decimal("0"), account_val - margin_used)
        return account_val, available

    def account_value(self) -> Decimal:
        """Compatibilità: ritorna solo il valore totale del conto."""
        total, _ = self.account_summary()
        return total

    def spot_user_state(self, address: str | None = None) -> dict[str, Any]:
        info = self._info()
        addr = address or self._account_address()
        spot_method = getattr(info, "spot_user_state", None)
        if callable(spot_method):
            return spot_method(addr)
        import httpx
        response = httpx.post(
            self.settings.info_url,
            json={"type": "spotClearinghouseState", "user": addr},
            timeout=20,
        )
        response.raise_for_status()
        return response.json()

    def user_state(self, address: str | None = None) -> dict[str, Any]:
        info = self._info()
        return info.user_state(address or self._account_address())

    def user_fills(self, address: str | None = None) -> list[dict[str, Any]]:
        info = self._info()
        fills = info.user_fills(address or self._account_address())
        return fills if isinstance(fills, list) else []

    def open_orders(self, address: str | None = None) -> list[dict[str, Any]]:
        info = self._info()
        result = info.open_orders(address or self._account_address())
        return result if isinstance(result, list) else []

    def frontend_open_orders(self, address: str | None = None) -> list[dict[str, Any]]:
        info = self._info()
        result = info.frontend_open_orders(address or self._account_address())
        return result if isinstance(result, list) else []

    def all_mids(self) -> dict[str, str]:
        """Restituisce {coin: mid_price_str} per tutti i mercati HL.

        Fallback usato dalla reconciliation per derivare il mark price quando
        `assetPositions` non lo include (HL non lo restituisce sempre nel payload
        di user_state).
        """
        info = self._info()
        result = info.all_mids()
        return result if isinstance(result, dict) else {}

    def available_fill_notional(
        self,
        coin: str,
        is_buy: bool,
        slippage_pct: float,
        mid_price: float,
    ) -> Decimal:
        """Stima il notional USD fillabile al prezzo di slippage leggendo il book L2.

        Per un SHORT (is_buy=False): somma i bid con px >= limit_px.
        Per un LONG (is_buy=True): somma gli ask con px <= limit_px.
        Restituisce il notional totale fillabile. Ritorna Decimal('Inf') in
        caso di errore per non bloccare il trade in modo conservativo.
        """
        try:
            info = self._info()
            book = info.l2_snapshot(coin)
            levels = book.get("levels", [[], []])
            slippage_factor = slippage_pct / 100.0
            if is_buy:
                limit_px = mid_price * (1 + slippage_factor)
                side_levels = levels[1] if len(levels) > 1 else []
                total_size = sum(
                    float(lvl["sz"])
                    for lvl in side_levels
                    if float(lvl["px"]) <= limit_px
                )
            else:
                limit_px = mid_price * (1 - slippage_factor)
                side_levels = levels[0] if levels else []
                total_size = sum(
                    float(lvl["sz"])
                    for lvl in side_levels
                    if float(lvl["px"]) >= limit_px
                )
            return Decimal(str(total_size * mid_price))
        except Exception:
            return Decimal("Inf")

    def submit_market_order(
        self,
        coin: str,
        is_buy: bool,
        size: Decimal,
        max_slippage_pct: float,
        expires_after_seconds: int,
        cloid: str | None = None,
    ) -> HyperliquidOrderResult:
        exchange = self._exchange(expires_after_seconds)
        kwargs: dict[str, Any] = {"slippage": max(0.0, max_slippage_pct) / 100}
        cloid_obj = _build_cloid(cloid)
        if cloid_obj is not None:
            kwargs["cloid"] = cloid_obj
        response = exchange.market_open(coin, is_buy, _size_to_float(size), **kwargs)
        return parse_order_result(response)

    def submit_market_close(
        self,
        coin: str,
        is_buy: bool,
        size: Decimal,
        max_slippage_pct: float,
        expires_after_seconds: int,
    ) -> HyperliquidOrderResult:
        exchange = self._exchange(expires_after_seconds)
        response = exchange.order(
            coin,
            is_buy,
            _size_to_float(size),
            exchange._slippage_price(coin, is_buy, max(0.0, max_slippage_pct) / 100),
            order_type={"limit": {"tif": "Ioc"}},
            reduce_only=True,
        )
        return parse_order_result(response)

    def submit_trigger_order(
        self,
        coin: str,
        is_buy: bool,
        size: Decimal,
        trigger_price: Decimal,
        tpsl: str,
        expires_after_seconds: int,
        cloid: str | None = None,
    ) -> HyperliquidOrderResult:
        exchange = self._exchange(expires_after_seconds)
        rounded_price = _round_price_for_coin(trigger_price, coin, getattr(exchange, "info", None))
        order_kwargs: dict[str, Any] = {
            "order_type": {
                "trigger": {
                    "isMarket": True,
                    "triggerPx": rounded_price,
                    "tpsl": tpsl,
                }
            },
            "reduce_only": True,
        }
        cloid_obj = _build_cloid(cloid)
        if cloid_obj is not None:
            order_kwargs["cloid"] = cloid_obj
        response = exchange.order(coin, is_buy, _size_to_float(size), rounded_price, **order_kwargs)
        return parse_order_result(response)

    def set_leverage(self, coin: str, leverage: int, is_cross: bool = False) -> None:
        exchange = self._exchange(0)
        response = exchange.update_leverage(leverage, coin, is_cross)
        if isinstance(response, dict) and response.get("status") == "err":
            raise HyperliquidError(f"set_leverage failed: {response}")

    def predicted_fundings(self) -> dict[str, float]:
        return fetch_predicted_fundings(self.settings.info_url)

    def cancel_order(
        self,
        coin: str,
        oid: int,
        expires_after_seconds: int,
    ) -> dict[str, Any]:
        exchange = self._exchange(expires_after_seconds)
        response = exchange.cancel(coin, oid)
        if not isinstance(response, dict):
            raise HyperliquidError("Hyperliquid cancel returned an invalid response")
        if response.get("status") == "err":
            raise HyperliquidError(str(response))
        # HL returns status "ok" even when individual cancels fail — check inner statuses
        try:
            statuses = response["response"]["data"]["statuses"]
            for status in statuses:
                if isinstance(status, dict) and "error" in status:
                    raise HyperliquidError(f"Cancel order {oid} failed: {status['error']}")
        except (KeyError, TypeError):
            pass
        return response

    def _account_address(self) -> str:
        if not self.settings.hyperliquid_account_address:
            raise HyperliquidError("HYPERLIQUID_ACCOUNT_ADDRESS is required for live trading")
        return self.settings.hyperliquid_account_address

    def _private_key(self) -> str:
        if not self.settings.hyperliquid_private_key:
            raise HyperliquidError("HYPERLIQUID_PRIVATE_KEY is required for live trading")
        return self.settings.hyperliquid_private_key

    def _base_url(self) -> str:
        try:
            constants = import_module("hyperliquid.utils.constants")
        except ImportError as exc:
            raise HyperliquidError("Install hyperliquid-python-sdk to enable live trading") from exc
        if self.settings.hyperliquid_network == "mainnet":
            return constants.MAINNET_API_URL
        return constants.TESTNET_API_URL

    def _info(self):
        try:
            Info = import_module("hyperliquid.info").Info
        except ImportError as exc:
            raise HyperliquidError("Install hyperliquid-python-sdk to enable live trading") from exc
        return Info(self._base_url(), skip_ws=True)

    def _exchange(self, expires_after_seconds: int):
        try:
            eth_account = import_module("eth_account")
            Exchange = import_module("hyperliquid.exchange").Exchange
        except ImportError as exc:
            raise HyperliquidError("Install hyperliquid-python-sdk to enable live trading") from exc
        wallet = eth_account.Account.from_key(self._private_key())
        exchange = Exchange(
            wallet,
            base_url=self._base_url(),
            account_address=self._account_address(),
        )
        if expires_after_seconds > 0:
            exchange.set_expires_after(int(time.time() * 1000) + (expires_after_seconds * 1000))
        return exchange


def _size_to_float(size: Decimal, max_decimals: int = 8) -> float:
    """Quantize a Decimal size to a fixed number of decimals BEFORE converting
    to float, so binary-fp drift cannot leak into the order payload sent to
    Hyperliquid."""
    if size <= 0:
        return 0.0
    quant = Decimal(1).scaleb(-max_decimals)
    return float(size.quantize(quant))


def _build_cloid(cloid: str | None) -> Any | None:
    if not cloid:
        return None
    for module_path in (
        "hyperliquid.utils.types",
        "hyperliquid.utils.signing",
    ):
        try:
            module = import_module(module_path)
        except ImportError:
            continue
        Cloid = getattr(module, "Cloid", None)
        if Cloid is None:
            continue
        for ctor in ("from_str", "from_int"):
            factory = getattr(Cloid, ctor, None)
            if factory is None:
                continue
            try:
                return factory(cloid if ctor == "from_str" else int(cloid, 16))
            except Exception:
                continue
        try:
            return Cloid(cloid)
        except Exception:
            continue
    return None


def _round_price(price: Decimal, sig_figs: int = 5, max_decimals: int = 6) -> float:
    f = float(price)
    if f == 0.0:
        return 0.0
    # Mirror HL SDK _slippage_price: 5 significant figures then cap decimal places
    return round(float(f"{f:.{sig_figs}g}"), max_decimals)


def _price_decimals_for_coin(coin: str, info: Any | None) -> int | None:
    if info is None:
        return None
    try:
        name_to_coin = getattr(info, "name_to_coin", {})
        coin_to_asset = getattr(info, "coin_to_asset", {})
        asset_to_sz_decimals = getattr(info, "asset_to_sz_decimals", {})
        canonical_coin = name_to_coin.get(coin, coin)
        asset = coin_to_asset.get(canonical_coin)
        if asset is None:
            return None
        sz_decimals = asset_to_sz_decimals.get(asset)
        if not isinstance(sz_decimals, int):
            return None
        is_spot = asset >= 10_000
        return max(0, (8 if is_spot else 6) - sz_decimals)
    except Exception:
        return None


def _round_price_for_coin(price: Decimal, coin: str, info: Any | None, sig_figs: int = 5) -> float:
    max_decimals = _price_decimals_for_coin(coin, info)
    if max_decimals is None:
        return _round_price(price, sig_figs=sig_figs)
    return _round_price(price, sig_figs=sig_figs, max_decimals=max_decimals)


def parse_order_result(response: dict[str, Any]) -> HyperliquidOrderResult:
    status = str(response.get("status", "unknown"))
    oid: str | None = None
    avg_price: Decimal | None = None
    filled_size: Decimal | None = None
    statuses = (
        response.get("response", {})
        .get("data", {})
        .get("statuses", [])
    )
    fee: Decimal | None = None
    if statuses:
        first = statuses[0]
        if "filled" in first:
            filled = first["filled"]
            oid = str(filled.get("oid")) if filled.get("oid") is not None else None
            avg_price = _decimal(filled.get("avgPx"))
            filled_size = _decimal(filled.get("totalSz"))
            fee = _decimal(filled.get("fee"))
        elif "resting" in first:
            resting = first["resting"]
            oid = str(resting.get("oid")) if resting.get("oid") is not None else None
        elif "error" in first:
            status = "error"
    return HyperliquidOrderResult(
        status=status,
        oid=oid,
        avg_price=avg_price,
        filled_size=filled_size,
        raw_response=response,
        fee=fee,
    )


def _decimal(value: str | int | float | Decimal | None) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None

def parse_hl_candle(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "open_time": datetime.fromtimestamp(int(row["t"]) / 1000, tz=timezone.utc),
        "open": Decimal(str(row["o"])),
        "high": Decimal(str(row["h"])),
        "low": Decimal(str(row["l"])),
        "close": Decimal(str(row["c"])),
        "volume": Decimal(str(row["v"])),
    }
