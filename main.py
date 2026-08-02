"""CLI 入口 — 参数解析 + 交互 REPL，支持多 API provider"""

import argparse
import json
import os
import sys
import uuid
import urllib.request
import urllib.error
from pathlib import Path
import ui
from agent import run, resolve_api_config, save_api_config, get_context_window
from memory import list_memories, get_memory_dir

# ─── Tab 补全 ─────────────────────────────────────────────

COMMANDS = ["/quit", "/clear", "/plan", "/model", "/config",
            "/memory", "/skills", "/cd", "/context", "/mcp", "/help"]

_cached_skill_names: list[str] | None = None

def _refresh_skill_names() -> list[str]:
    """获取技能名列表（带缓存，调用 reset_skill_names_cache 刷新）"""
    global _cached_skill_names
    if _cached_skill_names is None:
        try:
            from skills import discover_skills
            _cached_skill_names = ["/" + s.name for s in discover_skills()]
        except Exception:
            _cached_skill_names = []
    return _cached_skill_names


def reset_skill_names_cache() -> None:
    """使技能名缓存失效（切换项目或手动刷新技能时调用）"""
    global _cached_skill_names
    _cached_skill_names = None
    # 同时刷新 skills 模块内部缓存
    try:
        from skills import reset_skill_cache
        reset_skill_cache()
    except Exception:
        pass


try:
    import readline
    def _completer(text, state):
        if text.startswith("/"):
            matches = [c for c in COMMANDS if c.startswith(text)]
            for name in _refresh_skill_names():
                if name.startswith(text) and name not in matches:
                    matches.append(name)
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
    使用迭代循环而非递归，避免深层调用栈溢出。
    """
    all_models = fetch_models(base_url, api_key)

    while True:
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

        # 否则当作过滤关键词，继续循环
        keyword = choice


# ─── 参数解析 ──────────────────────────────────────────

def parse_args(argv: list[str]) -> argparse.Namespace:
    """使用 argparse 解析 CLI 参数，替代原先脆弱的手动 while 循环"""
    parser = argparse.ArgumentParser(
        description="Mini Agent CLI — 多 API provider 的交互式编程助手",
        allow_abbrev=False,
    )
    # 权限模式（互斥组，最后一个生效）
    perm = parser.add_mutually_exclusive_group()
    perm.add_argument("--yolo", "-y", action="store_const", dest="permission_mode",
                      const="bypassPermissions", help="绕过所有权限检查")
    perm.add_argument("--plan", action="store_const", dest="permission_mode",
                      const="plan", help="只读规划模式")
    perm.add_argument("--accept-edits", action="store_const", dest="permission_mode",
                      const="acceptEdits", help="自动接受编辑操作")
    perm.add_argument("--dont-ask", action="store_const", dest="permission_mode",
                      const="dontAsk", help="拒绝危险操作而不询问")

    # API 配置
    parser.add_argument("--provider", default="deepseek",
                        help="API provider (deepseek/openai/openrouter)")
    parser.add_argument("--api-key", default=None, help="API Key")
    parser.add_argument("--api-base", default=None, help="API Base URL")
    parser.add_argument("--model", default=None, help="模型名")
    parser.add_argument("--cwd", default=None, help="切换工作目录")
    parser.add_argument("--no-mcp", action="store_true", default=False,
                        help="禁用 MCP 外部工具（默认已禁用）")
    parser.add_argument("--dangerously-bypass-sandbox", action="store_true",
                        default=False, help="绕过文件系统沙箱（允许写任意路径）")
    parser.add_argument("--resume", default=None,
                        help="恢复指定会话 ID")

    # 位置参数：一次性 prompt（多个单词自动拼接）
    parser.add_argument("prompt", nargs="*", help="一次性提问（多个参数自动拼接）")

    return parser.parse_args(argv)


# ─── 命令分发 ──────────────────────────────────────────

# 返回值语义： "quit" = 退出 REPL, "handled" = 命令已处理继续循环,
#             None = 不是命令，作为普通消息传给 agent
QUIT = "quit"
HANDLED = "handled"


def _dispatch_command(user_input: str, state: dict) -> str | None:
    """
    分发 REPL 命令到对应处理逻辑。
    state 是一个可变字典，包含：api_config, permission_mode, plan_mode,
    enable_mcp, chat_messages, chat_stats。
    """
    # 提取命令名和参数
    space_idx = user_input.find(" ")
    cmd = user_input[:space_idx] if space_idx > 0 else user_input
    arg = user_input[space_idx + 1:] if space_idx > 0 else ""

    # ── /quit ──
    if cmd in ("/quit", "/q"):
        return QUIT

    # ── /help ──
    if cmd in ("/help", "/h", "/?"):
        print("""
  /quit        退出
  /clear       清空对话历史
  /plan        切换只读规划模式
  /model [关键词] 选择模型（可选关键词过滤）
  /config      手动设置 API 配置
  /save <名称>  保存当前对话
  /load <名称>  加载历史对话
  /sessions    列出已保存的对话
  /memory      查看记忆列表
  /skills      查看技能列表
  /skill-install <URL>  安装社区技能
  /cd <路径>    切换工作目录
  /rag <查询>    搜索相关代码
  /context     查看上下文窗口用量
  /mcp         开关 MCP 外部工具
  /<技能名>    调用技能（如 /commit fix bug）
  /help        显示此帮助
