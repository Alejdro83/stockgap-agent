"""Unit tests for the spread engine (TDD: written before stockgap/spread.py).

Three spreads:
1. gross spread        — token implied share price vs reference price
2. net executable gap  — gross spread minus round-trip execution costs
3. cross-issuer spread — same underlying, different issuers/platforms

Key domain rule (verified live, docs are WRONG): the field is
``tokenToShareRatio`` (string decimal) and
    referencePrice = tokenPrice / tokenToShareRatio
i.e. implied share price = tokenPrice / tokenToShareRatio.
"""

import unittest
from decimal import Decimal

from stockgap.spread import (
    cross_issuer_spreads,
    extract_fee_slippage_fields,
    gross_spread,
    implied_share_price,
    net_executable_gap,
)


class TestImpliedSharePrice(unittest.TestCase):
    def test_divides_by_token_to_share_ratio(self):
        # live fixture ACNon: tokenPrice 226.7605..., ratio 1.0308... -> ~219.97
        implied = implied_share_price("226.76050039854725297124477292440268",
                                     "1.030852839739623902")
        self.assertAlmostEqual(float(implied), 219.9737, places=3)

    def test_ratio_of_two(self):
        self.assertEqual(implied_share_price("206", "2"), Decimal("103"))

    def test_missing_ratio_rejected_never_assume_one_to_one(self):
        # 228/488 tokens have ratio 1, but NFLXon=10, PALLon=5, ENLVon=0.066667
        # — a missing ratio must never be assumed to be 1 share per token.
        with self.assertRaises(ValueError):
            implied_share_price("100", None)
        with self.assertRaises(ValueError):
            implied_share_price("100", "")

    def test_wild_ratios(self):
        self.assertEqual(implied_share_price("1000", "10"), Decimal("100"))   # NFLXon
        self.assertEqual(implied_share_price("50", "5"), Decimal("10"))       # PALLon
        # ENLVon ratio 0.066667 -> 1 token ≈ 15 shares
        self.assertAlmostEqual(float(implied_share_price("1", "0.066667")),
                               14.999925, places=5)

    def test_zero_ratio_rejected(self):
        with self.assertRaises(ValueError):
            implied_share_price("100", "0")

    def test_negative_ratio_rejected(self):
        with self.assertRaises(ValueError):
            implied_share_price("100", "-1")

    def test_non_positive_price_rejected(self):
        with self.assertRaises(ValueError):
            implied_share_price("0", "1")
        with self.assertRaises(ValueError):
            implied_share_price(None, "1")


class TestGrossSpread(unittest.TestCase):
    def test_exact_premium(self):
        g = gross_spread("110", "1", "100")
        self.assertEqual(g["implied_share_price"], Decimal("110"))
        self.assertEqual(g["spread_abs"], Decimal("10"))
        self.assertEqual(g["spread_bps"], Decimal("1000"))
        self.assertEqual(g["spread_pct"], Decimal("10"))

    def test_exact_discount(self):
        g = gross_spread("90", "1", "100")
        self.assertEqual(g["spread_abs"], Decimal("-10"))
        self.assertEqual(g["spread_bps"], Decimal("-1000"))

    def test_ratio_normalization_applies(self):
        # 1 token = 2 shares: token 210 -> implied share 105 -> +5% vs 100
        g = gross_spread("210", "2", "100")
        self.assertEqual(g["implied_share_price"], Decimal("105"))
        self.assertEqual(g["spread_bps"], Decimal("500"))

    def test_zero_spread(self):
        g = gross_spread("103", "1.03", "100")
        self.assertEqual(g["spread_abs"], Decimal("0"))
        self.assertEqual(g["spread_bps"], Decimal("0"))

    def test_reference_below_token_ratio_identity(self):
        # The live identity: implied == reference when prices are consistent.
        g = gross_spread("226.76050039854725297124477292440268",
                         "1.030852839739623902",
                         "219.973687472038344448")
        self.assertAlmostEqual(float(g["spread_bps"]), 0.0, places=6)

    def test_invalid_reference_rejected(self):
        for ref in (None, "0", "-5", ""):
            with self.assertRaises(ValueError):
                gross_spread("110", "1", ref)


class TestNetExecutableGap(unittest.TestCase):
    def test_no_costs_gap_equals_gross(self):
        n = net_executable_gap("110", "1", "100")
        self.assertEqual(n["net_gap_bps"], Decimal("1000"))
        self.assertEqual(n["gross_spread_bps"], Decimal("1000"))

    def test_premium_less_round_trip_cost(self):
        n = net_executable_gap("110", "1", "100", round_trip_cost_bps="300")
        self.assertEqual(n["net_gap_bps"], Decimal("700"))
        self.assertEqual(n["round_trip_cost_bps"], Decimal("300"))

    def test_discount_direction_keeps_sign(self):
        # token cheap vs reference: gap is negative (buy token / short underlying)
        n = net_executable_gap("90", "1", "100", round_trip_cost_bps="300")
        self.assertEqual(n["net_gap_bps"], Decimal("-700"))

    def test_costs_can_kill_the_spread(self):
        n = net_executable_gap("110", "1", "100", round_trip_cost_bps="1200")
        self.assertEqual(n["net_gap_bps"], Decimal("-200"))
        self.assertFalse(n["executable"])

    def test_positive_gap_is_executable(self):
        n = net_executable_gap("110", "1", "100", round_trip_cost_bps="100")
        self.assertTrue(n["executable"])

    def test_gas_cost_amortized_over_notional(self):
        # 20 USD gas on 10_000 USD notional = 20 bps
        n = net_executable_gap("110", "1", "100",
                               round_trip_cost_bps="300",
                               gas_cost_usd="20", notional_usd="10000")
        self.assertEqual(n["gas_cost_bps"], Decimal("20"))
        self.assertEqual(n["net_gap_bps"], Decimal("680"))

    def test_gas_without_notional_rejected(self):
        with self.assertRaises(ValueError):
            net_executable_gap("110", "1", "100", gas_cost_usd="20")

    def test_zero_gap_is_not_executable(self):
        n = net_executable_gap("110", "1", "100", round_trip_cost_bps="1000")
        self.assertEqual(n["net_gap_bps"], Decimal("0"))
        self.assertFalse(n["executable"])


