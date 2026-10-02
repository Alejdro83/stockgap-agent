"""StockGap spread engine.

Three spreads for tokenized stocks (BSC mainnet, Binance Web3 API):

1. **gross spread**        — token implied share price vs. traditional-market
   reference price.
2. **net executable gap**  — the gross spread minus round-trip execution costs
   (fees, slippage, amortized gas) — what an arbitrageur can actually keep.
3. **cross-issuer spread** — the same underlying equity tokenized by different
   issuers/platforms trading at different implied share prices. Needs no
   reference price, so it works 24/7 even when US markets are closed.

Domain rule (verified live 2026-10-02; the docs are WRONG): the conversion
field is ``tokenToShareRatio`` (string decimal, shares per token) and

    implied share price = tokenPrice / tokenToShareRatio

The docs call it ``sharesMultiplier`` and suggest multiplication — that is
incorrect; ``rwa/price`` responses confirm division (tokenPrice/ratio ==
referencePrice at consistency).

All money math is :class:`decimal.Decimal`; string inputs are accepted because
the API returns prices as strings. Invalid/missing prices raise
``ValueError`` so callers can skip the row instead of recording garbage.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, Optional

__all__ = [
    "cross_issuer_spreads",
    "extract_fee_slippage_fields",
    "gross_spread",
    "implied_share_price",
    "independent_share_close",
    "net_executable_gap",
    "real_spread",
]

BPS = Decimal("10000")

# Field names carrying fee/slippage/exec-cost information in aggregator quotes.
_FEE_SLIPPAGE_RE = re.compile(
    r"fee|slippage|price[\s_]?impact|min[\s_]?receive|gas", re.IGNORECASE
)


def _dec(value: Any, name: str = "value") -> Decimal:
    """Parse an API value (str/int/float/Decimal) into a Decimal."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise ValueError(f"{name}: bool is not a number")
    if isinstance(value, float):
        value = repr(value)  # avoid binary-float artifacts
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, ArithmeticError) as exc:
        raise ValueError(f"{name}: not a number: {value!r}") from exc
    if not result.is_finite():
        raise ValueError(f"{name}: not finite: {value!r}")
    return result


def implied_share_price(token_price: Any, token_to_share_ratio: Any = None) -> Decimal:
    """Implied price per underlying share: ``tokenPrice / tokenToShareRatio``.

    The ratio is NEVER assumed: ratios in the universe range from 0.066667
    (ENLVon) to 10 (NFLXon/PPLTon); only 228/488 tokens are 1:1. A missing,
    zero or negative ratio — or a non-positive token price — raises
    ``ValueError`` (unpriced/untradeable row).
    """
    if token_price is None or (isinstance(token_price, str) and not token_price.strip()):
        raise ValueError("token_price: missing")
    price = _dec(token_price, "token_price")
    if price <= 0:
        raise ValueError(f"token_price: must be positive, got {price}")

    if token_to_share_ratio is None or (
        isinstance(token_to_share_ratio, str) and not token_to_share_ratio.strip()
    ):
        raise ValueError("tokenToShareRatio: missing — never assume 1 token = 1 share")
    ratio = _dec(token_to_share_ratio, "tokenToShareRatio")
    if ratio <= 0:
        raise ValueError(f"tokenToShareRatio: must be positive, got {ratio}")

    return price / ratio


def gross_spread(token_price: Any, token_to_share_ratio: Any, reference_price: Any) -> dict:
    """Spread of token implied share price over the reference price.

    Positive = token trades RICH (premium) to the underlying market;
    negative = token trades CHEAP (discount). ``spread_bps`` is signed,
    relative to the reference price.
    """
    if reference_price is None or (
        isinstance(reference_price, str) and not reference_price.strip()
    ):
        raise ValueError("reference_price: missing")
    ref = _dec(reference_price, "reference_price")
    if ref <= 0:
        raise ValueError(f"reference_price: must be positive, got {ref}")

    implied = implied_share_price(token_price, token_to_share_ratio)
    spread_abs = implied - ref
    spread_bps = (spread_abs / ref) * BPS
    return {
        "implied_share_price": implied,
        "reference_price": ref,
        "spread_abs": spread_abs,
        "spread_bps": spread_bps,
        "spread_pct": spread_bps / 100,
    }


