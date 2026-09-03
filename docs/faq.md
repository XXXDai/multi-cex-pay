# 常见问题

## 需要商户资质吗？

不需要。交易所的商户收款 API 只对企业主体开放，个人拿不到。本项目走的是
"买家转账到你的个人账户 + 只读 API 自动核对"这条路，用的是任何个人账户
都有的普通 API Key。

## 它会碰我的钱吗？

不会，设计上就不能。整个仓库只调用六个 GET 接口（三个拉进账、三个查权限），
没有下单、划转、提币的调用。你给的是只读 Key，默认配置下启动时还会校验一遍，
发现 Key 带提币或交易权限就拒绝启动。详见 [security.md](security.md)。

## 一个二维码能三家通用吗？

不能。Binance 的码内容是 `https://app.binance.com/uni-qr/...`，
Bitget 的是 `https://www.bitget.com/pay/receive?...`，各家 App 只认自己的域名，
扫到别家的链接最多只能打开浏览器。

本项目做的是视觉聚合：一张图上并排三格，每格顶上有品牌色标题栏，
用哪家 App 就扫对应那一格。任何声称"一个万能码三家通吃"的说法都是假的。

## 聚合图从相册识别为什么会串？

因为一张图里有三个码，App 从相册解码时通常只返回它先找到的那一个，
不会让你选。哪个"先"取决于它的实现，不可控。

所以要引导用户用相机对准所需品牌那一格扫，不要从相册识别。
收银台在窄屏（手机）已经默认显示单码大图，聚合图更适合桌面端和打印张贴。

## 为什么必须让用户付一个带零碎小数的金额？

这是唯一能做到"买家零输入 + 自动核销"的办法：金额本身就是订单指纹。
`9.9` 会变成 `9.9001`、`9.9002`……4 位小数意味着同一价位可以并发 9999 笔订单。

不想改金额可以关掉（`CEXPAY_UNIQUE_AMOUNT=false`），代价是必须让买家在收银台
补填付款方标识（昵称 / UID 后三位 / 提币 ID 后三位），成功率和体验都会下降。

## 支持哪些币种和链？

默认只处理 USDT（`CEXPAY_CURRENCY` 可改，但一次只能一种）。

链这一层：Binance Pay 是站内支付，没有链的概念。OKX 和 Bitget 的进账记录
同时覆盖站内转账和链上充值，两者都能核销。站内转账秒到、免手续费，是推荐路径。
链上充值要等确认，但时间窗（默认订单创建前 30 分钟到过期后 1 小时）足够覆盖
常见的确认时间。

## 能不能加别的交易所？

可以。实现 `ExchangeAdapter` 的四个方法：

```python
class GateAdapter(ExchangeAdapter):
    name = "gate"
    def fetch_incoming(self, start_ms, end_ms, *, limit=100) -> list[Transaction]: ...
    def check_permissions(self) -> PermissionReport: ...
    def identifier_spec(self) -> IdentifierSpec: ...
```

