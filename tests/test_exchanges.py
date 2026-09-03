"""交易所适配器：用假的 HTTP 返回体验证解析与权限判定，不打真实接口。"""

from decimal import Decimal

import pytest

from cexpay.config import ExchangeCredential, Settings
from cexpay.errors import ExchangeAPIError
from cexpay.exchanges import ADAPTERS, BinanceAdapter, BitgetAdapter, OKXAdapter
from cexpay.exchanges.base import to_decimal, to_ms


@pytest.fixture()
def settings(data_dir) -> Settings:
    return Settings()


def make(cls, settings, responses):
    """构造一个适配器，把 _request 换成按 URL 返回预置数据的假实现。"""
    cred = ExchangeCredential(
        exchange=cls.name, api_key="key", api_secret="secret", passphrase="pass"
    )
    adapter = cls(cred, settings)
    calls = []

    def fake_request(method, url, headers=None, params=None):
        calls.append(url)
        for needle, payload in responses.items():
            if needle in url:
                if isinstance(payload, Exception):
                    raise payload
                return payload
        raise AssertionError(f"未预置的请求: {url}")

    adapter._request = fake_request
    adapter.calls = calls
    return adapter


# ---------------------------------------------------------------- 工具函数
@pytest.mark.parametrize(
    "raw,expected",
    [("9.9", Decimal("9.9")), (5, Decimal("5")), (None, None), ("", None), ("x", None)],
)
def test_to_decimal(raw, expected):
    assert to_decimal(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        (1700000000000, 1700000000000),   # 已经是毫秒
        (1700000000, 1700000000000),      # 秒 -> 毫秒
        ("1700000000000", 1700000000000),
        (None, None), ("", None), ("abc", None),
    ],
)
def test_to_ms(raw, expected):
    assert to_ms(raw) == expected


# ---------------------------------------------------------------- Binance
BINANCE_PAY = {
    "code": "000000",
    "message": "success",
    "success": True,
    "data": [
        {
            "orderType": "C2C",
            "transactionId": "M_4358O",
            "transactionTime": 1637890265000,
            "amount": "9.9001",
            "currency": "USDT",
            "walletType": 1,
            "note": "订单 123456",
            "payerInfo": {"name": "Ming***Li", "type": "USER", "binanceId": "24000000"},
        },
        {   # 支出，必须被忽略
            "orderType": "PAY",
            "transactionId": "OUT_1",
            "transactionTime": 1637890265000,
            "amount": "-5",
            "currency": "USDT",
        },
        {   # 非 USDT，解析保留，由 matching 层按币种过滤
            "orderType": "C2C",
            "transactionId": "BTC_1",
            "transactionTime": 1637890265000,
            "amount": "0.001",
            "currency": "BTC",
            "payerInfo": {"name": "Bob"},
        },
    ],
}


def test_binance_parses_incoming_only(settings):
    adapter = make(BinanceAdapter, settings, {"pay/transactions": BINANCE_PAY})
    txs = adapter.fetch_incoming(0, 0)
    assert [t.tx_id for t in txs] == ["M_4358O", "BTC_1"]

    first = txs[0]
    assert first.amount == Decimal("9.9001")
    assert first.payer_name == "Ming***Li"
    assert first.payer_uid == "24000000"
    assert first.memo == "订单 123456"
    assert first.exchange == "binance"


def test_binance_raises_on_success_false(settings):
    adapter = make(
        BinanceAdapter, settings,
        {"pay/transactions": {"success": False, "message": "无权限"}},
    )
    with pytest.raises(ExchangeAPIError, match="无权限"):
        adapter.fetch_incoming(0, 0)


def test_binance_permission_rejects_withdraw_key(settings):
    adapter = make(
        BinanceAdapter, settings,
        {"apiRestrictions": {"ipRestrict": False, "enableWithdrawals": True,
                             "enableSpotAndMarginTrading": False}},
    )
    report = adapter.check_permissions()
    assert report.read_only is False
    assert "提币" in report.detail


