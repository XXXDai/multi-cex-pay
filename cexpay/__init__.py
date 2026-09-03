"""多交易所聚合支付 (multi-cex-pay).

把 Binance / OKX / Bitget 的收款校验逻辑统一成一套接口：
商家自己配置只读 API 与收款二维码，系统负责生成聚合收款图、
轮询各所入账记录、按"唯一金额 / 备注码 / 付款方标识"自动核销订单。

    from cexpay import PaymentGateway
    gw = PaymentGateway()
    order = gw.create_order("9.9")
    gw.sweep()
"""

from typing import TYPE_CHECKING, Any

__version__ = "0.1.0"

# 懒加载：`import cexpay` 时不去拉 requests / opencv 这些重依赖
_LAZY = {
    "PaymentGateway": ("cexpay.gateway", "PaymentGateway"),
    "Settings": ("cexpay.config", "Settings"),
    "MatchPolicy": ("cexpay.config", "MatchPolicy"),
    "OrderStore": ("cexpay.store", "OrderStore"),
    "Order": ("cexpay.store", "Order"),
    "Transaction": ("cexpay.exchanges.base", "Transaction"),
    "crop_qr": ("cexpay.qr", "crop_qr"),
    "compose": ("cexpay.qr", "compose"),
    "Panel": ("cexpay.qr", "Panel"),
    "detect_qrcodes": ("cexpay.qr", "detect_qrcodes"),
}

if TYPE_CHECKING:  # pragma: no cover
    # 这些只给类型检查器和 IDE 用；运行期走下面的 __getattr__ 懒加载。
    # __all__ 是从 _LAZY 动态拼出来的，静态分析看不到，所以要显式 noqa。
    from .config import MatchPolicy, Settings  # noqa: F401
    from .exchanges.base import Transaction  # noqa: F401
    from .gateway import PaymentGateway  # noqa: F401
    from .qr import Panel, compose, crop_qr, detect_qrcodes  # noqa: F401
    from .store import Order, OrderStore  # noqa: F401


def __getattr__(name: str) -> Any:
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    return getattr(importlib.import_module(target[0]), target[1])


__all__ = ["__version__", *_LAZY]
