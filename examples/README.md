# 示例

按从简到繁排列。所有示例都不需要真实的交易所凭据也能跑起来看效果。

| 文件 | 说明 | 需要凭据？ |
|---|---|---|
| [`aggregate_qr.py`](aggregate_qr.py) | 二维码全链路：截图、自动裁剪、聚合、回读校验 | 不需要 |
| [`quickstart.py`](quickstart.py) | 直接用 `cexpay` 包（不走 HTTP）：下单、轮询核销、打印结果 | 可选（`--demo` 用伪造进账） |
| [`flask_shop.py`](flask_shop.py) | Flask 迷你商城，走 HTTP API + Python SDK，含 webhook 验签 | 需要 |
| [`express_shop.mjs`](express_shop.mjs) | 同上的 Node 版本，用 Node SDK | 需要 |
| [`webhook_receiver.php`](webhook_receiver.php) | 最小 PHP 回调端点 | 需要 |

## 二维码链路（不需要凭据）

造三张假的"App 收款页截图"，然后跑全链路：

```bash
.venv/bin/python - <<'PY'
from PIL import Image
from cexpay.qr import render_qr
import pathlib
pathlib.Path("/tmp/shots").mkdir(exist_ok=True)
demo = {"bn": "https://app.binance.com/uni-qr/DEMO",
        "okx": "https://www.okx.com/pay/receive?uid=1",
        "bg": "https://www.bitget.com/pay/receive?qrAction=pay&uid=2"}
for name, url in demo.items():
    page = Image.new("RGB", (700, 1100), "#f0f0f0")     # 模拟 App 页面底色
    page.paste(render_qr(url, size=360), (170, 340))
    page.save(f"/tmp/shots/{name}.png")
print("已生成 /tmp/shots/*.png")
PY

.venv/bin/python examples/aggregate_qr.py /tmp/shots/bn.png /tmp/shots/okx.png /tmp/shots/bg.png
```

会打印每张图识别出的品牌和内容、合成后的画布尺寸，以及逐格回读校验的结果。

## 核销链路（演示模式，不请求交易所）

```bash
.venv/bin/python examples/quickstart.py --amount 9.9 --demo
```

`--demo` 会伪造一笔金额刚好命中的进账，让你看到 T1 唯一金额是怎么核销的。
去掉 `--demo` 就会真的去打你配好的交易所接口。

## 接进真实业务

先把服务跑起来：

```bash
export CEXPAY_ADMIN_TOKEN=$(openssl rand -hex 24)
export CEXPAY_WEBHOOK_SECRET=$(openssl rand -hex 24)
.venv/bin/cexpay serve
```

### Flask

```bash
.venv/bin/pip install flask
CEXPAY_WEBHOOK_SECRET=$CEXPAY_WEBHOOK_SECRET .venv/bin/python examples/flask_shop.py
```

### Express

```bash
cd examples && npm init -y && npm i express
CEXPAY_WEBHOOK_SECRET=$CEXPAY_WEBHOOK_SECRET node express_shop.mjs
```

### PHP

```bash
CEXPAY_WEBHOOK_SECRET=$CEXPAY_WEBHOOK_SECRET php -S 127.0.0.1:8080 examples/webhook_receiver.php
```

## 容易踩的坑

1. 验签必须用原始请求体。先 `json.parse` 再 `stringify` 会改变空格和键序，
   签名一定对不上。Flask 用 `request.get_data()`，Express 用 `express.raw()`，
   PHP 用 `php://input`。
2. 回调处理必须对 `order_id` 幂等。网络抖动会导致同一笔回调投递多次，
   示例里都用一个 dict / map 做了去重演示，生产环境请落库并加唯一约束。
