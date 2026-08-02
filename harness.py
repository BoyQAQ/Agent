"""Tool Orchestration Harness — 工具编排层：重试、纠错、熔断、指标"""

import json
import time
import threading
from collections import defaultdict
from functools import wraps


# ─── 错误分类 ────────────────────────────────────────────

FATAL_PATTERNS = [
    "文件不存在", "FileNotFound", "file not found",
    "沙箱拦截", "sandbox",
    "权限不足", "Permission denied",
    "无法解析路径", "invalid",
]

RETRYABLE_PATTERNS = [
    "超时", "Timeout", "timeout", "timed out",
    "连接失败", "Connection", "connection refused",
    "暂时不可用", "temporarily unavailable", "503", "429",
    "重试", "retry",
    "资源忙", "busy", "locked",
]


def classify_error(error_msg: str) -> str:
    """将错误分为 fatal / retryable / unknown"""
    msg_lower = error_msg.lower()
    for p in FATAL_PATTERNS:
        if p.lower() in msg_lower:
            return "fatal"
    for p in RETRYABLE_PATTERNS:
        if p.lower() in msg_lower:
            return "retryable"
    return "unknown"


# ─── 熔断器 ──────────────────────────────────────────────

class CircuitBreaker:
    """熔断器：连续失败 N 次后暂停 M 秒"""

    def __init__(self, name: str, threshold: int = 3, cooldown: float = 30.0):
        self.name = name
        self.threshold = threshold
        self.cooldown = cooldown
        self._failures = 0
        self._last_failure = 0.0
        self._open = False

    @property
    def is_open(self) -> bool:
        if not self._open:
            return False
        if time.time() - self._last_failure > self.cooldown:
            self._open = False
            self._failures = 0
        return self._open

    def success(self):
        self._failures = 0
        self._open = False

    def failure(self):
        self._failures += 1
        self._last_failure = time.time()
        if self._failures >= self.threshold:
            self._open = True


# ─── 执行指标 ────────────────────────────────────────────

class ToolMetrics:
    """工具执行指标追踪"""

    def __init__(self):
        self._lock = threading.Lock()
        self._data: dict[str, dict] = defaultdict(lambda: {
            "calls": 0, "success": 0, "failures": 0,
            "retries": 0, "total_latency": 0.0, "last_error": "",
        })

    def record(self, tool_name: str, success: bool, latency: float,
               retries: int = 0, error: str = ""):
        with self._lock:
            d = self._data[tool_name]
            d["calls"] += 1
            if success:
                d["success"] += 1
            else:
                d["failures"] += 1
            d["retries"] += retries
            d["total_latency"] += latency
            if error:
                d["last_error"] = error[:200]

    def stats(self) -> dict:
        with self._lock:
            result = {}
            for name, d in sorted(self._data.items()):
                total = d["calls"]
                if total == 0:
                    continue
                result[name] = {
                    "calls": total,
                    "success_rate": f"{d['success'] / total * 100:.0f}%",
                    "avg_latency": f"{d['total_latency'] / total:.3f}s",
                    "retries": d["retries"],
                    "last_error": d["last_error"][:80],
                }
            return result

    def summary(self) -> str:
        s = self.stats()
        if not s:
            return "(无指标)"
        lines = [f"{k}: {v['calls']}次, {v['success_rate']}成功率, {v['avg_latency']}" for k, v in s.items()]
        return "\n".join(lines)


# 全局指标实例
metrics = ToolMetrics()


# ─── 工具执行器 ──────────────────────────────────────────

class ToolExecutor:
    """带重试和纠错的工具执行器

    用法:
        executor = ToolExecutor(max_retries=2)
        result = executor.execute("read_file", {"file_path": "main.py"})
    """

    def __init__(self, max_retries: int = 2, timeout: float = 60.0):
        self.max_retries = max_retries
        self.timeout = timeout
        self._breakers: dict[str, CircuitBreaker] = {}

    def _get_breaker(self, tool_name: str) -> CircuitBreaker:
        if tool_name not in self._breakers:
            self._breakers[tool_name] = CircuitBreaker(tool_name)
        return self._breakers[tool_name]

    def execute(self, tool_name: str, args: dict, execute_fn) -> tuple[str, int]:
        """
        执行工具，自动重试。

        返回: (result_string, retry_count)
        """
        breaker = self._get_breaker(tool_name)
        if breaker.is_open:
            return f"🛡 熔断: {tool_name} 连续失败 {breaker.threshold} 次，暂停 {breaker.cooldown}s", 0

        last_error = ""
        corrected_args = dict(args)

        for attempt in range(self.max_retries + 1):
            t0 = time.time()
            try:
                # 超时保护（用线程）
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(execute_fn, tool_name, corrected_args)
                    try:
                        result = future.result(timeout=self.timeout)
                    except concurrent.futures.TimeoutError:
                        return f"⏱ 工具执行超时 ({self.timeout}s): {tool_name}", attempt

                latency = time.time() - t0
                breaker.success()
                metrics.record(tool_name, True, latency, attempt)
                return result, attempt

            except Exception as e:
                latency = time.time() - t0
                error_msg = str(e)
                category = classify_error(error_msg)

                if category == "fatal":
                    breaker.failure()
                    metrics.record(tool_name, False, latency, attempt, error_msg)
                    return f"❌ 致命错误: {error_msg}", attempt

                last_error = error_msg
                breaker.failure()

                if attempt < self.max_retries:
                    # 尝试修正参数
                    corrected_args = self._correct_params(tool_name, args, error_msg)
                    delay = min(2 ** attempt, 10)
                    time.sleep(delay)

        metrics.record(tool_name, False, time.time() - t0, self.max_retries, last_error)
        return f"❌ 重试 {self.max_retries} 次后仍失败: {last_error}", self.max_retries

    def _correct_params(self, tool_name: str, args: dict, error: str) -> dict:
        """根据错误信息尝试修正参数"""
        corrected = dict(args)
        err_lower = error.lower()

        if tool_name == "read_file":
            # 文件不存在 + 路径看起来是相对路径 → 尝试加项目根目录
            if "文件不存在" in error or "not found" in err_lower:
                fp = args.get("file_path", "")
                if fp and not fp.startswith("/"):
                    from pathlib import Path
                    alt = str(Path.cwd() / fp)
                    if Path(alt).is_file():
                        corrected["file_path"] = alt

        elif tool_name in ("write_file", "edit_file"):
            # 权限错误 → 尝试 .mini-agent 目录
            if "permission" in err_lower or "sandbox" in err_lower:
                fp = args.get("file_path", "")
                if fp:
                    from pathlib import Path
                    alt = str(Path.cwd() / ".mini-agent" / Path(fp).name)
                    corrected["file_path"] = alt

        elif tool_name == "run_shell":
            # Windows 路径分隔符问题 → 尝试转换
            cmd = args.get("command", "")
            if "\\" in cmd and "not found" in err_lower:
                corrected["command"] = cmd.replace("\\", "/")

        return corrected


# ─── 全局执行器 ──────────────────────────────────────────

_executor: ToolExecutor | None = None


def get_executor(max_retries: int = 2) -> ToolExecutor:
    global _executor
    if _executor is None:
        _executor = ToolExecutor(max_retries=max_retries)
    return _executor


def execute_with_retry(tool_name: str, args: dict, execute_fn) -> str:
    """便捷入口：自动重试执行工具，返回结果字符串"""
    exe = get_executor()
    result, retries = exe.execute(tool_name, args, execute_fn)
    return result
