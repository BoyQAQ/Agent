"""Agent 核心循环 — 流式 API + 压缩管道 + 权限集成"""

import os
import time
import random
import json
from pathlib import Path

from openai import OpenAI

from tools import TOOLS, execute_tool, check_permission, is_retryable, get_active_tools
from prompt import build_system_prompt
from memory import (
    start_memory_prefetch, format_memories_for_injection,
    MemoryPrefetch,
)
from subagent import get_sub_agent_config
import skills as _skills_mod
import ui

# ─── 默认配置 ────────────────────────────────────────────

DEFAULT_PROVIDERS = {
    "deepseek": {
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "env_key": "DEEPSEEK_API_KEY",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o",
        "env_key": "OPENAI_API_KEY",
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "model": "anthropic/claude-sonnet-4",
        "env_key": "OPENROUTER_API_KEY",
    },
}


# ─── 配置持久化 ──────────────────────────────────────────

def _config_path() -> Path:
    d = Path.home() / ".mini-agent"
    d.mkdir(parents=True, exist_ok=True)
    return d / "config.json"


def save_api_config(config: dict) -> None:
    """保存配置到 ~/.mini-agent/config.json"""
    saved = {
        "base_url": config["base_url"],
        "model": config["model"],
        "api_key": config.get("api_key", ""),
    }
    try:
        _config_path().write_text(json.dumps(saved, indent=2, ensure_ascii=False))
    except Exception:
        pass


def load_api_config() -> dict | None:
    """从 ~/.mini-agent/config.json 加载上次保存的配置"""
    p = _config_path()
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
        return {
            "base_url": data.get("base_url", ""),
            "model": data.get("model", ""),
            "api_key": data.get("api_key", ""),
        }
    except Exception:
        return None


def resolve_api_config(*, provider: str | None = None,
                       api_key: str | None = None,
                       api_base: str | None = None,
                       model: str | None = None) -> dict:
    """
    解析 API 配置，优先级：参数 > 环境变量 > 持久化配置 > 默认 provider 配置。
    """
    provider = provider or "deepseek"
    provider_cfg = DEFAULT_PROVIDERS.get(provider, DEFAULT_PROVIDERS["deepseek"])

    # 加载持久化配置
    saved = load_api_config()

    resolved_key = api_key or os.environ.get(provider_cfg["env_key"]) or ""
    resolved_base = api_base or provider_cfg["base_url"]
    resolved_model = model or provider_cfg["model"]

    # 通用环境变量兜底
    if not resolved_key:
        resolved_key = os.environ.get("LLM_API_KEY", "")
    if os.environ.get("LLM_API_BASE"):
        resolved_base = os.environ["LLM_API_BASE"]
    if os.environ.get("LLM_MODEL"):
        resolved_model = os.environ["LLM_MODEL"]

    # 持久化配置兜底（最低优先级）
    if saved:
        if not resolved_key:
            resolved_key = saved.get("api_key") or resolved_key
        if not resolved_base or resolved_base == provider_cfg["base_url"]:
            resolved_base = saved.get("base_url") or resolved_base
        if not resolved_model or resolved_model == provider_cfg["model"]:
            resolved_model = saved.get("model") or resolved_model

    return {
        "api_key": resolved_key,
        "base_url": resolved_base,
        "model": resolved_model,
    }


# ─── 上下文压缩常量 ──────────────────────────────────────

# 模型上下文窗口（token 数）
MODEL_CONTEXT_WINDOWS = {
    "deepseek-chat": 128000,
    "deepseek-reasoner": 64000,
    "deepseek-v4-pro": 128000,
    "deepseek-v4-flash": 128000,
    "gpt-4o": 128000,
    "gpt-4o-mini": 128000,
    "gpt-4-turbo": 128000,
    "gpt-3.5-turbo": 16384,
}

