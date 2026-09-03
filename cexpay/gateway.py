"""聚合支付网关：把配置、交易所、匹配、存储、回调串起来。

这是对外的主要 Python 入口：

    from cexpay import PaymentGateway
    gw = PaymentGateway()
    order = gw.create_order(amount="9.9")
    ...
    gw.sweep()          # 拉取各所进账并核销
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

from .config import (
    CredentialStore,
    MatchPolicy,
    Settings,
    get_credential_store,
    get_settings,
)
from .errors import ConfigError, ExchangeAPIError, OrderError
from .exchanges import ExchangeAdapter, active_adapters, build_adapter
from .exchanges.base import Transaction
from .matching import (
    TIER_IDENTIFIER,
    TIER_MANUAL,
    TIER_MEMO,
    TIER_UNIQUE_AMOUNT,
    MatchCandidate,
    allocate_unique_amount,
    find_match,
    generate_memo,
)
from .notify import MAX_ATTEMPTS, deliver, next_delay_s
from .store import (
    STATUS_PENDING,
    Order,
    OrderStore,
    new_order_id,
    now_ms,
)

log = logging.getLogger("cexpay.gateway")


def _to_decimal(value: Any, field: str) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise OrderError(f"{field} 不是合法数字：{value!r}") from exc
    if amount <= 0:
        raise OrderError(f"{field} 必须大于 0")
    return amount


class PaymentGateway:
    def __init__(
        self,
        settings: Settings | None = None,
        store: OrderStore | None = None,
        credentials: CredentialStore | None = None,
    ):
        self.settings = settings or get_settings()
        self.credentials = credentials or get_credential_store()
        self.store = store or OrderStore(self.settings.db_path)

    # ------------------------------------------------------------------
    # 交易所
    # ------------------------------------------------------------------
    @property
    def policy(self) -> MatchPolicy:
        return self.settings.policy

    def adapters(self) -> list[ExchangeAdapter]:
        return active_adapters(settings=self.settings, store=self.credentials)

    def adapter(self, exchange: str) -> ExchangeAdapter | None:
        return build_adapter(exchange, settings=self.settings, store=self.credentials)

    def exchange_info(self) -> list[dict[str, Any]]:
        """给收银台用的交易所清单。"""
        out = []
        for adapter in self.adapters():
            info = adapter.info()
            info["account_label"] = adapter.credential.account_label
            info["has_qr"] = (self.settings.qr_dir / f"{adapter.name}.png").exists()
            out.append(info)
        return out

    def check_permissions(self, exchanges: Sequence[str] | None = None) -> list[dict[str, Any]]:
        names = list(exchanges) if exchanges else [a.name for a in self.adapters()]
        reports = []
        for name in names:
            adapter = self.adapter(name)
            if adapter is None:
                reports.append(
                    {
                        "exchange": name,
                        "ok": False,
                        "read_only": None,
                        "detail": "凭据未配置或已停用",
                        "permissions": [],
                    }
                )
                continue
            reports.append(adapter.check_permissions().to_dict())
        return reports

    def assert_readonly(self) -> None:
        """启动时的安全闸门：发现带写权限的 Key 就直接拒绝启动。"""
        if not self.settings.enforce_readonly:
            return
        for report in self.check_permissions():
            if report.get("read_only") is False:
                raise ConfigError(
                    f"[{report['exchange']}] {report['detail']}\n"
                    "本项目只需要读取权限。如确认要跳过该检查，"
                    "请设置 CEXPAY_ENFORCE_READONLY=false（不推荐）。"
                )

    # ------------------------------------------------------------------
    # 下单
    # ------------------------------------------------------------------
    def create_order(
        self,
        amount: Any,
        *,
        exchange: str | None = None,
        currency: str | None = None,
        merchant_ref: str | None = None,
        callback_url: str | None = None,
        ttl_s: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Order:
        """创建一笔待付订单。

        ``exchange=None`` 表示用户可以在任意已配置的交易所付款。
        """
        base_amount = _to_decimal(amount, "amount")
        currency = (currency or self.policy.currency).upper()

        if exchange:
            exchange = exchange.lower()
            if self.adapter(exchange) is None:
                raise OrderError(f"交易所 {exchange} 未配置或已停用")

        # 幂等：同一个 merchant_ref 命中未过期的待付订单时直接复用
        if merchant_ref:
            existing = self.store.get_by_ref(merchant_ref)
            if existing and existing.status == STATUS_PENDING and not existing.is_expired:
                return existing

        if self.settings.unique_amount:
            taken = self.store.locked_amounts(currency)
            pay_amount = allocate_unique_amount(
                base_amount, taken, decimals=self.settings.unique_amount_decimals
            )
        else:
            pay_amount = base_amount

        ttl = ttl_s if ttl_s is not None else self.settings.order_ttl_s
        created = now_ms()
        order = Order(
            order_id=new_order_id(),
            merchant_ref=merchant_ref,
            exchange=exchange,
            base_amount=base_amount,
            pay_amount=pay_amount,
            currency=currency,
            status=STATUS_PENDING,
            memo=generate_memo(self.policy.memo_length) if self.policy.enable_memo_match else None,
            created_ms=created,
            expires_ms=created + ttl * 1000,
            callback_url=callback_url,
            metadata=metadata or {},
        )
        self.store.create(order)
        if self.settings.unique_amount:
            # 金额锁的冷却时间必须覆盖订单有效期，否则会串单
            cooldown = max(self.policy.amount_cooldown_s, ttl)
            self.store.lock_amount(currency, pay_amount, order.order_id, cooldown)

        log.info(
            "订单已创建 %s: %s %s (原价 %s) exchange=%s",
            order.order_id, pay_amount, currency, base_amount, exchange or "any",
        )
        return order

    def get_order(self, order_id: str) -> Order | None:
        order = self.store.get(order_id)
        if order and order.is_expired:
            self.store.expire_stale()
            order = self.store.get(order_id)
        return order

    def submit_identifier(self, order_id: str, kind: str, value: str) -> Order:
        """用户提交付款方标识（昵称 / UID 后三位 / 提币 ID 后三位）。"""
        order = self.store.get(order_id)
        if order is None:
            raise OrderError("订单不存在")
        if order.status != STATUS_PENDING:
            raise OrderError(f"订单当前状态为 {order.status}，无法提交标识")

        value = (value or "").strip()
        if not value:
            raise OrderError("标识不能为空")
        if kind in ("payer_uid_last3", "withdraw_id_last3"):
            if not (len(value) == 3 and value.isdigit()):
                raise OrderError("请填写 3 位数字")
        elif kind == "payer_name":
            if len(value) > 64:
                raise OrderError("昵称过长")
        else:
            raise OrderError(f"未知的标识类型：{kind}")

        updated = self.store.set_identifier(order_id, kind, value)
        if updated is None:
            raise OrderError("订单不存在")
        return updated

    # ------------------------------------------------------------------
    # 核销
    # ------------------------------------------------------------------
    def fetch_transactions(
        self,
        start_ms: int,
        end_ms: int,
        *,
        exchanges: Sequence[str] | None = None,
        limit: int = 100,
    ) -> tuple[list[Transaction], list[str]]:
        """拉取各所进账，返回 (记录, 错误描述)。单家失败不影响其它家。"""
        adapters = (
            [a for a in self.adapters() if a.name in set(exchanges)]
            if exchanges
            else self.adapters()
        )
        transactions: list[Transaction] = []
        errors: list[str] = []
        for adapter in adapters:
            try:
                transactions.extend(
                    adapter.fetch_incoming(start_ms, end_ms, limit=limit)
                )
            except ExchangeAPIError as exc:
                log.warning("拉取 %s 进账失败: %s", adapter.name, exc)
                errors.append(str(exc))
            except Exception as exc:  # pragma: no cover - 防御性
                log.exception("拉取 %s 进账时出现未预期错误", adapter.name)
                errors.append(f"[{adapter.name}] {exc}")
        return transactions, errors

    def sweep(self, *, order_id: str | None = None) -> dict[str, Any]:
        """核销一轮：拉进账 → 匹配待付订单 → 落库 → 触发回调。

        ``order_id`` 只跑指定订单（用户点"我已支付"时走这条路，响应更快）。
        """
        self.store.expire_stale()

        if order_id:
            one = self.store.get(order_id)
            pending = [one] if one and one.status == STATUS_PENDING else []
        else:
            pending = self.store.pending_orders()

        result: dict[str, Any] = {
            "checked": len(pending),
            "settled": [],
            "errors": [],
            "transactions": 0,
        }
        if not pending:
            return result

        # 时间窗覆盖最早的待付订单
        policy = self.policy
        earliest = min(o.created_ms for o in pending)
        start_ms = earliest - policy.window_before_s * 1000
        end_ms = now_ms() + 60_000

        needed = {o.exchange for o in pending}
        exchanges = None if None in needed else [e for e in needed if e]

        transactions, errors = self.fetch_transactions(
            start_ms, end_ms, exchanges=exchanges
        )
        result["transactions"] = len(transactions)
        result["errors"] = errors

        used = self.store.used_tx_keys()
        for order in pending:
            allowed = [TIER_UNIQUE_AMOUNT]
            if policy.enable_memo_match and order.memo:
                allowed.append(TIER_MEMO)
            if order.identifier_kind and order.identifier_value:
                allowed.append(TIER_IDENTIFIER)
            elif policy.require_identifier:
                # 强制要求标识但用户还没填，跳过
                continue

            candidate = find_match(
                order.to_view(),
                transactions,
                policy,
                used_tx_ids=used,
                allow_tiers=allowed,
            )
            if candidate is None:
                continue

            settled = self._settle(order, candidate)
            if settled is not None:
                used.add(f"{candidate.transaction.exchange}:{candidate.transaction.tx_id}")
                result["settled"].append(
                    {
                        "order_id": settled.order_id,
                        "match": candidate.to_dict(),
                    }
                )
        return result

    def _settle(self, order: Order, candidate: MatchCandidate) -> Order | None:
        tx = candidate.transaction
        settled = self.store.settle(
            order.order_id,
            exchange=tx.exchange,
            tx_id=tx.tx_id,
            amount=tx.amount,
            tier=candidate.tier,
            reason=candidate.reason,
        )
        if settled is None:
            log.info(
                "订单 %s 核销未生效（流水 %s 已被占用或订单状态已变）",
                order.order_id, tx.tx_id,
            )
            return None
        log.info(
            "订单 %s 已核销：%s %s via %s (%s)",
            settled.order_id, tx.amount, tx.currency, tx.exchange, candidate.tier_label,
        )
        self.dispatch_callbacks(order_id=settled.order_id)
        return settled

    def manual_settle(
        self, order_id: str, *, exchange: str, tx_id: str, note: str = ""
    ) -> Order:
        """后台人工核销。"""
        order = self.store.get(order_id)
        if order is None:
            raise OrderError("订单不存在")
        if order.status != STATUS_PENDING:
            raise OrderError(f"订单当前状态为 {order.status}，无法人工核销")

        settled = self.store.settle(
            order_id,
            exchange=exchange,
            tx_id=tx_id,
            amount=order.pay_amount,
            tier=TIER_MANUAL,
            reason=note or "人工核销",
        )
        if settled is None:
            existing = self.store.settled_tx_of(exchange, tx_id)
            raise OrderError(
                f"核销失败：流水 {tx_id} 已被订单 {existing} 使用" if existing else "核销失败"
            )
        self.dispatch_callbacks(order_id=order_id)
        return settled

    # ------------------------------------------------------------------
    # 回调
    # ------------------------------------------------------------------
    def dispatch_callbacks(self, *, order_id: str | None = None, limit: int = 20) -> int:
        """投递到期的回调，返回本轮成功数。"""
        if order_id:
            one = self.store.get(order_id)
            due = [one] if one and one.callback_state == "pending" else []
        else:
            due = self.store.callbacks_due(limit=limit)

        succeeded = 0
        for order in due:
            if not order.callback_url:
                self.store.update_callback(
                    order.order_id, state="none", attempts=order.callback_attempts, next_ms=None
                )
                continue

            payload = {
                "event": "order.paid",
                "order": order.to_dict(public=True),
            }
            ok, detail = deliver(
                order.callback_url,
                payload,
                secret=self.settings.webhook_secret,
                timeout=self.settings.http_timeout_s,
            )
            attempts = order.callback_attempts + 1
            if ok:
                self.store.update_callback(
                    order.order_id, state="delivered", attempts=attempts, next_ms=None
                )
                succeeded += 1
                log.info("回调成功 %s -> %s", order.order_id, order.callback_url)
                continue

            delay = next_delay_s(attempts)
            if delay is None:
                self.store.update_callback(
                    order.order_id, state="failed", attempts=attempts, next_ms=None
                )
                log.error(
                    "回调最终失败 %s（已重试 %s 次）：%s", order.order_id, attempts, detail
                )
            else:
                self.store.update_callback(
                    order.order_id,
                    state="pending",
                    attempts=attempts,
                    next_ms=now_ms() + delay * 1000,
                )
                log.warning(
                    "回调失败 %s（第 %s/%s 次），%ss 后重试：%s",
                    order.order_id, attempts, MAX_ATTEMPTS, delay, detail,
                )
        return succeeded

    # ------------------------------------------------------------------
    def stats(self) -> dict[str, Any]:
        data = self.store.stats()
        data["exchanges"] = self.credentials.configured()
        return data
