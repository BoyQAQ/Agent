"""终端 UI — 参考 mini-claude 风格"""

import os
import sys
import time
from contextlib import contextmanager

from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.markdown import Markdown
from rich.rule import Rule
from rich.text import Text
from rich import box

console = Console(highlight=False, force_terminal=True, legacy_windows=False)

# ─── 流式响应状态 ─────────────────────────────────────────

_stream_text: str = ""           # 流式累积文本
_stream_model: str = ""          # 当前模型名
_stream_start: float = 0.0       # 开始时间
_stream_tokens: int = 0          # API 返回的 input tokens
_stream_ctx_win: int = 0         # 上下文窗口大小


def _build_footer(elapsed: float) -> str:
    """构建状态栏文本（模型 | 耗时 | Token用量）"""
    parts = []
    if _stream_model:
        parts.append(_stream_model)
    parts.append(f"{elapsed:.1f}s")
    if _stream_tokens and _stream_ctx_win:
        pct = _stream_tokens / _stream_ctx_win * 100
        parts.append(
            f"{_stream_tokens / 1000:.1f}K/"
            f"{_stream_ctx_win / 1000:.0f}K ({pct:.0f}%)"
        )
    return "  │  ".join(parts)


def set_live_tokens(input_tokens: int, context_window: int) -> None:
    """API 返回后设置 token 用量（在 live_response 内部调用）"""
    global _stream_tokens, _stream_ctx_win
    _stream_tokens = input_tokens
    _stream_ctx_win = context_window


@contextmanager
def live_response(model: str = ""):
    """
    流式响应上下文管理器。
    流式阶段：sys.stdout.write 逐字输出，零 Rich 介入，emoji 不乱码。
    流式结束：console.print 渲染一个完整 Rich Panel 包裹正文 + 脚注。
    """
    global _stream_text, _stream_model, _stream_start, _stream_tokens, _stream_ctx_win
    _stream_text = ""
    _stream_model = model
    _stream_start = time.time()
    _stream_tokens = 0
    _stream_ctx_win = 0
    console.print()
    try:
        yield
    finally:
        # 流式结束，用 Rich 渲染一个完整面板包裹所有内容
        elapsed = time.time() - _stream_start
        footer_text = _build_footer(elapsed)
        console.print(Panel(
            Group(
                Markdown(_stream_text or "(无输出)", code_theme="monokai"),
                Rule(style="dim"),
                Text(footer_text, style="dim"),
            ),
            border_style="green", box=box.ROUNDED,
        ))
        # 重置状态
        _stream_model = ""
        _stream_start = 0.0
        _stream_tokens = 0
        _stream_ctx_win = 0


def print_assistant_text(text: str):
    """逐字流式输出（纯 stdout，不经过 Rich）"""
    global _stream_text
    sys.stdout.write(text)
    sys.stdout.flush()
    _stream_text += text


# ─── 工具图标 ────────────────────────────────────────────

TOOL_ICONS = {
    "read_file": "📖", "write_file": "✏️", "run_shell": "💻",
    "list_files": "📁", "grep_search": "🔍", "skill": "⚡",
    "agent": "🤖", "enter_plan_mode": "📋", "exit_plan_mode": "✅",
    "tool_search": "🔎",
}


def _tool_summary(name: str, args: dict) -> str:
    if name == "read_file":
        return args.get("file_path", "")
    if name == "write_file":
        return f"{args.get('file_path', '')} ({len(args.get('content',''))} 字)"
    if name == "run_shell":
        cmd = args.get("command", "")
        return cmd[:60] + "..." if len(cmd) > 60 else cmd
    if name == "list_files":
        return args.get("pattern", "")
    if name == "grep_search":
        return args.get("pattern", "")
    if name == "skill":
        return f"{args.get('skill_name','')} {args.get('args','')}"
    if name == "agent":
        return f"[{args.get('type','general')}] {args.get('description','')}"
    return ""


# ─── 工具调用打印 ────────────────────────────────────────

def print_tool_call(name: str, args: dict):
    """打印工具调用状态行"""
    icon = TOOL_ICONS.get(name, "🔧")
    summary = _tool_summary(name, args)
    console.print(f"  {icon} [bold blue]{name}[/bold blue] [dim]{summary}[/dim]")