# 压缩常量
BUDGET_THRESHOLD = 0.50
SNIP_THRESHOLD = 0.70       # 从 0.60 提高到 0.70 — 少裁剪
BUDGET_TIGHT = 0.80         # 从 0.70 提高到 0.80 — 少截断
AUTOCOMPACT_THRESHOLD = 0.90  # 从 0.85 提高到 0.90 — 晚压缩
MICROCOMPACT_IDLE_S = 10 * 60  # 从 5 分钟提高到 10 分钟

SNIP_PLACEHOLDER = "[已裁剪 - 需要时重新读取]"
KEEP_RECENT_RESULTS = 8     # 从 3 提高到 8 — 保留更多历史


def get_context_window(model: str) -> int:
    """根据模型名获取上下文窗口大小"""
    for key, size in MODEL_CONTEXT_WINDOWS.items():
        if key in model.lower():
            return size
    return 128000  # 默认


def get_effective_window(model: str) -> int:
    return get_context_window(model) - 20000


def get_context_info(model: str, last_input_tokens: int,
                     total_input_tokens: int) -> dict:
    """返回上下文诊断信息"""
    win = get_context_window(model)
    eff = get_effective_window(model)
    util = last_input_tokens / eff if last_input_tokens and eff else 0
    return {
        "model": model,
        "context_window": win,
        "effective_window": eff,
        "last_input_tokens": last_input_tokens,
        "total_input_tokens": total_input_tokens,
        "utilization": util,
    }


# ─── call_api（流式版）───────────────────────────────────

def call_api(messages: list[dict], client: OpenAI, model: str,
             extra_tools: list[dict] | None = None) -> dict:
    """流式调用 API，带指数退避重试"""
    last_error = None
    all_tools = get_active_tools() + (extra_tools or [])

    for attempt in range(4):
        try:
            stream = client.chat.completions.create(
                model=model,
                messages=messages,
                tools=all_tools,
                stream=True,
                stream_options={"include_usage": True},
            )

            content = ""
            tool_calls_by_index: dict[int, dict] = {}
            input_tokens = 0

            with ui.live_response(model=model):
                for chunk in stream:
                    if chunk.usage:
                        input_tokens = chunk.usage.prompt_tokens

                    if not chunk.choices:
                        continue

                    delta = chunk.choices[0].delta

                    if delta and delta.content:
                        ui.print_assistant_text(delta.content)
                        content += delta.content

                    if delta and delta.tool_calls:
                        for tc in delta.tool_calls:
                            existing = tool_calls_by_index.get(tc.index)
                            if existing:
                                if tc.function and tc.function.arguments:
                                    existing["function"]["arguments"] += tc.function.arguments
                            else:
                                tool_calls_by_index[tc.index] = {
                                    "id": tc.id or "",
                                    "type": "function",
                                    "function": {
                                        "name": tc.function.name if tc.function else "",
                                        "arguments": tc.function.arguments or "",
                                    },
                                }

                # 流式结束后、Live 退出前设置 token 信息
                ui.set_live_tokens(input_tokens, get_context_window(model))

            sorted_calls = [
                tool_calls_by_index[i] for i in sorted(tool_calls_by_index.keys())
            ]

            return {
                "role": "assistant",
                "content": content or None,
                "tool_calls": sorted_calls if sorted_calls else None,
                "_input_tokens": input_tokens,
            }

        except Exception as e:
            last_error = e
            if attempt >= 3 or not is_retryable(e):
                raise
            delay = min(2 ** attempt, 30) + random.random()
            ui.append_tool(attempt + 1, 3, str(last_error)[:50])
            time.sleep(delay)

    raise last_error


# ─── 四层压缩管道 ────────────────────────────────────────

