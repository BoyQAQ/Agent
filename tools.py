"""工具定义 + 执行 + 权限检查 + 错误重试"""

import json
import re
import subprocess
from pathlib import Path

# ─── 工具定义 ────────────────────────────────────────────

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取文件内容，返回带行号的文本",
            "parameters": {
                "type": "object",
                "properties": {"file_path": {"type": "string", "description": "文件路径"}},
                "required": ["file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "写入内容到文件",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "文件路径"},
                    "content": {"type": "string", "description": "要写入的内容"},
                },
                "required": ["file_path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_shell",
            "description": "执行 shell 命令",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string", "description": "命令"}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "列出匹配 glob 模式的文件",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "glob 模式，如 *.py 或 src/**/*.ts"},
                    "path": {"type": "string", "description": "搜索目录，默认当前目录"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep_search",
            "description": "在文件中搜索正则表达式，返回匹配行",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "正则表达式"},
                    "path": {"type": "string", "description": "搜索目录，默认当前目录"},
                    "include": {"type": "string", "description": "文件过滤，如 *.py"},
                },
                "required": ["pattern"],
            },
        },
    },
    # Plan Mode（deferred）
    {
        "type": "function",
        "function": {
            "name": "enter_plan_mode",
            "description": "进入只读规划模式",
            "parameters": {"type": "object", "properties": {}},
        },
        "deferred": True,
    },
    {
        "type": "function",
        "function": {
            "name": "exit_plan_mode",
            "description": "退出规划模式，提交 plan 文件供用户审批",
            "parameters": {"type": "object", "properties": {}},
        },
        "deferred": True,
    },
    # tool_search
    {
        "type": "function",
        "function": {
            "name": "tool_search",
            "description": "搜索可用工具，用于激活延迟加载的工具",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "工具名或关键词"}},
                "required": ["query"],
            },
        },
    },
    # skill — 调用技能
    {
        "type": "function",
        "function": {
            "name": "skill",
            "description": "按名称调用已注册的技能。技能是 prompt 模板，展开后作为指令执行。",
            "parameters": {
                "type": "object",
                "properties": {
                    "skill_name": {"type": "string", "description": "技能名称"},
                    "args": {"type": "string", "description": "传递给技能的参数（可选）"},
                },
                "required": ["skill_name"],
            },
        },
    },
    # agent — 派生子 Agent
    {
        "type": "function",
        "function": {
            "name": "agent",
            "description": "派生子 Agent 执行独立任务。类型: explore(只读搜索), plan(只读方案设计), general(完整工具集)。",
            "parameters": {
                "type": "object",
                "properties": {
                    "description": {"type": "string", "description": "任务简述（3-5 词）"},
                    "prompt": {"type": "string", "description": "详细任务指令"},
                    "type": {"type": "string", "enum": ["explore", "plan", "general"], "description": "Agent 类型，默认 general"},
                },
                "required": ["description", "prompt"],
            },
        },
    },
]

# ─── Deferred 工具激活 ────────────────────────────────────

_activated_tools: set[str] = set()


def get_active_tools() -> list[dict]:
    result = []
    for t in TOOLS:
        if t.get("deferred") and t["function"]["name"] not in _activated_tools:
            continue
        result.append(t)
    return result


def activate_deferred(query: str) -> list[dict] | None:
    deferred = [t for t in TOOLS if t.get("deferred")]
    matches = [
        t for t in deferred
        if query.lower() in t["function"]["name"].lower()
        or query.lower() in t["function"].get("description", "").lower()
    ]
    if not matches:
        return None
    for m in matches:
        _activated_tools.add(m["function"]["name"])
    return matches


# ─── 沙箱配置 ────────────────────────────────────────────

# 系统路径黑名单 — 绝不读写
BLOCKED_PATHS = ["/etc", "/boot", "/sys", "/proc", "/dev",
                 "C:\\Windows", "C:\\System32", "/Windows", "/System32"]

# 网络相关命令（沙箱下全部拦截，防止数据外泄）
NETWORK_COMMANDS = {"curl", "wget", "nc", "netcat", "telnet", "ftp",
                    "ssh", "scp", "rsync", "nslookup", "dig", "ping"}

# 输出重定向到文件 — 拦截 >
REDIRECT_PATTERN = re.compile(r"\|\s*(curl|wget|nc|bash|sh|python|node)\b"
                              r"|\bcurl\b|\bwget\b")


def _sandbox_command(cmd: str) -> str | None:
    """沙箱过滤：对危险操作返回拒绝原因，否则返回 None"""
    parts = cmd.strip().split()
    if not parts:
        return None

    base = parts[0].split("/")[-1].split("\\")[-1]

    # 1. 拦截网络命令（防止数据外泄）
    if base in NETWORK_COMMANDS:
        return f"沙箱禁止网络命令: {base}"

    # 2. 拦截写入系统路径
    for blocked in BLOCKED_PATHS:
        if blocked.lower() in cmd.lower():
            return f"沙箱禁止访问系统路径: {blocked}"

    # 3. 拦截输出重定向到系统路径
    if ">" in cmd:
        # 提取重定向目标
        after = cmd.split(">")[-1].strip().split()[0]
        for blocked in BLOCKED_PATHS:
            if blocked.lower() in after.lower():
                return f"沙箱禁止写入系统路径: {after}"

    # 4. 拦截管道到外部（curl | bash 类）
    if REDIRECT_PATTERN.search(cmd):
        return "沙箱禁止管道到外部命令"

    return None

# ─── 工具执行 ────────────────────────────────────────────

