"""网关：下单、核销、回调的集成行为（交易所层用假数据）。"""

from decimal import Decimal

import pytest

from cexpay.errors import OrderError
from cexpay.exchanges.base import Transaction
from cexpay.store import STATUS_PAID, STATUS_PENDING, now_ms


def fake_fetch(gateway, transactions):
    """把 gateway.fetch_transactions 换成固定返回。"""
    gateway.fetch_transactions = lambda *a, **k: (list(transactions), [])


def test_create_order_allocates_unique_amount(gateway):
    a = gateway.create_order("10")
    b = gateway.create_order("10")
    assert a.pay_amount != b.pay_amount
    assert a.base_amount == b.base_amount == Decimal("10")


def test_create_order_rejects_bad_amount(gateway):
    for bad in ("0", "-3", "abc", ""):
        with pytest.raises(OrderError):
            gateway.create_order(bad)


def test_create_order_rejects_unconfigured_exchange(gateway):
    with pytest.raises(OrderError):
        gateway.create_order("1", exchange="binance")   # 没配凭据


def test_sweep_settles_on_exact_amount(gateway):
    order = gateway.create_order("10")
    fake_fetch(gateway, [Transaction(
        exchange="binance", tx_id="T1", amount=order.pay_amount,
        currency="USDT", timestamp_ms=now_ms(),
    )])
    result = gateway.sweep()
    assert len(result["settled"]) == 1
    assert gateway.get_order(order.order_id).status == STATUS_PAID


def test_sweep_does_not_settle_twice_with_one_tx(gateway):
    a = gateway.create_order("10")
    gateway.create_order("10")
    fake_fetch(gateway, [Transaction(
        exchange="binance", tx_id="ONLY", amount=a.pay_amount,
        currency="USDT", timestamp_ms=now_ms(),
    )])
    gateway.sweep()
    result = gateway.sweep()
    assert result["settled"] == []


def test_sweep_ignores_unrelated_amount(gateway):
    order = gateway.create_order("10")
    fake_fetch(gateway, [Transaction(
        exchange="binance", tx_id="T2", amount=Decimal("3"),
        currency="USDT", timestamp_ms=now_ms(),
    )])
    gateway.sweep()
    assert gateway.get_order(order.order_id).status == STATUS_PENDING


def test_sweep_scoped_to_single_order(gateway):
    a = gateway.create_order("10")
    b = gateway.create_order("20")
    fake_fetch(gateway, [
        Transaction(exchange="okx", tx_id="A", amount=a.pay_amount,
                    currency="USDT", timestamp_ms=now_ms()),
        Transaction(exchange="okx", tx_id="B", amount=b.pay_amount,
                    currency="USDT", timestamp_ms=now_ms()),
    ])
    result = gateway.sweep(order_id=b.order_id)
    assert result["checked"] == 1
    assert gateway.get_order(a.order_id).status == STATUS_PENDING
    assert gateway.get_order(b.order_id).status == STATUS_PAID


def test_sweep_settles_via_identifier(gateway):
    order = gateway.create_order("10")
    order = gateway.submit_identifier(order.order_id, "payer_uid_last3", "165")
    fake_fetch(gateway, [Transaction(
        exchange="bitget", tx_id="U1", amount=order.pay_amount + Decimal("0.01"),
        currency="USDT", timestamp_ms=now_ms(), payer_uid="1000000165",
    )])
    gateway.sweep()
    settled = gateway.get_order(order.order_id)
    assert settled.status == STATUS_PAID
    assert settled.match_tier == 3


def test_require_identifier_blocks_until_submitted(gateway, monkeypatch):
    monkeypatch.setenv("CEXPAY_REQUIRE_IDENTIFIER", "true")
    order = gateway.create_order("10")
    fake_fetch(gateway, [Transaction(
        exchange="binance", tx_id="R1", amount=order.pay_amount,
        currency="USDT", timestamp_ms=now_ms(),
    )])
    gateway.sweep()
    assert gateway.get_order(order.order_id).status == STATUS_PENDING

    gateway.submit_identifier(order.order_id, "payer_name", "MingLi")
    gateway.sweep()
    assert gateway.get_order(order.order_id).status == STATUS_PAID


def test_submit_identifier_on_paid_order_fails(gateway):
    order = gateway.create_order("10")
    gateway.manual_settle(order.order_id, exchange="binance", tx_id="M1")
    with pytest.raises(OrderError):
        gateway.submit_identifier(order.order_id, "payer_name", "MingLi")


def test_manual_settle_rejects_reused_tx(gateway):
    a = gateway.create_order("10")
    b = gateway.create_order("10")
    gateway.manual_settle(a.order_id, exchange="okx", tx_id="SAME")
    with pytest.raises(OrderError, match="已被订单"):
        gateway.manual_settle(b.order_id, exchange="okx", tx_id="SAME")


def test_expired_order_is_not_settled(gateway):
    order = gateway.create_order("10", ttl_s=60)
    # 把过期时间挪到过去
    gateway.store._conn.execute(
        "UPDATE orders SET expires_ms = ? WHERE order_id = ?",
        (now_ms() - 10_000, order.order_id),
    )
    gateway.store._conn.commit()
    fake_fetch(gateway, [Transaction(
        exchange="binance", tx_id="E1", amount=order.pay_amount,
        currency="USDT", timestamp_ms=now_ms(),
    )])
    gateway.sweep()
    assert gateway.get_order(order.order_id).status == "expired"


def test_callback_is_marked_pending_then_failed(gateway, monkeypatch):
    calls = []

    def fake_deliver(url, payload, **kwargs):
        calls.append(url)
        return False, "HTTP 500"

    monkeypatch.setattr("cexpay.gateway.deliver", fake_deliver)
    order = gateway.create_order("10", callback_url="https://example.invalid/hook")
    gateway.manual_settle(order.order_id, exchange="okx", tx_id="CB1")

    assert calls == ["https://example.invalid/hook"]
    refreshed = gateway.store.get(order.order_id)
    assert refreshed.callback_state == "pending"
    assert refreshed.callback_attempts == 1


def test_callback_success_marks_delivered(gateway, monkeypatch):
    monkeypatch.setattr("cexpay.gateway.deliver", lambda url, payload, **kw: (True, "HTTP 200"))
    order = gateway.create_order("10", callback_url="https://example.invalid/hook")
    gateway.manual_settle(order.order_id, exchange="okx", tx_id="CB2")
    assert gateway.store.get(order.order_id).callback_state == "delivered"


def test_merchant_ref_reuses_pending_order(gateway):
    a = gateway.create_order("10", merchant_ref="SHOP-9")
    b = gateway.create_order("10", merchant_ref="SHOP-9")
    assert a.order_id == b.order_id


def test_merchant_ref_creates_new_order_after_paid(gateway):
    a = gateway.create_order("10", merchant_ref="SHOP-9")
    gateway.manual_settle(a.order_id, exchange="okx", tx_id="P1")
    b = gateway.create_order("10", merchant_ref="SHOP-9")
    assert a.order_id != b.order_id


def test_stats(gateway):
    gateway.create_order("10")
    assert gateway.stats()["by_status"]["pending"] == 1
