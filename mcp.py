"""MCP 集成 — JSON-RPC over stdio，动态加载外部工具"""

import json
import os
import subprocess
import time
from pathlib import Path


# ─── McpConnection — 单个服务器连接 ──────────────────────

class McpConnection:
    """管理一个 MCP 服务器子进程，通过 stdin/stdout 做 JSON-RPC 通信"""

    def __init__(self, server_name: str, config: dict):
        self.name = server_name
        self.config = config
        self.process: subprocess.Popen | None = None
        self._next_id = 1
        self._pending: dict[int, dict] = {}  # id → {"resolve": ..., "reject": ...}
        self._buffer = b""
        self._tools: list[dict] = []

    @property
    def tools(self) -> list[dict]:
        return self._tools

    # ── 连接生命周期 ──────────────────────────────────

    def connect(self) -> None:
        """启动子进程"""
        cmd = self.config["command"]
        args = self.config.get("args", [])
        env = {**os.environ, **(self.config.get("env") or {})}

        self.process = subprocess.Popen(
            [cmd] + args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        print(f"[mcp] 已连接 {self.name}: {cmd} {' '.join(args)}")

    def initialize(self, timeout: float = 15) -> None:
        """握手：initialize → notifications/initialized"""
        self._send_request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "mini-agent", "version": "1.0.0"},
        }, timeout)
        self._send_notification("notifications/initialized")

    def discover_tools(self, timeout: float = 15) -> list[dict]:
        """调用 tools/list 发现工具"""
        result = self._send_request("tools/list", {}, timeout)
        tools = result.get("tools", [])
        self._tools = tools
        return tools

    def call_tool(self, tool_name: str, arguments: dict,
                  timeout: float = 60) -> str:
        """调用工具，返回文本结果"""
        result = self._send_request("tools/call", {
            "name": tool_name,
            "arguments": arguments,
        }, timeout)

        # 提取 text 类型 content
        content = result.get("content", [])
        texts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                texts.append(item["text"])
            elif isinstance(item, str):
                texts.append(item)
        return "\n".join(texts) if texts else json.dumps(result)

    def close(self) -> None:
        """关闭连接"""
        if self.process:
            try:
                self.process.stdin.close()
                self.process.terminate()
                self.process.wait(timeout=3)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass
            self.process = None
            self._pending.clear()

    # ── JSON-RPC 通信 ─────────────────────────────────

    def _send_request(self, method: str, params: dict,
                      timeout: float) -> dict:
        """发送请求，等待响应（同步阻塞，MCP 只在启动时用）"""
        if not self.process or self.process.poll() is not None:
            raise RuntimeError(f"MCP 服务器 {self.name} 已断开")

        msg_id = self._next_id
        self._next_id += 1

        request = json.dumps({
            "jsonrpc": "2.0", "id": msg_id,
            "method": method, "params": params,
        }, ensure_ascii=False)

        try:
            self.process.stdin.write((request + "\n").encode())
            self.process.stdin.flush()
        except Exception as e:
            raise RuntimeError(f"写入 {self.name} 失败: {e}")

        # 读取响应行
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.process.poll() is not None:
                stderr = ""
                try:
                    stderr = self.process.stderr.read().decode(errors="replace")
                except Exception:
                    pass
                raise RuntimeError(f"MCP 服务器 {self.name} 意外退出: {stderr[:200]}")

            try:
                line = self.process.stdout.readline()
                if not line:
                    time.sleep(0.05)
                    continue
                response = json.loads(line.decode())
                if response.get("id") == msg_id:
                    if "error" in response:
                        err = response["error"]
                        raise RuntimeError(
                            f"MCP error {err.get('code')}: {err.get('message')}"
                        )
                    return response.get("result", {})
            except json.JSONDecodeError:
                continue  # 跳过非 JSON 行（服务器日志等）
            except Exception:
                time.sleep(0.05)

        raise TimeoutError(f"MCP 请求 {method} 超时 ({timeout}s)")

    def _send_notification(self, method: str, params: dict = None) -> None:
        """发送通知（无 id，不等响应）"""
        if not self.process or self.process.poll() is not None:
            return
        msg = json.dumps({
            "jsonrpc": "2.0", "method": method,
            "params": params or {},
        }, ensure_ascii=False)
        try:
            self.process.stdin.write((msg + "\n").encode())
            self.process.stdin.flush()
        except Exception:
            pass