def execute_tool(name: str, args: dict) -> str:
    if name == "read_file":
        try:
            content = Path(args["file_path"]).read_text(encoding="utf-8")
            lines = content.split("\n")
            return "\n".join(f"{i+1:4d}| {line}" for i, line in enumerate(lines))
        except FileNotFoundError:
            return f"错误: 文件不存在 — {args['file_path']}"
        except UnicodeDecodeError:
            try:
                content = Path(args["file_path"]).read_text(encoding="gbk")
                lines = content.split("\n")
                return "\n".join(f"{i+1:4d}| {line}" for i, line in enumerate(lines))
            except Exception as e:
                return f"错误: 文件编码无法识别 — {e}"
        except Exception as e:
            return f"错误: {e}"

    elif name == "write_file":
        try:
            p = Path(args["file_path"])
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(args["content"], encoding="utf-8")
            return f"成功写入 {args['file_path']} ({len(args['content'])} 字符)"
        except Exception as e:
            return f"错误: {e}"
    elif name == "run_shell":
        import locale
        cmd = args.get("command", "")

        # 沙箱拦截
        rejection = _sandbox_command(cmd)
        if rejection:
            return f"🛡 沙箱拦截: {rejection}"

        try:
            enc = locale.getpreferredencoding()
            result = subprocess.run(
                cmd, shell=True, capture_output=True, text=True,
                timeout=30, encoding=enc, errors="replace"
            )
            output = ""
            if result.stdout:
                output += result.stdout
            if result.stderr:
                output += f"\n[stderr]\n{result.stderr}"
            output = output[:4000]
            if result.returncode != 0:
                return f"命令失败 (exit {result.returncode})\n{output}"
            return output or "(无输出)"
        except subprocess.TimeoutExpired:
            return "命令超时 (30秒)"

    elif name == "tool_search":
        result = activate_deferred(args.get("query", ""))
        if not result:
            return "没有匹配的延迟加载工具。"
        return json.dumps(
            [{"name": t["function"]["name"], "description": t["function"]["description"],
              "parameters": t["function"]["parameters"]} for t in result],
            indent=2, ensure_ascii=False,
        )

    elif name == "list_files":
        import glob
        try:
            p = Path(args.get("path") or ".")
            pattern = args["pattern"]
            files = [str(f.relative_to(p)) for f in p.glob(pattern) if f.is_file()]
            files = [f for f in files if "node_modules" not in f and ".git/" not in f]
            return "\n".join(files[:100]) or "(无匹配)"
        except Exception as e:
            return f"错误: {e}"

    elif name == "grep_search":
        try:
            import fnmatch
            p = Path(args.get("path") or ".")
            regex = re.compile(args["pattern"])
            include = args.get("include")
            matches = []
            for f in p.rglob("*"):
                if not f.is_file():
                    continue
                if include and not fnmatch.fnmatch(f.name, include):
                    continue
                if "node_modules" in str(f) or ".git" in str(f):
                    continue
                try:
                    for i, line in enumerate(f.read_text(encoding="utf-8", errors="replace").split("\n"), 1):
                        if regex.search(line):
                            matches.append(f"{f}:{i}: {line[:120]}")
                            if len(matches) >= 100:
                                break
                except Exception:
                    pass
                if len(matches) >= 100:
                    break
            return "\n".join(matches) or "(无匹配)"
        except Exception as e:
            return f"错误: {e}"

    return f"未知工具: {name}"


# ─── 权限系统 ────────────────────────────────────────────

DANGEROUS_PATTERNS = [
    re.compile(r"\brm\s"),
    re.compile(r"\bgit\s+(push|reset|clean)\b"),
    re.compile(r"\bsudo\b"),
    re.compile(r">\s*/dev/"),
    re.compile(r"\bkill\b"),
    re.compile(r"\bchmod\s+777\b"),
    re.compile(r"\bformat\s", re.IGNORECASE),
    re.compile(r"\bdd\s+if="),
]

READ_TOOLS = {"read_file", "list_files", "grep_search"}
EDIT_TOOLS = {"write_file", "edit_file"}


def check_permission(tool_name: str, args: dict,
                     mode: str = "default",
                     plan_file_path: str | None = None) -> dict:
    if mode == "bypassPermissions":
        return {"action": "allow"}

    if mode == "plan":
        if tool_name in EDIT_TOOLS:
            fp = args.get("file_path") or args.get("path")
            if plan_file_path and fp == plan_file_path:
                return {"action": "allow"}
            return {"action": "deny", "message": f"[plan] {tool_name} 被拦截"}
        if tool_name == "run_shell":
            return {"action": "deny", "message": "[plan] shell 命令被拦截"}

    if tool_name in READ_TOOLS:
        return {"action": "allow"}

    if mode == "acceptEdits" and tool_name in EDIT_TOOLS:
        return {"action": "allow"}

    if tool_name == "run_shell":
        cmd = args.get("command", "")
        for pattern in DANGEROUS_PATTERNS:
            if pattern.search(cmd):
                if mode == "dontAsk":
                    return {"action": "deny",
                            "message": f"[dontAsk] 危险命令自动拒绝: {cmd[:60]}"}
                return {"action": "confirm",
                        "message": f"危险命令: {cmd[:100]}"}

    if tool_name == "write_file":
        if not Path(args.get("file_path", "")).exists():
            if mode == "dontAsk":
                return {"action": "deny", "message": "[dontAsk] 创建新文件被拒绝"}
            return {"action": "confirm",
                    "message": f"创建新文件: {args.get('file_path', '')}"}

    return {"action": "allow"}


def is_retryable(error: Exception) -> bool:
    status = getattr(error, "status_code", None)
    if status in (429, 503, 529):
        return True
    msg = str(error).lower()
    return any(kw in msg for kw in ("overloaded", "econnreset", "etimedout"))
