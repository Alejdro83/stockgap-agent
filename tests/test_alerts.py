"""Tests for the alert engine: threshold crossings, rate limiting, ratio != 1
formatting, and actionability classification (platform + market hours).

Domain facts under test (verified live, see DX-LOG.md):
* ``tokenToShareRatio`` is never assumed to be 1 — a ratio != 1 must be
  spelled out in the alert text.
* xStock = executable 24/7; ondo/bstock = US market hours only (the API
  rejects off-hours orders with 40367/40369).
"""

import unittest
from decimal import Decimal

from stockgap.alerts import (
    RateLimiter,
    classify_actionability,
    find_crossings,
    format_cross_alert,
    format_spread_alert,
    is_one_to_one,
    is_threshold_crossing,
    load_config,
    pair_actionability,
    ratio_note,
)


class TestThresholdCrossing(unittest.TestCase):
    def test_fires_when_abs_spread_crosses_up(self):
        self.assertTrue(is_threshold_crossing(80, 150, 150))
        self.assertTrue(is_threshold_crossing("10", "200", "150"))

    def test_no_fire_below_threshold(self):
        self.assertFalse(is_threshold_crossing(0, 149, 150))
        self.assertFalse(is_threshold_crossing(None, 0, 150))

    def test_negative_spread_uses_abs(self):
        # premium flip to a -2% discount is still a cross
        self.assertTrue(is_threshold_crossing(-30, -200, 150))
        self.assertFalse(is_threshold_crossing(-10, -60, 150))

    def test_first_observation_over_the_line_fires(self):
        self.assertTrue(is_threshold_crossing(None, 180, 150))

    def test_stays_silent_while_above_then_rearms_after_dip(self):
        self.assertTrue(is_threshold_crossing(100, 200, 150))
        # still above: no new crossing
        self.assertFalse(is_threshold_crossing(200, 250, 150))
        # dips below: re-armed
        self.assertFalse(is_threshold_crossing(250, 140, 150))
        self.assertTrue(is_threshold_crossing(140, 151, 150))

    def test_invalid_bps_rows_are_skipped_never_garbage_alerts(self):
        rows = [
            {"token_symbol": "X", "spread_bps": "not-a-number"},
            {"token_symbol": "X", "spread_bps": "180"},
        ]
        prev = {}
        hits = find_crossings(rows, prev, 150, lambda r: r["token_symbol"], metric="derived")
        self.assertEqual([h["row"]["spread_bps"] for h in hits], ["180"])

    def test_find_crossings_tracks_prev_per_key(self):
        rows = [
            {"token_symbol": "A", "spread_bps": "200"},   # crosses (prev None)
            {"token_symbol": "B", "spread_bps": "50"},    # below
            {"token_symbol": "A", "spread_bps": "260"},   # still above: silent
            {"token_symbol": "A", "spread_bps": "100"},   # dip re-arms
            {"token_symbol": "A", "spread_bps": "-180"},  # crosses again
        ]
        prev = {}
        hits = find_crossings(rows, prev, 150, lambda r: r["token_symbol"], metric="derived")
        self.assertEqual(len(hits), 2)
        self.assertEqual(hits[0]["key"], "A")
        self.assertEqual(hits[1]["row"]["spread_bps"], "-180")
        self.assertEqual(prev["A"], "-180")


class TestRateLimiting(unittest.TestCase):
    def test_max_one_alert_per_token_per_window(self):
        rl = RateLimiter(30 * 60, clock=lambda: 1000.0)
        self.assertTrue(rl.allow("spread:AAPLon"))
        self.assertFalse(rl.allow("spread:AAPLon", now=1000.0 + 60))
        self.assertFalse(rl.allow("spread:AAPLon", now=1000.0 + 30 * 60 - 1))

    def test_allows_again_after_window(self):
        rl = RateLimiter(30 * 60, clock=lambda: 0.0)
        rl.allow("spread:AAPLon")
        self.assertTrue(rl.allow("spread:AAPLon", now=30 * 60 + 1))

    def test_tokens_are_independent(self):
        rl = RateLimiter(1800, clock=lambda: 0.0)
        self.assertTrue(rl.allow("spread:AAPLon"))
        self.assertTrue(rl.allow("spread:NVDAon"))
        self.assertTrue(rl.allow("cross:WDC"))
        self.assertFalse(rl.allow("spread:AAPLon", now=10))

    def test_window_is_configurable(self):
        rl = RateLimiter(60, clock=lambda: 0.0)  # 1 min window
        self.assertTrue(rl.allow("k"))
        self.assertFalse(rl.allow("k", now=30))
        self.assertTrue(rl.allow("k", now=61))

    def test_state_survives_reinstantiation(self):
        state = {}
        rl1 = RateLimiter(1800, state=state, clock=lambda: 0.0)
        rl1.allow("spread:AAPLon")
        rl2 = RateLimiter(1800, state=state, clock=lambda: 0.0)
        self.assertFalse(rl2.allow("spread:AAPLon", now=100))
        self.assertEqual(rl2.last_alert("spread:AAPLon"), 0.0)