def test_binance_permission_accepts_readonly_key(settings):
    adapter = make(
        BinanceAdapter, settings,
        {"apiRestrictions": {"ipRestrict": True, "enableWithdrawals": False,
                             "enableSpotAndMarginTrading": False, "enableFutures": False}},
    )
    report = adapter.check_permissions()
    assert report.read_only is True
    assert report.ip_restricted is True


def test_binance_permission_hints_ip_whitelist(settings):
    adapter = make(
        BinanceAdapter, settings,
        {"apiRestrictions": {"ipRestrict": False, "enableWithdrawals": False}},
    )
    assert "IP 白名单" in adapter.check_permissions().detail


def test_binance_permission_survives_api_error(settings):
    adapter = make(
        BinanceAdapter, settings,
        {"apiRestrictions": ExchangeAPIError("binance", "HTTP 401")},
    )
    report = adapter.check_permissions()
    assert report.ok is False and report.read_only is None


# ---------------------------------------------------------------- OKX
OKX_DEPOSITS = {
    "code": "0",
    "msg": "",
    "data": [
        {   # 内部转账，成功
            "depId": "88165", "ccy": "USDT", "amt": "9.9002", "state": "2",
            "ts": "1700000000000", "from": "138****8888", "fromWdId": "99887728",
            "chain": "", "txId": "",
        },
        {   # 链上充值，成功
            "depId": "88166", "ccy": "USDT", "amt": "20", "state": "2",
            "ts": "1700000001000", "to": "0xabc", "txId": "0xdeadbeef",
            "chain": "USDT-TRC20",
        },
        {   # 未到账，必须忽略
            "depId": "88167", "ccy": "USDT", "amt": "5", "state": "0",
            "ts": "1700000002000",
        },
    ],
}


def test_okx_parses_success_only_and_marks_channel(settings):
    adapter = make(OKXAdapter, settings, {"deposit-history": OKX_DEPOSITS})
    txs = adapter.fetch_incoming(1_699_000_000_000, 1_700_000_100_000)
    assert [t.tx_id for t in txs] == ["88165", "88166"]

    internal, on_chain = txs
    assert internal.channel == "internal"
    assert internal.withdraw_id == "99887728"
    assert internal.payer_name == "138****8888"
    assert on_chain.channel == "on_chain"
    assert on_chain.withdraw_id is None


def test_okx_signs_query_string(settings):
    """签名必须覆盖 query string，否则 OKX 会返回 50113。"""
    adapter = make(OKXAdapter, settings, {"deposit-history": OKX_DEPOSITS})
    adapter.fetch_incoming(1000, 2000)
    assert "ccy=USDT" in adapter.calls[0]
    assert "before=1000" in adapter.calls[0]
    assert "after=2000" in adapter.calls[0]


def test_okx_raises_on_error_code(settings):
    adapter = make(OKXAdapter, settings,
                   {"deposit-history": {"code": "50113", "msg": "签名无效", "data": []}})
    with pytest.raises(ExchangeAPIError, match="签名无效"):
        adapter.fetch_incoming(0, 1)


def test_okx_permission_rejects_trade_perm(settings):
    adapter = make(OKXAdapter, settings,
                   {"account/config": {"code": "0",
                                       "data": [{"perm": "read_only,trade", "uid": "123"}]}})
    report = adapter.check_permissions()
    assert report.read_only is False
    assert "trade" in report.detail


def test_okx_permission_accepts_read_only(settings):
    adapter = make(OKXAdapter, settings,
                   {"account/config": {"code": "0",
                                       "data": [{"perm": "read_only", "uid": "7147", "ip": "1.2.3.4"}]}})
    report = adapter.check_permissions()
    assert report.read_only is True
    assert report.account_label == "7147"


def test_okx_permission_unknown_when_perm_missing(settings):
    adapter = make(OKXAdapter, settings,
                   {"account/config": {"code": "0", "data": [{"uid": "1"}]}})
    assert adapter.check_permissions().read_only is None


