"""CLI 入口 — 参数解析 + 交互 REPL，支持多 API provider"""

import json
import os
import sys
import urllib.request
import urllib.error
import ui
from agent import run, resolve_api_config, save_api_config

# ─── Tab 补全 ─────────────────────────────────────────────

COMMANDS = ["/quit", "/clear", "/plan", "/model", "/config",
            "/memory", "/skills", "/cd", "/context", "/mcp", "/help"]

try:
    import readline
    def _completer(text, state):
        if text.startswith("/"):
            matches = [c for c in COMMANDS if c.startswith(text)]
            try:
                from skills import discover_skills
                for s in discover_skills():
                    name = "/" + s.name
                    if name.startswith(text) and name not in matches:
                        matches.append(name)
            except Exception:
                pass
            return matches[state] if state < len(matches) else None
        return None
    readline.set_completer(_completer)
    readline.parse_and_bind("tab: complete")
except ImportError:
    pass


# ─── 模型列表查询 ────────────────────────────────────────

def fetch_models(base_url: str, api_key: str) -> list[str]:
    """从 OpenAI 兼容 API 获取可用模型列表"""
    url = base_url.rstrip("/") + "/models"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            models = [m["id"] for m in data.get("data", [])]
            # 过滤掉非聊天模型
            chat_keywords = ("gpt", "claude", "deepseek", "chat", "llama",
                             "qwen", "gemini", "command", "mistral", "mixtral")
            chat_models = [m for m in models if any(k in m.lower()
                          for k in chat_keywords)]
            return chat_models if chat_models else models[:30]
    except Exception as e:
        print(f"  ⚠ 获取模型列表失败: {e}")
        return []


def model_picker(base_url: str, api_key: str, current_model: str,
                 keyword: str = "") -> dict | None:
    """
    交互式模型选择器。支持关键词过滤和序号/名称选择。
    """
    all_models = fetch_models(base_url, api_key)

    # 关键词过滤
    if keyword and all_models:
        filtered = [m for m in all_models if keyword.lower() in m.lower()]
        if filtered:
            models = filtered
        else:
            ui.print_info(f"没有包含 '{keyword}' 的模型，显示全部")
            models = all_models
    else:
        models = all_models

    ui.print_config({"base_url": base_url, "api_key": api_key, "model": current_model})

    if models:
        ui.print_model_list(models, current_model, keyword)
    else:
        ui.print_info("无法获取模型列表，直接输入模型名，回车取消")
        models = []
    print()
    try:
        choice = input("  > ").strip()
    except EOFError:
        return None

    if not choice:
        return None

    # 尝试按序号选择
    try:
        idx = int(choice)
        if 1 <= idx <= len(models):
            return {"model": models[idx - 1]}
        else:
            print(f"  ⚠ 序号超出范围 (1-{len(models)})")
            return None
    except ValueError:
        pass

    # 非数字：当作模型名或过滤关键词
    # 如果输入匹配某个模型名（大小写不敏感），直接选它
    if models:
        for m in models:
            if choice.lower() == m.lower():
                return {"model": m}

    # 否则当作过滤关键词，递归显示过滤结果
    return model_picker(base_url, api_key, current_model, keyword=choice)


# ─── 主入口 ──────────────────────────────────────────────

