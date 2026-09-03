"""SQLite 存储层。

只用标准库 ``sqlite3``，零额外依赖，单文件即可跑起来。

三张表：
  orders        订单
  settled_tx    已被使用的进账流水（唯一索引，保证一笔钱只核销一张单）
  amount_locks  唯一金额的占用与冷却（避免用户晚付导致串单）
"""

from __future__ import annotations

import builtins
import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from .matching import OrderView

STATUS_PENDING = "pending"
STATUS_PAID = "paid"
STATUS_EXPIRED = "expired"
STATUS_CANCELLED = "cancelled"

ACTIVE_STATUSES = (STATUS_PENDING,)

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    order_id        TEXT PRIMARY KEY,
    merchant_ref    TEXT,
    exchange        TEXT,
    base_amount     TEXT NOT NULL,
    pay_amount      TEXT NOT NULL,
    currency        TEXT NOT NULL,
    status          TEXT NOT NULL,
    memo            TEXT,
    identifier_kind TEXT,
    identifier_value TEXT,
    created_ms      INTEGER NOT NULL,
    expires_ms      INTEGER NOT NULL,
    paid_ms         INTEGER,
    matched_exchange TEXT,
    matched_tx_id   TEXT,
    match_tier      INTEGER,
    match_reason    TEXT,
    callback_url    TEXT,
    callback_state  TEXT NOT NULL DEFAULT 'none',
    callback_attempts INTEGER NOT NULL DEFAULT 0,
    callback_next_ms INTEGER,
    metadata        TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status, expires_ms);
CREATE INDEX IF NOT EXISTS idx_orders_ref ON orders(merchant_ref);

CREATE TABLE IF NOT EXISTS settled_tx (
    exchange   TEXT NOT NULL,
    tx_id      TEXT NOT NULL,
    order_id   TEXT NOT NULL,
    amount     TEXT NOT NULL,
    settled_ms INTEGER NOT NULL,
    PRIMARY KEY (exchange, tx_id)
);

