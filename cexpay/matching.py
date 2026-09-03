"""订单核销引擎：把一笔进账记录配到一张待付订单上。

匹配是分层的，从最可靠到最兜底：

  T1 唯一金额  ── 下单时给每笔订单分配互不相同的尾数（10 → 10.0037），
                  金额本身就是订单指纹。三家交易所都支持，用户零输入。
  T2 备注码    ── 转账备注里带 6 位码。目前只有 Binance Pay 能读到备注。
  T3 付款方标识 ── 用户主动提交：Binance 昵称（模糊匹配）/ Bitget UID 后三位 /
                  OKX 提币申请 ID 后三位。
  T4 人工核销  ── 前三层都没命中时，商家在后台手动放行。

任何一层命中，都还要通过时间窗和金额校验；并且一笔 tx 只能核销一张订单。
"""

from __future__ import annotations

import random
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from difflib import SequenceMatcher
from typing import Any

from .config import MatchPolicy
from .exchanges.base import Transaction

MEMO_ALPHABET = "0123456789"

# 昵称相似度的两道保险：公共子串至少这么长才算数，
# 以及有效字符少于阈值时的分数上限（默认阈值 0.6，所以 0.5 等于直接否掉）
MIN_LCS_CHARS = 2
MIN_CONFIDENT_CHARS = 3
SHORT_NAME_SCORE_CAP = 0.5

# 匹配层级，数字越小越可信
TIER_UNIQUE_AMOUNT = 1
TIER_MEMO = 2
TIER_IDENTIFIER = 3
TIER_MANUAL = 4

TIER_LABELS = {
    TIER_UNIQUE_AMOUNT: "唯一金额",
    TIER_MEMO: "备注码",
    TIER_IDENTIFIER: "付款方标识",
    TIER_MANUAL: "人工核销",
}


# --------------------------------------------------------------------------
# 唯一金额
# --------------------------------------------------------------------------
def allocate_unique_amount(
    base_amount: Decimal,
    taken: Iterable[Decimal],
    *,
    decimals: int = 4,
) -> Decimal:
    """在 ``base_amount`` 上追加一个尚未被占用的尾数。

    4 位小数意味着同一个价位可以同时挂 9999 笔订单（0.0001 ~ 0.9999）。
    尾数从 1 开始顺序找，找不到空位就随机试，都失败则原样返回。
    """
    if decimals <= 0:
        return base_amount

    step = Decimal(1).scaleb(-decimals)          # 10^-decimals
    slots = 10 ** decimals
    base = base_amount.quantize(step)
    taken_set = {Decimal(str(t)).quantize(step) for t in taken}

    # 顺序扫描：低并发时结果稳定、可读性好
    for i in range(1, slots):
        candidate = base + step * i
        if candidate not in taken_set:
            return candidate

    # 理论上到不了这里（说明同价位并发已经打满）
    for _ in range(64):
        candidate = base + step * random.randint(1, slots - 1)
        if candidate not in taken_set:
            return candidate
    return base


def generate_memo(length: int = 6) -> str:
    """生成数字备注码。"""
    return "".join(random.choice(MEMO_ALPHABET) for _ in range(length))


# --------------------------------------------------------------------------
# 字符串相似度（用于 Binance 昵称模糊匹配）
# --------------------------------------------------------------------------
def _longest_common_substring_len(s1: str, s2: str) -> int:
    if not s1 or not s2:
        return 0
    prev = [0] * (len(s2) + 1)
    best = 0
    for i in range(1, len(s1) + 1):
        cur = [0] * (len(s2) + 1)
        for j in range(1, len(s2) + 1):
            if s1[i - 1] == s2[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def string_similarity(a: str, b: str) -> float:
    """0~1 的相似度。

    交易所会把昵称脱敏成 ``Ming*****Li`` 这种形式，所以不能只用编辑距离：
    这里取"最长公共子串占比"和 difflib 比值里更大的那个，并对包含关系加成。
    """
    if not a or not b:
        return 0.0
    a = re.sub(r"\s+", "", a.lower())
    b = re.sub(r"\s+", "", b.lower())
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0

    # 去掉脱敏星号后再比一次，命中率更高
    a_clean = a.replace("*", "")
    b_clean = b.replace("*", "")

    scores = [SequenceMatcher(None, a, b).ratio()]
    if a_clean and b_clean:
        scores.append(SequenceMatcher(None, a_clean, b_clean).ratio())
        if a_clean in b_clean or b_clean in a_clean:
            shorter, longer = sorted((a_clean, b_clean), key=len)
            scores.append(0.1 + 0.9 * len(shorter) / len(longer))

    lcs = _longest_common_substring_len(a_clean or a, b_clean or b)
    shortest = min(len(a_clean or a), len(b_clean or b))
    # 公共子串太短时不足以说明问题（"张三" vs "张*" 不该算命中）
    if shortest and lcs >= MIN_LCS_CHARS:
        scores.append(lcs / shortest)

    score = max(scores)
    # 名字本身太短时信息量不足，无论算出多高都不给满分。
    # 这里量的是脱敏前的长度："张*丰" 有 3 个字符位，"张*" 只有 2 个。
    if min(len(a), len(b)) < MIN_CONFIDENT_CHARS:
        score = min(score, SHORT_NAME_SCORE_CAP)
    return score


# --------------------------------------------------------------------------
# 匹配
# --------------------------------------------------------------------------
@dataclass
class MatchCandidate:
    """一次成功的配对。"""

    transaction: Transaction
    tier: int
    score: float = 1.0
    reason: str = ""

    @property
    def tier_label(self) -> str:
        return TIER_LABELS.get(self.tier, str(self.tier))

    def to_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier,
            "tier_label": self.tier_label,
            "score": round(self.score, 4),
            "reason": self.reason,
            "transaction": self.transaction.to_dict(),
        }