def main():
    args = sys.argv[1:]

    # ── 权限模式 ──
    permission_mode = "default"
    for flag in args:
        if flag in ("--yolo", "-y"):
            permission_mode = "bypassPermissions"
        elif flag == "--plan":
            permission_mode = "plan"
        elif flag == "--accept-edits":
            permission_mode = "acceptEdits"
        elif flag == "--dont-ask":
            permission_mode = "dontAsk"

    # ── API 配置 ──
    provider = "deepseek"
    api_key = None
    api_base = None
    model = None
    workdir = None
    enable_mcp = False   # 默认不连接 MCP，用 --mcp 或 /mcp 手动开启

    i = 0
    while i < len(args):
        if args[i] == "--no-mcp":
            enable_mcp = False
            i += 1
        elif args[i] == "--provider" and i + 1 < len(args):
            provider = args[i + 1]
            i += 2
        elif args[i] == "--api-key" and i + 1 < len(args):
            api_key = args[i + 1]
            i += 2
        elif args[i] == "--api-base" and i + 1 < len(args):
            api_base = args[i + 1]
            i += 2
        elif args[i] == "--model" and i + 1 < len(args):
            model = args[i + 1]
            i += 2
        elif args[i] == "--cwd" and i + 1 < len(args):
            workdir = args[i + 1]
            i += 2
        else:
            i += 1

    # 切换工作目录
    if workdir:
        os.chdir(workdir)

    api_config = resolve_api_config(
        provider=provider, api_key=api_key,
        api_base=api_base, model=model,
    )

    # ── 一次性模式检查 ──
    skip_next = False
    clean_args = []
    for a in args:
        if skip_next:
            skip_next = False
            continue
        if a in ("--provider", "--model", "--api-key", "--api-base"):
            skip_next = True
            continue
        if not a.startswith("-"):
            clean_args.append(a)
    if clean_args:
        prompt = " ".join(clean_args)
        print(f"Provider: {provider} | Model: {api_config['model']}")
        print(f"API: {api_config['base_url']} | Mode: {permission_mode}")
        run(prompt, permission_mode, api_config=api_config, enable_mcp=enable_mcp)
        sys.exit(0)

    # ── 交互 REPL ──
    ui.print_welcome(api_config["base_url"], api_config["model"], permission_mode)

    plan_mode = (permission_mode == "plan")
    chat_messages = None  # None = 新会话，之后复用
    chat_stats = None     # 跨轮累计 token 统计
    while True:
        try:
            user_input = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye!")
            break
        if not user_input:
            continue
        if user_input == "/quit":
            break

        if user_input in ("/help", "/h", "/?"):
            print("""
  /quit        退出
  /clear       清空对话历史
  /plan        切换只读规划模式
  /model [关键词] 选择模型（可选关键词过滤）
  /config      手动设置 API 配置
  /memory      查看记忆列表
  /skills      查看技能列表
  /cd <路径>    切换工作目录
  /context     查看上下文窗口用量
  /mcp         开关 MCP 外部工具
  /<技能名>    调用技能（如 /commit fix bug）
  /help        显示此帮助
""")
            continue

        if user_input == "/clear":
            chat_messages = None
            chat_stats = None
            ui.print_info("对话历史已清空")
            continue

        if user_input == "/plan":
            plan_mode = not plan_mode
            current = "plan" if plan_mode else permission_mode
            print(f"  模式切换 → {current}")
            continue

        if user_input == "/mcp":
            enable_mcp = not enable_mcp
            chat_messages = None  # 切换后重置对话
            chat_stats = None
            ui.print_info(f"MCP {'启用' if enable_mcp else '禁用'}（对话已重置）")
            continue

        # ── /cd: 切换工作目录 ──
        if user_input.startswith("/cd"):
            path = user_input[3:].strip()
            if not path:
                ui.print_info("用法: /cd <目录路径>")
                continue
            try:
                os.chdir(path)
                ui.print_welcome(
                    api_config["base_url"], api_config["model"],
                    "plan" if plan_mode else permission_mode
                )
            except Exception as e:
                ui.print_error(f"切换失败: {e}")
            continue

        # ── /model: 交互式切换模型（支持 /model <关键词> 过滤）──
        if user_input.startswith("/model"):
            keyword = user_input[6:].strip()  # "/model chat" → "chat"
            result = model_picker(
                api_config["base_url"],
                api_config["api_key"],
                api_config["model"],
                keyword=keyword,
            )
            if result:
                api_config["model"] = result["model"]
                save_api_config(api_config)
                ui.print_info(f"模型切换 → {api_config['model']}（已保存）")
            continue

        # ── /memory: 列出所有记忆 ──
        if user_input == "/memory":
            from memory import list_memories
            memories = list_memories()
            if not memories:
                ui.print_info("暂无保存的记忆。")
                ui.print_info(
                    f"存储位置: {__import__('memory').get_memory_dir()}"
                )
            else:
                ui.print_info(f"{len(memories)} 条记忆:")
                for m in memories:
                    print(f"    [{m['type']}] {m['name']} — {m['description']}")
            continue

        # ── /context: 上下文诊断 ──
        if user_input == "/context":
            ui.print_info("上下文诊断信息（下一轮对话后可用）")
            ui.print_info(f"模型: {api_config['model']}")
            win = __import__('agent').get_context_window(api_config['model'])
            ui.print_info(f"模型: {api_config['model']} | 窗口: {win} tokens")
            ui.print_info(f"阈值: Snip 70% | Budget 80% | Compact 90% | Keep 8 条")
            if chat_stats:
                ui.print_info(f"累计输入: {chat_stats['total_input']:,} tokens | 轮次: {chat_stats['turns']}")
                util = chat_stats.get('last_input', 0) / (win - 20000) * 100 if chat_stats.get('last_input') else 0
                ui.print_info(f"上次输入: {chat_stats.get('last_input', 0):,} tokens ({util:.0f}%)")
            continue

        # ── /skills: 列出技能 ──
        if user_input == "/skills":
            from skills import discover_skills
            skills = discover_skills()
            if not skills:
                ui.print_info("暂无技能。在 .mini-agent/skills/<name>/SKILL.md 创建。")
            else:
                ui.print_info(f"{len(skills)} 个技能:")
                for s in skills:
                    tag = "🔀" if s.context == "fork" else "📝"
                    print(f"    {tag} /{s.name} — {s.description}")
            continue

        # ── /<skill-name>: 调用技能 ──
        if user_input.startswith("/"):
            from skills import get_skill_by_name, resolve_skill_prompt
            space_idx = user_input.find(" ")
            cmd_name = user_input[1:space_idx] if space_idx > 0 else user_input[1:]
            cmd_args = user_input[space_idx + 1:] if space_idx > 0 else ""
            skill = get_skill_by_name(cmd_name)
            if skill and skill.user_invocable:
                resolved = resolve_skill_prompt(skill, cmd_args)
                ui.print_info(f"调用技能: {skill.name}")
                chat_messages, chat_stats = run(resolved, "plan" if plan_mode else permission_mode,
                    api_config=api_config, messages=chat_messages, enable_mcp=enable_mcp, stats=chat_stats)
                print()
                continue

        # ── /config: 手动设置 API 配置 ──
        if user_input == "/config":
            print(f"\n  当前配置:")
            print(f"  base_url: {api_config['base_url']}")
            print(f"  api_key:  {api_config['api_key'][:15]}...")
            print(f"  model:    {api_config['model']}")
            print()

            try:
                new_base = input("  base_url (回车跳过): ").strip()
            except EOFError:
                new_base = ""
            try:
                new_key = input("  api_key (回车跳过): ").strip()
            except EOFError:
                new_key = ""
            try:
                new_model = input("  model (回车跳过): ").strip()
            except EOFError:
                new_model = ""

            if new_base:
                api_config["base_url"] = new_base
            if new_key:
                api_config["api_key"] = new_key
            if new_model:
                api_config["model"] = new_model

            if new_base or new_key or new_model:
                save_api_config(api_config)
                ui.print_info("配置已更新（已保存）")
            continue

        chat_messages, chat_stats = run(user_input, "plan" if plan_mode else permission_mode,
            api_config=api_config, messages=chat_messages, enable_mcp=enable_mcp, stats=chat_stats)
        print()


if __name__ == "__main__":
    main()
