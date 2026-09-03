# 接入指南

从零到收到第一笔钱。**挑一种接法就行，不用全看。**

| 你的情况 | 用哪种 | 改动量 |
|---|---|---|
| 有个网页，想加个「USDT 支付」按钮 | [① 嵌入式弹窗](#-嵌入式弹窗最省事) | 一行 `<script>` |
| 有后端，想完全控制流程 | [② 服务端 API](#-服务端-api标准做法) | 两个接口 |
| 只想要金额和二维码，UI 自己画 | [③ 纯数据模式](#-纯数据模式ui-全自己画) | 一个接口 |
| 不是 Web，比如 Telegram Bot / 桌面端 | [③ 纯数据模式](#-纯数据模式ui-全自己画) | 一个接口 |

三种接法共用同一套订单和核销逻辑，**随时可以换**。

---

## 前置：把网关跑起来

这一步是收款方（你）做的，接入方如果和你不是同一个人，只需要拿到网关地址。

```bash
git clone https://github.com/XXXDai/multi-cex-pay.git && cd multi-cex-pay
python3 -m venv .venv && .venv/bin/pip install -e .

.venv/bin/cexpay creds set binance --account-label "Pay ID 你的PayID"   # 只读 Key
.venv/bin/cexpay creds test                                            # 必须全绿

.venv/bin/cexpay qr crop ~/Desktop/binance收款页.png -e binance         # 截图丢进来即可

export CEXPAY_ADMIN_TOKEN=$(openssl rand -hex 24)
export CEXPAY_WEBHOOK_SECRET=$(openssl rand -hex 24)
.venv/bin/cexpay serve
```

只读 Key 怎么建见 [security.md](security.md#创建只读-api-key)。Docker 用户：`cp .env.example .env && docker compose up -d`。

---

## ① 嵌入式弹窗（最省事）

一行 script，一个函数调用，不用碰后端。

```html
<script src="https://你的网关域名/embed.js"></script>

<button id="pay">USDT 支付</button>

<script>
document.getElementById('pay').onclick = async () => {
  // 订单在你自己的后端创建，浏览器只拿 order_id —— 金额不可被篡改
  const { order_id } = await fetch('/my-api/create-order', { method: 'POST' })
    .then(r => r.json());

  CexPay.open({
    orderId: order_id,
    onPaid:  order => location.href = '/thanks?o=' + order.order_id,
    onClose: ()    => console.log('用户关掉了弹窗'),
  });
};
</script>
```

弹窗会自己处理：选交易所、显示金额和二维码、倒计时、轮询状态、支付成功后关闭。
高度跟随内容自适应，窄屏自动切成单码大图。

### 快速试用（金额从前端传）

```js
CexPay.open({ amount: '9.9', onPaid: o => console.log(o) });
```

> ⚠️ 这样金额由浏览器决定，用户改一下就能 1 分钱买走你的东西。
> **只适合内部工具、演示、或金额本来就无所谓的场景。** 对外一定用 `orderId`。

### `CexPay.open(opts)` 参数

| 参数 | 说明 |
|---|---|
| `orderId` | 服务端已创建的订单号（**推荐**） |
| `amount` | 或直接给金额，由浏览器下单（不可信） |
| `exchange` | 限定只能用某一家付款，留空 = 任意 |
| `ref` | 商户单号，同一个 `ref` 会复用未过期的待付订单（幂等） |
| `theme` | `'dark'` / `'light'`，默认跟随收银台自身设置 |
| `metadata` | 任意对象，会原样出现在回调里 |
| `onPaid(order)` | 支付成功 |
| `onExpired(order)` | 订单过期 |
| `onClose()` | 用户关闭弹窗 |
| `onError(err)` | 下单失败 |
| `autoClose` | 默认 `true`，支付成功后自动关（`autoCloseDelay` 默认 1600ms） |
| `autoHeight` | 默认 `true`，弹窗高度跟随内容 |
| `closeOnBackdrop` | 默认 `true`，点遮罩关闭 |

其它方法：`CexPay.close()`、`CexPay.isOpen()`、`CexPay.status(orderId)`、`CexPay.origin`。

> **别把 `onPaid` 当发货依据。** 它跑在用户浏览器里，用户可以自己伪造调用。
> 发货必须由服务端在收到 [Webhook](#webhook) 后执行；`onPaid` 只用来跳转页面、更新 UI。

---

## ② 服务端 API（标准做法）

两步：下单 → 收回调。

### 下单

```bash
curl -X POST https://你的网关/api/orders \
  -H 'Content-Type: application/json' \
  -d '{
        "amount": "9.9",
        "merchant_ref": "SHOP-1001",
        "callback_url": "https://myshop.com/webhook",
        "metadata": {"user_id": 42, "sku": "vip-1m"}
      }'
```

```json
{
  "order": { "order_id": "6dbd97a2…", "pay_amount": "9.9001", "...": "" },
  "checkout_url": "/checkout?order_id=6dbd97a2…",
  "qr_url": "/api/orders/6dbd97a2…/qr.png"
}
```

把用户跳转到 `checkout_url`（拼上网关域名），或者用 ① 的弹窗打开。

- **`pay_amount` 才是要让用户付的金额**，不是 `amount`。尾数是订单指纹。
- 带同一个 `merchant_ref` 重复下单会返回同一张订单，**天然幂等**，不用自己去重。
- `metadata` 原样存、原样回调，放你的业务字段。

### 收回调

服务端收到 `order.paid` → 验签 → 按 `order_id` 幂等发货。

<details open>
<summary><b>Python / Flask</b></summary>

```python
from flask import Flask, request
from cexpay_client import CexPayClient          # sdk/python/cexpay_client.py

app = Flask(__name__)
client = CexPayClient("https://你的网关", webhook_secret=os.environ["CEXPAY_WEBHOOK_SECRET"])

@app.post("/webhook")
def webhook():
    raw = request.get_data()                    # 原始字节，不要用 request.json
    if not client.verify_webhook(raw, request.headers.get("X-CexPay-Timestamp"),
                                 request.headers.get("X-CexPay-Signature")):
        return "", 400
    order = request.get_json()["order"]
    deliver_once(order["order_id"], order)      # 幂等
    return "", 200
```
</details>

<details>
<summary><b>Node / Express</b></summary>

```js
import express from "express";
import { CexPayClient } from "./sdk/node/cexpay.mjs";

const app = express();
const client = new CexPayClient("https://你的网关",
  { webhookSecret: process.env.CEXPAY_WEBHOOK_SECRET });

// express.raw 很关键：json 解析过的 body 签名对不上
app.post("/webhook", express.raw({ type: "*/*" }), (req, res) => {
  const ok = client.verifyWebhook(req.body, req.get("X-CexPay-Timestamp"),
                                  req.get("X-CexPay-Signature"));
  if (!ok) return res.sendStatus(400);
  const { order } = JSON.parse(req.body.toString("utf8"));
  deliverOnce(order.order_id, order);
  res.sendStatus(200);
});
```
</details>

<details>
<summary><b>PHP</b></summary>

```php
require 'sdk/php/CexPayClient.php';
$client = new CexPayClient('https://你的网关',
    ['webhook_secret' => getenv('CEXPAY_WEBHOOK_SECRET')]);

$raw = file_get_contents('php://input');        // 原始内容
if (!$client->verifyWebhook($raw, $_SERVER['HTTP_X_CEXPAY_TIMESTAMP'],
                                  $_SERVER['HTTP_X_CEXPAY_SIGNATURE'])) {
    http_response_code(400); exit;
}
$order = json_decode($raw, true)['order'];
deliver_once($order['order_id'], $order);
http_response_code(200);
```
</details>

<details>
<summary><b>Go</b></summary>

```go
client := cexpay.New("https://你的网关",
    cexpay.Options{WebhookSecret: os.Getenv("CEXPAY_WEBHOOK_SECRET")})

http.HandleFunc("/webhook", func(w http.ResponseWriter, r *http.Request) {
    body, _ := io.ReadAll(r.Body)               // 原始字节
    event, err := client.ParseWebhook(body,
        r.Header.Get("X-CexPay-Timestamp"), r.Header.Get("X-CexPay-Signature"))
    if err != nil { w.WriteHeader(400); return }
    deliverOnce(event.Order.OrderID, event.Order)
    w.WriteHeader(200)
})
```
</details>

### 本地就能把验签调通

不用等真有人付款——发一条**签名正确的假回调**给自己：

```bash
cexpay webhook-test http://127.0.0.1:5000/webhook
```

```
POST http://127.0.0.1:5000/webhook
X-CexPay-Timestamp: 1788450796
X-CexPay-Signature: a6d1003eec114248c060309027380…
被签名的字符串: 1788450796.<原始请求体>

✓ 对方返回 HTTP 200 —— 2xx 即视为投递成功，不会重试。
```

失败时会直接给出排查顺序。测幂等就固定订单号发两次：

```bash
cexpay webhook-test http://127.0.0.1:5000/webhook --order-id SAME-ORDER
cexpay webhook-test http://127.0.0.1:5000/webhook --order-id SAME-ORDER
```

---

## ③ 纯数据模式（UI 全自己画）

不用收银台页面，只要数据。适合 Telegram Bot、桌面端、小程序、或者你有自己的设计。

```bash
# 1. 下单，拿到要付的金额
curl -sX POST https://你的网关/api/orders -H 'Content-Type: application/json' \
     -d '{"amount":"9.9"}'

# 2. 拿二维码图片（PNG）
curl -s 'https://你的网关/api/orders/<order_id>/qr.png?exchange=binance' -o bn.png
curl -s 'https://你的网关/api/orders/<order_id>/qr.png?layout=row'        -o 聚合图.png

# 3. 有哪些渠道、每个渠道让用户填什么
curl -s https://你的网关/api/exchanges

# 4. 轮询状态（有 webhook 的话不需要这步）
curl -s https://你的网关/api/orders/<order_id>
```

Python SDK 里有个阻塞等待，写脚本很方便：

```python
order = client.create_order("9.9")["order"]
print(f"请付 {order['pay_amount']} {order['currency']}")
final = client.wait_for_payment(order["order_id"], timeout_s=900)
print("结果:", final["status"])
```

### 生成客户端

```bash
cexpay openapi -o openapi.json
```

丢给 `openapi-generator` / `oapi-codegen` 就能生成任意语言的 client。

---

## Webhook

| 项 | 值 |
|---|---|
| 方法 | `POST`，`Content-Type: application/json` |
| 事件 | `order.paid` |
| 头 | `X-CexPay-Timestamp`（unix 秒）、`X-CexPay-Signature`（hex HMAC-SHA256） |
| 被签名的串 | `f"{timestamp}.{原始请求体}"`，密钥是 `CEXPAY_WEBHOOK_SECRET` |
| 成功 | 返回 2xx |
| 重试 | 0s / 15s / 1m / 5m / 30m / 2h / 6h，共 7 次 |

### 三条铁律

1. **用原始字节验签。** 先 `json.parse` 再 `stringify` 会改动空格和键序，签名必然对不上。
   这是接入时最常见的坑。
2. **按 `order_id` 幂等。** 网络抖动会导致同一笔回调投递多次。
3. **发货只认 Webhook。** 浏览器里的 `onPaid` 可以被伪造。

四种语言的验签实现都在 [`sdk/`](../sdk/)，零第三方依赖，单文件拷走即用。
它们的签名口径由 [`tests/test_sdk_signature.py`](../tests/test_sdk_signature.py) 跨语言对齐。

---

## 常见问题

**用户付了钱但订单没核销。**
先看后台的「最近进账」或 `cexpay tx --minutes 60`，把交易所返回的金额和订单的
`pay_amount` 对一下。九成是金额不对（用户手输时抹掉了小数）或超了时间窗。
完整排查步骤见 [matching.md](matching.md#排查指南钱到账了但订单没核销)。

**能不能不改金额？**
可以，`CEXPAY_UNIQUE_AMOUNT=false`。代价是必须让用户在收银台补填付款方标识
（昵称 / UID 后三位），成功率和体验都会下降。

**跨域报错。**
`/api/*` 和 `/embed.js` 都允许跨域。如果你在反向代理上覆盖了 CORS 头，记得放行。

**弹窗打不开 / 被拦。**
`CexPay.open()` 是点击事件里同步调用的，不会被弹窗拦截器拦（它用的是 DOM 弹层，不是 `window.open`）。
如果没反应，看控制台是不是 `embed.js` 404 了——检查网关地址。

**升级网关后接入方要改东西吗？**
不用。前端资源带内容指纹，浏览器会自动拿新版；`/api` 的字段只增不减。

**我不想让接入方看到管理后台。**
在反向代理上把 `/admin` 和 `/api/admin/` 限制到内网，见
[security.md](security.md#部署加固清单)。`/api/orders` 和 `/embed.js` 保持公开即可。
