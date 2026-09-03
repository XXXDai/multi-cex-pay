"""存储层：重点是"一笔钱只能核销一张单"这个不变量。"""

from decimal import Decimal

from cexpay.store import (
    STATUS_CANCELLED,
    STATUS_EXPIRED,
    STATUS_PAID,
    STATUS_PENDING,
    Order,
    OrderStore,
    new_order_id,
    now_ms,
)


def make_order(store: OrderStore, amount="9.9001", ttl_ms=1800_000) -> Order:
    created = now_ms()
    order = Order(
        order_id=new_order_id(),
        base_amount=Decimal("9.9"),
        pay_amount=Decimal(amount),
        currency="USDT",
        status=STATUS_PENDING,
        created_ms=created,
        expires_ms=created + ttl_ms,
    )
    return store.create(order)


def test_settle_marks_paid(tmp_path):
    store = OrderStore(tmp_path / "t.sqlite3")
    order = make_order(store)
    settled = store.settle(
        order.order_id, exchange="binance", tx_id="TX1",
        amount=order.pay_amount, tier=1, reason="金额命中",
    )
    assert settled.status == STATUS_PAID
    assert settled.matched_tx_id == "TX1"
    assert settled.paid_ms is not None


def test_same_tx_cannot_settle_two_orders(tmp_path):
    store = OrderStore(tmp_path / "t.sqlite3")
    a = make_order(store, "9.9001")
    b = make_order(store, "9.9002")

    assert store.settle(a.order_id, exchange="okx", tx_id="DUP",
                        amount=a.pay_amount, tier=1, reason="") is not None
    # 同一笔流水第二次使用必须失败
    assert store.settle(b.order_id, exchange="okx", tx_id="DUP",
                        amount=b.pay_amount, tier=1, reason="") is None
    assert store.get(b.order_id).status == STATUS_PENDING


def test_settling_twice_is_idempotent_no_op(tmp_path):
    store = OrderStore(tmp_path / "t.sqlite3")
    order = make_order(store)
    store.settle(order.order_id, exchange="okx", tx_id="T1",
                 amount=order.pay_amount, tier=1, reason="")
    # 订单已是 paid，再核销（换一个流水号）应该被拒
    assert store.settle(order.order_id, exchange="okx", tx_id="T2",
                        amount=order.pay_amount, tier=1, reason="") is None


def test_used_tx_keys(tmp_path):
    store = OrderStore(tmp_path / "t.sqlite3")
    order = make_order(store)
    store.settle(order.order_id, exchange="bitget", tx_id="X9",
                 amount=order.pay_amount, tier=1, reason="")
    assert "bitget:X9" in store.used_tx_keys()


def test_expire_stale(tmp_path):
    store = OrderStore(tmp_path / "t.sqlite3")
    order = make_order(store, ttl_ms=-1000)  # 已过期
    assert store.expire_stale() == [order.order_id]
    assert store.get(order.order_id).status == STATUS_EXPIRED


def test_cancel(tmp_path):
    store = OrderStore(tmp_path / "t.sqlite3")
    order = make_order(store)
    assert store.cancel(order.order_id).status == STATUS_CANCELLED


def test_amount_locks_and_release(tmp_path):
    store = OrderStore(tmp_path / "t.sqlite3")
    store.lock_amount("USDT", Decimal("9.9001"), "o1", 3600)
    assert Decimal("9.9001") in store.locked_amounts("USDT")
    store.release_amount("USDT", Decimal("9.9001"))
    assert Decimal("9.9001") not in store.locked_amounts("USDT")


def test_expired_locks_are_purged(tmp_path):
    store = OrderStore(tmp_path / "t.sqlite3")
    store.lock_amount("USDT", Decimal("1.0001"), "o1", -10)  # 已到期
    assert store.locked_amounts("USDT") == []


def test_callback_state_transitions(tmp_path):
    store = OrderStore(tmp_path / "t.sqlite3")
    created = now_ms()
    order = store.create(Order(
        order_id=new_order_id(),
        base_amount=Decimal("1"), pay_amount=Decimal("1.0001"), currency="USDT",
        status=STATUS_PENDING, created_ms=created, expires_ms=created + 60_000,
        callback_url="https://example.invalid/hook",
    ))
    store.settle(order.order_id, exchange="okx", tx_id="C1",
                 amount=order.pay_amount, tier=1, reason="")
    due = store.callbacks_due()
    assert [o.order_id for o in due] == [order.order_id]

    store.update_callback(order.order_id, state="delivered", attempts=1, next_ms=None)
    assert store.callbacks_due() == []


def test_no_callback_url_means_no_pending_callback(tmp_path):
    store = OrderStore(tmp_path / "t.sqlite3")
    order = make_order(store)
    store.settle(order.order_id, exchange="okx", tx_id="N1",
                 amount=order.pay_amount, tier=1, reason="")
    assert store.get(order.order_id).callback_state == "none"
    assert store.callbacks_due() == []


def test_get_by_ref_returns_latest(tmp_path):
    store = OrderStore(tmp_path / "t.sqlite3")
    created = now_ms()
    for i, amount in enumerate(["1.0001", "1.0002"]):
        store.create(Order(
            order_id=new_order_id(), merchant_ref="REF-1",
            base_amount=Decimal("1"), pay_amount=Decimal(amount), currency="USDT",
            status=STATUS_PENDING, created_ms=created + i, expires_ms=created + 60_000,
        ))
    assert store.get_by_ref("REF-1").pay_amount == Decimal("1.0002")


def test_stats(tmp_path):
    store = OrderStore(tmp_path / "t.sqlite3")
    a = make_order(store, "2.0001")
    make_order(store, "2.0002")
    store.settle(a.order_id, exchange="okx", tx_id="S1",
                 amount=a.pay_amount, tier=1, reason="")
    stats = store.stats()
    assert stats["paid_count"] == 1
    assert stats["by_status"]["pending"] == 1
