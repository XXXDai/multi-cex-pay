"""交易所适配层的统一接口。

每个交易所只需要回答这几个问题：
  1. 最近有哪些"进账"记录（统一成 :class:`Transaction`）
  2. 这把 API Key 是不是只读的
  3. 用户付款后能提供什么标识供人工核对
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

import requests

from ..config import ExchangeCredential, Settings
from ..errors import ExchangeAPIError


def to_decimal(value: Any) -> Decimal | None:
    """尽最大努力把交易所返回的金额转成 Decimal。"""
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def to_ms(value: Any) -> int | None:
    """把各种形态的时间戳统一成整数毫秒。"""
    if value is None or value == "":
        return None
    try:
        number = int(float(value))
    except (ValueError, TypeError):
        return None
    # 10 位是秒，13 位是毫秒
    if number < 10_000_000_000:
        number *= 1000
    return number


@dataclass
class Transaction:
    """跨交易所统一的进账记录。"""

    exchange: str
    tx_id: str
    amount: Decimal
    currency: str
    timestamp_ms: int
    # 用于人工核对的标识（不同交易所能拿到的东西不一样）
    payer_name: str | None = None      # Binance Pay：付款方昵称
    payer_uid: str | None = None       # Bitget 内部转账：付款方 UID
    withdraw_id: str | None = None     # OKX 内部转账：提币申请 ID
    memo: str | None = None            # 转账备注（Binance Pay）
    channel: str = ""                     # pay / deposit / internal ...
    raw: dict[str, Any] = field(default_factory=dict)

    def identifiers(self) -> dict[str, str | None]:
        return {
            "payer_name": self.payer_name,
            "payer_uid": self.payer_uid,
            "withdraw_id": self.withdraw_id,
            "memo": self.memo,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "exchange": self.exchange,
            "tx_id": self.tx_id,
            "amount": str(self.amount),
            "currency": self.currency,
            "timestamp_ms": self.timestamp_ms,
            "channel": self.channel,
            **{k: v for k, v in self.identifiers().items() if v},
        }


@dataclass
class PermissionReport:
    """只读校验结果。"""

    exchange: str
    ok: bool                       # 连通性是否正常
    read_only: bool | None      # True=确认只读 / False=确认有写权限 / None=无法判定
    detail: str = ""
    permissions: list[str] = field(default_factory=list)
    ip_restricted: bool | None = None
    account_label: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "exchange": self.exchange,
            "ok": self.ok,
            "read_only": self.read_only,
            "detail": self.detail,
            "permissions": self.permissions,
            "ip_restricted": self.ip_restricted,
            "account_label": self.account_label,
        }


@dataclass
class IdentifierSpec:
    """告诉前端"请用户填什么"。"""

    kind: str          # payer_name / payer_uid_last3 / withdraw_id_last3
    label: str         # 中文提示
    placeholder: str
    pattern: str = ""  # 前端校验用的正则
    help_text: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "label": self.label,
            "placeholder": self.placeholder,
            "pattern": self.pattern,
            "help_text": self.help_text,
        }


class ExchangeAdapter(ABC):
    """所有交易所适配器的基类。"""

    name: str = ""
    display_name: str = ""
    brand_color: str = "#888888"
    # 该交易所是否支持从转账备注里读到备注码
    supports_memo: bool = False
    # 收款方式说明（展示在收银台上）
    pay_hint: str = ""

    def __init__(self, credential: ExchangeCredential, settings: Settings):
        self.credential = credential
        self.settings = settings
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "multi-cex-pay/0.1"})

    # --- 子类实现 ---
    @abstractmethod
    def fetch_incoming(
        self, start_ms: int, end_ms: int, *, limit: int = 100
    ) -> list[Transaction]:
        """拉取时间区间内的进账记录。"""

    @abstractmethod
    def check_permissions(self) -> PermissionReport:
        """检查 API Key 的权限，用于拒绝带提币/交易权限的 Key。"""

    @abstractmethod
    def identifier_spec(self) -> IdentifierSpec:
        """用户手动核销时需要提供的标识。"""

    # --- 公共工具 ---
    def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        try:
            response = self._session.request(
                method,
                url,
                headers=headers,
                params=params,
                timeout=self.settings.http_timeout_s,
            )
        except requests.RequestException as exc:
            raise ExchangeAPIError(self.name, f"网络请求失败: {exc}") from exc

        if response.status_code != 200:
            raise ExchangeAPIError(
                self.name,
                f"HTTP {response.status_code}: {response.text[:300]}",
                status=response.status_code,
            )
        try:
            return response.json()
        except ValueError as exc:
            raise ExchangeAPIError(
                self.name, f"返回体不是 JSON: {response.text[:200]}"
            ) from exc

    @staticmethod
    def now_ms() -> int:
        return int(time.time() * 1000)

    def info(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "brand_color": self.brand_color,
            "supports_memo": self.supports_memo,
            "pay_hint": self.pay_hint,
            "identifier": self.identifier_spec().to_dict(),
        }
