# 贡献指南

## 开发环境

```bash
git clone https://github.com/XXXDai/multi-cex-pay.git && cd multi-cex-pay
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

跑测试，不需要交易所凭据，所有交易所交互在测试里都是假响应体：

```bash
.venv/bin/python -m pytest -q          # 254 passed
.venv/bin/ruff check .
```

跑服务：

```bash
export CEXPAY_ADMIN_TOKEN=dev-token
export CEXPAY_DATA_DIR=/tmp/cexpay-dev
.venv/bin/cexpay serve --reload
```

`--reload` 会在改代码后自动重启。前端是纯静态的三个 HTML + 一个 CSS + 两个 JS，
改完刷新浏览器即可，没有构建步骤。

## 代码风格

- `ruff check .` 必须过，行宽 100。
- 注释和文档用中文，说明"为什么这么做"而不是"这行在做什么"。
  代码里已有的注释就是标准，照着写。
- 类型标注尽量给全，模块顶部统一 `from __future__ import annotations`。
- 钱相关的数值一律用 `Decimal`，不要出现 `float`。金额在 HTTP 层用字符串传递。
- 新功能要带测试。改了匹配逻辑、存储不变量、交易所解析这三块，
  没有测试的 PR 不会被合。

## 提 PR 之前

- [ ] `.venv/bin/python -m pytest -q` 全绿
- [ ] `.venv/bin/ruff check .` 无告警
- [ ] 改了 SDK 的话，`tests/test_sdk_signature.py` 仍然通过（四种语言的签名口径必须一致）
- [ ] 改了接口的话，`docs/api.md` 同步更新
- [ ] 改了匹配逻辑的话，`docs/matching.md` 同步更新
- [ ] PR 里不含任何真实的 API Key、UID、收款码，截图记得脱敏

## 如何新增一个交易所

要改的地方就下面四处，架构是照着这件事设计的。

### 1. 写适配器

新建 `cexpay/exchanges/gate.py`，继承 `ExchangeAdapter`，实现三个抽象方法：

```python
from .base import (
    ExchangeAdapter, IdentifierSpec, PermissionReport, Transaction, to_decimal, to_ms,
)

class GateAdapter(ExchangeAdapter):
    name = "gate"                     # 内部标识，全小写，会出现在 URL 和数据库里
    display_name = "Gate.io"          # 展示名
    brand_color = "#2354E6"           # 收银台标签和聚合图标题栏的颜色
    supports_memo = False             # 该所的进账记录里能不能读到转账备注
    pay_hint = "打开 Gate App → ..."   # 收银台上的操作提示

    def fetch_incoming(self, start_ms: int, end_ms: int, *, limit: int = 100) -> list[Transaction]:
        """拉时间区间内的**进账**记录，归一成 Transaction。

        注意：
          - 只返回成功状态、金额 > 0 的记录，支出和待确认要过滤掉
          - 时间戳统一用 to_ms() 转成整数毫秒
          - 金额统一用 to_decimal()，不要用 float
          - tx_id 必须是该所稳定唯一的流水号，它是"一笔钱只核销一张单"的键
        """

    def check_permissions(self) -> PermissionReport:
        """读该所的权限接口，判断 Key 是否只读。

        三态语义很重要：
          read_only=True  确认只读
          read_only=False 确认带写权限 -> 默认配置下服务拒绝启动
          read_only=None  接口没给权限字段，无法判定 -> 允许启动但记警告
        连不上时返回 ok=False, read_only=None，detail 放原始错误。
        """

    def identifier_spec(self) -> IdentifierSpec:
        """T3 让用户填什么。kind 只能是这三个之一：
        payer_name / payer_uid_last3 / withdraw_id_last3
        """
```

签名逻辑写在适配器内部，HTTP 请求统一走基类的 `self._request()`
（它已经处理了超时、非 200、非 JSON 这些情况）。

OKX 和 Bitget 的签名都要覆盖 query string，所以必须自己拼好 `request_path` 再签，
发出去的 URL 要和签名用的字节完全一致。参考 `okx.py` / `bitget.py` 里的 `_get()`。

### 2. 注册

`cexpay/exchanges/__init__.py`：

```python
from .gate import GateAdapter

ADAPTERS = {
    ...,
    GateAdapter.name: GateAdapter,
}
```

`cexpay/config.py`：

```python
CREDENTIAL_FIELDS = {
    ...,
    "gate": ("api_key", "api_secret"),        # 要 passphrase 就加上
}
```

`SUPPORTED_EXCHANGES` 是从 `CREDENTIAL_FIELDS` 之外单独列的常量，也要加上。

### 3. 前端和聚合图

- `web/admin.js` 顶部的 `EXCHANGES` 数组加一项（`needsPassphrase` 决定是否显示口令输入框）。
- `cexpay/qr/compose.py` 的 `BRAND_STYLE` 加一项（标题文字 + 背景色 + 前景色）。
- `cexpay/qr/detect.py` 的 `BRAND_PATTERNS` 加一项，这样上传收款码时能自动识别归属、
  传错所会告警。

收银台、匹配引擎、后台订单页、CLI 都会自动认识新交易所，不用改。

### 4. 测试

照着 `tests/test_exchanges.py` 的写法：用 `make()` 辅助函数把 `_request` 换成假实现，
喂一段真实抓下来的响应体（记得脱敏），断言：

- 成功记录被正确解析（金额、时间、标识字段）
- 支出 / 待确认 / 失败状态被过滤掉
- 错误码会抛 `ExchangeAPIError`
- query string 里带了签名必需的参数
- `check_permissions()` 对只读 Key 返回 `True`、对带写权限的 Key 返回 `False`、
  字段缺失时返回 `None`

另外 `test_registry_covers_three_exchanges` 和几个参数化测试里的交易所列表也要更新。

## 报告问题

开 issue 时请带上：

- `cexpay --version` 和 Python 版本
- 复现步骤
- 相关日志，先脱敏，API Key、UID、收款码、付款方昵称都要去掉
- 如果是"钱到账了但没核销"，先按 [docs/matching.md 的排查指南](docs/matching.md#排查指南钱到账了但订单没核销)
  走一遍，并附上 `cexpay tx --minutes 60` 的脱敏输出和订单的 `pay_amount`

**安全问题不要开公开 issue**，见 [SECURITY.md](SECURITY.md)。
