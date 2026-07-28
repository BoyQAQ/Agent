"""终端 UI — 参考 mini-claude 风格"""

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box

console = Console(highlight=False)

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


# ─── 启动横幅 ────────────────────────────────────────────

def print_welcome(api_base: str, model: str, mode: str, workdir: str = ""):
    import os
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


# ─── 输出函数 ────────────────────────────────────────────

def print_assistant_text(text: str):
    import sys
    sys.stdout.write(text)
    sys.stdout.flush()


def print_tool_call(name: str, args: dict):
    icon = TOOL_ICONS.get(name, "🔨")
    summary = _tool_summary(name, args)
    console.print(f"\n  [yellow]{icon} {name}[/yellow][dim] {summary}[/dim]")


def print_tool_result(result: str):
    lines = result.count("\n") + 1
    chars = len(result)
    preview = result[:200].replace("\n", " ")
    if result.startswith("错误") or result.startswith("❌"):
        console.print(f"    [red]✗ {preview}[/red]")
    else:
        console.print(f"    [dim]→ {lines}行 {chars}字[/dim]")


def print_permission_deny(tool: str, message: str):
    console.print(f"\n  [red]⊘ {tool} — {message}[/red]")


def print_permission_confirm(message: str):
    console.print(f"\n  [yellow]⚠ {message}[/yellow]")


def print_token_usage(input_tokens: int, effective_window: int):
    util = input_tokens / effective_window if effective_window else 0
    bar_len = 20
    filled = int(bar_len * util)
    bar = "█" * filled + "░" * (bar_len - filled)
    color = "green" if util < 0.5 else "yellow" if util < 0.85 else "red"
    console.print(f"\n  [{color}]{bar}[/{color}] {util:.0%}", end="")


def print_compact_start():
    console.print("  [yellow]⟳ 压缩中...[/yellow]")


def print_compact_done(summary_len: int):
    console.print(f"  [green]✓ 压缩完成 ({summary_len} 字)[/green]")


def print_retry(attempt: int, max_retries: int, reason: str):
    console.print(f"  [yellow]↻ 重试 {attempt}/{max_retries}[/yellow]")


def print_model_list(models: list[str], current_model: str, keyword: str = ""):
    if keyword:
        console.print(f"\n  [dim]过滤: '{keyword}' → {len(models)} 匹配[/dim]")
    table = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
    table.add_column("#", style="dim", width=4)
    table.add_column("Model", style="white")
    for i, m in enumerate(models, 1):
        if m == current_model:
            table.add_row(str(i), f"[green]{m} ← 当前[/green]")
        else:
            table.add_row(str(i), m)
    console.print(table)
    console.print("\n  [dim]序号=选择 | 关键词=过滤 | 模型名=直接切换 | 回车=取消[/dim]")


def print_config(api_config: dict):
    console.print()
    console.print(f"  [dim]base_url:[/dim] {api_config['base_url']}")
    key_preview = api_config['api_key'][:15] + "..." if api_config['api_key'] else "(未设置)"
    console.print(f"  [dim]api_key:[/dim]  {key_preview}")
    console.print(f"  [dim]model:[/dim]    [green]{api_config['model']}[/green]")


def print_info(msg: str):
    console.print(f"  [dim]{msg}[/dim]")


def print_error(msg: str):
    console.print(f"  [red]✗ {msg}[/red]")