def print_tool_result(result: str):
    """紧凑打印工具返回结果"""
    if not result:
        return
    # 截断显示
    short = result.replace("\n", " ")[:100]
    if len(result) > 100:
        short += f" ... ({len(result)} 字符)"
    console.print(f"    [dim]→ {short}[/dim]")


def print_permission_confirm(message: str):
    """权限确认提示"""
    console.print(f"  [yellow]⚠ {message}[/yellow]")


def print_permission_deny(tool_name: str, message: str):
    """权限拒绝提示"""
    console.print(f"  [red]🚫 {tool_name}: {message}[/red]")


def print_compact_start():
    """压缩开始提示"""
    console.print("  [yellow]⟳ 自动压缩中...[/yellow]", end="")


def print_compact_done(chars: int):
    """压缩完成提示"""
    console.print(f" [green]✓ 已压缩为 {chars} 字符[/green]")


def print_info(msg: str):
    """通用信息提示"""
    console.print(f"  [dim]ℹ {msg}[/dim]")


def print_error(msg: str):
    """通用错误提示"""
    console.print(f"  [red]✗ {msg}[/red]")


def print_config(config: dict):
    """打印当前配置"""
    table = Table(box=box.ROUNDED, border_style="cyan")
    table.add_column("配置项", style="dim")
    table.add_column("值")
    table.add_row("base_url", str(config.get("base_url", "")))
    table.add_row("api_key", (config.get("api_key", "")[:15] + "...") if config.get("api_key") else "(未设置)")
    table.add_row("model", str(config.get("model", "")))
    console.print(table)


def print_model_list(models: list[str], current: str, keyword: str = ""):
    """打印模型列表表格"""
    title = f"可用模型 (共 {len(models)} 个)"
    if keyword:
        title += f" — 过滤: '{keyword}'"
    table = Table(title=title, box=box.ROUNDED, border_style="cyan")
    table.add_column("#", style="dim", width=4)
    table.add_column("模型名")
    table.add_column("状态", width=10)
    for i, m in enumerate(models, 1):
        if m == current:
            table.add_row(str(i), m, "[green]● 当前[/green]")
        else:
            table.add_row(str(i), m, "")
    console.print(table)


def append_tool(attempt: int, max_attempts: int, error_msg: str):
    """API 重试状态提示"""
    console.print(f"  [yellow]↻ 重试 {attempt}/{max_attempts} — {error_msg}[/yellow]")


# ─── 启动横幅 ────────────────────────────────────────────

def print_welcome(api_base: str, model: str, mode: str, workdir: str = ""):
    cwd = workdir or os.getcwd()
    console.clear()
    console.print()
    console.print("""[bold cyan]
 █████╗ ███████╗████████╗██████╗  ██████╗ ███╗   ██╗ █████╗ ██╗   ██╗████████╗
██╔══██╗██╔════╝╚══██╔══╝██╔══██╗██╔═══██╗████╗  ██║██╔══██╗██║   ██║╚══██╔══╝
███████║███████╗   ██║   ██████╔╝██║   ██║██╔██╗ ██║███████║██║   ██║   ██║
██╔══██║╚════██║   ██║   ██╔══██╗██║   ██║██║╚██╗██║██╔══██║██║   ██║   ██║
██║  ██║███████║   ██║   ██║  ██║╚██████╔╝██║ ╚████║██║  ██║╚██████╔╝   ██║
╚═╝  ╚═╝╚══════╝   ╚═╝   ╚═╝  ╚═╝ ╚═════╝ ╚═╝  ╚═══╝╚═╝  ╚═╝ ╚═════╝    ╚═╝
[/bold cyan]""")
    console.print()
    console.print(Panel(
        f"[dim]{cwd}[/dim]\n"
        f"[dim]{api_base}[/dim]  |  [green]{model}[/green]  |  [yellow]{mode}[/yellow]",
        box=box.ROUNDED, border_style="cyan",
    ))
    console.print("[dim]  /help 查看所有命令[/dim]")