CREATE TABLE IF NOT EXISTS amount_locks (
    currency    TEXT NOT NULL,
    pay_amount  TEXT NOT NULL,
    order_id    TEXT NOT NULL,
    released_ms INTEGER NOT NULL,
    PRIMARY KEY (currency, pay_amount)
);
CREATE INDEX IF NOT EXISTS idx_locks_release ON amount_locks(released_ms);
"""


def now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class Order:
    order_id: str
    base_amount: Decimal
    pay_amount: Decimal
    currency: str
    status: str
    created_ms: int
    expires_ms: int
    exchange: str | None = None
    merchant_ref: str | None = None
    memo: str | None = None
    identifier_kind: str | None = None
    identifier_value: str | None = None
    paid_ms: int | None = None
    matched_exchange: str | None = None
    matched_tx_id: str | None = None
    match_tier: int | None = None
    match_reason: str | None = None
    callback_url: str | None = None
    callback_state: str = "none"
    callback_attempts: int = 0
    callback_next_ms: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_expired(self) -> bool:
        return self.status == STATUS_PENDING and now_ms() > self.expires_ms

    def to_view(self) -> OrderView:
        return OrderView(
            order_id=self.order_id,
            exchange=self.exchange,
            pay_amount=self.pay_amount,
            currency=self.currency,
            created_ms=self.created_ms,
            expires_ms=self.expires_ms,
            memo=self.memo,
            identifier_kind=self.identifier_kind,
            identifier_value=self.identifier_value,
        )

    def to_dict(self, *, public: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "order_id": self.order_id,
            "merchant_ref": self.merchant_ref,
            "exchange": self.exchange,
            "base_amount": str(self.base_amount),
            "pay_amount": str(self.pay_amount),
            "currency": self.currency,
            "status": self.status,
            "memo": self.memo,
            "created_ms": self.created_ms,
            "expires_ms": self.expires_ms,
            "expires_in_s": max(0, (self.expires_ms - now_ms()) // 1000),
            "paid_ms": self.paid_ms,
            "metadata": self.metadata,
        }
        if self.status == STATUS_PAID:
            data["settlement"] = {
                "exchange": self.matched_exchange,
                "tx_id": self.matched_tx_id,
                "tier": self.match_tier,
                "reason": self.match_reason,
            }
        if not public:
            data.update(
                {
                    "identifier_kind": self.identifier_kind,
                    "identifier_value": self.identifier_value,
                    "callback_url": self.callback_url,
                    "callback_state": self.callback_state,
                    "callback_attempts": self.callback_attempts,
                }
            )
        return data


def _row_to_order(row: sqlite3.Row) -> Order:
    return Order(
        order_id=row["order_id"],
        merchant_ref=row["merchant_ref"],
        exchange=row["exchange"],
        base_amount=Decimal(row["base_amount"]),
        pay_amount=Decimal(row["pay_amount"]),
        currency=row["currency"],
        status=row["status"],
        memo=row["memo"],
        identifier_kind=row["identifier_kind"],
        identifier_value=row["identifier_value"],
        created_ms=row["created_ms"],
        expires_ms=row["expires_ms"],
        paid_ms=row["paid_ms"],
        matched_exchange=row["matched_exchange"],
        matched_tx_id=row["matched_tx_id"],
        match_tier=row["match_tier"],
        match_reason=row["match_reason"],
        callback_url=row["callback_url"],
        callback_state=row["callback_state"],
        callback_attempts=row["callback_attempts"],
        callback_next_ms=row["callback_next_ms"],
        metadata=json.loads(row["metadata"] or "{}"),
    )


class OrderStore:
    """线程安全的订单存储。"""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------
    # 唯一金额
    # ------------------------------------------------------------------
    def locked_amounts(self, currency: str) -> builtins.list[Decimal]:
        """当前仍在占用（含冷却期）的金额。"""
        with self._lock:
            self._conn.execute(
                "DELETE FROM amount_locks WHERE released_ms < ?", (now_ms(),)
            )
            self._conn.commit()
            rows = self._conn.execute(
                "SELECT pay_amount FROM amount_locks WHERE currency = ?",
                (currency.upper(),),
            ).fetchall()
        return [Decimal(r["pay_amount"]) for r in rows]

    def lock_amount(
        self, currency: str, pay_amount: Decimal, order_id: str, cooldown_s: int
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO amount_locks"
                " (currency, pay_amount, order_id, released_ms) VALUES (?, ?, ?, ?)",
                (currency.upper(), str(pay_amount), order_id, now_ms() + cooldown_s * 1000),
            )
            self._conn.commit()

    def release_amount(self, currency: str, pay_amount: Decimal) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM amount_locks WHERE currency = ? AND pay_amount = ?",
                (currency.upper(), str(pay_amount)),
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # 订单
    # ------------------------------------------------------------------
    def create(self, order: Order) -> Order:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO orders (
                    order_id, merchant_ref, exchange, base_amount, pay_amount, currency,
                    status, memo, identifier_kind, identifier_value, created_ms, expires_ms,
                    callback_url, metadata
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    order.order_id,
                    order.merchant_ref,
                    order.exchange,
                    str(order.base_amount),
                    str(order.pay_amount),
                    order.currency.upper(),
                    order.status,
                    order.memo,
                    order.identifier_kind,
                    order.identifier_value,
                    order.created_ms,
                    order.expires_ms,
                    order.callback_url,
                    json.dumps(order.metadata, ensure_ascii=False),
                ),
            )
            self._conn.commit()
        return order

    def get(self, order_id: str) -> Order | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM orders WHERE order_id = ?", (order_id,)
            ).fetchone()
        return _row_to_order(row) if row else None

    def get_by_ref(self, merchant_ref: str) -> Order | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM orders WHERE merchant_ref = ? ORDER BY created_ms DESC LIMIT 1",
                (merchant_ref,),
            ).fetchone()
        return _row_to_order(row) if row else None

    def list(
        self,
        *,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> builtins.list[Order]:
        query = "SELECT * FROM orders"
        params: list[Any] = []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY created_ms DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [_row_to_order(r) for r in rows]

    def pending_orders(self) -> builtins.list[Order]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM orders WHERE status = ? ORDER BY created_ms ASC",
                (STATUS_PENDING,),
            ).fetchall()
        return [_row_to_order(r) for r in rows]

    def set_identifier(self, order_id: str, kind: str, value: str) -> Order | None:
        with self._lock:
            self._conn.execute(
                "UPDATE orders SET identifier_kind = ?, identifier_value = ?"
                " WHERE order_id = ? AND status = ?",
                (kind, value, order_id, STATUS_PENDING),
            )
            self._conn.commit()
        return self.get(order_id)

    def cancel(self, order_id: str) -> Order | None:
        with self._lock:
            self._conn.execute(
                "UPDATE orders SET status = ? WHERE order_id = ? AND status = ?",
                (STATUS_CANCELLED, order_id, STATUS_PENDING),
            )
            self._conn.commit()
        return self.get(order_id)

    def expire_stale(self) -> builtins.list[str]:
        """把过期的待付订单标记为 expired，返回受影响的订单号。"""
        cutoff = now_ms()
        with self._lock:
            rows = self._conn.execute(
                "SELECT order_id FROM orders WHERE status = ? AND expires_ms < ?",
                (STATUS_PENDING, cutoff),
            ).fetchall()
            if rows:
                self._conn.execute(
                    "UPDATE orders SET status = ? WHERE status = ? AND expires_ms < ?",
                    (STATUS_EXPIRED, STATUS_PENDING, cutoff),
                )
                self._conn.commit()
        return [r["order_id"] for r in rows]

    # ------------------------------------------------------------------
    # 核销
    # ------------------------------------------------------------------
    def used_tx_keys(self) -> set[str]:
        with self._lock:
            rows = self._conn.execute("SELECT exchange, tx_id FROM settled_tx").fetchall()
        return {f"{r['exchange']}:{r['tx_id']}" for r in rows}

    def settle(
        self,
        order_id: str,
        *,
        exchange: str,
        tx_id: str,
        amount: Decimal,
        tier: int,
        reason: str,
    ) -> Order | None:
        """把订单标记为已付。

        ``settled_tx`` 的主键保证同一笔流水不会核销两张单：
        并发下第二次插入会抛 IntegrityError，直接返回 None。
        """
        stamp = now_ms()
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO settled_tx (exchange, tx_id, order_id, amount, settled_ms)"
                    " VALUES (?,?,?,?,?)",
                    (exchange, tx_id, order_id, str(amount), stamp),
                )
            except sqlite3.IntegrityError:
                self._conn.rollback()
                return None

            cursor = self._conn.execute(
                """
                UPDATE orders SET
                    status = ?, paid_ms = ?, matched_exchange = ?, matched_tx_id = ?,
                    match_tier = ?, match_reason = ?,
                    callback_state = CASE WHEN callback_url IS NULL OR callback_url = ''
                                          THEN 'none' ELSE 'pending' END,
                    callback_next_ms = ?
                WHERE order_id = ? AND status = ?
                """,
                (
                    STATUS_PAID, stamp, exchange, tx_id, tier, reason, stamp,
                    order_id, STATUS_PENDING,
                ),
            )
            if cursor.rowcount == 0:
                # 订单已经不是 pending 了，回滚流水占用
                self._conn.rollback()
                return None
            self._conn.commit()
        return self.get(order_id)

    def settled_tx_of(self, exchange: str, tx_id: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT order_id FROM settled_tx WHERE exchange = ? AND tx_id = ?",
                (exchange, tx_id),
            ).fetchone()
        return row["order_id"] if row else None

    # ------------------------------------------------------------------
    # 回调状态
    # ------------------------------------------------------------------
    def callbacks_due(self, limit: int = 20) -> builtins.list[Order]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM orders WHERE callback_state = 'pending'"
                " AND (callback_next_ms IS NULL OR callback_next_ms <= ?)"
                " ORDER BY callback_next_ms ASC LIMIT ?",
                (now_ms(), limit),
            ).fetchall()
        return [_row_to_order(r) for r in rows]

    def update_callback(
        self, order_id: str, *, state: str, attempts: int, next_ms: int | None
    ) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE orders SET callback_state = ?, callback_attempts = ?,"
                " callback_next_ms = ? WHERE order_id = ?",
                (state, attempts, next_ms, order_id),
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    def stats(self) -> dict[str, Any]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM orders GROUP BY status"
            ).fetchall()
            paid = self._conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(CAST(pay_amount AS REAL)), 0) AS total"
                " FROM orders WHERE status = ?",
                (STATUS_PAID,),
            ).fetchone()
        return {
            "by_status": {r["status"]: r["n"] for r in rows},
            "paid_count": paid["n"],
            "paid_total": round(paid["total"], 4),
        }


def new_order_id() -> str:
    return uuid.uuid4().hex[:20]