# ─── McpManager — 管理器 ──────────────────────────────────

class McpManager:
    """加载配置、连接所有服务器、提供工具定义和路由"""

    def __init__(self):
        self._connections: dict[str, McpConnection] = {}
        self._tools: list[dict] = []  # {"name": "mcp__srv__tool", "schema": ..., "server": "srv", "orig_name": "tool"}

    @property
    def tools(self) -> list[dict]:
        return self._tools

    # ── 配置加载 ──────────────────────────────────────

    def _load_configs(self) -> dict[str, dict]:
        """加载 settings.json 和 .mcp.json"""
        merged: dict[str, dict] = {}

        # 用户级
        user_path = Path.home() / ".mini-agent" / "mcp.json"
        self._merge_file(user_path, merged)

        # 项目级
        project_path = Path.cwd() / ".mcp.json"
        self._merge_file(project_path, merged)

        # 旧版 settings.json
        settings_path = Path.cwd() / ".mini-agent" / "settings.json"
        self._merge_file(settings_path, merged, key="mcpServers")

        return merged

    def _merge_file(self, path: Path, target: dict, key: str = None):
        if not path.is_file():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            servers = data.get(key) if key else data
            if isinstance(servers, dict):
                for name, cfg in servers.items():
                    if isinstance(cfg, dict) and "command" in cfg:
                        target[name] = cfg
        except Exception:
            pass

    # ── 连接与发现 ────────────────────────────────────

    def connect_all(self, timeout: float = 15):
        """连接所有配置的服务器，发现工具（已连接则跳过）"""
        if self._connections:
            return  # 已连接，跳过

        configs = self._load_configs()
        if not configs:
            return

        for name, cfg in configs.items():
            try:
                conn = McpConnection(name, cfg)
                conn.connect()
                conn.initialize(timeout)
                tools = conn.discover_tools(timeout)

                # 注册工具：mcp__serverName__toolName
                for t in tools:
                    tool_name = t.get("name", "unknown")
                    self._tools.append({
                        "full_name": f"mcp__{name}__{tool_name}",
                        "server": name,
                        "orig_name": tool_name,
                        "schema": t,
                    })

                self._connections[name] = conn
                print(f"[mcp] {name}: {len(tools)} 工具")

            except Exception as e:
                print(f"[mcp] {name} 连接失败: {e}")
                if name in self._connections:
                    self._connections[name].close()
                    del self._connections[name]

    # ── 工具定义转换 ──────────────────────────────────

    def get_tool_definitions(self) -> list[dict]:
        """转换为 OpenAI tool schema 格式"""
        defs = []
        for t in self._tools:
            schema = t["schema"]
            defs.append({
                "type": "function",
                "function": {
                    "name": t["full_name"],
                    "description": schema.get("description", f"MCP tool from {t['server']}"),
                    "parameters": schema.get("inputSchema",
                        {"type": "object", "properties": {}}),
                },
            })
        return defs

    # ── 路由与调用 ────────────────────────────────────

    def is_mcp_tool(self, name: str) -> bool:
        return name.startswith("mcp__")

    def call_tool(self, full_name: str, args: dict) -> str:
        """根据三段式名称路由到对应服务器"""
        parts = full_name.split("__", 2)
        if len(parts) < 3:
            return f"无效 MCP 工具名: {full_name}"
        server_name = parts[1]
        tool_name = parts[2]

        conn = self._connections.get(server_name)
        if not conn:
            return f"MCP 服务器 '{server_name}' 未连接"

        try:
            return conn.call_tool(tool_name, args)
        except Exception as e:
            return f"MCP 调用失败: {e}"

    def close_all(self):
        for conn in self._connections.values():
            conn.close()
        self._connections.clear()


# ─── 全局实例 ────────────────────────────────────────────

_manager: McpManager | None = None


def get_mcp_manager() -> McpManager:
    global _manager
    if _manager is None:
        _manager = McpManager()
        try:
            _manager.connect_all()
        except Exception:
            pass
    return _manager