def _compression_pipeline(messages: list[dict],
                          last_input_tokens: int,
                          last_api_call_time: float,
                          model: str) -> None:
    """API 调用前执行三层零成本压缩"""
    eff_win = get_effective_window(model)
    utilization = last_input_tokens / eff_win if last_input_tokens else 0

    if utilization >= BUDGET_THRESHOLD:
        budget = 15000 if utilization >= BUDGET_TIGHT else 30000
        for msg in messages:
            if msg.get("role") != "tool":
                continue
            content = msg.get("content", "")
            if isinstance(content, str) and len(content) > budget:
                keep = (budget - 80) // 2
                msg["content"] = (
                    content[:keep]
                    + f"\n\n[... budgeted: {len(content) - keep*2} chars ...]\n\n"
                    + content[-keep:]
                )

    if utilization >= SNIP_THRESHOLD:
        tool_indices = [
            i for i, m in enumerate(messages)
            if m.get("role") == "tool" and m.get("content") != SNIP_PLACEHOLDER
        ]
        if len(tool_indices) > KEEP_RECENT_RESULTS:
            for i in tool_indices[:-KEEP_RECENT_RESULTS]:
                messages[i]["content"] = SNIP_PLACEHOLDER

    if (last_api_call_time
            and (time.time() - last_api_call_time) >= MICROCOMPACT_IDLE_S):
        tool_indices = [
            i for i, m in enumerate(messages)
            if m.get("role") == "tool"
            and m.get("content") not in (SNIP_PLACEHOLDER, "[Old result cleared]")
        ]
        for i in tool_indices[:-KEEP_RECENT_RESULTS]:
            messages[i]["content"] = "[Old result cleared]"


# ─── LLM 摘要压缩 ─────────────────────────────────────────

def _try_compact(messages: list[dict], client: OpenAI, model: str,
                 system_prompt: str | None = None) -> bool:
    """尝试用 LLM 摘要压缩对话历史。成功返回 True，失败返回 False。
    压缩后将 messages[1:] 替换为摘要，保留 system prompt (index 0)。
    """
    ui.print_compact_start()
    try:
        sp = system_prompt or "用一段话总结以下对话，保留关键决策、文件路径。"
        summary_resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": sp},
                *messages[1:-1],
                {"role": "user", "content": "用一段话总结以上对话。"},
            ],
        )
        summary = summary_resp.choices[0].message.content or ""
        messages[1:] = [
            {"role": "user", "content": f"[之前的对话摘要]\n{summary}"},
            {"role": "assistant", "content": "明白了，我已了解之前的上下文。请继续。"},
        ]
        ui.print_compact_done(len(summary))
        return True
    except Exception:
        return False


# ─── Plan Mode 处理 ──────────────────────────────────────

# 模块级状态（plan mode 跨 run() 调用需要持久化）
_plan_file_path: str | None = None
_pre_plan_mode: str = "default"


def _generate_plan_path() -> str:
    d = Path.home() / ".mini-agent" / "plans"
    d.mkdir(parents=True, exist_ok=True)
    ts = str(int(time.time()))
    return str(d / f"plan-{ts}.md")


def _handle_enter_plan(messages: list[dict], model: str) -> str:
    """进入 plan mode：保存当前模式、生成 plan 文件、更新 system prompt"""
    global _pre_plan_mode, _plan_file_path
    _plan_file_path = _generate_plan_path()

    # 更新 system prompt 注入 plan mode 约束（前缀优先，更高注意力权重）
    plan_prompt = f"""
═══════════════════════════════════════════
  PLAN MODE — 只读规划模式
═══════════════════════════════════════════
你现在处于只读规划阶段。你的唯一目标是：先搞清楚代码现状，再设计实现方案。

你可以做的事：
  read_file — 读取代码文件（唯一推荐的操作）
  run_shell — 仅限只读命令（ls, git log, dir 等）

你绝不能做的事（系统会强制拦截）：
  write_file — 只有 {_plan_file_path} 可以写
  run_shell — 任何修改性命令都会被拦截

工作流：
  1. 读代码了解现状（用 read_file）
  2. 思考设计方案
  3. 用 write_file 把计划写入 {_plan_file_path}
  4. 调用 exit_plan_mode 提交审批

重要:
- 不需要写代码！只写计划，用自然语言描述步骤和关键文件路径。
- 完成计划后必须调用 exit_plan_mode，不要问用户"可以吗"。
- 如果系统拦截了你的 write_file，检查目标路径是否等于上面的 plan 文件路径。

"""
    if messages and messages[0]["role"] == "system":
        messages[0]["content"] = plan_prompt + messages[0]["content"]

    ui.print_info(f"进入 Plan Mode。Plan 文件: {_plan_file_path}")
    return f"已进入 Plan Mode。Plan 文件: {_plan_file_path}\n请按工作流探索代码、写计划、然后调用 exit_plan_mode。"


