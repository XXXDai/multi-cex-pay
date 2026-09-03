"""交易所适配器注册表。

想接入新的交易所，只需要：
  1. 写一个继承 :class:`ExchangeAdapter` 的类
  2. 在 ``ADAPTERS`` 里登记
  3. 在 ``cexpay.config.CREDENTIAL_FIELDS`` 里声明它需要哪些凭据字段
"""

from __future__ import annotations

from ..config import CredentialStore, Settings, get_credential_store, get_settings
from .base import (
    ExchangeAdapter,
    IdentifierSpec,
    PermissionReport,
    Transaction,
)
from .binance import BinanceAdapter
from .bitget import BitgetAdapter
from .okx import OKXAdapter

ADAPTERS: dict[str, type[ExchangeAdapter]] = {
    BinanceAdapter.name: BinanceAdapter,
    OKXAdapter.name: OKXAdapter,
    BitgetAdapter.name: BitgetAdapter,
}

__all__ = [
    "ADAPTERS",
    "BinanceAdapter",
    "BitgetAdapter",
    "ExchangeAdapter",
    "IdentifierSpec",
    "OKXAdapter",
    "PermissionReport",
    "Transaction",
    "active_adapters",
    "build_adapter",
]


def build_adapter(
    exchange: str,
    *,
    settings: Settings | None = None,
    store: CredentialStore | None = None,
) -> ExchangeAdapter | None:
    """按名字构造适配器；凭据不全时返回 None。"""
    exchange = exchange.lower()
    cls = ADAPTERS.get(exchange)
    if cls is None:
        return None
    settings = settings or get_settings()
    store = store or get_credential_store()
    credential = store.get(exchange)
    if not credential.enabled or not credential.is_complete():
        return None
    return cls(credential, settings)


def active_adapters(
    *,
    settings: Settings | None = None,
    store: CredentialStore | None = None,
) -> list[ExchangeAdapter]:
    """所有已配置好、启用中的适配器。"""
    settings = settings or get_settings()
    store = store or get_credential_store(refresh=True)
    out: list[ExchangeAdapter] = []
    for name in ADAPTERS:
        adapter = build_adapter(name, settings=settings, store=store)
        if adapter is not None:
            out.append(adapter)
    return out
