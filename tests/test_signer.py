"""Unit tests for the OC HMAC signer.

The expected signatures below were produced independently with OpenSSL:
    echo -n "$PREHASH" | openssl dgst -sha256 -hmac "$SECRET" -binary | base64
"""

import base64
import hashlib
import hmac
import unittest

from stockgap.signer import build_headers, build_query, iso_timestamp, percent_encode, pre_hash, sign

TS = "2026-01-01T00:00:00.000Z"
SECRET = "test-secret"
# openssl: echo -n '2026-01-01T00:00:00.000ZGET/build/api/v1/dex/market/rwa/tokens' \
#   | openssl dgst -sha256 -hmac 'test-secret' -binary | base64
EXPECTED_GET_SIG = "4l58Zrx4MgzzOEexO9w8OL+pcHGJfhJO223EUbVDaxk="


class TestPreHash(unittest.TestCase):
    def test_get_empty_body(self):
        self.assertEqual(
            pre_hash(TS, "GET", "/build/api/v1/dex/market/rwa/tokens", ""),
            "2026-01-01T00:00:00.000ZGET/build/api/v1/dex/market/rwa/tokens",
        )

    def test_method_is_uppercased(self):
        self.assertEqual(
            pre_hash(TS, "get", "/build/x", ""),
            pre_hash(TS, "GET", "/build/x", ""),
        )

    def test_query_is_part_of_request_path(self):
        ph = pre_hash(TS, "GET", "/build/api/v1/dex/market/rwa/price?symbols=AAPLx", "")
        self.assertTrue(ph.endswith("/build/api/v1/dex/market/rwa/price?symbols=AAPLx"))

    def test_body_appended_for_post(self):
        self.assertEqual(
            pre_hash(TS, "POST", "/build/api/v1/x", '{"a":1}'),
            '2026-01-01T00:00:00.000ZPOST/build/api/v1/x{"a":1}',
        )

    def test_build_prefix_required(self):
        # Signing without /build is the #1 cause of 40102 — assert the two differ.
        self.assertNotEqual(
            pre_hash(TS, "GET", "/api/v1/dex/market/rwa/tokens", ""),
            pre_hash(TS, "GET", "/build/api/v1/dex/market/rwa/tokens", ""),
        )


class TestSignature(unittest.TestCase):
    def test_matches_openssl_vector(self):
        sig = sign(SECRET, pre_hash(TS, "GET", "/build/api/v1/dex/market/rwa/tokens", ""))
        self.assertEqual(sig, EXPECTED_GET_SIG)

    def test_matches_pure_hmac_reference(self):
        ph = pre_hash(TS, "POST", "/build/api/v2/b402/verify", '{"body":{}}')
        ref = base64.b64encode(
            hmac.new(SECRET.encode(), ph.encode(), hashlib.sha256).digest()
        ).decode()
        self.assertEqual(sign(SECRET, ph), ref)

    def test_signature_is_base64_of_32_bytes(self):
        sig = sign(SECRET, "anything")
        self.assertEqual(len(base64.b64decode(sig)), 32)


class TestEncoding(unittest.TestCase):
    def test_spaces_are_percent20_not_plus(self):
        self.assertEqual(percent_encode("ETH USDT"), "ETH%20USDT")

    def test_query_preserves_order_and_encodes(self):
        q = build_query({"chainId": 56, "symbol": "ETH USDT"})
        self.assertEqual(q, "chainId=56&symbol=ETH%20USDT")

    def test_bools_lowercased(self):
        self.assertEqual(build_query({"a": True}), "a=true")


class TestTimestamp(unittest.TestCase):
    def test_iso_millis_utc_z(self):
        ts = iso_timestamp()
        self.assertRegex(ts, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")

    def test_not_epoch_millis(self):
        ts = iso_timestamp()
        self.assertNotRegex(ts, r"^\d+$")


class TestHeaders(unittest.TestCase):
    def test_required_headers(self):
        h = build_headers("KEY", SECRET, "GET", "/build/api/v1/dex/market/rwa/tokens", timestamp=TS)
        self.assertEqual(h["X-OC-APIKEY"], "KEY")
        self.assertEqual(h["X-OC-TIMESTAMP"], TS)
        self.assertEqual(h["X-OC-SIGN"], EXPECTED_GET_SIG)
        self.assertNotIn("X-OC-RECV-WINDOW", h)

    def test_optional_headers(self):
        h = build_headers("KEY", SECRET, "GET", "/build/x", timestamp=TS, recv_window=60000, nonce="n1")
        self.assertEqual(h["X-OC-RECV-WINDOW"], "60000")
        self.assertEqual(h["X-OC-NONCE"], "n1")


if __name__ == "__main__":
    unittest.main()