def _handle_exit_plan(messages: list[dict]) -> str:
    """退出 plan mode：读取 plan 文件、展示审批选项"""
    global _plan_file_path

    plan_content = "(未找到 plan 文件)"
    if _plan_file_path and Path(_plan_file_path).exists():
        plan_content = Path(_plan_file_path).read_text(encoding="utf-8")

    # 展示给用户
    ui.console.print()
    ui.console.print("[bold cyan]━━━ Plan 审批 ━━━[/bold cyan]")
    ui.console.print(plan_content[:3000])
    if len(plan_content) > 3000:
        ui.console.print(f"[dim]... (共 {len(plan_content)} 字符，已截断)[/dim]")
    ui.console.print()
    ui.console.print("[bold]选择:[/bold]")
    ui.console.print("  [green]1[/green]. Clear + Execute  清空上下文，执行计划")
    ui.console.print("  [green]2[/green]. Execute           保留上下文，执行计划")
    ui.console.print("  [yellow]3[/yellow]. Manual            退出 plan，手动操作")
    ui.console.print("  [yellow]4[/yellow]. Keep Planning     继续修改计划")

    try:
        choice = input("  > ").strip()
    except EOFError:
        choice = "3"

    # 恢复 system prompt
    if messages and messages[0]["role"] == "system":
        from prompt import build_system_prompt
        messages[0]["content"] = build_system_prompt()

    if choice == "1":  # Clear + Execute
        messages[1:] = [
            {"role": "user", "content": f"执行以下计划:\n\n{plan_content}"},
            {"role": "assistant", "content": "收到，开始执行。"},
        ]
        ui.print_info("清空上下文，开始执行计划")
        return f"审批通过（Clear + Execute）。请执行计划:\n\n{plan_content}"

    elif choice == "2":  # Execute
        ui.print_info("保留上下文，开始执行计划")
        return f"审批通过（Execute）。请执行计划:\n\n{plan_content}"

    elif choice == "4":  # Keep Planning
        # 重新注入 plan prompt
        if messages and messages[0]["role"] == "system":
            plan_prompt = f"""
# Plan Mode 已激活
你是只读规划模式。Plan 文件: {_plan_file_path}
请根据用户反馈修改计划，完成后调用 exit_plan_mode。"""
            messages[0]["content"] += plan_prompt
        feedback = input("  修改意见: ").strip()
        ui.print_info("继续规划中...")
        return f"用户要求继续规划。反馈: {feedback or '请修改计划'}\n修改完计划后调用 exit_plan_mode。"

    else:  # Manual
        ui.print_info("退出 Plan Mode，恢复手动操作")
        return "审批通过（Manual）。退出 Plan Mode，由你自行操作。"


# ─── 子 Agent 执行 ──────────────────────────────────────

