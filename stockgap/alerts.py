"""StockGap alert engine: spread-threshold crossings → Telegram + file log.

All behavior is configurable via env vars or a dotenv-style config file
(:func:`load_config`) — thresholds, rate limits, digest mode and per-channel
toggles are never hardcoded.

Domain notes baked into the *message text* (not the behavior):

* ``tokenToShareRatio`` is NEVER assumed to be 1 (see DX-LOG.md): when the
  ratio differs from 1 the alert spells out the conversion.
* Actionability: xStock tokens are executable 24/7; ondo/bstock orders are
  rejected off-hours by the API (errors 40367/40369) — only US market hours
  count for those platforms.

The Telegram token is accepted here but NEVER logged or included in error
messages (:meth:`TelegramTransport.send` redacts it).
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

__all__ = [
    "Actionability",
    "AlertConfig",
    "AlertEvent",
    "CompositeTransport",
    "FileLogTransport",
    "RateLimiter",
    "TelegramTransport",
    "build_transport",
    "classify_actionability",
    "find_crossings",
    "format_cross_alert",
    "format_spread_alert",
    "is_one_to_one",
    "is_threshold_crossing",
    "load_config",
    "metric_column",
    "pair_actionability",
    "ratio_note",
]

log = logging.getLogger("stockgap.alerts")

# ---------------- config ----------------

def _dec(value: Any, name: str = "value") -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise ValueError(f"{name}: bool is not a number")
    if isinstance(value, float):
        value = repr(value)
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, ArithmeticError) as exc:
        raise ValueError(f"{name}: not a number: {value!r}") from exc
    if not result.is_finite():
        raise ValueError(f"{name}: not finite: {value!r}")
    return result


def _truthy(value: Any, default: bool) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def parse_config_file(path: Any) -> Dict[str, str]:
    """Parse a dotenv-style file (``KEY=VALUE`` lines, ``#`` comments)."""
    out: Dict[str, str] = {}
    p = Path(path)
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


@dataclass
class AlertConfig:
    """Every threshold/toggle lives here — nothing is hardcoded downstream."""

    enabled: bool = True                       # ALERTS_ENABLED
    spread_enabled: bool = True                # ALERT_SPREAD_ENABLED
    cross_enabled: bool = True                 # ALERT_CROSS_ENABLED
    spread_threshold_bps: Decimal = Decimal("150")   # SPREAD_THRESHOLD_BPS
    cross_threshold_bps: Decimal = Decimal("100")    # CROSS_ISSUER_THRESHOLD_BPS
    rate_limit_min: float = 30.0               # ALERT_RATE_LIMIT_MIN
    digest_mode: bool = False                  # ALERT_DIGEST_MODE
    digest_interval_min: float = 30.0          # ALERT_DIGEST_INTERVAL_MIN
    telegram_enabled: bool = True              # TELEGRAM_ALERTS
    file_log_enabled: bool = True              # ALERT_FILE_LOG
    telegram_bot_token: str = ""               # TELEGRAM_BOT_TOKEN (never logged)
    telegram_chat_id: str = "6438086899"       # TELEGRAM_CHAT_ID
    alert_log_path: str = "data/alerts.jsonl"  # ALERT_LOG
    scan_interval_s: float = 30.0              # ALERT_SCAN_INTERVAL
    alert_metric: str = "real"                 # ALERT_METRIC = real | derived

    @property
    def rate_limit_s(self) -> float:
        return self.rate_limit_min * 60.0

    @property
    def digest_interval_s(self) -> float:
        return self.digest_interval_min * 60.0


def load_config(
    environ: Optional[Dict[str, str]] = None,
    config_file: Any = None,
) -> AlertConfig:
    """Build :class:`AlertConfig`: defaults < config file < real environment.

    ``config_file`` defaults to ``$STOCKGAP_ALERTS_CONFIG`` or the repo's
    ``config/alerts.env`` when present.
    """
    env = dict(os.environ if environ is None else environ)
    if config_file is None:
        config_file = env.get("STOCKGAP_ALERTS_CONFIG") or (
            Path(__file__).resolve().parent.parent / "config" / "alerts.env"
        )
    file_vals = parse_config_file(config_file)

    def get(key: str, default: str = "") -> str:
        # real environment wins over the config file
        return env.get(key, file_vals.get(key, default))

    cfg = AlertConfig()
    cfg.enabled = _truthy(get("ALERTS_ENABLED"), True)
    cfg.spread_enabled = _truthy(get("ALERT_SPREAD_ENABLED"), True)
    cfg.cross_enabled = _truthy(get("ALERT_CROSS_ENABLED"), True)
    cfg.spread_threshold_bps = _dec(
        get("SPREAD_THRESHOLD_BPS", "150"), "SPREAD_THRESHOLD_BPS"
    )
    cfg.cross_threshold_bps = _dec(
        get("CROSS_ISSUER_THRESHOLD_BPS", "100"), "CROSS_ISSUER_THRESHOLD_BPS"
    )
    cfg.rate_limit_min = float(get("ALERT_RATE_LIMIT_MIN", "30"))
    cfg.digest_mode = _truthy(get("ALERT_DIGEST_MODE"), False)
    cfg.digest_interval_min = float(get("ALERT_DIGEST_INTERVAL_MIN", "30"))
    cfg.telegram_enabled = _truthy(get("TELEGRAM_ALERTS"), True)
    cfg.file_log_enabled = _truthy(get("ALERT_FILE_LOG"), True)
    cfg.telegram_bot_token = get("TELEGRAM_BOT_TOKEN", "")
    cfg.telegram_chat_id = get("TELEGRAM_CHAT_ID", "6438086899")
    cfg.alert_log_path = get("ALERT_LOG", "data/alerts.jsonl")
    cfg.scan_interval_s = float(get("ALERT_SCAN_INTERVAL", "30"))
    cfg.alert_metric = (get("ALERT_METRIC", "real") or "real").strip().lower()
    if cfg.alert_metric not in METRIC_COLUMNS:
        raise ValueError(
            f"ALERT_METRIC: must be one of {sorted(METRIC_COLUMNS)},"
            f" got {cfg.alert_metric!r}"
        )
    return cfg


# ---------------- metric selection ----------------

# 'real'     → spread_bps_real: vs the INDEPENDENT underlying close — real gaps.
# 'derived'  → spread_bps: vs rwa/price.referencePrice, which is DERIVED from
#              the token itself (= tokenPrice/ratio) and therefore ~0 bps by
#              construction (legacy metric, kept for the old columns/dashboard).
METRIC_COLUMNS = {"real": "spread_bps_real", "derived": "spread_bps"}


def metric_column(metric: str = "real") -> str:
    """Spread-bps column backing an alert metric ('real' | 'derived')."""
    try:
        return METRIC_COLUMNS[str(metric).strip().lower()]
    except KeyError as exc:
        raise ValueError(f"metric: must be one of {sorted(METRIC_COLUMNS)}, got {metric!r}") from exc


# ---------------- threshold crossing ----------------

def is_threshold_crossing(
    prev_bps: Any, curr_bps: Any, threshold_bps: Any
) -> bool:
    """True when ``abs(spread_bps)`` crosses from below to >= threshold.

    ``prev_bps`` of ``None`` (first observation for the key) counts as below —
    a first observation already over the line is a crossing. While the value
    stays over the line no new crossing fires; a dip below re-arms it.
    """
    thr = abs(_dec(threshold_bps, "threshold_bps"))
    curr = abs(_dec(curr_bps, "curr_bps"))
    if curr < thr:
        return False
    if prev_bps is None:
        return True
    return abs(_dec(prev_bps, "prev_bps")) < thr


# ---------------- rate limiting ----------------

class RateLimiter:
    """Max 1 alert per key per ``limit_s`` seconds (state dict is injectable)."""

    def __init__(
        self,
        limit_s: float,
        state: Optional[Dict[str, float]] = None,
        clock: Callable[[], float] = time.time,
    ):
        self.limit_s = float(limit_s)
        self.state: Dict[str, float] = state if state is not None else {}
        self.clock = clock

    def allow(self, key: str, now: Optional[float] = None) -> bool:
        now = self.clock() if now is None else float(now)
        last = self.state.get(key)
        if last is not None and (now - float(last)) < self.limit_s:
            return False
        self.state[key] = now
        return True

    def last_alert(self, key: str) -> Optional[float]:
        return self.state.get(key)


# ---------------- ratio handling ----------------

def is_one_to_one(ratio: Any) -> bool:
    """True when tokenToShareRatio == 1 (numeric compare, not string)."""
    if ratio is None or (isinstance(ratio, str) and not ratio.strip()):
        return False
    try:
        return _dec(ratio, "tokenToShareRatio") == 1
    except ValueError:
        return False


def ratio_note(ratio: Any) -> Optional[str]:
    """Ratio-conversion line when ratio != 1, else ``None`` (line omitted)."""
    if ratio is None or (isinstance(ratio, str) and not ratio.strip()):
        return None
    try:
        d = _dec(ratio, "tokenToShareRatio")
    except ValueError:
        return None
    if d == 1:
        return None
    pretty = format(d.normalize(), "f")
    return f"⚖️ 1 token = {pretty} acciones (tokenToShareRatio)"


# ---------------- actionability ----------------

@dataclass(frozen=True)
class Actionability:
    """Can the spread be acted on right now?"""

    executable_now: Optional[bool]  # True / False / None (unknown platform)
    line: str  # full Spanish line, ready for the message


def classify_actionability(platform_id: Any, open_state: Any) -> Actionability:
    """xStock = ejecutable 24/7; ondo/bstock = solo horario de mercado US.

    Outside US hours the API rejects ondo/bstock orders with 40367/40369, so
    an off-hours spread on those platforms is NOT actionable.
    """
    platform = str(platform_id or "").strip().lower()
    try:
        open_ = bool(int(open_state))
    except (TypeError, ValueError):
        open_ = bool(open_state)

    if platform.startswith("xstock"):
        return Actionability(
            True,
            "✅ Acción posible 24/7 (xStock): ejecutable también fuera de horario US.",
        )
    if platform.startswith("ondo") or platform.startswith("bstock"):
        if open_:
            return Actionability(
                True,
                "✅ Acción posible SOLO en horario de mercado US — ahora ABIERTO: ejecutable.",
            )
        return Actionability(
            False,
            "⛔ NO ejecutable ahora: ondo/bstock solo opera en horario de mercado US "
            "(la API rechaza órdenes fuera de horario: 40367/40369).",
        )
    return Actionability(
        None,
        f"❓ Plataforma sin clasificar ({platform_id or 'desconocida'}): "
        "verificar ejecutabilidad a mano.",
    )


def pair_actionability(
    cheap_platform: Any, rich_platform: Any, open_state: Any = None
) -> Actionability:
    """Actionability of a cross-issuer pair (both legs must be tradeable)."""
    plats = [str(cheap_platform or "").strip().lower(),
             str(rich_platform or "").strip().lower()]
    if all(p.startswith("xstock") for p in plats):
        return Actionability(
            True, "✅ Acción posible 24/7 (ambas piernas xStock)."
        )
    known = all(p.startswith(("xstock", "ondo", "bstock")) for p in plats)
    if not known:
        return Actionability(
            None,
            "❓ Plataforma sin clasificar en el par: verificar ejecutabilidad a mano.",
        )
    if open_state is None:
        return Actionability(
            None,
            "⚠️ Ejecución solo en horario de mercado US (alguna pierna es ondo/bstock; "
            "la API rechaza fuera de horario: 40367/40369). Estado del mercado desconocido.",
        )
    try:
        open_ = bool(int(open_state))
    except (TypeError, ValueError):
        open_ = bool(open_state)
    if open_:
        return Actionability(
            True,
            "✅ Acción posible SOLO en horario de mercado US — ahora ABIERTO: ambas piernas ejecutables.",
        )
    return Actionability(
        False,
        "⛔ NO ejecutable ahora: alguna pierna (ondo/bstock) solo opera en horario de "
        "mercado US (la API rechaza órdenes fuera de horario: 40367/40369).",
    )


# ---------------- message formatting (ES, mobile-friendly) ----------------

def _fmt_price(value: Any) -> str:
    if value is None or (isinstance(value, str) and not value.strip()):
        return "—"
    try:
        return f"{_dec(value):.2f}"
    except ValueError:
        return str(value)


def _fmt_signed(value: Any, decimals: int = 2, suffix: str = "") -> str:
    if value is None or (isinstance(value, str) and not value.strip()):
        return "—"
    try:
        return f"{_dec(value):+.{decimals}f}{suffix}"
    except ValueError:
        return str(value)


def _direction(bps: Any) -> str:
    try:
        v = _dec(bps, "spread_bps")
    except ValueError:
        return "en par vs referencia"
    if v > 0:
        return "token RICO (prima) vs referencia"
    if v < 0:
        return "token BARATO (descuento) vs referencia"
    return "en par vs referencia"


def _market_line(market_status: Any, open_state: Any) -> str:
    try:
        open_ = bool(int(open_state))
    except (TypeError, ValueError):
        open_ = bool(open_state)
    status = str(market_status) if market_status not in (None, "") else "desconocido"
    return f"🕐 Mercado: {status} ({'abierto' if open_ else 'cerrado'})"


def format_spread_alert(row: Dict[str, Any], metric: str = "real") -> str:
    """Spanish, mobile-friendly spread alert: ticker, platform, spread %, token
    vs reference price, ratio note (only when ratio != 1), market status and
    actionability.

    ``metric`` (``'real'`` | ``'derived'``) selects which spread is displayed —
    normally the one that triggered the crossing. ``'real'`` shows
    ``spread_bps_real`` (vs the INDEPENDENT underlying close); rows without it
    (legacy data / cold cache) fall back to the derived numbers and the
    reference line says so explicitly (``reference_kind='derived'``).
    """
    col = metric_column(metric)
    metric_bps = row.get(col)
    if metric_bps is None or (isinstance(metric_bps, str) and not str(metric_bps).strip()):
        metric_bps, metric_pct = row.get("spread_bps"), row.get("spread_pct")
    else:
        try:
            metric_pct = _dec(metric_bps) / 100
        except ValueError:
            metric_pct = row.get("spread_pct")
    if row.get("reference_close_independent") not in (None, ""):
        ref_line = (
            f"🏛 Ref independiente (cierre subyacente): "
            f"{_fmt_price(row.get('reference_close_independent'))} USD · rwa/underlying-market"
        )
    elif row.get("reference_kind") == "derived":
        ref_line = (
            f"🏛 Ref (mercado): {_fmt_price(row.get('reference_price'))} USD "
            f"— DERIVADA del propio token, spread NO informativo"
        )
    else:
        ref_line = f"🏛 Ref (mercado): {_fmt_price(row.get('reference_price'))} USD"
    act = classify_actionability(row.get("platform_id"), row.get("open_state"))
    lines = [
        f"🚨 SPREAD {row.get('token_symbol') or '?'} "
        f"({row.get('underlying_ticker') or '?'}) · {row.get('platform_id') or '?'}",
        f"📈 {_fmt_signed(metric_pct, 2, '%')} "
        f"({_fmt_signed(metric_bps, 1, ' bps')}) — {_direction(metric_bps)}",
        f"💵 Token: {_fmt_price(row.get('token_price'))} USD → "
        f"acción implícita: {_fmt_price(row.get('implied_share_price'))} USD",
        ref_line,
    ]
    note = ratio_note(row.get("token_to_share_ratio"))
    if note:
        lines.append(note)
    lines.append(_market_line(row.get("market_status"), row.get("open_state")))
    lines.append(act.line)
    lines.append(f"⏱ {row.get('ts_utc') or ''}")
    return "\n".join(lines)


def format_cross_alert(
    row: Dict[str, Any],
    open_state: Any = None,
    market_status: Any = None,
) -> str:
    """Spanish, mobile-friendly cross-issuer alert (cheap vs rich legs)."""
    act = pair_actionability(
        row.get("cheap_platform"), row.get("rich_platform"), open_state
    )
    lines = [
        f"🔀 CROSS-ISSUER {row.get('underlying_ticker') or '?'} "
        f"({row.get('n_issuers') or '?'} emisores)",
        f"📈 {_fmt_signed(row.get('spread_bps'), 1, ' bps')} "
        f"(dif. {_fmt_price(row.get('spread_abs'))} USD) — comprar barato / vender rico",
        f"💰 Barato: {row.get('cheap_symbol') or '?'} ({row.get('cheap_platform') or '?'}) "
        f"{_fmt_price(row.get('cheap_implied'))} USD",
        f"💸 Rico: {row.get('rich_symbol') or '?'} ({row.get('rich_platform') or '?'}) "
        f"{_fmt_price(row.get('rich_implied'))} USD",
    ]
    if open_state is not None:
        lines.append(_market_line(market_status, open_state))
    lines.append(act.line)
    lines.append(f"⏱ {row.get('ts_utc') or ''}")
    return "\n".join(lines)


@dataclass
class AlertEvent:
    kind: str  # 'spread' | 'cross'
    key: str
    message: str
    row: Dict[str, Any] = field(default_factory=dict)


# ---------------- transports (pluggable) ----------------

class FileLogTransport:
    """Append-only JSONL log — always available, works without Telegram."""

    def __init__(self, path: Any):
        self.path = Path(path)

    def send(self, text: str) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"ts_utc": ts, "text": text},
                                    ensure_ascii=False) + "\n")
            return True
        except OSError as exc:
            log.error("file log write failed: %s", exc)
            return False