然后注册两处：`cexpay/exchanges/__init__.py` 的 `ADAPTERS`，
和 `cexpay/config.py` 的 `CREDENTIAL_FIELDS`（声明它要哪些凭据字段）。
匹配引擎、收银台、后台、聚合图都会自动认识新的交易所。
完整步骤见 [CONTRIBUTING.md](../CONTRIBUTING.md#如何新增一个交易所)。

## 多商户 / 多个子账号怎么办？

不支持多租户，一个实例就是一个商户。要跑多个商户就跑多个实例，
各自用不同的 `CEXPAY_DATA_DIR` 和端口。这是刻意的取舍：多租户会把凭据隔离、
订单归属、金额池隔离都复杂化，而大多数用户只有一个收款账号。

## 订单过期后用户才付款怎么处理？

过期订单不会被自动核销（`expired` 是终态）。这时候：

1. 打开后台的最近进账，确认这笔钱确实到了；
2. 让用户重新下单，用人工核销（T4）把新订单和这笔流水绑定；
3. 或者直接退款给用户。

唯一金额有 24 小时冷却（默认），这笔钱的金额不会在这期间被分配给别的订单，
你有时间处理。想要更长的缓冲就把 `CEXPAY_ORDER_TTL` 调大。

## 用户少付了 0.01 怎么办？

不会自动核销。少付一律不放行，容差只用于放宽多付。
少付要么让用户补齐差额（会变成另一笔进账），要么后台人工核销，要么退款。

## 为什么不直接用链上收款？

两条路各有取舍：

| | 交易所收款（本项目） | 链上收款（epusdt 等） |
|---|---|---|
| 买家手续费 | 站内转账免费 | 要付 gas |
| 到账速度 | 秒到 | 等区块确认 |
| 买家门槛 | 已有交易所账号即可 | 要有链上钱包、要选对链 |
| 转错的风险 | 几乎没有 | 选错链可能丢币 |
| 你的账号风险 | 有（个人账号做经营性收款） | 无 |
| 自动化难度 | 需要本项目这类工具 | 链上数据公开，容易 |

两者不冲突，可以同时挂：交易所通道给"有交易所账号的买家"，
链上通道给"只有钱包的买家"。[epusdt](https://github.com/assimon/epusdt) /
[BEpusdt](https://github.com/v03413/BEpusdt) 是这个方向成熟的开源实现。

## 为什么是 Python 而不是一个单二进制？

二维码的识别、透视校正、重绘、聚合都依赖 OpenCV + Pillow，
用 Go 重写要么放弃这些能力，要么引入 CGO。

代价是部署比一个静态二进制重。所以提供了 Docker 镜像：

```bash
cp .env.example .env && docker compose up -d
```

镜像用 `opencv-python-headless`，两阶段构建，不带编译工具链。

## 数据存在哪里，怎么备份？

全部在 `CEXPAY_DATA_DIR`（默认 `./data`，Docker 里是 `/data`）：

```
data/
├── cexpay.sqlite3        订单、已用流水、金额锁
├── credentials.json      API 凭据（0600，可选 Fernet 加密）
└── qr/
    ├── binance.png       各所收款码
    ├── okx.png
    ├── bitget.png
    └── aggregate.png     最近一次生成的聚合图
```

SQLite 开了 WAL，热备份建议用：

```bash
sqlite3 data/cexpay.sqlite3 ".backup 'backup.sqlite3'"
```

直接 `cp` 的话要把 `-wal` 和 `-shm` 一起拷。整个 `data/` 目录里有个人信息
（付款方昵称、UID）和 API 凭据，**备份请加密**。

## 怎么升级？

```bash
git pull && .venv/bin/pip install -e . && .venv/bin/python -m pytest -q
```

数据库表结构用 `CREATE TABLE IF NOT EXISTS`，加字段是向后兼容的。
升级前备份 `data/` 总是对的。Docker 用户 `docker compose pull && docker compose up -d`。

## 轮询会不会打爆交易所的频率限制？

默认 20 秒一轮，而且只在有待付订单时才真正发请求（没有待付订单时 `sweep()`
直接返回）。一轮最多三个 GET。这个频率对三家的限额来说都很宽松。

`POST /api/orders/{id}/check` 是例外：用户每点一次「我已支付」就会立刻打三家 API。
公开暴露时**必须限流**，见 [security.md](security.md#部署加固清单)。

## 钱到账了但订单没核销，怎么查？

排查步骤见 [matching.md 的排查指南](matching.md#排查指南钱到账了但订单没核销)。
先打开后台的最近进账（或 `cexpay tx --minutes 60`），看交易所 API 返回了什么，
和订单的 `pay_amount` 对一下，一般就能定位。

## 收银台能自己改样式吗？

能。`web/` 下就是三个静态 HTML + 一个 CSS + 两个 JS，没有构建步骤、没有框架。
配色都是 CSS 变量（`web/app.css` 顶部），亮/暗双主题。
也可以完全不用自带收银台：`POST /api/orders` 拿到 `order_id` 和
`/api/orders/{id}/qr.png` 之后，用你自己的页面渲染即可。
