"""后台轮询：定时跑 ``gateway.sweep()`` 和回调重投。"""

from __future__ import annotations

import logging
import threading

from .gateway import PaymentGateway

log = logging.getLogger("cexpay.poller")


class Poller:
    """一个可启停的后台线程。"""

    def __init__(self, gateway: PaymentGateway, interval_s: int | None = None):
        self.gateway = gateway
        self.interval_s = (
            interval_s if interval_s is not None else gateway.settings.poll_interval_s
        )
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def enabled(self) -> bool:
        return self.interval_s > 0

    def start(self) -> None:
        if not self.enabled:
            log.info("轮询已关闭（CEXPAY_POLL_INTERVAL=0），只走手动核销")
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="cexpay-poller", daemon=True)
        self._thread.start()
        log.info("后台轮询已启动，间隔 %ss", self.interval_s)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        log.info("后台轮询已停止")

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                result = self.gateway.sweep()
                if result["settled"]:
                    log.info("本轮核销 %s 笔订单", len(result["settled"]))
                self.gateway.dispatch_callbacks()
            except Exception:  # pragma: no cover - 后台线程不能挂
                log.exception("轮询出错，将在下一轮继续")
            self._stop.wait(self.interval_s)