class TestRatioFormatting(unittest.TestCase):
    def test_ratio_one_has_no_note(self):
        self.assertIsNone(ratio_note("1"))
        self.assertIsNone(ratio_note("1.000000000000000000"))  # live form
        self.assertTrue(is_one_to_one("1.000000000000000000"))

    def test_ratio_not_one_spells_out_conversion(self):
        # NFLXon live ratio: 10 shares per token
        note = ratio_note("10") or ""
        self.assertIn("1 token = 10 acciones", note)
        # ENLVon live ratio: 0.066667
        note = ratio_note("0.066667") or ""
        self.assertIn("0.066667", note)
        self.assertFalse(is_one_to_one("0.066667"))

    def test_missing_ratio_never_assumed_one(self):
        self.assertIsNone(ratio_note(None))     # line omitted...
        self.assertIsNone(ratio_note(""))
        self.assertFalse(is_one_to_one(None))   # ...but NOT treated as 1

    def test_spread_alert_includes_note_only_when_ratio_differs(self):
        base = {
            "token_symbol": "NFLXon", "underlying_ticker": "NFLX",
            "platform_id": "ondo", "token_price": "2262.73",
            "token_to_share_ratio": "10", "reference_price": "219.50",
            "implied_share_price": "226.27", "spread_bps": "180",
            "spread_pct": "1.80", "market_status": "open", "open_state": 1,
            "ts_utc": "2026-10-02T00:00:00.000Z",
        }
        self.assertIn("1 token = 10 acciones", format_spread_alert(base))
        base["token_to_share_ratio"] = "1.000000000000000000"
        self.assertNotIn("1 token =", format_spread_alert(base))


class TestActionability(unittest.TestCase):
    def test_xstock_is_executable_24_7_even_when_market_closed(self):
        act = classify_actionability("xstock", 0)
        self.assertTrue(act.executable_now)
        self.assertIn("24/7", act.line)
        self.assertTrue(classify_actionability("xstock", 1).executable_now)

    def test_ondo_open_is_executable_but_us_hours_only(self):
        act = classify_actionability("ondo", 1)
        self.assertTrue(act.executable_now)
        self.assertIn("horario de mercado US", act.line)

    def test_ondo_closed_is_not_executable_with_api_error_codes(self):
        act = classify_actionability("ondo", 0)
        self.assertFalse(act.executable_now)
        self.assertIn("40367/40369", act.line)
        self.assertIn("NO ejecutable", act.line)

    def test_bstock_same_rule_as_ondo(self):
        self.assertTrue(classify_actionability("bstock", 1).executable_now)
        act = classify_actionability("bstock", 0)
        self.assertFalse(act.executable_now)
        self.assertIn("40367/40369", act.line)

    def test_market_status_paused_treated_as_closed(self):
        act = classify_actionability("bstock", 0)
        self.assertFalse(act.executable_now)

    def test_unknown_platform_is_unknown_not_yes(self):
        act = classify_actionability("someother", 1)
        self.assertIsNone(act.executable_now)

    def test_pair_both_xstock_always_executable(self):
        act = pair_actionability("xstock", "xstock", open_state=0)
        self.assertTrue(act.executable_now)
        self.assertIn("24/7", act.line)

    def test_pair_cross_platform_follows_us_hours(self):
        self.assertTrue(pair_actionability("ondo", "bstock", 1).executable_now)
        act = pair_actionability("ondo", "bstock", 0)
        self.assertFalse(act.executable_now)
        self.assertIn("40367/40369", act.line)

    def test_pair_unknown_market_state_is_honest(self):
        act = pair_actionability("ondo", "bstock", None)
        self.assertIsNone(act.executable_now)

    def test_cross_alert_text_carries_actionability(self):
        row = {
            "underlying_ticker": "WDC", "n_issuers": 2,
            "cheap_symbol": "WDCB", "cheap_platform": "bstock",
            "cheap_implied": "460.41", "rich_symbol": "WDCon",
            "rich_platform": "ondo", "rich_implied": "460.58",
            "spread_bps": "3.7", "spread_abs": "0.17",
            "ts_utc": "2026-10-02T00:42:23.885Z",
        }
        msg = format_cross_alert(row, open_state=0, market_status="overnight")
        self.assertIn("CROSS-ISSUER WDC", msg)
        self.assertIn("40367/40369", msg)
        self.assertIn("cerrado", msg)


class TestConfigToggles(unittest.TestCase):
    def test_thresholds_and_toggles_via_env(self):
        cfg = load_config(environ={
            "SPREAD_THRESHOLD_BPS": "250",
            "CROSS_ISSUER_THRESHOLD_BPS": "75",
            "ALERT_RATE_LIMIT_MIN": "45",
            "ALERT_DIGEST_MODE": "1",
            "ALERT_SPREAD_ENABLED": "0",
            "TELEGRAM_ALERTS": "off",
        })
        self.assertEqual(cfg.spread_threshold_bps, Decimal("250"))
        self.assertEqual(cfg.cross_threshold_bps, Decimal("75"))
        self.assertEqual(cfg.rate_limit_min, 45.0)
        self.assertTrue(cfg.digest_mode)
        self.assertFalse(cfg.spread_enabled)
        self.assertFalse(cfg.telegram_enabled)

    def test_defaults(self):
        cfg = load_config(environ={})
        self.assertEqual(cfg.spread_threshold_bps, Decimal("150"))
        self.assertEqual(cfg.cross_threshold_bps, Decimal("100"))
        self.assertEqual(cfg.rate_limit_min, 30.0)
        self.assertFalse(cfg.digest_mode)
        self.assertTrue(cfg.enabled and cfg.spread_enabled and cfg.cross_enabled)


if __name__ == "__main__":
    unittest.main()