def _execute_agent_tool(args: dict, client: OpenAI, model: str,
                        parent_mode: str) -> str:
    """派生子 Agent，执行任务后返回结果"""
    agent_type = args.get("type", "general")
    description = args.get("description", "子任务")
    prompt = args.get("prompt", "")

    config = get_sub_agent_config(agent_type)

    ui.console.print(f"\n  [bold magenta]🤖 子 Agent: {agent_type}[/bold magenta] [dim]— {description}[/dim]")

    # 子 Agent 默认 bypassPermissions（父已授权），plan 模式例外
    sub_mode = "plan" if parent_mode == "plan" else "bypassPermissions"

    # 直接用 call_api 实现简化版子 agent loop
    sub_tools = config["tools"]
    sub_system = config["system_prompt"]
    sub_messages = [
        {"role": "system", "content": sub_system},
        {"role": "user", "content": prompt},
    ]

    for _ in range(10):  # 最多 10 轮
        resp = _sub_call(client, model, sub_tools, sub_messages)
        sub_messages.append(resp)

        tool_calls = resp.get("tool_calls")
        if not tool_calls:
            ui.console.print(f"  [dim]✓ 子 Agent 完成 ({len(sub_messages)} 条消息)[/dim]")
            return resp.get("content") or "(子 Agent 无输出)"

        for tc in tool_calls:
            fn = tc["function"]
            t_name = fn["name"]
            t_args = json.loads(fn["arguments"])
            t_result = execute_tool(t_name, t_args)
            sub_messages.append({
                "role": "tool", "tool_call_id": tc["id"],
                "content": t_result,
            })

    return "(子 Agent 达到最大轮次)"


def _sub_call(client: OpenAI, model: str, tools: list[dict],
              messages: list[dict]) -> dict:
    """子 Agent 专用 API 调用（非流式，收集结果）"""
    resp = client.chat.completions.create(
        model=model,
        messages=messages,
        tools=tools,
    )
    msg = resp.choices[0].message
    return {
        "role": "assistant",
        "content": msg.content,
        "tool_calls": [
            {
                "id": tc.id, "type": "function",
                "function": {"name": tc.function.name, "arguments": tc.function.arguments},
            }
            for tc in (msg.tool_calls or [])
        ] if msg.tool_calls else None,
    }


# ─── 技能执行 ────────────────────────────────────────────

def _execute_skill_tool(skill_name: str, args: str, client=None, model="") -> str:
    """调用技能：解析 prompt 模板，展开为指令。
    Fork 模式：创建独立子 Agent 执行，只返回最终结果。"""
    result = _skills_mod.execute_skill(skill_name, args)
    if not result:
        _skills_mod.record_skill_usage(skill_name, False)
        return f"未知技能: {skill_name}。用 /skills 查看可用技能列表。"

    _skills_mod.record_skill_usage(skill_name, True)

    if result["context"] == "fork":
        ui.console.print(f"\n  [bold blue]🔀 Skill fork: {skill_name}[/bold blue]")
        allowed = set(result["allowed_tools"]) if result["allowed_tools"] else None
        if allowed:
            sub_tools = [t for t in TOOLS if t["function"]["name"] in allowed]
        else:
            sub_tools = [t for t in TOOLS if t["function"]["name"] != "agent"]

        if client is None:
            return f'[技能 "{skill_name}" 已激活 (fork - 无独立上下文)]\n\n{result["prompt"]}'

        sub_messages = [
            {"role": "system", "content": result["prompt"]},
            {"role": "user", "content": args or "请执行此技能的任务。"},
        ]

        for _ in range(10):
            resp = _sub_call(client, model, sub_tools, sub_messages)
            sub_messages.append(resp)
            tcs = resp.get("tool_calls")
            if not tcs:
                ui.console.print("  [dim]✓ Fork skill 完成[/dim]")
                return resp.get("content") or "(Fork skill 无输出)"
            for tc in tcs:
                fn = tc["function"]
                t_args = json.loads(fn["arguments"])
                t_result = execute_tool(fn["name"], t_args)
                sub_messages.append({"role": "tool", "tool_call_id": tc["id"], "content": t_result})

        return "(Fork skill 达到最大轮次)"

    # Inline 模式：展开 prompt，注入对话
    return f'[技能 "{skill_name}" 已激活]\n\n{result["prompt"]}'


# ─── Agent 主循环 ────────────────────────────────────────

# 会话级权限白名单
_session_allowlist: set[str] = set()