@dataclass
class OrderView:
    """匹配器只关心订单的这几个字段。

    这样 matching 模块不依赖存储层，方便单测。
    """

    order_id: str
    exchange: str | None          # None 表示接受任意已配置的交易所
    pay_amount: Decimal
    currency: str
    created_ms: int
    expires_ms: int
    memo: str | None = None
    identifier_kind: str | None = None
    identifier_value: str | None = None


def _amount_ok(order: OrderView, tx: Transaction, policy: MatchPolicy) -> bool:
    """少付不通过；多付在容差内通过；多付太多也不自动核销。"""
    delta = tx.amount - order.pay_amount
    if delta < -policy.amount_tolerance:
        return False    # 少付
    # 多付太多不自动核销：可能是一笔大额充值刚好落在时间窗里，配到小额订单上就亏了
    return not (policy.max_overpay is not None and delta > policy.max_overpay)


def _time_ok(order: OrderView, tx: Transaction, policy: MatchPolicy) -> bool:
    window_start = order.created_ms - policy.window_before_s * 1000
    window_end = max(order.expires_ms, order.created_ms) + policy.window_after_s * 1000
    return window_start <= tx.timestamp_ms <= window_end


def _basic_ok(order: OrderView, tx: Transaction, policy: MatchPolicy) -> bool:
    if order.exchange and tx.exchange != order.exchange:
        return False
    if tx.currency and order.currency and tx.currency.upper() != order.currency.upper():
        return False
    if not _amount_ok(order, tx, policy):
        return False
    return _time_ok(order, tx, policy)


def _exact_amount(order: OrderView, tx: Transaction) -> bool:
    """唯一金额必须严格相等（多付会破坏指纹，交给其它层处理）。"""
    return tx.amount == order.pay_amount


def _memo_hit(order: OrderView, tx: Transaction) -> bool:
    if not order.memo or not tx.memo:
        return False
    digits = re.sub(r"\D", "", tx.memo)
    return order.memo in tx.memo or order.memo in digits


def _identifier_hit(
    order: OrderView, tx: Transaction, policy: MatchPolicy
) -> tuple[bool, float, str]:
    kind = order.identifier_kind
    value = (order.identifier_value or "").strip()
    if not kind or not value:
        return False, 0.0, ""

    if kind == "payer_name":
        if not tx.payer_name:
            return False, 0.0, ""
        score = string_similarity(value, tx.payer_name)
        if score >= policy.name_similarity_threshold:
            return True, score, f"昵称「{tx.payer_name}」相似度 {score:.2f}"
        return False, score, ""

    if kind == "payer_uid_last3":
        uid = (tx.payer_uid or "").strip()
        if len(uid) >= 3 and uid[-3:] == value:
            return True, 1.0, f"付款方 UID 尾号 {value}"
        return False, 0.0, ""

    if kind == "withdraw_id_last3":
        wid = (tx.withdraw_id or "").strip()
        if len(wid) >= 3 and wid[-3:] == value:
            return True, 1.0, f"提币申请 ID 尾号 {value}"
        return False, 0.0, ""

    return False, 0.0, ""


def find_match(
    order: OrderView,
    transactions: Sequence[Transaction],
    policy: MatchPolicy,
    *,
    used_tx_ids: set[str] | None = None,
    allow_tiers: Sequence[int] = (TIER_UNIQUE_AMOUNT, TIER_MEMO, TIER_IDENTIFIER),
) -> MatchCandidate | None:
    """在一批进账记录里找出能核销该订单的那一笔。

    返回层级最高（数字最小）、分数最高的候选；没有则返回 None。
    """
    used = used_tx_ids or set()
    candidates: list[MatchCandidate] = []

    for tx in transactions:
        key = f"{tx.exchange}:{tx.tx_id}"
        if key in used or tx.tx_id in used:
            continue
        if not _basic_ok(order, tx, policy):
            continue

        if TIER_UNIQUE_AMOUNT in allow_tiers and _exact_amount(order, tx):
            candidates.append(
                MatchCandidate(tx, TIER_UNIQUE_AMOUNT, 1.0, f"金额精确命中 {tx.amount}")
            )
            continue

        if TIER_MEMO in allow_tiers and policy.enable_memo_match and _memo_hit(order, tx):
            candidates.append(
                MatchCandidate(tx, TIER_MEMO, 1.0, f"备注码命中 {order.memo}")
            )
            continue

        if TIER_IDENTIFIER in allow_tiers:
            hit, score, reason = _identifier_hit(order, tx, policy)
            if hit:
                candidates.append(MatchCandidate(tx, TIER_IDENTIFIER, score, reason))
                continue

    if not candidates:
        return None

    # 层级优先，其次分数，最后取时间最早的一笔（先付先核销）
    candidates.sort(key=lambda c: (c.tier, -c.score, c.transaction.timestamp_ms))
    return candidates[0]