class TestCrossIssuerSpread(unittest.TestCase):
    ROWS = [
        {"underlyingTicker": "TSLA", "tokenSymbol": "TSLAon", "platformId": "ondo",
         "tokenPrice": "210", "tokenToShareRatio": "2"},   # implied 105
        {"underlyingTicker": "TSLA", "tokenSymbol": "TSLAB", "platformId": "bstock",
         "tokenPrice": "100", "tokenToShareRatio": "1"},   # implied 100
        {"underlyingTicker": "AAPL", "tokenSymbol": "AAPLon", "platformId": "ondo",
         "tokenPrice": "100", "tokenToShareRatio": "1"},   # alone -> omitted
    ]

    def test_groups_by_underlying_across_platforms(self):
        result = cross_issuer_spreads(self.ROWS)
        self.assertEqual(set(result), {"TSLA"})
        self.assertIn("AAPL", cross_issuer_spreads(self.ROWS, include_singles=True))

    def test_spread_between_min_and_max_implied(self):
        t = cross_issuer_spreads(self.ROWS)["TSLA"]
        self.assertEqual(t["cheap_symbol"], "TSLAB")
        self.assertEqual(t["rich_symbol"], "TSLAon")
        self.assertEqual(t["cheap_implied"], Decimal("100"))
        self.assertEqual(t["rich_implied"], Decimal("105"))
        self.assertEqual(t["spread_bps"], Decimal("500"))  # (105-100)/100

    def test_pair_identifies_platforms(self):
        t = cross_issuer_spreads(self.ROWS)["TSLA"]
        self.assertEqual(t["cheap_platform"], "bstock")
        self.assertEqual(t["rich_platform"], "ondo")

    def test_three_way_group(self):
        rows = self.ROWS + [
            {"underlyingTicker": "TSLA", "tokenSymbol": "TSLAx", "platformId": "xstocks",
             "tokenPrice": "204", "tokenToShareRatio": "2"},  # implied 102
        ]
        t = cross_issuer_spreads(rows)["TSLA"]
        self.assertEqual(t["n_issuers"], 3)
        self.assertEqual(t["spread_bps"], Decimal("500"))  # still 105 vs 100

    def test_skips_unpriced_rows(self):
        rows = [
            {"underlyingTicker": "TSLA", "tokenSymbol": "TSLAon", "platformId": "ondo",
             "tokenPrice": None, "tokenToShareRatio": "2"},
            {"underlyingTicker": "TSLA", "tokenSymbol": "TSLAB", "platformId": "bstock",
             "tokenPrice": "100", "tokenToShareRatio": "1"},
        ]
        self.assertNotIn("TSLA", cross_issuer_spreads(rows))


class TestExtractFeeSlippageFields(unittest.TestCase):
    RESPONSE = {
        "code": 0,
        "data": {
            "routes": [
                {
                    "quoteId": "q1",
                    "executionMode": "RFQ",
                    "vendorName": "PcsXRfq",
                    "toTokenAmount": "1000",
                    "slippagePercent": "0.5",
                    "fee": {"feePercent": "0.1", "feeSource": "FROM_TOKEN"},
                    "out": {"priceImpact": "0.02", "minReceive": "995"},
                }
            ],
            "gasCost": "2100000000000000",
        },
    }

    def test_collects_fee_slippage_impact_receive_gas(self):
        fields = extract_fee_slippage_fields(self.RESPONSE)
        self.assertEqual(
            sorted(fields),
            ["fee", "feePercent", "feeSource", "gasCost",
             "minReceive", "priceImpact", "slippagePercent"],
        )

    def test_deduplicates_and_sorts(self):
        dup = {"a": {"slippageBps": "1"}, "b": [{"slippageBps": "2"}]}
        self.assertEqual(extract_fee_slippage_fields(dup), ["slippageBps"])

    def test_ignores_unrelated_fields(self):
        self.assertEqual(extract_fee_slippage_fields({"quoteId": "x", "data": {"route": "y"}}), [])

    def test_empty_input(self):
        self.assertEqual(extract_fee_slippage_fields({}), [])
        self.assertEqual(extract_fee_slippage_fields(None), [])


if __name__ == "__main__":
    unittest.main()
