"""测试辅助工具。"""
import time

from app.wasm_runner import RunResult, ValidatorEngine


class SlowValidator(ValidatorEngine):
    """每次校验 sleep，放大并行窗口，便于稳定观测并行度。"""

    def __init__(self, delay: float = 0.25) -> None:
        super().__init__()
        self.delay = delay
        self.concurrent = 0
        self.max_concurrent = 0

    def run(self, conn_limit: int, timeout_ms: int, fuel_budget: int) -> RunResult:
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        try:
            time.sleep(self.delay)
            return super().run(conn_limit, timeout_ms, fuel_budget)
        finally:
            self.concurrent -= 1