""")
        return HANDLED

    # ── /clear ──
    if cmd == "/clear":
        state["chat_messages"] = None
        state["chat_stats"] = None
        state["session_id"] = str(uuid.uuid4())
        try:
            from session import delete_session
            delete_session("__auto__")
        except Exception:
            pass
        ui.print_info("对话历史已清空（新会话 ID）")
        return HANDLED

    # ── /plan ──
    if cmd == "/plan":
        state["plan_mode"] = not state["plan_mode"]
        current = "plan" if state["plan_mode"] else state["permission_mode"]
        print(f"  模式切换 → {current}")
        return HANDLED

    # ── /mcp ──
    if cmd == "/mcp":
        state["enable_mcp"] = not state["enable_mcp"]
        state["chat_messages"] = None
        state["chat_stats"] = None
        ui.print_info(f"MCP {'启用' if state['enable_mcp'] else '禁用'}（对话已重置）")
        return HANDLED

    # ── /cd ──
    if cmd == "/cd":
        if not arg:
            ui.print_info("用法: /cd <目录路径>")
            return HANDLED
        try:
            os.chdir(arg)
            ui.print_welcome(
                state["api_config"]["base_url"],
                state["api_config"]["model"],
                "plan" if state["plan_mode"] else state["permission_mode"],
            )
        except Exception as e:
            ui.print_error(f"切换失败: {e}")
        return HANDLED

    # ── /save ──
    if cmd == "/save":
        if not arg:
            ui.print_info("用法: /save <会话名>")
            return HANDLED
        if not state["chat_messages"]:
            ui.print_info("当前没有活跃的对话可保存。")
            return HANDLED
        try:
            from session import save_session
            sid = save_session(
                name=arg,
                messages=state["chat_messages"],
                stats=state["chat_stats"],
                model=state["api_config"]["model"],
            )
            ui.print_info(f"会话已保存 (id={sid})")
        except Exception as e:
            ui.print_error(f"保存失败: {e}")
        return HANDLED

    # ── /load ──
    if cmd == "/load":
        if not arg:
            ui.print_info("用法: /load <会话名或ID>")
            return HANDLED
        try:
            from session import load_session
            # 支持按 ID 加载
            try:
                lookup = int(arg)
            except ValueError:
                lookup = arg
            session = load_session(lookup)
            if not session:
                ui.print_error(f"未找到会话: {arg}")
                return HANDLED
            state["chat_messages"] = session["messages"]
            state["chat_stats"] = session.get("stats")
            # 如果会话保存了 model，可选择性恢复
            if session.get("model") and session["model"] != state["api_config"]["model"]:
                ui.print_info(f"提示: 会话使用的模型是 {session['model']}，当前是 {state['api_config']['model']}")
            msg_count = len(session.get("messages", []))
            ui.print_info(f"已加载会话 '{session['name']}' ({msg_count} 条消息)")
        except Exception as e:
            ui.print_error(f"加载失败: {e}")
        return HANDLED

    # ── /sessions ──
    if cmd == "/sessions":
        try:
            from session import list_sessions
            sessions = list_sessions()
            if not sessions:
                ui.print_info("暂无保存的会话。使用 /save <名称> 保存。")
            else:
                ui.print_info(f"{len(sessions)} 个会话:")
                import datetime
                for s in sessions:
                    t = datetime.datetime.fromtimestamp(
                        s["saved_at"]
                    ).strftime("%m-%d %H:%M") if s.get("saved_at") else "未知"
                    print(
                        f"    [id={s['id']}] {s['name']}  "
                        f"[dim]({s['message_count']} 条消息, {t})[/dim]"
                    )
        except Exception as e:
            ui.print_error(f"查询失败: {e}")
        return HANDLED

    # ── /skill-install ──
    if cmd == "/skill-install":
        if not arg:
            ui.print_info("Usage: /skill-install <URL or gh:user/repo>")
            ui.print_info("Example: /skill-install gh:someone/skill-name")
            return HANDLED
        try:
            url = arg
            if arg.startswith("gh:"):
                url = "https://raw.githubusercontent.com/" + arg[3:] + "/main/SKILL.md"
            elif not arg.startswith("http"):
                url = "https://raw.githubusercontent.com/" + arg + "/main/SKILL.md"
            req = urllib.request.Request(url, headers={"User-Agent": "mini-agent"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read().decode("utf-8")
            from memory import _parse_frontmatter
            meta, _ = _parse_frontmatter(raw)
            name = meta.get("name") or url.rstrip("/").split("/")[-1].replace(".md", "")
            if not name:
                ui.print_error("cannot determine skill name")
                return HANDLED
            d = Path.cwd() / ".mini-agent" / "skills" / name
            d.mkdir(parents=True, exist_ok=True)
            (d / "SKILL.md").write_text(raw, encoding="utf-8")
            from skills import reset_skill_cache
            reset_skill_cache()
            ui.print_info("Skill installed: /" + name)
        except urllib.error.HTTPError as e:
            ui.print_error("HTTP " + str(e.code))
        except Exception as e:
            ui.print_error("install failed: " + str(e))
        return HANDLED


    # ── /rag ──
    if cmd == "/rag":
        if arg == "--rebuild":
            try:
                from rag import get_index
                n = get_index(force_rebuild=True).chunks if hasattr(get_index(force_rebuild=True), '__iter__') else get_index(force_rebuild=True)
                ui.print_info("RAG 索引已重建")
            except Exception as e:
                ui.print_error(f"重建失败: {e}")
            return HANDLED
        if not arg:
            ui.print_info("用法: /rag <查询> | /rag --rebuild")
            return HANDLED
        try:
            from rag import search
            results = search(arg, top_k=5)
            if not results:
                ui.print_info("未找到相关代码")
            else:
                ui.print_info(f"找到 {len(results)} 个代码块:")
                for r in results:
                    preview = r['text'][:120].replace('\n', ' ')
                    print(f"  {r['file']}:{r['start']}-{r['end']} ({r['score']})")
                    print(f"  [dim]{preview}...[/dim]\n")
        except Exception as e:
            ui.print_error(f"RAG 失败: {e}")
        return HANDLED

    # ── /harness ──
    if cmd == "/harness":
        try:
            from harness import metrics
            s = metrics.summary()
            ui.print_info("工具执行指标:")
            for line in s.split("\n"):
                print(f"  {line}")
        except Exception as e:
            ui.print_error(f"查询失败: {e}")
        return HANDLED

    # ── /model ──
    if cmd == "/model":
        result = model_picker(
            state["api_config"]["base_url"],
            state["api_config"]["api_key"],
            state["api_config"]["model"],
            keyword=arg,
        )
        if result:
            state["api_config"]["model"] = result["model"]
            save_api_config(state["api_config"])
            ui.print_info(f"模型切换 → {state['api_config']['model']}（已保存）")
        return HANDLED

    # ── /memory ──
    if cmd == "/memory":
        memories = list_memories()
        if not memories:
            ui.print_info("暂无保存的记忆。")
            ui.print_info(f"存储位置: {get_memory_dir()}")
        else:
            ui.print_info(f"{len(memories)} 条记忆:")
            for m in memories:
                print(f"    [{m['type']}] {m['name']} — {m['description']}")
        return HANDLED

    # ── /context ──
    if cmd == "/context":
        ui.print_info("上下文诊断信息（下一轮对话后可用）")
        win = get_context_window(state["api_config"]["model"])
        ui.print_info(f"模型: {state['api_config']['model']} | 窗口: {win} tokens")
        ui.print_info("阈值: Snip 70% | Budget 80% | Compact 90% | Keep 8 条")
        chat_stats = state["chat_stats"]
        if chat_stats:
            ui.print_info(
                f"累计输入: {chat_stats['total_input']:,} tokens "
                f"| 轮次: {chat_stats['turns']}"
            )
            eff = win - 20000
            last_input = chat_stats.get("last_input", 0)
            util = last_input / eff * 100 if last_input and eff else 0
            ui.print_info(f"上次输入: {last_input:,} tokens ({util:.0f}%)")
        return HANDLED

    # ── /skills ──
    if cmd == "/skills":
        reset_skill_names_cache()  # 刷新缓存，保证展示最新
        from skills import discover_skills
        skills = discover_skills()
        if not skills:
            ui.print_info("暂无技能。在 .mini-agent/skills/<name>/SKILL.md 创建。")
        else:
            ui.print_info(f"{len(skills)} 个技能:")
            for s in skills:
                tag = "🔀" if s.context == "fork" else "📝"
                print(f"    {tag} /{s.name} — {s.description}")
        return HANDLED

    # ── /config ──
    if cmd == "/config":
        api_config = state["api_config"]
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
        return HANDLED

    # ── /<skill-name>: 动态技能调用 ──
    if cmd.startswith("/"):
        from skills import get_skill_by_name, resolve_skill_prompt
        skill_name = cmd[1:]  # 去掉 "/"
        skill = get_skill_by_name(skill_name)
        if skill and skill.user_invocable:
            resolved = resolve_skill_prompt(skill, arg)
            ui.print_info(f"调用技能: {skill.name}")
            effective_mode = "plan" if state["plan_mode"] else state["permission_mode"]
            state["chat_messages"], state["chat_stats"] = run(
                resolved, effective_mode,
                api_config=state["api_config"],
                messages=state["chat_messages"],
                enable_mcp=state["enable_mcp"],
                stats=state["chat_stats"],
            )
            print()
            return HANDLED

    return None  # 不是命令，作为普通消息传给 agent


# ─── 主入口 ──────────────────────────────────────────────

def main():
    args = parse_args(sys.argv[1:])

    # 权限模式
    permission_mode = args.permission_mode or "default"

    # 工作目录切换
    if args.cwd:
        os.chdir(args.cwd)

    api_config = resolve_api_config(
        provider=args.provider,
        api_key=args.api_key,
        api_base=args.api_base,
        model=args.model,
    )

    # 沙箱绕过
    if args.dangerously_bypass_sandbox:
        from tools import enable_sandbox_bypass
        enable_sandbox_bypass()
        print("⚠ 沙箱已绕过 — 允许写任意路径")

    # MCP 默认关闭，通过 REPL 中 /mcp 手动开启
    enable_mcp = False

    # ── 一次性模式（提供了位置参数 prompt）──
    if args.prompt:
        prompt = " ".join(args.prompt)
        print(f"Provider: {args.provider} | Model: {api_config['model']}")
        print(f"API: {api_config['base_url']} | Mode: {permission_mode}")
        run(prompt, permission_mode, api_config=api_config, enable_mcp=enable_mcp)
        sys.exit(0)

    # ── 交互 REPL ──
    ui.print_welcome(api_config["base_url"], api_config["model"], permission_mode)

    # 可变状态字典，贯穿整个 REPL 生命周期
    state: dict = {
        "api_config": api_config,
        "permission_mode": permission_mode,
        "plan_mode": (permission_mode == "plan"),
        "enable_mcp": enable_mcp,
        "chat_messages": None,   # None = 新会话
        "chat_stats": None,      # 跨轮累计 token 统计
        "session_id": str(uuid.uuid4()),  # 本次会话唯一标识
    }

    # ── 自动保存 / 恢复 ──
    def _auto_save():
        """每轮静默保存：__auto__ 用于崩溃恢复，session_id 用于存档"""
        if state["chat_messages"]:
            try:
                from session import save_session
                save_session(
                    name="__auto__",
                    messages=state["chat_messages"],
                    stats=state["chat_stats"],
                    model=state["api_config"]["model"],
                )
                save_session(
                    name=state["session_id"],
                    messages=state["chat_messages"],
                    stats=state["chat_stats"],
                    model=state["api_config"]["model"],
                )
            except Exception:
                pass

    # --resume 恢复指定会话
    resume_id = getattr(args, "resume", None)
    if resume_id:
        try:
            from session import load_session
            s = load_session(resume_id)
            if s and s.get("messages"):
                state["chat_messages"] = s["messages"]
                state["chat_stats"] = s.get("stats")
                state["session_id"] = s.get("name", state["session_id"])
                msg_count = len(s["messages"])
                ui.print_info(f"已恢复会话 {state['session_id']} ({msg_count} 条消息)")
            else:
                ui.print_error(f"未找到会话: {resume_id}")
        except Exception as e:
            ui.print_error(f"恢复失败: {e}")

    effective_mode = permission_mode

    # -- Dreaming: idle-time self-reflection --
    from dream import get_dreamer
    dreamer = get_dreamer(idle_seconds=60)
    dreamer.start()

    while True:
        # Show dream results when user returns
        dream_output = dreamer.result
        if dream_output:
            ui.print_info("[Zz] Dream reflection done:")
            print(f"  [dim]{dream_output[:500]}[/dim]")

        try:
            user_input = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            _auto_save()
            print(f"\n  Resume this session with:")
            print(f"  python main.py --resume {state['session_id']}\n")
            dreamer.stop()
            break
        if not user_input:
            continue

        dreamer.touch()

        result = _dispatch_command(user_input, state)
        if result == QUIT:
            _auto_save()
            print(f"\n  Resume this session with:")
            print(f"  python main.py --resume {state['session_id']}\n")
            dreamer.stop()
            break
        if result == HANDLED:
            continue

        # 普通消息 → 调用 agent
        effective_mode = "plan" if state["plan_mode"] else state["permission_mode"]
        state["chat_messages"], state["chat_stats"] = run(
            user_input, effective_mode,
            api_config=state["api_config"],
            messages=state["chat_messages"],
            enable_mcp=state["enable_mcp"],
            stats=state["chat_stats"],
        )
        _auto_save()
        print()


if __name__ == "__main__":
    main()
