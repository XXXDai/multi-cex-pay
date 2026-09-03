"""匹配引擎的单测：这是全项目最需要正确的部分。"""

from decimal import Decimal

import pytest

from cexpay.config import MatchPolicy
from cexpay.exchanges.base import Transaction
from cexpay.matching import (
    TIER_IDENTIFIER,
    TIER_MEMO,
    TIER_UNIQUE_AMOUNT,
    OrderView,
    allocate_unique_amount,
    find_match,
    generate_memo,
    string_similarity,
)

NOW = 1_700_000_000_000


def make_order(**kwargs) -> OrderView:
    defaults = {
        "order_id": "o1",
        "exchange": None,
        "pay_amount": Decimal("9.9001"),
        "currency": "USDT",
        "created_ms": NOW,
        "expires_ms": NOW + 1800_000,
    }
    defaults.update(kwargs)
    return OrderView(**defaults)


def make_tx(**kwargs) -> Transaction:
    defaults = {
        "exchange": "binance",
        "tx_id": "tx1",
        "amount": Decimal("9.9001"),
        "currency": "USDT",
        "timestamp_ms": NOW + 60_000,
    }
    defaults.update(kwargs)
    return Transaction(**defaults)


@pytest.fixture()
def policy() -> MatchPolicy:
    return MatchPolicy()


# ---------------------------------------------------------------- 唯一金额
def test_unique_amount_is_sequential_and_skips_taken():
    base = Decimal("10")
    first = allocate_unique_amount(base, [])
    assert first == Decimal("10.0001")
    second = allocate_unique_amount(base, [first])
    assert second == Decimal("10.0002")
    third = allocate_unique_amount(base, [first, second, Decimal("10.0003")])
    assert third == Decimal("10.0004")


def test_unique_amount_respects_decimals():
    assert allocate_unique_amount(Decimal("5"), [], decimals=2) == Decimal("5.01")
    assert allocate_unique_amount(Decimal("5"), [], decimals=0) == Decimal("5")


def test_exact_amount_matches_tier1(policy):
    order = make_order()
    match = find_match(order, [make_tx()], policy)
    assert match is not None
    assert match.tier == TIER_UNIQUE_AMOUNT


# ---------------------------------------------------------------- 金额边界
def test_underpay_is_rejected(policy):
    order = make_order()
    tx = make_tx(amount=Decimal("9.80"))
    assert find_match(order, [tx], policy) is None


def test_underpay_within_tolerance_needs_another_tier(policy):
    """容差内的少付不会被 T1 命中（金额不精确），但有标识时可以核销。"""
    order = make_order(identifier_kind="payer_uid_last3", identifier_value="123")
    tx = make_tx(exchange="bitget", amount=Decimal("9.89"), payer_uid="999123")
    match = find_match(order, [tx], policy)
    assert match is not None
    assert match.tier == TIER_IDENTIFIER


def test_large_overpay_is_not_auto_settled(policy):
    order = make_order(identifier_kind="payer_uid_last3", identifier_value="123")
    tx = make_tx(exchange="bitget", amount=Decimal("500"), payer_uid="999123")
    assert find_match(order, [tx], policy) is None


# ---------------------------------------------------------------- 时间窗
def test_tx_before_window_is_rejected(policy):
    order = make_order()
    tx = make_tx(timestamp_ms=NOW - 2 * 3600 * 1000)
    assert find_match(order, [tx], policy) is None


def test_tx_after_window_is_rejected(policy):
    order = make_order()
    tx = make_tx(timestamp_ms=order.expires_ms + policy.window_after_s * 1000 + 60_000)
    assert find_match(order, [tx], policy) is None


def test_early_payment_within_grace_is_accepted(policy):
    """用户先付款、后下单的场景。"""
    order = make_order()
    tx = make_tx(timestamp_ms=NOW - 600_000)
    assert find_match(order, [tx], policy) is not None


# ---------------------------------------------------------------- 币种 / 渠道
def test_wrong_currency_is_rejected(policy):
    assert find_match(make_order(), [make_tx(currency="USDC")], policy) is None


def test_order_pinned_to_exchange_ignores_others(policy):
    order = make_order(exchange="okx")
    assert find_match(order, [make_tx(exchange="binance")], policy) is None