def run(user_message: str, permission_mode: str = "default",
        *, api_config: dict | None = None,
        messages: list[dict] | None = None,
        enable_mcp: bool = False,
        stats: dict | None = None) -> tuple[list[dict], dict]:
    """
    运行 agent。传入 messages 可延续之前的对话历史。
    enable_mcp=False 禁用 MCP 外部工具加载。
    返回 (更新后的 messages, 累计统计信息)。
    """
    cfg = api_config or resolve_api_config()

    if not cfg["api_key"]:
        ui.print_error("未设置 API Key。请通过 --api-key 参数或环境变量传入。")
        return messages or [], stats or {}

    # 初始化统计
    if stats is None:
        stats = {"total_input": 0, "total_output": 0, "turns": 0, "model": cfg["model"], "session_start": time.time()}

    client = OpenAI(api_key=cfg["api_key"], base_url=cfg["base_url"])
    model = cfg["model"]

    # ── MCP: 懒加载外部工具 ──
    mcp_manager = None
    if enable_mcp:
        try:
            from mcp import get_mcp_manager
            mcp_manager = get_mcp_manager()
        except Exception:
            pass

    # 如果传入了历史，延续对话；否则创建新对话
    if messages is not None:
        messages.append({"role": "user", "content": user_message})
    else:
        messages = [
            {"role": "system", "content": build_system_prompt()},
            {"role": "user", "content": user_message},
        ]

        # 仅新会话时注入 plan mode 提示词
        if permission_mode == "plan":
            _handle_enter_plan(messages, model)
        _session_allowlist.clear()

    # ── sideQuery: 复用同一模型做记忆召回 ──
    def _side_query(system: str, user_msg: str) -> str:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_msg},
            ],
        )
        return resp.choices[0].message.content or ""

    # ── 记忆系统状态（新会话时初始化）──
    if messages is None or len(messages) <= 2:
        already_surfaced: set[str] = set()
        session_memory_bytes = 0
    else:
        already_surfaced = getattr(run, "_already_surfaced", set())
        session_memory_bytes = getattr(run, "_session_memory_bytes", 0)

    # 启动异步记忆预取（非阻塞，与第一轮 API 调用并行）
    memory_prefetch = start_memory_prefetch(
        user_message, _side_query, already_surfaced, session_memory_bytes
    )

    last_input_tokens = 0
    last_api_call_time = 0.0
    turn_start = time.time()

    while True:
        # 消费记忆预取结果（非阻塞轮询）
        if (memory_prefetch and memory_prefetch.settled
                and not memory_prefetch.consumed):
            memory_prefetch.consumed = True
            try:
                memories = memory_prefetch.get_result()
                if memories:
                    injection = format_memories_for_injection(memories)
                    last = messages[-1] if messages else None
                    if last and last.get("role") == "user":
                        last["content"] = str(last.get("content", "")) + "\n\n" + injection
                    else:
                        messages.append({"role": "user", "content": injection})
                    for m in memories:
                        already_surfaced.add(m.path)
                        session_memory_bytes += len(m.content.encode())
            except Exception:
                pass

        _compression_pipeline(messages, last_input_tokens, last_api_call_time, model)

        # 预检：估算消息 token 数，超 95% 强制压缩
        est_tokens = sum(len(json.dumps(m, ensure_ascii=False)) for m in messages) // 3
        eff_win = get_effective_window(model)
        if est_tokens > eff_win * 0.95 and len(messages) > 6:
            if _try_compact(messages, client, model):
                last_input_tokens = 0
                continue

        print()
        assistant_msg = call_api(messages, client, model)
        input_tokens = assistant_msg.pop("_input_tokens", 0)
        messages.append(assistant_msg)

        last_input_tokens = input_tokens
        stats["total_input"] += input_tokens
        stats["last_input"] = input_tokens
        stats["turns"] += 1
        last_api_call_time = time.time()

        util = input_tokens / get_effective_window(model) if input_tokens else 0

        if util >= AUTOCOMPACT_THRESHOLD and len(messages) > 6:
            if _try_compact(
                messages, client, model,
                system_prompt="你是一个对话摘要器。用一段话总结以下对话，保留关键决策、文件路径和上下文。",
            ):
                continue

        tool_calls = assistant_msg.get("tool_calls")
        if not tool_calls:
            eff = get_effective_window(model)
            util = last_input_tokens / eff if last_input_tokens and eff else 0
            bar = "█" * int(10 * min(util, 1)) + "░" * max(0, 10 - int(10 * min(util, 1)))
            elapsed = time.time() - turn_start
            session = time.time() - stats.get("session_start", time.time())
            return messages, stats

        for tc in tool_calls:
            fn = tc["function"]
            name = fn["name"]
            args = json.loads(fn["arguments"])

            # ── Plan Mode: enter_plan_mode / exit_plan_mode ──
            if name == "enter_plan_mode":
                messages.append({
                    "role": "tool", "tool_call_id": tc["id"],
                    "content": _handle_enter_plan(messages, model),
                })
                break  # 状态已变，重新调 API
            if name == "exit_plan_mode":
                _handle_exit_plan(messages)
                # 审批流程已在 _handle_exit_plan 中完成（可能清空或保留 messages），
                # 不需要追加 tool result，直接下一轮让模型基于更新后的 messages 继续
                continue

            # ── skill 工具：调用技能 ──
            if name == "skill":
                result = _execute_skill_tool(args.get("skill_name", ""),
                                              args.get("args", ""),
                                              client=client, model=model)
                ui.print_tool_call("skill", {
                    "name": args.get("skill_name", ""),
                    "args": args.get("args", ""),
                })
                messages.append({
                    "role": "tool", "tool_call_id": tc["id"],
                    "content": result,
                })
                ui.print_tool_result(result)
                continue

            # ── agent 工具：派生子 Agent ──
            if name == "agent":
                result = _execute_agent_tool(
                    args, client, model, permission_mode
                )
                ui.print_tool_call("agent", {
                    "type": args.get("type", "general"),
                    "desc": args.get("description", ""),
                })
                messages.append({
                    "role": "tool", "tool_call_id": tc["id"],
                    "content": result,
                })
                ui.print_tool_result(result)
                continue

            # ── MCP 工具：路由到外部服务器 ──
            if name.startswith("mcp__") and mcp_manager:
                ui.print_tool_call(name, args)
                result = mcp_manager.call_tool(name, args)
                messages.append({
                    "role": "tool", "tool_call_id": tc["id"],
                    "content": result,
                })
                ui.print_tool_result(result)
                continue

            perm = check_permission(name, args, permission_mode, _plan_file_path)
            if perm["action"] == "deny":
                result = f"❌ {perm['message']}"
                ui.print_permission_deny(name, perm["message"])
            elif perm["action"] == "confirm":
                confirm_key = f"{name}:{perm.get('message', '')}"
                if confirm_key in _session_allowlist:
                    result = execute_tool(name, args)
                    ui.print_tool_call(name, args)
                    ui.print_tool_result(result)
                else:
                    ui.print_permission_confirm(perm["message"])
                    ui.console.print(
                        "  [dim]y=允许一次  a=本次对话始终允许  n=拒绝[/dim]"
                    )
                    try:
                        answer = input("  > ").strip().lower()
                    except EOFError:
                        answer = "n"
                    if answer == "a":
                        _session_allowlist.add(confirm_key)
                        ui.print_info("已加入会话白名单")
                        result = execute_tool(name, args)
                    elif answer.startswith("y"):
                        result = execute_tool(name, args)
                    else:
                        result = "❌ 用户拒绝了此操作"
                    ui.print_tool_call(name, args)
                ui.print_tool_result(result)
            else:
                result = execute_tool(name, args)
                ui.print_tool_call(name, args)
                ui.print_tool_result(result)

            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": result,
            })
