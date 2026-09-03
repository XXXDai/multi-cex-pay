"""嵌入式收银台的接入契约。

这些是接入方直接依赖的东西：脚本路径、CORS、embed/theme 参数、缓存指纹。
改坏任何一条，别人的站点就会静默出问题，所以钉在测试里。
"""

import re

AUTH = {"Authorization": "Bearer test-token"}


# ---------------------------------------------------------------- embed.js
def test_embed_js_served_at_root(client):
    """接入方写的是 <script src="…/embed.js">，路径必须稳定在根下。"""
    res = client.get("/embed.js")
    assert res.status_code == 200
    assert "javascript" in res.headers["content-type"]


def test_embed_js_allows_cross_origin(client):
    """脚本会被别人的域名加载，必须允许跨域。"""
    res = client.get("/embed.js")
    assert res.headers["access-control-allow-origin"] == "*"


def test_embed_js_is_revalidated_not_frozen(client):
    """URL 里没有指纹，所以不能长时间强缓存，否则发版后别人拿不到新脚本。"""
    cache = client.get("/embed.js").headers["cache-control"]
    assert "must-revalidate" in cache
    max_age = int(re.search(r"max-age=(\d+)", cache).group(1))
    assert max_age <= 600


def test_embed_js_exposes_the_documented_api(client):
    """CexPay.open/close/isOpen/status 是文档承诺的接口。"""
    body = client.get("/embed.js").text
    for name in ("open:", "close:", "isOpen:", "status:", "origin:"):
        assert name in body, f"embed.js 缺少 {name}"


def test_embed_js_validates_message_origin(client):
    """必须校验 postMessage 的来源，否则任何页面都能伪造 paid 事件。"""
    body = client.get("/embed.js").text
    assert "e.origin !== ORIGIN" in body


def test_embed_js_has_no_hardcoded_host(client):
    """网关地址要从自己的 script src 推断，接入方不该再配一遍。"""
    body = client.get("/embed.js").text
    assert "new URL(script.src).origin" in body
    assert "localhost" not in body and "127.0.0.1" not in body


# ---------------------------------------------------------------- 收银台的嵌入模式
def test_checkout_page_ships_embed_support(client):
    body = client.get("/checkout").text
    assert 'src="/static/checkout.js' in body


def test_checkout_js_reports_state_to_parent(client):
    """paid / expired / height 三种消息是 embed.js 依赖的。"""
    body = client.get("/static/checkout.js").text
    for kind in ("cexpay:${type}", "cexpay:height"):
        assert kind in body
    assert "postMessage" in body


def test_checkout_js_does_not_measure_in_raf(client):
    """回归：文档隐藏/被节流时 rAF 回调不执行，高度就永远报不出去。

    历史上 reportHeight 把测量放在 requestAnimationFrame 里，宿主页面在后台
    标签打开时高度上报完全失效。现在必须是同步测量 + ResizeObserver。
    """
    body = client.get("/static/checkout.js").text
    assert "ResizeObserver" in body
    # reportHeight 函数体里不该再出现 requestAnimationFrame
    start = body.index("function reportHeight()")
    end = body.index("function watchHeight()")
    assert "requestAnimationFrame" not in body[start:end]


# ---------------------------------------------------------------- 静态资源指纹
def test_static_assets_are_fingerprinted(client):
    """升级后接入方的用户不该还在跑旧前端。"""
    for page in ("/", "/checkout", "/admin"):
        body = client.get(page).text
        for asset in re.findall(r'/static/[A-Za-z0-9_.\-]+\.(?:css|js)[^"]*', body):
            assert "?v=" in asset, f"{page} 的 {asset} 没有版本指纹"


def test_fingerprint_tracks_file_content(client, data_dir):
    """指纹是内容哈希，改了文件就必须变——否则开发时要手动清缓存。"""
    from pathlib import Path

    from cexpay.server import WEB_DIR

    css = Path(WEB_DIR) / "app.css"
    original = css.read_text(encoding="utf-8")
    before = re.search(r"/static/app\.css\?v=([\w.\-]+)", client.get("/checkout").text).group(1)
    try:
        css.write_text(original + "\n/* fingerprint probe */\n", encoding="utf-8")
        after = re.search(r"/static/app\.css\?v=([\w.\-]+)", client.get("/checkout").text).group(1)
    finally:
        css.write_text(original, encoding="utf-8")
    assert before != after


def test_html_pages_are_not_cached(client):
    """HTML 必须回源，否则拿不到新的指纹。"""
    for page in ("/", "/checkout", "/admin"):
        assert "no-cache" in client.get(page).headers["cache-control"]