class TelegramTransport:
    """sendMessage via the Telegram Bot API. The token is never logged."""

    def __init__(
        self,
        token: str,
        chat_id: str,
        api_base: str = "https://api.telegram.org",
        timeout: float = 15.0,
    ):
        if not token or not str(token).strip():
            raise ValueError("TELEGRAM_BOT_TOKEN vacío — no se puede enviar")
        self._token = str(token).strip()
        self.chat_id = str(chat_id).strip()
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout

    def _redact(self, text: str) -> str:
        return text.replace(self._token, "***")

    def send(self, text: str) -> bool:
        url = f"{self.api_base}/bot{self._token}/sendMessage"
        payload = json.dumps(
            {"chat_id": self.chat_id, "text": text, "disable_web_preview": True}
        ).encode("utf-8")
        req = urllib.request.Request(
            url, data=payload, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.load(resp)
            if not data.get("ok"):
                log.error("telegram rechazó el envío: %s",
                          self._redact(str(data)[:200]))
                return False
            return True
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log.error("telegram send failed: %s", self._redact(str(exc)[:200]))
            return False

    def __repr__(self) -> str:  # never leak the token in logs/reprs
        return f"TelegramTransport(chat_id={self.chat_id!r}, token='***')"


class CompositeTransport:
    def __init__(self, transports: List[Any]):
        self.transports = list(transports)

    def send(self, text: str) -> bool:
        results = [t.send(text) for t in self.transports]
        return any(results) if results else False


def build_transport(cfg: AlertConfig) -> CompositeTransport:
    """Enabled transports per toggles (file log + optional Telegram)."""
    transports: List[Any] = []
    if cfg.file_log_enabled:
        transports.append(FileLogTransport(cfg.alert_log_path))
    if cfg.telegram_enabled and cfg.telegram_bot_token:
        transports.append(
            TelegramTransport(cfg.telegram_bot_token, cfg.telegram_chat_id)
        )
    elif cfg.telegram_enabled:
        log.warning("TELEGRAM_ALERTS=1 pero TELEGRAM_BOT_TOKEN vacío — solo file log")
    return CompositeTransport(transports)


# ---------------- crossing scan ----------------

def find_crossings(
    rows: List[Dict[str, Any]],
    prev_bps_map: Dict[str, Any],
    threshold_bps: Any,
    key_fn: Callable[[Dict[str, Any]], str],
    metric: str = "real",
) -> List[Dict[str, Any]]:
    """Rows (in id order) whose ``abs(<metric bps>)`` crosses ``threshold_bps``.

    ``metric`` selects the checked column (:func:`metric_column`): ``'real'``
    (default) = ``spread_bps_real``, the spread vs the INDEPENDENT underlying
    close; ``'derived'`` = legacy ``spread_bps`` (structurally ~0 — kept for
    cross-issuer rows and legacy data). Rows whose metric column is NULL or
    unparseable are skipped — never garbage-alert (e.g. historical rows whose
    independent close was never fetched, labelled ``reference_kind='derived'``).

    ``prev_bps_map`` maps key → previous bps (any parseable form) and is
    updated in place for EVERY row, so a value that stays above the threshold
    fires once and re-arms only after dipping below. A first-ever observation
    already over the line counts as a crossing (``prev`` is ``None``).
    """
    col = metric_column(metric)
    crossings: List[Dict[str, Any]] = []
    for row in rows:
        key = key_fn(row)
        try:
            curr = _dec(row.get(col), col)
        except ValueError:
            continue  # unpriced/unreferenced row — skip, never garbage-alert
        prev = prev_bps_map.get(key)
        if is_threshold_crossing(prev, curr, threshold_bps):
            crossings.append({"key": key, "row": row})
        prev_bps_map[key] = str(curr)
    return crossings