# ---------------------------------------------------------------- 备注码
def test_memo_match(policy):
    order = make_order(memo="123456")
    tx = make_tx(amount=Decimal("9.95"), memo="订单 123456 谢谢")
    match = find_match(order, [tx], policy)
    assert match is not None
    assert match.tier == TIER_MEMO


def test_memo_tolerates_separators(policy):
    order = make_order(memo="123456")
    tx = make_tx(amount=Decimal("9.95"), memo="12-34-56")
    match = find_match(order, [tx], policy)
    assert match is not None and match.tier == TIER_MEMO


def test_wrong_memo_does_not_match(policy):
    order = make_order(memo="123456")
    tx = make_tx(amount=Decimal("9.95"), memo="654321")
    assert find_match(order, [tx], policy) is None


# ---------------------------------------------------------------- 付款方标识
@pytest.mark.parametrize(
    "typed,masked,should_match",
    [
        ("MingLi", "Ming***Li", True),
        ("Satoshi", "Sat***shi", True),
        ("wang wei", "Wang***Wei", True),
        ("张三丰", "张*丰", True),
        ("alice", "bob", False),
        ("张三", "张*", False),      # 太短，信息量不足
        ("李", "李四", False),
    ],
)
def test_payer_name_similarity(policy, typed, masked, should_match):
    order = make_order(
        pay_amount=Decimal("9.9"),
        identifier_kind="payer_name",
        identifier_value=typed,
    )
    tx = make_tx(amount=Decimal("9.91"), payer_name=masked)
    assert (find_match(order, [tx], policy) is not None) is should_match


def test_uid_last3_match(policy):
    order = make_order(identifier_kind="payer_uid_last3", identifier_value="165")
    tx = make_tx(exchange="bitget", amount=Decimal("9.91"), payer_uid="1000000165")
    match = find_match(order, [tx], policy)
    assert match is not None and match.tier == TIER_IDENTIFIER


def test_uid_last3_mismatch(policy):
    order = make_order(identifier_kind="payer_uid_last3", identifier_value="164")
    tx = make_tx(exchange="bitget", amount=Decimal("9.91"), payer_uid="1000000165")
    assert find_match(order, [tx], policy) is None


def test_withdraw_id_last3_match(policy):
    order = make_order(identifier_kind="withdraw_id_last3", identifier_value="728")
    tx = make_tx(exchange="okx", amount=Decimal("9.91"), withdraw_id="99887728")
    match = find_match(order, [tx], policy)
    assert match is not None and match.tier == TIER_IDENTIFIER


# ---------------------------------------------------------------- 去重 / 优先级
def test_used_tx_is_skipped(policy):
    order = make_order()
    tx = make_tx()
    used = {f"{tx.exchange}:{tx.tx_id}"}
    assert find_match(order, [tx], policy, used_tx_ids=used) is None


def test_tier1_wins_over_tier3(policy):
    order = make_order(identifier_kind="payer_name", identifier_value="MingLi")
    exact = make_tx(tx_id="exact")
    fuzzy = make_tx(tx_id="fuzzy", amount=Decimal("9.95"), payer_name="Ming***Li")
    match = find_match(order, [fuzzy, exact], policy)
    assert match.transaction.tx_id == "exact"
    assert match.tier == TIER_UNIQUE_AMOUNT


def test_earliest_tx_wins_within_same_tier(policy):
    order = make_order()
    late = make_tx(tx_id="late", timestamp_ms=NOW + 600_000)
    early = make_tx(tx_id="early", timestamp_ms=NOW + 10_000)
    assert find_match(order, [late, early], policy).transaction.tx_id == "early"


def test_allow_tiers_can_disable_identifier(policy):
    order = make_order(
        pay_amount=Decimal("9.9"),
        identifier_kind="payer_name",
        identifier_value="MingLi",
    )
    tx = make_tx(amount=Decimal("9.91"), payer_name="Ming***Li")
    assert find_match(order, [tx], policy, allow_tiers=(TIER_UNIQUE_AMOUNT,)) is None


def test_generate_memo_length():
    assert len(generate_memo(6)) == 6
    assert generate_memo(4).isdigit()


def test_similarity_is_symmetric():
    assert string_similarity("abc", "abcd") == string_similarity("abcd", "abc")
