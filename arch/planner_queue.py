"""
Planner：一次 LLM 调用产出 JSON 任务表；TaskQueue：顺序执行 shell / validator / read_file。
由 main.run_continuously 在 MAIN_STRATEGY=queue 时调用；不依赖 main 的循环 import。
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from sandbox_run_cmd import clamp_shell_timeout_sec

JsonTasks = List[Dict[str, Any]]


def _queue_on_step_fail() -> str:
    v = (os.getenv("QUEUE_ON_STEP_FAIL") or "stop").strip().lower()
    return "continue" if v in ("continue", "skip", "go_on") else "stop"


def _parse_shell_exit_code(output: str) -> Optional[int]:
    for line in output.splitlines():
        if line.strip().lower().startswith("exit code:"):
            rest = line.split(":", 1)[1].strip()
            m = re.match(r"(-?\d+)", rest)
            if m:
                return int(m.group(1))
    return None


def _validate_tasks(raw: Any) -> Tuple[Optional[JsonTasks], str]:
    if not isinstance(raw, dict):
        return None, "Planner JSON 根须为 object"
    tasks = raw.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        return None, "缺少非空 tasks 数组"
    out: JsonTasks = []
    for i, t in enumerate(tasks):
        if not isinstance(t, dict):
            return None, f"tasks[{i}] 须为 object"
        typ = (t.get("type") or "").strip().lower()
        if typ == "shell":
            cmd = t.get("command")
            if not isinstance(cmd, str) or not cmd.strip():
                return None, f"tasks[{i}].shell 需要非空 command"
            try:
                to = int(t.get("timeout_sec", 120))
            except (TypeError, ValueError):
                to = 120
            out.append({"type": "shell", "command": cmd.strip(), "timeout_sec": clamp_shell_timeout_sec(to)})
        elif typ in ("validator", "validate"):
            inst = t.get("instruction")
            if not isinstance(inst, str) or not inst.strip():
                return None, f"tasks[{i}].validator 需要非空 instruction"
            try:
                mr = int(t.get("max_rounds", 12))
            except (TypeError, ValueError):
                mr = 12
            out.append({"type": "validator", "instruction": inst.strip(), "max_rounds": max(1, min(mr, 50))})
        elif typ in ("read_file", "readfile"):
            p = t.get("path")
            if not isinstance(p, str) or not p.strip():
                return None, f"tasks[{i}].read_file 需要 path"
            try:
                mb = int(t.get("max_bytes", 524288))
            except (TypeError, ValueError):
                mb = 524288
            out.append({"type": "read_file", "path": p.strip(), "max_bytes": max(1024, min(mb, 2_097_152))})
        else:
            return None, f"tasks[{i}] 未知 type: {typ!r}"
    return out, ""


async def plan_tasks_json(
    client: Any,
    *,
    model: str,
    system_prompt: str,
    user_body: str,
    target_hint: str,
    log_main_usage: Optional[Callable[[Any, str], None]],
    usage_to_plain: Callable[[Any], Any],
) -> Tuple[Optional[JsonTasks], str]:
    """
    调用 chat.completions 产出 {"tasks":[...]}；成功返回 (tasks, "")，失败返回 (None, err)。
    """
    planner_sys = (
        system_prompt
        + "\n\n【Planner 模式】你是任务分解器。仅输出一个 JSON 对象，格式严格为："
        '{"tasks":[{"type":"shell","command":"...","timeout_sec":120},'
        '{"type":"validator","instruction":"...","max_rounds":12},'
        '{"type":"read_file","path":"/abs/path","max_bytes":524288}]} 。'
        "type 只能是 shell | validator | read_file。shell 用于 curl/bash 等可复现命令；"
        "凡含 curl/wget/nc 等网络访问的 shell，command 内必须写工具自带超时（如 curl --connect-timeout 与 --max-time）；"
        "timeout_sec 为外层整段命令上限。validator 用于需要多轮工具推理的验证说明；read_file 读取文本证据。"
        "不要输出 markdown 代码围栏或任何 JSON 外文字。"
    )
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": planner_sys},
        {"role": "user", "content": user_body},
    ]

    last_err = ""
    for attempt in range(2):
        raw_txt = ""
        try:
            resp = await client.chat.completions.create(
                model=model,
                messages=messages,
                response_format={"type": "json_object"},
            )
            msg = resp.choices[0].message
            uplain = usage_to_plain(getattr(resp, "usage", None))
            if log_main_usage and uplain is not None:
                log_main_usage(uplain, target_hint)
            raw_txt = (msg.content or "").strip()
            if not raw_txt:
                last_err = "Planner 返回空 content"
            else:
                data = json.loads(raw_txt)
                tasks, verr = _validate_tasks(data)
                if tasks is not None:
                    return tasks, ""
                last_err = verr
        except json.JSONDecodeError as e:
            last_err = f"JSON 解析失败: {e}"
        except Exception as e:
            last_err = str(e)
        if attempt == 0:
            messages.append({"role": "assistant", "content": raw_txt or "(empty)"})
            messages.append(
                {
                    "role": "user",
                    "content": "上次输出无效，请仅输出合法 JSON 对象：{\"tasks\":[...]}，tasks 内 type 仅 shell|validator|read_file。",
                }
            )
    return None, last_err or "Planner 失败"


async def run_planned_queue(
    tasks: JsonTasks,
    *,
    shell_runner: Callable[[str, int], Awaitable[str]],
    validator_runner: Callable[[str, int], Awaitable[str]],
    get_total_tokens: Callable[[], int],
    max_total_tokens: int,
    max_wall_sec: int,
    wall_t0: float,
) -> Tuple[str, str, bool]:
    """
    顺序执行任务。返回 (report_markdown, main_exit_reason, validation_success)。
    validation_success：全部步骤完成且最终报告非空、未触墙/token、且 shell 步骤 exit 0。
    """
    parts: List[str] = ["## Planner 任务队列\n\n", "```json\n", json.dumps(tasks, ensure_ascii=False, indent=2), "\n```\n\n## 执行记录\n\n"]
    on_fail = _queue_on_step_fail()
    lim_hit = False
    for idx, step in enumerate(tasks):
        if max_wall_sec > 0 and (time.monotonic() - wall_t0) >= max_wall_sec:
            parts.append(f"\n### 步骤 {idx + 1} 中止：墙钟上限\n")
            lim_hit = True
            return "".join(parts), "wall_timeout", False
        if max_total_tokens > 0 and get_total_tokens() >= max_total_tokens:
            parts.append(f"\n### 步骤 {idx + 1} 中止：token 预算\n")
            lim_hit = True
            return "".join(parts), "token_budget", False

        typ = step["type"]
        parts.append(f"### 步骤 {idx + 1}: `{typ}`\n\n")
        try:
            if typ == "shell":
                out = await shell_runner(step["command"], int(step["timeout_sec"]))
                parts.append("```\n" + out[:12000] + "\n```\n\n")
                ec = _parse_shell_exit_code(out)
                if ec is not None and ec != 0:
                    parts.append(f"**非零退出码 {ec}**\n\n")
                    if on_fail == "stop":
                        return "".join(parts), "complete", False
            elif typ == "validator":
                out = await validator_runner(step["instruction"], int(step["max_rounds"]))
                parts.append(out[:24000] + "\n\n")
            elif typ == "read_file":
                p = Path(step["path"]).expanduser()
                if not p.is_absolute():
                    p = (Path.cwd() / p).resolve()
                else:
                    p = p.resolve()
                mb = int(step["max_bytes"])
                if not p.is_file():
                    parts.append(f"Error: not a file: {p}\n\n")
                    if on_fail == "stop":
                        return "".join(parts), "complete", False
                else:
                    data = p.read_bytes()[:mb]
                    parts.append(data.decode("utf-8", errors="replace")[:12000] + "\n\n")
        except Exception as e:
            parts.append(f"**异常**: {e}\n\n")
            if on_fail == "stop":
                return "".join(parts), "complete", False

    parts.append("\n## 队列结论\n\n队列已按序执行完毕（未因失败策略提前停止）。\n")
    text = "".join(parts)
    ok = bool(text.strip()) and not lim_hit
    return text, "complete", ok
