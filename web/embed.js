/*!
 * multi-cex-pay 嵌入式收银台
 *
 * 接入方式（一行 script）：
 *   <script src="https://你的网关域名/embed.js"></script>
 *
 * 推荐用法，订单在你自己的后端创建，浏览器只拿 order_id：
 *   const { order_id } = await fetch('/my-api/create-order', {method:'POST'}).then(r=>r.json());
 *   CexPay.open({ orderId: order_id, onPaid: o => location.href = '/thanks' });
 *
 * 快速试用，直接由浏览器下单（金额来自前端，不可信，仅适合内部工具或演示）：
 *   CexPay.open({ amount: '9.9', onPaid: o => console.log('paid', o) });
 *
 * 所有回调：onPaid(order) / onExpired(order) / onClose() / onError(err)
 */
(function () {
  "use strict";

  // 网关地址从本脚本自己的 src 推断，接入方不用再配一遍
  var script = document.currentScript ||
    (function () {
      var all = document.getElementsByTagName("script");
      return all[all.length - 1];
    })();
  var ORIGIN = (function () {
    try {
      return new URL(script.src).origin;
    } catch (e) {
      return window.location.origin;
    }
  })();

  var STYLE_ID = "cexpay-embed-style";
  var CSS =
    // 不用 backdrop-filter：低端机上掉帧，而且会让部分浏览器的截图和录屏合成出错。
    // 纯色半透明遮罩在所有环境下表现一致。
    ".cexpay-mask{position:fixed;inset:0;z-index:2147483000;background:rgba(15,18,24,.72);" +
    "display:flex;align-items:center;justify-content:center;" +
    "padding:16px;opacity:0;transition:opacity .18s ease}" +
    ".cexpay-mask.cexpay-in{opacity:1}" +
    ".cexpay-box{position:relative;width:100%;max-width:460px;height:min(760px,92vh);" +
    "background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 24px 64px rgba(0,0,0,.4);" +
    "transform:translateY(8px) scale(.99);transition:transform .18s ease}" +
    ".cexpay-mask.cexpay-in .cexpay-box{transform:none}" +
    ".cexpay-box iframe{width:100%;height:100%;border:0;display:block}" +
    ".cexpay-x{position:absolute;top:10px;right:10px;z-index:2;width:30px;height:30px;" +
    "border:0;border-radius:50%;background:rgba(127,127,127,.18);color:#666;font-size:17px;" +
    "line-height:30px;cursor:pointer;padding:0}" +
    ".cexpay-x:hover{background:rgba(127,127,127,.32)}" +
    ".cexpay-load{position:absolute;inset:0;display:flex;align-items:center;" +
    "justify-content:center;font:14px/1.6 -apple-system,BlinkMacSystemFont,'PingFang SC'," +
    "'Microsoft YaHei',sans-serif;color:#888;background:#fff}" +
    "@media (max-width:520px){.cexpay-box{max-width:none;height:100%;border-radius:0}" +
    ".cexpay-mask{padding:0}}";

  function injectStyle() {
    if (document.getElementById(STYLE_ID)) return;
    var el = document.createElement("style");
    el.id = STYLE_ID;
    el.textContent = CSS;
    document.head.appendChild(el);
  }

  var state = null;   // { mask, opts, onMessage, onKey }

  function noop() {}

  function call(fn, arg) {
    if (typeof fn === "function") {
      try { fn(arg); } catch (e) { console.error("[CexPay] 回调抛错:", e); }
    }
  }

  function close(reason) {
    if (!state) return;
    var s = state;
    state = null;
    window.removeEventListener("message", s.onMessage);
    document.removeEventListener("keydown", s.onKey);
    s.mask.classList.remove("cexpay-in");
    setTimeout(function () {
      if (s.mask.parentNode) s.mask.parentNode.removeChild(s.mask);
    }, 200);
    if (reason !== "paid") call(s.opts.onClose);
  }

  function mount(orderId, opts) {
    injectStyle();

    var mask = document.createElement("div");
    mask.className = "cexpay-mask";
    mask.setAttribute("role", "dialog");
    mask.setAttribute("aria-modal", "true");
    mask.setAttribute("aria-label", "扫码支付");

    var box = document.createElement("div");
    box.className = "cexpay-box";

    var loading = document.createElement("div");
    loading.className = "cexpay-load";
    loading.textContent = "正在打开收银台…";

    var frame = document.createElement("iframe");
    var qs = "order_id=" + encodeURIComponent(orderId) + "&embed=1";
    if (opts.theme) qs += "&theme=" + encodeURIComponent(opts.theme);
    frame.src = ORIGIN + "/checkout?" + qs;
    frame.setAttribute("allow", "clipboard-write");
    frame.addEventListener("load", function () {
      if (loading.parentNode) loading.parentNode.removeChild(loading);
    });

    var x = document.createElement("button");
    x.className = "cexpay-x";
    x.type = "button";
    x.innerHTML = "&times;";
    x.setAttribute("aria-label", "关闭");
    x.onclick = function () { close("user"); };

    box.appendChild(frame);
    box.appendChild(loading);
    box.appendChild(x);
    mask.appendChild(box);

    if (opts.closeOnBackdrop !== false) {
      mask.addEventListener("click", function (e) {
        if (e.target === mask) close("user");
      });
    }

    function onKey(e) {
      if (e.key === "Escape") close("user");
    }

    function onMessage(e) {
      // 只接受网关自己发来的消息
      if (e.origin !== ORIGIN) return;
      var data = e.data;
      if (!data || typeof data !== "object" || String(data.type || "").indexOf("cexpay:") !== 0) {
        return;
      }
      if (data.type === "cexpay:paid") {
        call(opts.onPaid, data.order);
        if (opts.autoClose !== false) {
          setTimeout(function () { close("paid"); }, opts.autoCloseDelay || 1600);
        }
      } else if (data.type === "cexpay:expired") {
        call(opts.onExpired, data.order);
      } else if (data.type === "cexpay:close") {
        close("user");
      } else if (data.type === "cexpay:height" && opts.autoHeight !== false) {
        // 让弹窗贴合内容高度，上限仍受 92vh 约束（CSS 里的 min() 兜底）
        var h = Number(data.height);
        if (Number.isFinite(h) && h > 200) {
          box.style.height = "min(" + Math.round(h) + "px, 92vh)";
        }
      }
    }

    document.addEventListener("keydown", onKey);
    window.addEventListener("message", onMessage);
    document.body.appendChild(mask);
    requestAnimationFrame(function () { mask.classList.add("cexpay-in"); });

    state = { mask: mask, opts: opts, onMessage: onMessage, onKey: onKey };
    return mask;
  }

  var CexPay = {
    /** 网关地址（从 script src 推断出来的），需要自己调 API 时可以用 */
    origin: ORIGIN,

    /**
     * 打开收银台弹窗。
     * @param {object} opts
     *   orderId  已在服务端创建好的订单号（推荐）
     *   amount   或者直接给金额，由浏览器下单（金额不可信，仅限内部使用）
     *   exchange 限定只能用某一家付款，留空 = 任意
     *   ref      商户单号（merchant_ref），同一个 ref 会复用未过期的待付订单
     *   theme    'dark' | 'light'
     *   onPaid / onExpired / onClose / onError
     *   autoClose(默认 true) / autoCloseDelay(默认 1600ms) / closeOnBackdrop(默认 true)
 *   autoHeight(默认 true，弹窗高度跟随收银台内容)
     * @returns {Promise<string>} order_id
     */
    open: function (opts) {
      opts = opts || {};
      if (state) close("user");

      if (opts.orderId) {
        mount(opts.orderId, opts);
        return Promise.resolve(opts.orderId);
      }

      if (!opts.amount) {
        var err = new Error("CexPay.open 需要 orderId 或 amount");
        call(opts.onError, err);
        return Promise.reject(err);
      }

      var body = { amount: String(opts.amount) };
      if (opts.exchange) body.exchange = opts.exchange;
      if (opts.ref) body.merchant_ref = opts.ref;
      if (opts.currency) body.currency = opts.currency;
      if (opts.metadata) body.metadata = opts.metadata;

      return fetch(ORIGIN + "/api/orders", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      })
        .then(function (res) {
          return res.text().then(function (text) {
            var data = {};
            try { data = text ? JSON.parse(text) : {}; } catch (e) { data = { detail: text }; }
            if (!res.ok) throw new Error(data.detail || "HTTP " + res.status);
            return data;
          });
        })
        .then(function (data) {
          mount(data.order.order_id, opts);
          return data.order.order_id;
        })
        .catch(function (e) {
          call(opts.onError, e);
          throw e;
        });
    },

    /** 主动关掉弹窗 */
    close: function () { close("user"); },

    /** 弹窗是否开着 */
    isOpen: function () { return state !== null; },

    /** 查一笔订单的状态（轮询兜底用；正常应该在服务端收 webhook） */
    status: function (orderId) {
      return fetch(ORIGIN + "/api/orders/" + encodeURIComponent(orderId))
        .then(function (r) { return r.json(); })
        .then(function (d) { return d.order; });
    },
  };

  window.CexPay = CexPay;
  if (typeof module === "object" && module.exports) module.exports = CexPay;
})();