def independent_share_close(
    underlying_market_reference_price: Any, token_to_share_ratio: Any
) -> Decimal:
    """True underlying share close from ``rwa/underlying-market``.

    CRITICAL normalization (data-derived, NOT in the docs — see DX-LOG.md):
    the endpoint's ``referencePrice`` is divided by ``tokenToShareRatio`` one
    time MORE than ``tokenPrice`` is, so the per-share close is
    ``referencePrice * tokenToShareRatio``. Without the multiplication NFLX
    showed +90000 bps. This helper is THE single source of truth for that
    normalization — the oracle cache fetch and the collector both use it.

    Missing/non-positive inputs raise ``ValueError`` (unusable reference).
    """
    if underlying_market_reference_price is None or (
        isinstance(underlying_market_reference_price, str)
        and not underlying_market_reference_price.strip()
    ):
        raise ValueError("underlying_market_reference_price: missing")
    ref = _dec(underlying_market_reference_price, "underlying_market_reference_price")
    if ref <= 0:
        raise ValueError(
            f"underlying_market_reference_price: must be positive, got {ref}"
        )
    if token_to_share_ratio is None or (
        isinstance(token_to_share_ratio, str) and not token_to_share_ratio.strip()
    ):
        raise ValueError("tokenToShareRatio: missing — never assume 1 token = 1 share")
    ratio = _dec(token_to_share_ratio, "tokenToShareRatio")
    if ratio <= 0:
        raise ValueError(f"tokenToShareRatio: must be positive, got {ratio}")
    return ref * ratio


def real_spread(
    token_price: Any, token_to_share_ratio: Any, independent_close: Any
) -> dict:
    """Spread of the token's implied share price vs the INDEPENDENT close.

    This is the REAL spread metric. Contrast with :func:`gross_spread`, whose
    ``referencePrice`` comes from the SAME ``rwa/price`` response as the token
    and is DERIVED (= ``tokenPrice / tokenToShareRatio``), so its spread is
    structurally ~0 bps for every token. Here the reference is the underlying
    market's own close (already normalized per share via
    :func:`independent_share_close` at fetch time / ``oracle_reference`` cache).

        spread_bps_real = (implied_share_price - independent_share_close)
                          / independent_share_close * 10000

    Positive = token trades RICH (premium) to the underlying close.
    """
    if independent_close is None or (
        isinstance(independent_close, str) and not independent_close.strip()
    ):
        raise ValueError("independent_close: missing")
    ref = _dec(independent_close, "independent_close")
    if ref <= 0:
        raise ValueError(f"independent_close: must be positive, got {ref}")

    implied = implied_share_price(token_price, token_to_share_ratio)
    spread_abs_real = implied - ref
    spread_bps_real = (spread_abs_real / ref) * BPS
    return {
        "implied_share_price": implied,
        "reference_close_independent": ref,
        "spread_abs_real": spread_abs_real,
        "spread_bps_real": spread_bps_real,
        "spread_pct_real": spread_bps_real / 100,
        "reference_kind": "independent",
    }


