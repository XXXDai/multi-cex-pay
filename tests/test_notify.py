"""回调签名与重试阶梯。"""

import json

import pytest

from cexpay.notify import (
    MAX_ATTEMPTS,
    RETRY_LADDER,
    next_delay_s,
    sign_payload,
    verify_signature,
)


def test_sign_and_verify_roundtrip():
    body = json.dumps({"event": "order.paid"}, separators=(",", ":"))
    sig = sign_payload("s3cret", 1700000000, body)
    assert verify_signature("s3cret", 1700000000, body, sig)


@pytest.mark.parametrize(
    "secret,ts,body",
    [("wrong", 1700000000, '{"a":1}'), ("s3cret", 1700000001, '{"a":1}'), ("s3cret", 1700000000, '{"a":2}')],
)
def test_verify_rejects_tampering(secret, ts, body):
    sig = sign_payload("s3cret", 1700000000, '{"a":1}')
    assert not verify_signature(secret, ts, body, sig)


def test_verify_rejects_empty_signature():
    assert not verify_signature("s", 1, "{}", "")


def test_signature_is_hex_sha256():
    sig = sign_payload("s", 1, "{}")
    assert len(sig) == 64
    int(sig, 16)  # 必须是合法 hex


def test_retry_ladder_is_increasing():
    assert list(RETRY_LADDER) == sorted(RETRY_LADDER)
    assert RETRY_LADDER[0] == 0          # 第一次立即重试


def test_next_delay_walks_the_ladder():
    assert next_delay_s(0) == RETRY_LADDER[0]
    assert next_delay_s(1) == RETRY_LADDER[1]
    assert next_delay_s(MAX_ATTEMPTS - 1) == RETRY_LADDER[-1]


def test_next_delay_gives_up_after_max():
    assert next_delay_s(MAX_ATTEMPTS) is None
    assert next_delay_s(MAX_ATTEMPTS + 5) is None