# ---------------------------------------------------------------- Bitget
BITGET_DEPOSITS = {
    "code": "00000",
    "msg": "success",
    "data": [
        {   # 内部转账
            "orderId": "1234567890", "tradeId": "", "coin": "USDT", "size": "9.9003",
            "status": "success", "dest": "internal", "chain": "",
            "fromAddress": "1000000165", "toAddress": "8888888888",
            "cTime": "1700000000000",
        },
        {   # 链上
            "orderId": "1234567891", "tradeId": "0xabc", "coin": "USDT", "size": "50",
            "status": "success", "dest": "on_chain", "chain": "TRC20",
            "fromAddress": "TXyz...", "cTime": "1700000001000",
        },
        {   # 确认中，忽略
            "orderId": "1234567892", "coin": "USDT", "size": "1",
            "status": "pending", "cTime": "1700000002000",
        },
    ],
}


def test_bitget_parses_and_extracts_uid(settings):
    adapter = make(BitgetAdapter, settings, {"deposit-records": BITGET_DEPOSITS})
    txs = adapter.fetch_incoming(1_699_000_000_000, 1_700_000_100_000)
    assert [t.tx_id for t in txs] == ["1234567890", "1234567891"]

    internal, on_chain = txs
    assert internal.payer_uid == "1000000165"   # 纯数字 = UID
    assert internal.channel == "internal"
    assert on_chain.payer_uid is None           # 钱包地址不是 UID
    assert on_chain.channel == "on_chain"


def test_bitget_requires_time_range_in_query(settings):
    """Bitget 缺少 startTime/endTime 会返回 400172，所以必须始终带上。"""
    adapter = make(BitgetAdapter, settings, {"deposit-records": BITGET_DEPOSITS})
    adapter.fetch_incoming(1000, 2000)
    assert "startTime=1000" in adapter.calls[0]
    assert "endTime=2000" in adapter.calls[0]


def test_bitget_raises_on_error_code(settings):
    adapter = make(BitgetAdapter, settings,
                   {"deposit-records": {"code": "40012", "msg": "apikey 无效", "data": []}})
    with pytest.raises(ExchangeAPIError, match="apikey 无效"):
        adapter.fetch_incoming(0, 1)


def test_bitget_permission_rejects_trade(settings):
    adapter = make(BitgetAdapter, settings,
                   {"account/info": {"code": "00000",
                                     "data": {"userId": "1", "authorities": ["trade"]}}})
    assert adapter.check_permissions().read_only is False


def test_bitget_permission_accepts_readonly(settings):
    adapter = make(BitgetAdapter, settings,
                   {"account/info": {"code": "00000",
                                     "data": {"userId": "34195", "authorities": ["readonly"],
                                              "ips": "1.2.3.4"}}})
    report = adapter.check_permissions()
    assert report.read_only is True
    assert report.account_label == "34195"


# ---------------------------------------------------------------- 注册表
def test_registry_covers_three_exchanges():
    assert set(ADAPTERS) == {"binance", "okx", "bitget"}


@pytest.mark.parametrize("name", ["binance", "okx", "bitget"])
def test_identifier_specs_are_well_formed(settings, name):
    cred = ExchangeCredential(exchange=name, api_key="k", api_secret="s", passphrase="p")
    spec = ADAPTERS[name](cred, settings).identifier_spec()
    assert spec.kind in ("payer_name", "payer_uid_last3", "withdraw_id_last3")
    assert spec.label and spec.help_text


def test_only_binance_supports_memo(settings):
    def cred(name: str) -> ExchangeCredential:
        return ExchangeCredential(exchange=name, api_key="k", api_secret="s", passphrase="p")

    assert BinanceAdapter(cred("binance"), settings).supports_memo is True
    assert OKXAdapter(cred("okx"), settings).supports_memo is False
    assert BitgetAdapter(cred("bitget"), settings).supports_memo is False
