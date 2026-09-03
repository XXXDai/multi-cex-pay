"""统一异常类型。"""


class CexPayError(Exception):
    """项目内所有异常的基类。"""


class ConfigError(CexPayError):
    """配置缺失或非法。"""


class CredentialError(ConfigError):
    """API 凭据缺失、格式错误或权限不满足只读要求。"""


class ExchangeAPIError(CexPayError):
    """调用交易所接口失败。"""

    def __init__(self, exchange: str, message: str, *, status: int | None = None, payload=None):
        self.exchange = exchange
        self.status = status
        self.payload = payload
        super().__init__(f"[{exchange}] {message}")


class QRError(CexPayError):
    """二维码识别 / 裁剪 / 合成失败。"""


class OrderError(CexPayError):
    """订单状态非法。"""
