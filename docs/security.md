# 安全

配错一个勾选框，风险等级完全不同。

- [威胁模型](#威胁模型)
- [创建只读 API Key](#创建只读-api-key)
- [只读自检](#只读自检)
- [凭据存储](#凭据存储)
- [部署加固清单](#部署加固清单)
- [已知的安全边界](#已知的安全边界)
- [合规与账号风险](#合规与账号风险)
- [漏洞报告](#漏洞报告)

---

## 威胁模型

### 需要的权限

只需要读取你账户的进账记录。

| 交易所 | 调用的接口 | 权限要求 |
|---|---|---|
| Binance | `GET /sapi/v1/pay/transactions` | 只需 Enable Reading |
| OKX | `GET /api/v5/asset/deposit-history` | 只需 读取 / Read |
| Bitget | `GET /api/v2/spot/wallet/deposit-records` | 只需 只读 / Read-only |

外加三个纯读的权限自检接口。全仓库没有任何一处调用下单、划转、提币接口，
可以自己搜一遍 `cexpay/exchanges/` 里的 URL，一共就六个 GET。

### 能挡住的

| 场景 | 结果 |
|---|---|
| 服务器被入侵，攻击者拿到凭据文件 | 只读 Key + IP 白名单下，攻击者只能看你的进账记录，动不了钱 |
| 用户少付 | 一律不核销（无论差多少，见 [matching.md](matching.md#3-金额容差与少付为什么一律不放行)） |
| 同一笔转账想核销两张订单 | `settled_tx` 对 `(exchange, tx_id)` 有唯一约束，物理上不可能 |
| 用户拿旧转账记录来"付"新订单 | 时间窗 + 唯一金额 24h 冷却双重拦截 |
| 伪造回调骗你的业务系统发货 | HMAC-SHA256 签名 + 时间戳防重放 |
| 猜测别人的订单号 | `order_id` 是 20 位 hex（80 bit 熵） |

### 挡不住的，得自己补

| 风险 | 你需要做的 |
|---|---|
| 有人狂刷 `POST /api/orders`，把同价位的 9999 个唯一金额占满 | 反向代理限流 |
| 有人狂刷 `POST /check`，打爆交易所的频率限制 | 反向代理限流（每次调用要打三家 API） |
| 管理后台暴露在公网 | 只允许内网/VPN 访问 `/admin` 和 `/api/admin/*` |
| `callback_url` 由浏览器传入，可以拿来打 SSRF | 在你的业务后端写死 `callback_url`，别让前端决定 |
| 明文 HTTP 传输令牌 | 反向代理上配 TLS |
| 服务器被拖库 | 订单库里有付款方昵称/UID，属于个人信息，注意备份加密 |

---

## 创建只读 API Key

> 各所的后台界面会变，文字表述可能和下面略有差异。判断标准只有一条：
> **除了"读取"，其它权限一个都别勾。** 权限字段的权威定义见各所官方文档：
> [Binance](https://developers.binance.com/docs/binance-spot-api-docs) ·
> [OKX](https://www.okx.com/docs-v5/zh/) ·
> [Bitget](https://www.bitget.com/api-doc/common/intro)

### Binance

1. 网页端 → 右上头像 → API 管理（API Management）→ 创建 API → 选 System generated。
2. 权限里只保留 `Enable Reading`（启用读取，默认就开着）。以下全部不要勾：

   | 权限 | 说明 |
   |---|---|
   | `Enable Spot & Margin Trading` | 现货/杠杆交易 |
   | `Enable Withdrawals` | 提币，绝对不要勾 |
   | `Enable Internal Transfer` | 内部划转 |
   | `Permits Universal Transfer` | 万能划转 |
   | `Enable Futures` / `Enable Margin` / `Enable Vanilla Options` | 合约 / 杠杆 / 期权 |

3. 建议开 IP 白名单（Restrict access to trusted IPs only），填你服务器的出口 IP。
4. 不需要 passphrase。

```bash
cexpay creds set binance --account-label "Pay ID 你的PayID"
cexpay creds test -e binance
```

> Binance 的 Pay 收款 ID 在 App 的 Pay 页面可以看到，填进 `--account-label`
> 会展示在收银台上，方便用户直接转账而不扫码。

### OKX

1. 网页端 → 右上头像 → API → 创建 V5 API Key。
2. 权限选 只读 / Read，**不要勾 交易（Trade）和 提币（Withdraw）**。
3. 需要自己设一个 Passphrase，创建时只显示一次，记下来。
4. 建议绑定 IP。

```bash
cexpay creds set okx --account-label "UID 你的UID"     # 会交互提示输入 secret 和 passphrase
cexpay creds test -e okx
```

> OKX 的收款是站内转账，用户在 App 里向你的 UID 转账，秒到且免手续费，
> 所以 `--account-label` 建议填 UID。

### Bitget

1. 网页端 → 右上头像 → API 管理 → 新建 API。
2. 权限选 只读 / Read-only，**不要选 读写 / Read-Write**。
3. 需要自己设 Passphrase。
4. 建议绑定 IP。

```bash
cexpay creds set bitget --account-label "UID 你的UID"
cexpay creds test -e bitget
```

### 验收

```bash
cexpay creds test
```

三行都是 `✓ 只读 Key` 才算配好。出现 `✗` 说明确认带写权限，按提示回后台改；
出现 `?` 说明该所接口没返回权限字段、判定不了，自己回后台再确认一遍。

---

## 只读自检

`cexpay creds test` 和 `POST /api/admin/credentials/test` 会调各所的权限接口，
把结果归一成三态：

| 交易所 | 读取的接口与字段 |
|---|---|
| Binance | `GET /sapi/v1/account/apiRestrictions`，读 `enableWithdrawals`、`enableInternalTransfer`、`enableSpotAndMarginTrading`、`enableFutures`、`enableMargin`、`enableVanillaOptions`、`permitsUniversalTransfer`、`ipRestrict` |
| OKX | `GET /api/v5/account/config`，读 `perm`（如 `read_only,trade`）、`uid`、`ip` |
| Bitget | `GET /api/v2/spot/account/info`，读 `authorities`、`userId`、`ips` |

| `read_only` | 含义 | 默认行为 |
|---|---|---|
| `true` | 确认只读 | 正常启动 |
| `false` | 确认带写权限，`detail` 里列出是哪些 | 拒绝启动 |
| `null` | 接口没给权限字段，判定不了 | 允许启动，但会记一条警告 |

`CEXPAY_ENFORCE_READONLY=true`（默认）时，启动阶段发现任何一家是 `false` 就直接
抛错退出。想跳过这个闸门必须显式设成 `false`，不建议这么做，那等于把整个
威胁模型的第一道防线拆了。

> `null` 就是判定不了。交易所改了字段名、返回了不认识的权限值，都会落到 `null`。
> 它不是"安全"的同义词，遇到 `null` 请自己去后台核对。

---

## 凭据存储

### 位置与权限

```
<CEXPAY_DATA_DIR>/credentials.json     # 默认 data/credentials.json，权限 0600
```

写入走"临时文件 + 原子替换"，临时文件在替换前就已经是 `0600`，不存在
"短暂 0644 窗口"。`data/` 已经在 `.gitignore` 里。

### 优先级

环境变量高于文件。容器里用 `CEXPAY_BINANCE_API_KEY` 之类的环境变量覆盖文件内容
是有效的，方便 K8s Secret / Docker secrets 这类部署方式。

```
CEXPAY_{BINANCE,OKX,BITGET}_{API_KEY,API_SECRET,PASSPHRASE,ACCOUNT_LABEL}
```

### 加密落盘（可选）

设置 `CEXPAY_MASTER_KEY` 后，`credentials.json` 会用 Fernet（AES-128-CBC + HMAC）
加密存储。口令可以是任意字符串，内部用 SHA-256 派生出合法的 32 字节密钥。

```bash
pip install cryptography              # 或 pip install -e ".[crypto]"
export CEXPAY_MASTER_KEY="$(openssl rand -hex 32)"
```

**丢了这个口令就解不开了**，只能删掉 `credentials.json` 重新配一遍凭据
（凭据本身在交易所那边还在，不会有资金损失）。口令自己单独存好，
别和 `credentials.json` 放在同一台机器的同一个备份里。

### 脱敏与日志

- 所有返回凭据的接口都做脱敏（`abcd******wxyz`，见 `_redact()`）；
  `api_secret` / `passphrase` 的原文不会出现在任何 HTTP 响应里。
- `cexpay creds set` 的 secret 和 passphrase 走 `getpass` 交互输入，
  不会进 shell history，也不会出现在 `ps` 的命令行里。
- 日志里只记交易所名、订单号、金额，不记凭据。

---

## 部署加固清单

逐条过一遍再上线：

- [ ] 只读 Key：`cexpay creds test` 三行都判成只读。
- [ ] IP 白名单：三家都绑了服务器出口 IP。
- [ ] `CEXPAY_ENFORCE_READONLY` 保持默认 `true`。
- [ ] 强令牌：`CEXPAY_ADMIN_TOKEN=$(openssl rand -hex 24)`。不设的话后台接口直接禁用（503），
      但那样你也没法用后台。
- [ ] 回调密钥：`CEXPAY_WEBHOOK_SECRET=$(openssl rand -hex 24)`，并在业务侧验签。
- [ ] 只听本机：保持 `CEXPAY_HOST=127.0.0.1`，前面放 Nginx / Caddy 做 TLS。
      Docker Compose 里端口也已经绑成 `127.0.0.1:8787:8787`。
- [ ] 后台限制来源：在反向代理上把 `/admin` 和 `/api/admin/` 限制到内网或 VPN。
- [ ] 限流：至少给 `POST /api/orders` 和 `POST /api/orders/{id}/check` 加上。
- [ ] `callback_url` 在服务端写死，不接受浏览器传入，防 SSRF。
- [ ] 备份 SQLite：`<DATA_DIR>/cexpay.sqlite3`。是 WAL 模式，
      备份时把 `-wal` / `-shm` 一起拷，或者用 `sqlite3 ... ".backup"`。
- [ ] 定期轮换 API Key，尤其是服务器换过手或有过异常访问之后。
- [ ] 别把 `data/`、`.env`、`aggregate.png` 提交进 git（已在 `.gitignore`）。

Nginx 参考片段：

```nginx
location /api/orders {
    limit_req zone=orders burst=10 nodelay;   # 需先定义 limit_req_zone
    proxy_pass http://127.0.0.1:8787;
}

location ~ ^/(admin|api/admin) {
    allow 10.0.0.0/8;        # 只放内网
    deny all;
    proxy_pass http://127.0.0.1:8787;
}

location / {
    proxy_pass http://127.0.0.1:8787;
}
```

---

## 已知的安全边界

| 项 | 现状 | 缓解 |
|---|---|---|
| CORS 是 `*` | 方便本地开发和跨域接入。管理接口靠 Bearer Token 保护，浏览器不会自动带上，所以 CSRF 拿不到后台权限 | 生产环境可以在反向代理上覆盖 CORS 头 |
| 无内置限流 | 公开接口可被刷 | 反向代理限流 |
| 上传无大小上限 | 依赖 uvicorn / 反向代理的请求体上限 | Nginx `client_max_body_size 8m;` |
| `callback_url` 不做出站白名单 | 谁能创建订单谁就能让服务器发一次 POST | `callback_url` 在你的后端写死 |
| 聚合图每次请求都重新合成 | CPU 开销 | 反代加缓存，或直接用落盘的 `<DATA_DIR>/qr/aggregate.png` |
| 单商户 | 没有多租户隔离 | 一个商户跑一个实例 |

---

## 合规与账号风险

- 本项目与 Binance、OKX、Bitget 均无任何关联。出现这些名字只是在说明
  "钱从哪条通道来"，不代表任何形式的授权、认证或背书。
- 只调用各所公开的只读 REST 接口，不模拟登录、不逆向私有协议、
  不绕过任何风控，也不发起任何转账。
- 用个人账户长期承接经营性收款，可能不符合交易所的用户协议，
  可能触发风控、限制出入金乃至冻结账号。这个风险与本项目的技术实现无关，
  它来自"用个人账号做生意"这件事本身。是否使用、如何使用，风险由部署者自行承担。
- 不同司法辖区对加密货币收款有不同的许可、税务和反洗钱要求。这不是法律建议，
  上线前请自行确认当地合规要求。
- 不要用它收取任何违法所得。

---

## 漏洞报告

发现安全问题请不要直接开公开 issue。按 [SECURITY.md](../SECURITY.md) 里的方式私下报告，
给一个修复窗口后再公开。