def net_executable_gap(
    token_price: Any,
    token_to_share_ratio: Any,
    reference_price: Any,
    *,
    round_trip_cost_bps: Any = "0",
    gas_cost_usd: Any = "0",
    notional_usd: Any = None,
) -> dict:
    """Gross spread minus round-trip execution costs (the net executable gap).

    ``round_trip_cost_bps`` — fees + slippage for BOTH legs in bps of notional
    (from aggregator quote probing). ``gas_cost_usd`` is amortized over
    ``notional_usd`` (required when gas > 0) and added to the cost.

    The gap keeps the sign of the gross spread: the residual edge available in
    the direction of the mispricing. ``executable`` is True only when the
    residual is strictly positive.
    """
    gross = gross_spread(token_price, token_to_share_ratio, reference_price)
    cost_bps = _dec(round_trip_cost_bps, "round_trip_cost_bps")
    if cost_bps < 0:
        raise ValueError(f"round_trip_cost_bps: must be >= 0, got {cost_bps}")

    gas = _dec(gas_cost_usd if gas_cost_usd is not None else "0", "gas_cost_usd")
    if gas < 0:
        raise ValueError(f"gas_cost_usd: must be >= 0, got {gas}")
    if gas != 0:
        if notional_usd is None:
            raise ValueError("notional_usd: required to amortize gas_cost_usd")
        notional = _dec(notional_usd, "notional_usd")
        if notional <= 0:
            raise ValueError(f"notional_usd: must be positive, got {notional}")
        gas_bps = (gas / notional) * BPS
    else:
        gas_bps = Decimal("0")

    total_cost_bps = cost_bps + gas_bps
    gross_bps = gross["spread_bps"]
    direction = Decimal("1") if gross_bps > 0 else (Decimal("-1") if gross_bps < 0 else Decimal("0"))
    net_gap_bps = gross_bps - direction * total_cost_bps

    return {
        **gross,
        "gross_spread_bps": gross_bps,
        "round_trip_cost_bps": cost_bps,
        "gas_cost_bps": gas_bps,
        "total_cost_bps": total_cost_bps,
        "net_gap_bps": net_gap_bps,
        "executable": net_gap_bps > 0,
    }


def cross_issuer_spreads(
    rows: Iterable[Mapping[str, Any]], include_singles: bool = False
) -> dict:
    """Inter-issuer spread per underlying ticker (Feature B).

    ``rows`` are token records (as in ``rwa/tokens`` / ``rwa/price``): at
    minimum ``underlyingTicker``, ``tokenSymbol``, ``platformId``,
    ``tokenPrice``, ``tokenToShareRatio``. Tokens are grouped by underlying
    ticker; when ≥ 2 issuers/platforms have prices, the spread between the
    richest and cheapest implied share price is computed (relative to the
    cheap leg — the self-financing buy-cheap/sell-rich edge).

    Unpriced rows are skipped. Groups with a single issuer are omitted unless
    ``include_singles``. No reference price is involved — works 24/7.
    """
    groups: dict = {}
    for row in rows:
        ticker = row.get("underlyingTicker")
        if not ticker:
            continue
        try:
            implied = implied_share_price(
                row.get("tokenPrice"), row.get("tokenToShareRatio")
            )
        except ValueError:
            continue  # unpriced row
        groups.setdefault(str(ticker), []).append(
            {
                "symbol": row.get("tokenSymbol"),
                "platform": row.get("platformId"),
                "implied": implied,
            }
        )

    result = {}
    for ticker, entries in groups.items():
        platforms = {e["platform"] for e in entries}
        if len(platforms) < 2 and not include_singles:
            continue
        ordered = sorted(entries, key=lambda e: (e["implied"], str(e["symbol"])))
        cheap, rich = ordered[0], ordered[-1]
        spread_abs = rich["implied"] - cheap["implied"]
        spread_bps = (spread_abs / cheap["implied"]) * BPS
        result[ticker] = {
            "underlying_ticker": ticker,
            "n_issuers": len(entries),
            "cheap_symbol": cheap["symbol"],
            "cheap_platform": cheap["platform"],
            "cheap_implied": cheap["implied"],
            "rich_symbol": rich["symbol"],
            "rich_platform": rich["platform"],
            "rich_implied": rich["implied"],
            "spread_abs": spread_abs,
            "spread_bps": spread_bps,
        }
    return result


def extract_fee_slippage_fields(obj: Any) -> list:
    """Sorted, de-duplicated field names carrying fee/slippage/cost info.

    Walks nested dicts/lists of an aggregator quote response and collects key
    names matching fee / slippage / price-impact / min-receive / gas patterns.
    Feeds the net-gap cost model (Feature A).
    """
    found: set = set()

    def walk(node: Any) -> None:
        if isinstance(node, Mapping):
            for key, value in node.items():
                if _FEE_SLIPPAGE_RE.search(str(key)):
                    found.add(str(key))
                walk(value)
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk(item)

    walk(obj)
    return sorted(found)
