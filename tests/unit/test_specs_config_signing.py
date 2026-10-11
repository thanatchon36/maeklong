import io
import json
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from riverbank.core.config import Config, SafetyError, load_config, resolve_mode
from riverbank.core.models import Mode
from riverbank.exchange import rest as rest_mod
from riverbank.exchange.rest import (
    ApiError,
    IpBanError,
    RateLimitError,
    RestClient,
    RetryableError,
    ThrottledError,
    UnknownExecutionError,
    assert_signed_allowed,
    classify,
)
from riverbank.exchange.signing import Credentials, HmacSigner, signed_query, signer_from_pem
from riverbank.exchange.specs import parse_exchange_info

ROOT = Path(__file__).resolve().parents[2]
FIX = ROOT / "tests" / "fixtures"


def load_info():
    return parse_exchange_info(json.loads((FIX / "exchange_info_sample.json").read_text()))


def test_parse_exchange_info_specs_and_limits():
    info = load_info()
    doge = info.specs["DOGEUSDT"]
    assert doge.tick == Decimal("0.000010") and doge.step == Decimal("1")
    assert doge.min_notional == Decimal("5")  # futures MIN_NOTIONAL field is `notional`
    assert doge.contract_type == "PERPETUAL" and doge.quote_asset == "USDT"
    assert doge.onboard_date_ms > 0
    w = info.rate_limit("REQUEST_WEIGHT", 60)
    assert w is not None and w.limit > 0 and w.header_suffix == "1M"
    o10 = info.rate_limit("ORDERS", 10)
    assert o10 is not None and o10.header_suffix == "10S"


def test_parse_skips_symbol_with_missing_filter():
    data = json.loads((FIX / "exchange_info_sample.json").read_text())
    data["symbols"][0]["filters"] = [f for f in data["symbols"][0]["filters"] if f["filterType"] != "LOT_SIZE"]
    info = parse_exchange_info(data)
    assert data["symbols"][0]["symbol"] in info.skipped


def test_default_config_file_valid():
    cfg = load_config(ROOT / "config" / "config.yaml")
    assert cfg.mode is Mode.SHADOW
    assert cfg.quoting.funding_guard_action == "pull"
    assert cfg.hedge.allow_taker is False
    assert "BTCUSDT" in cfg.universe.hedge_only


def test_config_rejects_unknown_and_bad_values():
    with pytest.raises(ValidationError):
        Config.model_validate({"risk": {"max_position_usdt": -1}})
    with pytest.raises(ValidationError):
        Config.model_validate({"quoting": {"typo_field": 1}})
    with pytest.raises(ValidationError):  # min half spread must exceed maker fee
        Config.model_validate({"quoting": {"min_half_spread_bps": 1.0}, "account": {"fee_maker_bps": 2.0}})


def test_resolve_mode_live_gate():
    assert resolve_mode(Mode.SHADOW, {}) is Mode.SHADOW
    assert resolve_mode(Mode.SHADOW, {"MODE": "demo"}) is Mode.DEMO
    with pytest.raises(SafetyError):
        resolve_mode(Mode.LIVE, {})  # config alone cannot enable live
    with pytest.raises(SafetyError):
        resolve_mode(Mode.SHADOW, {"MODE": "live"})
    with pytest.raises(SafetyError):
        resolve_mode(Mode.SHADOW, {"MODE": "live", "LIVE_CONFIRM": "yes"})
    assert resolve_mode(Mode.SHADOW, {"MODE": "live", "LIVE_CONFIRM": "I_UNDERSTAND_THE_RISK"}) is Mode.LIVE


def test_hmac_matches_binance_doc_example():
    # developers.binance.com USDⓈ-M general-info, "SIGNED Endpoint Examples ... Example 1"
    s = HmacSigner("2b5eb11e18796d12d88f13dc27dbbd02c2cc51ff7059765ed9821957d82bb4d9")
    payload = ("symbol=BTCUSDT&side=BUY&type=LIMIT&quantity=1&price=9000&timeInForce=GTC"
               "&recvWindow=5000&timestamp=1591702613943")
    assert s.sign(payload) == "3c661234138461fcc7a7d8746c6558c9842d4e10870d2ecbedf7777cad694af9"


def test_signed_query_appends_signature_last():
    s = HmacSigner("x" * 16)
    q = signed_query({"symbol": "DOGEUSDT", "reduceOnly": True, "skip": None}, s)
    assert q.startswith("symbol=DOGEUSDT&reduceOnly=true&signature=")


@pytest.mark.parametrize("kind", ["rsa", "ed25519"])
def test_pem_signers_verify(kind):
    import base64

    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

    key = rsa.generate_private_key(65537, 2048) if kind == "rsa" else ed25519.Ed25519PrivateKey.generate()
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    signer = signer_from_pem(pem)
    assert signer.kind == kind
    sig = base64.b64decode(signer.sign("a=1&b=2"))
    if kind == "rsa":
        key.public_key().verify(sig, b"a=1&b=2", padding.PKCS1v15(), hashes.SHA256())
    else:
        key.public_key().verify(sig, b"a=1&b=2")
    assert "redacted" in repr(signer)


def test_signed_requests_to_production_refused():
    rest_mod.lock_live_signing()
    with pytest.raises(SafetyError):
        assert_signed_allowed("https://fapi.binance.com")
    with pytest.raises(SafetyError):
        RestClient("https://fapi.binance.com", Credentials("k", HmacSigner("s" * 10)))
    assert_signed_allowed("https://demo-fapi.binance.com")  # demo is fine
    assert_signed_allowed("http://127.0.0.1:8080")  # local fake exchange in tests
    with pytest.raises(SafetyError):
        rest_mod.unlock_live_signing({"MODE": "shadow"})


def test_classify_http_errors():
    assert isinstance(classify(503, b'{"code":-1000,"msg":"Unknown error, please check your request or try '
                                    b'again later."}', {}), UnknownExecutionError)
    assert isinstance(classify(503, b'{"code":-1001,"msg":"Service Unavailable."}', {}), RetryableError)
    assert isinstance(classify(503, b'{"code":-1008,"msg":"Request throttled by system-level protection."}', {}),
                      ThrottledError)
    e = classify(429, b'{"code":-1003,"msg":"Too many requests"}', {"Retry-After": "7"})
    assert isinstance(e, RateLimitError) and e.retry_after_s == 7
    assert isinstance(classify(418, b"", {}), IpBanError)
    e = classify(400, b'{"code":-5022,"msg":"Post Only order will be rejected"}', {})
    assert isinstance(e, ApiError) and e.code == -5022


def test_logging_redacts_secrets(monkeypatch):
    from riverbank.ops import logging as rlog

    monkeypatch.setenv("BINANCE_API_KEY", "SUPERSECRETKEY123")
    buf = io.StringIO()
    rlog.setup_logging("INFO", {"noisy": "ERROR"}, stream=buf)
    log = rlog.get_logger("test")
    log.info("req", url="https://x?k=SUPERSECRETKEY123", api_key="abc", nested={"signature": "zzz"})
    rlog.get_logger("noisy").info("dropped")
    out = buf.getvalue()
    assert "SUPERSECRETKEY123" not in out and "zzz" not in out and "abc" not in out
    assert "dropped" not in out
    assert json.loads(out.strip().splitlines()[0])["component"] == "test"
