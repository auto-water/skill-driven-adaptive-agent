import os
import re
import json
import time
import uuid
import asyncio
import contextvars
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional, List, FrozenSet, Tuple
from dataclasses import dataclass
from openai import AsyncOpenAI
from openai._types import NOT_GIVEN
from datetime import datetime, UTC
import threading
import logging

from function_tool import function_tool
from sandbox_run_cmd import clamp_shell_timeout_sec
import json as json_module


def _load_env_file(path: Path) -> None:
    """Parse KEY=value .env into os.environ (later lines override)."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        # 与常见 dotenv 行为一致：不覆盖已在环境中设置的变量（便于命令行覆盖 .env）
        if key not in os.environ:
            os.environ[key] = val


_env_path = Path(__file__).resolve().parent / ".env"
_load_env_file(_env_path)

# OpenRouter: OpenAI-compatible Chat Completions base URL
_OPENROUTER_BASE = (
    os.getenv("OPENROUTER_BASE_URL")
    or os.getenv("OPEN_ROUTER_URL", "https://openrouter.ai/api/v1")
).rstrip("/")
_OPENROUTER_KEY = os.getenv("OPENROUTER_API_KEY") or os.getenv("OPEN_ROUTER_API_KEY", "")
CHAT_MODEL = os.getenv("OPENROUTER_MODEL") or os.getenv("MODEL", "openai/gpt-4o-mini")

_default_headers = {}
_ref = os.getenv("OPENROUTER_HTTP_REFERER")
if _ref:
    _default_headers["HTTP-Referer"] = _ref
_title = os.getenv("OPENROUTER_APP_TITLE")
if _title:
    _default_headers["X-Title"] = _title


def _optional_openai_timeout_and_retries() -> Tuple[Any, int]:
    """
    未设置 OPENROUTER_CHAT_TIMEOUT_SEC 时：不覆盖 SDK 默认（读超时 600s，与 init/main 一致），避免慢模型
    在 180s 被截断后触发 httpx 重试，把单次调用拖长到数分钟。

    若显式设置 OPENROUTER_CHAT_TIMEOUT_SEC：使用该秒数 [30,900] 作为 httpx 读超时；默认将 max_retries 置 0
    （可用 OPENROUTER_MAX_RETRIES 覆盖），避免「超时 → 再重试两轮」放大墙钟。
    """
    raw = (os.getenv("OPENROUTER_CHAT_TIMEOUT_SEC") or "").strip()
    if not raw:
        return NOT_GIVEN, int(os.getenv("OPENROUTER_MAX_RETRIES", "2"))
    try:
        v = float(raw)
        if v <= 0:
            return NOT_GIVEN, int(os.getenv("OPENROUTER_MAX_RETRIES", "2"))
        t = max(30.0, min(900.0, v))
    except ValueError:
        return NOT_GIVEN, int(os.getenv("OPENROUTER_MAX_RETRIES", "2"))
    mr_raw = (os.getenv("OPENROUTER_MAX_RETRIES") or "").strip()
    if mr_raw:
        try:
            mr = max(0, min(10, int(mr_raw)))
        except ValueError:
            mr = 0
    else:
        mr = 0
    return t, mr


_to, _mr = _optional_openai_timeout_and_retries()

# --- Setup ---
_client_kw: Dict[str, Any] = {
    "base_url": _OPENROUTER_BASE.rstrip("/"),
    "api_key": _OPENROUTER_KEY or "missing-key",
    "default_headers": _default_headers or None,
}
if _to is not NOT_GIVEN:
    _client_kw["timeout"] = _to
    _client_kw["max_retries"] = _mr
client = AsyncOpenAI(**_client_kw)


def _default_vulhub_root() -> Path:
    exp = os.getenv("VULHUB_ROOT")
    if exp:
        return Path(exp).expanduser().resolve()
    return (Path(__file__).resolve().parent.parent / "test_case").resolve()


VULHUB_ROOT: Path = _default_vulhub_root()
DOCKER_COMPOSE_TIMEOUT_SEC = int(os.getenv("DOCKER_COMPOSE_TIMEOUT_SEC", "600"))

# 运行产物根目录：codes/logs/<本文件父目录名>/<同名前缀>-<靶场或 PoC 父目录名>/（arch → codes/logs/arch/…）
_CODES_DIR = Path(__file__).resolve().parent.parent
SESSION_LOG_ROOT = _CODES_DIR / "logs" / Path(__file__).resolve().parent.name


def resolve_vulhub_case_dir(case_relative: str) -> Path:
    """
    解析靶场目录。封闭环境下不限制必须在 VULHUB_ROOT 下（支持符号链接解析到任意路径）。

    - case_relative 为空：返回 VULHUB_ROOT
    - 绝对路径：expanduser + resolve
    - 否则： (VULHUB_ROOT / case_relative).resolve()（可解析到 VULHUB_ROOT 外，如 vulhub 真实目录）
    """
    root = VULHUB_ROOT.resolve()
    raw = (case_relative or "").strip()
    if not raw:
        return root
    p = Path(raw).expanduser()
    if p.is_absolute():
        return p.resolve()
    rel = raw.replace("\\", "/").strip("/")
    return (root / rel).resolve()


def guess_compose_host_port(case_dir: Path) -> Optional[int]:
    """Best-effort: first host port in docker-compose.yml|.yaml (e.g. 10086:10086)."""
    for name in ("docker-compose.yml", "docker-compose.yaml"):
        fp = case_dir / name
        if not fp.is_file():
            continue
        text = fp.read_text(encoding="utf-8", errors="ignore")
        m = re.search(r'-\s*["\']?(\d+)\s*:\s*\d+', text)
        if m:
            return int(m.group(1))
    return None


async def _docker_compose(case_dir: Path, subcommand: List[str], timeout_sec: Optional[int] = None) -> str:
    """Run `docker compose <subcommand>` with cwd=case_dir."""
    t = timeout_sec if timeout_sec is not None else DOCKER_COMPOSE_TIMEOUT_SEC
    proc = await asyncio.create_subprocess_exec(
        "docker",
        "compose",
        *subcommand,
        cwd=str(case_dir),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=t)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return "Error: docker compose command timed out (see DOCKER_COMPOSE_TIMEOUT_SEC)."
    out = out_b.decode(errors="replace")
    err = err_b.decode(errors="replace")
    code = proc.returncode if proc.returncode is not None else -1
    return f"exit_code={code}\nSTDOUT\n{out}\nSTDERR\n{err}"


# Thread-local：运行输出目录等（不再持有沙箱实例；命令在宿主本地执行）
_thread_local = threading.local()

# 并行 asyncio 任务之间与 thread_local 隔离；用于子/主循环是否触发轮次上限
_round_limit_hit_var: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "round_limit_hit", default=False
)


def _script_parent_dir_name() -> str:
    """main.py 所在目录名，例如 init、arch（与会话目录 codes/logs/<该名>/ 一致）。"""
    return Path(__file__).resolve().parent.name


def session_label_vulhub(case_dir: Path) -> str:
    return f"{_script_parent_dir_name()}-{case_dir.resolve().name}"


def session_label_poc(poc_path: Path) -> str:
    p = poc_path.resolve()
    slug = p.parent.name if p.is_file() else (p.name or "poc")
    return f"{_script_parent_dir_name()}-{slug}"


def _sanitize_init_log_category(raw: str) -> str:
    """INIT_LOG_CATEGORY：去路径分量与 ..，避免写出 SESSION_LOG_ROOT 外。"""
    s = raw.strip()
    if not s:
        return ""
    s = s.replace("..", "_").replace("/", "_").replace("\\", "_")
    return s.strip("_") or "_"


def ensure_session_log_dir(session_label: str, subdir: Optional[str] = None) -> Path:
    """
    创建 codes/logs/<实现目录名>/[subdir/]<session_label>/，返回绝对路径。
    subdir 非空时来自 INIT_LOG_CATEGORY，便于按 baked_envs 批次分类。
    """
    root = SESSION_LOG_ROOT.resolve()
    base = root / _sanitize_init_log_category(subdir) if (subdir or "").strip() else root
    out = (base / session_label).resolve()
    try:
        out.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"session log path escapes SESSION_LOG_ROOT: {out}") from exc
    out.mkdir(parents=True, exist_ok=True)
    return out


def bind_run_output_dir(path: Path) -> Path:
    """将当前线程的运行输出目录绑定到 path（用量、报告默认写此处）。"""
    p = path.resolve()
    _thread_local.run_output_dir = p
    return p


def get_run_output_dir() -> Path:
    d = getattr(_thread_local, "run_output_dir", None)
    return Path(d).resolve() if d else Path.cwd().resolve()


def _ensure_session_writable_paths(run_dir: Path, report_path: Path) -> None:
    """保证会话目录与报告父目录存在，避免 write_text 时 FileNotFoundError。"""
    run_dir.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)


@dataclass
class RunOutcome:
    """单次 PoC / 靶场验证运行结果。"""

    text: str
    main_exit_reason: str  # complete | max_rounds | token_budget | wall_timeout | request_timeout
    validation_success: bool


def _slug_from_target_url(url: str) -> str:
    u = url.replace("https://", "").replace("http://", "")
    u = re.sub(r"[^a-zA-Z0-9._-]+", "_", u).strip("_")[:120]
    return u or "target"


async def _local_run_shell(command: str, timeout: int) -> str:
    """在宿主上以 shell 执行命令（授权实验环境；cwd 为当前 run 输出目录）。"""
    from host_exec import local_run_shell

    return await local_run_shell(command, timeout, str(get_run_output_dir()))


# Usage tracking
class UsageTracker:
    def __init__(self):
        self.main_agent_usage = []
        self.sandbox_agent_usage = []
        self.start_time = datetime.now(UTC)
        self.validation_success: Optional[bool] = None
        self.main_exit_reason: Optional[str] = None

    def set_validation_success(self, ok: bool) -> None:
        self.validation_success = bool(ok)

    def set_exit_metadata(self, outcome: RunOutcome) -> None:
        """写入 metrics / usage 摘要用的主循环退出原因。"""
        self.main_exit_reason = outcome.main_exit_reason
    
    def log_main_agent_usage(self, usage_data, target_url=""):
        """Log usage data from main agent responses."""
        entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "target_url": target_url,
            "agent_type": "main_agent",
            "usage": usage_data
        }
        self.main_agent_usage.append(entry)
        logging.info(f"Main Agent Usage - Target: {target_url}, Usage: {usage_data}")
    
    def log_sandbox_agent_usage(self, usage_data, target_url=""):
        """Log usage data from sandbox agent responses."""
        entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "target_url": target_url,
            "agent_type": "sandbox_agent", 
            "usage": usage_data
        }
        self.sandbox_agent_usage.append(entry)
        logging.info(f"Sandbox Agent Usage - Target: {target_url}, Usage: {usage_data}")
    
    def get_summary(self):
        """Get usage summary for all agents."""
        end = datetime.now(UTC)
        delta = end - self.start_time
        total_duration_ms = int(delta.total_seconds() * 1000)
        reason = (self.main_exit_reason or "").strip()
        return {
            "scan_duration": str(delta),
            "total_duration_ms": total_duration_ms,
            "validation_success": bool(self.validation_success)
            if self.validation_success is not None
            else False,
            "main_exit_reason": reason or None,
            "limit_hit_max_rounds": reason == "max_rounds",
            "limit_hit_token_budget": reason == "token_budget",
            "limit_hit_wall_timeout": reason == "wall_timeout",
            "limit_hit_request_timeout": reason == "request_timeout",
            "main_agent_calls": len(self.main_agent_usage),
            "sandbox_agent_calls": len(self.sandbox_agent_usage),
            "total_calls": len(self.main_agent_usage) + len(self.sandbox_agent_usage),
            "main_agent_usage": self.main_agent_usage,
            "sandbox_agent_usage": self.sandbox_agent_usage,
        }

    def save_to_file(self, output_dir: Optional[Path] = None) -> Tuple[str, Dict[str, Any]]:
        """写入固定文件名 usage.json，返回 (路径, 与文件一致的 summary 快照)。"""
        base = (output_dir or get_run_output_dir()).resolve()
        base.mkdir(parents=True, exist_ok=True)
        path = base / "usage.json"
        summary = self.get_summary()

        with open(path, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2, default=str)

        logging.info("Usage data saved to %s", path)
        return str(path), summary

# Thread-local storage for usage trackers
def get_current_usage_tracker():
    """Get the usage tracker for the current thread/scan."""
    return getattr(_thread_local, 'usage_tracker', None)

def set_current_usage_tracker(tracker):
    """Set the usage tracker for the current thread/scan."""
    _thread_local.usage_tracker = tracker


def rollup_tokens_and_cost(summary: Dict[str, Any]) -> Tuple[int, float]:
    """从 usage 记录汇总 token 与 cost（与 OpenRouter 返回字段一致）。"""
    total_tokens = 0
    total_cost = 0.0
    for key in ("main_agent_usage", "sandbox_agent_usage"):
        for entry in summary.get(key) or []:
            u = entry.get("usage") or {}
            total_tokens += int(u.get("total_tokens") or 0)
            c = u.get("cost")
            if c is not None:
                try:
                    total_cost += float(c)
                except (TypeError, ValueError):
                    pass
    return total_tokens, total_cost


def build_metrics_payload(
    target_label: str, summary: Dict[str, Any]
) -> Dict[str, Any]:
    """
    汇总指标（供 metrics+{靶场名}.json）。
    - main_agent_rounds: 主智能体 LLM 调用次数（与 main_agent_calls 一致）
    - total_llm_rounds: 主 + 子智能体全部 LLM 调用次数
    - limit_hit_request_timeout: 单次 chat.completions 超时（OPENROUTER_CHAT_TIMEOUT_SEC）
    """
    tokens, cost = rollup_tokens_and_cost(summary)
    reason = summary.get("main_exit_reason")
    return {
        "target_name": target_label,
        "validation_success": bool(summary.get("validation_success", False)),
        "main_exit_reason": reason,
        "limit_hit_max_rounds": bool(summary.get("limit_hit_max_rounds", False)),
        "limit_hit_token_budget": bool(summary.get("limit_hit_token_budget", False)),
        "limit_hit_wall_timeout": bool(summary.get("limit_hit_wall_timeout", False)),
        "limit_hit_request_timeout": bool(summary.get("limit_hit_request_timeout", False)),
        "total_duration_ms": int(summary.get("total_duration_ms", 0)),
        "main_agent_rounds": int(summary.get("main_agent_calls", 0)),
        "sandbox_agent_rounds": int(summary.get("sandbox_agent_calls", 0)),
        "total_llm_rounds": int(summary.get("total_calls", 0)),
        "total_tokens": tokens,
        "total_cost_usd": cost,
    }


def write_metrics_file(
    run_dir: Path, target_label: str, summary: Dict[str, Any]
) -> Path:
    """写入 codes/logs/<实现目录>/.../metrics+{target_label}.json"""
    run_dir.mkdir(parents=True, exist_ok=True)
    safe = target_label.replace("/", "_").replace("\\", "_").strip() or "unknown"
    path = run_dir / f"metrics+{safe}.json"
    payload = build_metrics_payload(target_label, summary)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    logging.info("Metrics saved to %s", path)
    return path


@function_tool
async def read_poc_file(file_path: str, max_bytes: int = 524288):
    """
    Read PoC / README text (UTF-8). 相对路径按当前工作目录解析；封闭环境不限制可读路径范围。

    Args:
        file_path: Relative or absolute path to the file.
        max_bytes: Maximum bytes to read (default 512 KiB).
    """
    try:
        p = Path(file_path).expanduser()
        if not p.is_absolute():
            p = (Path.cwd() / p).resolve()
        else:
            p = p.resolve()
        if not p.is_file():
            return f"Error: file not found: {p}"
        data = p.read_bytes()[: max(0, int(max_bytes))]
        return data.decode("utf-8", errors="replace")
    except Exception as e:
        return f"Error reading file: {e}"


@function_tool(name_override="vulhub_compose_pull")
async def tool_vulhub_compose_pull(case_dir_relative: str = ""):
    """
    在指定靶场目录执行 `docker compose pull`：case_dir_relative 可为相对 VULHUB_ROOT 的片段、或绝对路径；空字符串表示 VULHUB_ROOT。
    """
    return await _docker_compose(resolve_vulhub_case_dir(case_dir_relative), ["pull"])


@function_tool(name_override="vulhub_compose_up")
async def tool_vulhub_compose_up(case_dir_relative: str = ""):
    """
    在指定靶场目录下执行 `docker compose up -d` 启动实例。
    """
    return await _docker_compose(resolve_vulhub_case_dir(case_dir_relative), ["up", "-d"])


@function_tool(name_override="vulhub_compose_down")
async def tool_vulhub_compose_down(case_dir_relative: str = "", remove_volumes: bool = True):
    """
    在指定靶场目录下执行 `docker compose down`；默认带 `-v` 销毁卷。验证结束后必须 teardown。
    """
    args: List[str] = ["down"]
    if remove_volumes:
        args.append("-v")
    return await _docker_compose(resolve_vulhub_case_dir(case_dir_relative), args)


@function_tool(name_override="sandbox_agent")
async def run_sandbox_agent(instruction: str, max_rounds: int = 20):
    """
    子智能体：仅用本地执行工具完成指令（宿主 shell / python3，非隔离沙箱）。
    在达到 max_rounds 或无工具调用时返回最终文本。
    """
    sandbox_system_prompt = os.getenv(
        "SANDBOX_SYSTEM_PROMPT",
        (
            "You are an agent that runs on the local lab host using two tools: "
            "`sandbox_run_command` (bash on the host) and `sandbox_run_python` (Python on the host). "
            "For any command that performs network I/O (curl, wget, nc, telnet, openssl s_client, "
            "ssh to live hosts, etc.), you MUST include a tool-native time limit inside the shell command "
            "itself (e.g. `curl --connect-timeout 3 --max-time 8 ...`, `wget -T 8 ...`, `nc -w 3 ...`). "
            "Also pass a sensible `timeout` argument to `sandbox_run_command` as an outer cap (often 15–120). "
            "Keep responses within 30,000 characters; chunk large outputs. Think step-by-step before taking actions."
        ),
    )

    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": sandbox_system_prompt},
        {"role": "user", "content": instruction},
    ]
    text, _nested_reason = await _chat_tool_agent_loop(
        messages,
        frozenset({"sandbox_run_command", "sandbox_run_python"}),
        max_rounds,
        usage_agent="nested",
        agent_label="sandbox_agent",
    )
    return text
        
@function_tool(name_override="validator_agent")
async def run_validator_agent(instruction: str, max_rounds: int = 20):
    """
    子智能体：在本地宿主上复核 PoC（bash / python3），输出简洁结论。
    """
    validator_system_prompt = os.getenv(
        "VALIDATOR_SYSTEM_PROMPT",
        (
            "You validate security PoCs on the local lab host using two tools: "
            "`sandbox_run_command` (bash) and `sandbox_run_python` (Python). Your goal is to: "
            "(1) Reproduce the PoC minimally and safely, (2) Capture evidence (stdout, file diffs, HTTP responses), "
            "(3) Decide if the PoC reliably demonstrates a real vulnerability with impact, (4) Provide a concise verdict. "
            "Whenever you use network-reachability checks (curl, wget, nc, etc.), the shell command MUST include "
            "explicit per-tool timeouts (e.g. `curl --connect-timeout 3 --max-time 8 ...`) and you MUST set "
            "`sandbox_run_command`'s `timeout` parameter as a reasonable outer bound. "
            "Always think step-by-step before actions. Keep outputs within 30,000 characters and chunk large outputs. "
            "Avoid destructive actions unless explicitly required for validation."
        ),
    )

    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": validator_system_prompt},
        {"role": "user", "content": instruction},
    ]
    text, _nested_reason = await _chat_tool_agent_loop(
        messages,
        frozenset({"sandbox_run_command", "sandbox_run_python"}),
        max_rounds,
        usage_agent="nested",
        agent_label="validator_agent",
    )
    return text
        
@function_tool
async def sandbox_run_python(python_code: str, timeout: int = 120):
    """
    在宿主上通过临时文件执行 Python 代码，返回 stdout/stderr/exit code（输出过长会截断）。

    Args:
        python_code: 要执行的 Python 源码。
        timeout: 最长等待秒数。
    """
    print(f"Running Python code (local): {python_code[:100]}...")
    try:
        td = Path(tempfile.gettempdir())
        script_path = td / f"init_local_{uuid.uuid4().hex[:12]}.py"
        script_path.write_text(python_code, encoding="utf-8")
        try:
            proc = await asyncio.create_subprocess_exec(
                "python3",
                str(script_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(get_run_output_dir()),
            )
            try:
                out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return f"Exit code: -1\n\nSTDOUT\n\nSTDERR\nTimed out after {timeout}s (local python)"
            stdout_raw = out_b.decode(errors="replace") if out_b else ""
            stderr_raw = err_b.decode(errors="replace") if err_b else ""
            exit_code = proc.returncode if proc.returncode is not None else -1
            output = f"Exit code: {exit_code}\n\nSTDOUT\n{stdout_raw}\n\nSTDERR\n{stderr_raw}"
        finally:
            try:
                script_path.unlink(missing_ok=True)
            except OSError:
                pass
        if len(output) > 30000:
            output = output[:30000] + "\n...[OUTPUT TRUNCATED - EXCEEDED 30000 CHARACTERS]"
        return output
    except Exception as e:
        return f"Failed to run Python code locally: {e}"


@function_tool
async def sandbox_run_command(command: str, timeout: int = 120):
    """
    在宿主上以 bash 执行 shell 命令，返回 stdout/stderr/exit code（cwd 为当前会话输出目录）。

    Arguments:
        command: 要执行的 shell 命令（如 ls -la）。若含 curl/wget/nc 等网络访问，命令正文中必须写出
            工具自带的超时（例如 curl 的 --connect-timeout 与 --max-time），勿依赖无上限请求。
        timeout: 整段 shell（含 pipeline）的最长等待秒数；超时后运行时会终止进程组（见 sandbox_run_cmd）。
            受环境变量 SANDBOX_SHELL_TIMEOUT_DEFAULT / SANDBOX_SHELL_TIMEOUT_MAX 钳制；网络探测建议 15–120。
    """
    timeout = clamp_shell_timeout_sec(timeout)
    print(f"Running command (local, timeout={timeout}s): {command}")
    try:
        return await _local_run_shell(command, timeout)
    except Exception as e:
        return f"Failed to run command locally: {e}"

# Collect all function tools that were decorated
_function_tools = {
    "sandbox_run_command": sandbox_run_command,
    "sandbox_run_python": sandbox_run_python,
    "sandbox_agent": run_sandbox_agent,
    "validator_agent": run_validator_agent,
    "read_poc_file": read_poc_file,
    "vulhub_compose_pull": tool_vulhub_compose_pull,
    "vulhub_compose_up": tool_vulhub_compose_up,
    "vulhub_compose_down": tool_vulhub_compose_down,
}


def _env_nested_max_rounds_cap() -> int:
    try:
        return max(1, int(os.getenv("MAX_NESTED_ROUNDS", "20")))
    except ValueError:
        return 20


def _clamp_nested_tool_max_rounds(raw: Any) -> int:
    cap = _env_nested_max_rounds_cap()
    try:
        mr = int(raw) if raw is not None else 20
    except (TypeError, ValueError):
        mr = 20
    return max(1, min(mr, cap))


async def execute_tool(name: str, arguments: Dict[str, Any]) -> str:
    try:
        if name in _function_tools:
            func_tool = _function_tools[name]
            if name == "sandbox_agent":
                instruction = arguments.get("instruction", arguments.get("input", ""))
                max_rounds = _clamp_nested_tool_max_rounds(arguments.get("max_rounds", 20))
                out = await func_tool(instruction, max_rounds)
            elif name == "validator_agent":
                instruction = arguments.get("instruction", arguments.get("input", ""))
                max_rounds = _clamp_nested_tool_max_rounds(arguments.get("max_rounds", 20))
                out = await func_tool(instruction, max_rounds)
            else:
                out = await func_tool(**arguments)
        else:
            out = {"error": f"Unknown tool: {name}", "args": arguments}
    except Exception as e:
        out = {"error": str(e), "args": arguments}
    return out if isinstance(out, str) else json.dumps(out)


def generate_tools_from_function_tools():
    """Auto-generate tools list from decorated functions (internal flat shape)."""
    tools_out = []

    for _, func_tool in _function_tools.items():
        if hasattr(func_tool, "name") and hasattr(func_tool, "description") and hasattr(
            func_tool, "params_json_schema"
        ):
            tools_out.append(
                {
                    "type": "function",
                    "name": func_tool.name,
                    "description": func_tool.description,
                    "parameters": func_tool.params_json_schema,
                }
            )

    return tools_out


def tools_to_openai_chat_format(flat_tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Map flat function defs to OpenAI Chat Completions / OpenRouter tool schema."""
    out: List[Dict[str, Any]] = []
    for t in flat_tools:
        if t.get("type") != "function":
            continue
        out.append(
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description") or "",
                    "parameters": t.get("parameters")
                    or {"type": "object", "properties": {}, "additionalProperties": False},
                },
            }
        )
    return out


def _assistant_message_to_dict(msg: Any) -> Dict[str, Any]:
    """Serialize assistant message including tool_calls for the next chat turn."""
    if hasattr(msg, "model_dump"):
        d = msg.model_dump(exclude_none=True)
        return d
    d: Dict[str, Any] = {"role": getattr(msg, "role", "assistant")}
    if getattr(msg, "content", None):
        d["content"] = msg.content
    tcs = getattr(msg, "tool_calls", None)
    if tcs:
        d["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments or "{}",
                },
            }
            for tc in tcs
        ]
    return d


def _usage_to_plain(usage: Any) -> Any:
    if usage is None:
        return None
    if hasattr(usage, "model_dump"):
        return usage.model_dump()
    return usage


# Generate tools automatically from decorated functions
tools = generate_tools_from_function_tools()


def _env_int_nonnegative(name: str, default: int = 0) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        v = int(raw)
        return v if v >= 0 else default
    except ValueError:
        return default


def _main_strategy() -> str:
    """
    MAIN_STRATEGY=react（默认）：单段 ReAct 式工具循环。
    MAIN_STRATEGY=plan_solve：先仅 read_poc_file 规划，再全工具执行。
    MAIN_STRATEGY=queue：Planner JSON + 顺序 TaskQueue（硬编排）。
    """
    v = (os.getenv("MAIN_STRATEGY", "react") or "react").strip().lower().replace("-", "_")
    if v in ("plan_solve", "ps", "psolve"):
        return "plan_solve"
    if v in ("queue", "hard_queue", "task_queue"):
        return "queue"
    return "react"


def _plan_phase_max_rounds() -> int:
    """规划阶段工具往返上限；默认 8；<=0 或未解析时回退为 8；硬顶 100。"""
    default = 8
    raw = (os.getenv("PLAN_MAX_ROUNDS", "") or "").strip()
    if not raw:
        return default
    try:
        v = int(raw)
        if v <= 0:
            return default
        return min(v, 100)
    except ValueError:
        return default


_PLAN_PHASE_SYSTEM_SUFFIX = (
    "\n\n【阶段一：规划】\n"
    "你只能使用工具 read_poc_file 读取 PoC/README 等材料。\n"
    "禁止在本阶段调用 validator_agent、sandbox_agent 以及 vulhub_compose_*。\n"
    "读取完成后，输出一份结构化验证计划（目标与假设、前置条件、逐步操作、预期现象、"
    "需采集的证据、环境清理/回滚注意）。随后结束本阶段（不要再发起工具调用）。"
)

_EXEC_PHASE_SYSTEM_SUFFIX = (
    "\n\n【阶段二：执行】\n"
    "现已进入执行阶段：你可使用 read_poc_file、validator_agent、sandbox_agent、vulhub_compose_* 等全部工具，"
    "按上文材料与计划完成复现与取证，并输出最终漏洞验证报告（须含执行摘要、证据、明确结论，以及单独小节「关键 payload」）。\n"
    "经子智能体执行的网络类 shell：须在命令内写明 curl/wget/nc 等自带超时，并为 sandbox_run_command 设合理 timeout。"
)

_SOLVE_PHASE_USER = (
    "请开始阶段二：依据上文已读取内容与验证计划调用工具完成验证；"
    "最终回复为完整报告（非仅复述计划）。"
)

_FULL_MAIN_TOOLS: FrozenSet[str] = frozenset(
    {
        "read_poc_file",
        "validator_agent",
        "sandbox_agent",
        "vulhub_compose_pull",
        "vulhub_compose_up",
        "vulhub_compose_down",
    }
)


async def _chat_tool_agent_loop(
    messages: List[Dict[str, Any]],
    allowed_tool_names: FrozenSet[str],
    max_rounds: int,
    usage_agent: str,
    agent_label: str,
) -> Tuple[str, str]:
    """OpenRouter/OpenAI Chat Completions tool loop (ReAct-style)。返回 (文本, exit_reason)。"""
    flat = [t for t in tools if t.get("name") in allowed_tool_names]
    oai_tools = tools_to_openai_chat_format(flat)
    rounds = 0
    target_hint = getattr(_thread_local, "current_target_url", "")
    max_total_tokens = _env_int_nonnegative("MAX_TOTAL_TOKENS", 0)
    max_wall_sec = _env_int_nonnegative("MAX_WALL_CLOCK_SEC", 0)
    wall_t0 = time.monotonic()

    while True:
        if max_wall_sec > 0 and (time.monotonic() - wall_t0) >= max_wall_sec:
            _round_limit_hit_var.set(True)
            return (
                f"[{agent_label}] Reached wall clock limit: {max_wall_sec}s",
                "wall_timeout",
            )

        kwargs: Dict[str, Any] = {
            "model": CHAT_MODEL,
            "messages": messages,
        }
        if oai_tools:
            kwargs["tools"] = oai_tools
            kwargs["tool_choice"] = "auto"

        response = await client.chat.completions.create(**kwargs)
        msg = response.choices[0].message
        usage_tracker = get_current_usage_tracker()
        uplain = _usage_to_plain(getattr(response, "usage", None))
        if usage_tracker and uplain is not None:
            if usage_agent == "main":
                usage_tracker.log_main_agent_usage(uplain, target_hint)
            else:
                usage_tracker.log_sandbox_agent_usage(uplain, target_hint)

        if max_total_tokens > 0 and usage_tracker:
            snap = usage_tracker.get_summary()
            tok, _ = rollup_tokens_and_cost(snap)
            if tok >= max_total_tokens:
                _round_limit_hit_var.set(True)
                return (
                    f"[{agent_label}] Reached total token budget: {max_total_tokens}",
                    "token_budget",
                )

        if not msg.tool_calls:
            return ((msg.content or "").strip(), "complete")

        messages.append(_assistant_message_to_dict(msg))

        async def _run_one(tc: Any) -> Dict[str, Any]:
            name = tc.function.name
            raw = tc.function.arguments or "{}"
            try:
                args = json.loads(raw) if isinstance(raw, str) else dict(raw)
            except json.JSONDecodeError:
                args = {}
            result_str = await execute_tool(name, args)
            if not isinstance(result_str, str):
                result_str = json.dumps(result_str)
            return {"role": "tool", "tool_call_id": tc.id, "content": result_str}

        for tr in await asyncio.gather(*[_run_one(tc) for tc in msg.tool_calls]):
            messages.append(tr)

        rounds += 1
        if max_rounds and rounds >= max_rounds:
            _round_limit_hit_var.set(True)
            return (f"[{agent_label}] Reached max rounds limit: {max_rounds}", "max_rounds")


def read_targets_from_file(file_path: str) -> List[str]:
    """
    Read target URLs from a text file, one per line.
    Ignores empty lines and lines starting with #.
    """
    targets = []
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    targets.append(line)
        return targets
    except FileNotFoundError:
        print(f"Error: Target file '{file_path}' not found.")
        return []
    except Exception as e:
        print(f"Error reading target file: {e}")
        return []


async def _run_planner_queue_branch(
    max_rounds: int,
    user_prompt: str,
    system_prompt: str,
    target_url: str,
) -> RunOutcome:
    """MAIN_STRATEGY=queue：LLM 产出 JSON 任务表后由 planner_queue 顺序执行。"""
    import planner_queue

    logging.info(
        "Main strategy=queue (PLANNER_MODEL=%r, max_rounds=%s)",
        (os.getenv("PLANNER_MODEL") or "").strip() or CHAT_MODEL,
        max_rounds,
    )
    model = (os.getenv("PLANNER_MODEL") or "").strip() or CHAT_MODEL
    hint = target_url or ""

    def _log_main(u: Any, t_hint: str) -> None:
        tr = get_current_usage_tracker()
        if tr and u is not None:
            tr.log_main_agent_usage(u, t_hint)

    user_body = (
        user_prompt
        + f"\n\n【编排约束】主循环 max_rounds={max_rounds}；validator 类任务的 max_rounds 不得超过该值。"
    )
    tasks, err = await planner_queue.plan_tasks_json(
        client,
        model=model,
        system_prompt=system_prompt,
        user_body=user_body,
        target_hint=hint,
        log_main_usage=_log_main,
        usage_to_plain=_usage_to_plain,
    )
    if not tasks:
        return RunOutcome(
            text=err or "Planner failed",
            main_exit_reason="complete",
            validation_success=False,
        )

    wall_t0 = time.monotonic()
    max_wall = _env_int_nonnegative("MAX_WALL_CLOCK_SEC", 0)
    max_tok = _env_int_nonnegative("MAX_TOTAL_TOKENS", 0)

    async def _shell_runner(cmd: str, to: int) -> str:
        from host_exec import local_run_shell

        return await local_run_shell(cmd, clamp_shell_timeout_sec(to), str(get_run_output_dir()))

    async def _val_runner(inst: str, mr: int) -> str:
        mr_adj = min(mr, max_rounds) if max_rounds > 0 else mr
        mr2 = _clamp_nested_tool_max_rounds(mr_adj)
        return await run_validator_agent(inst, mr2)

    def _tok() -> int:
        tr = get_current_usage_tracker()
        if not tr:
            return 0
        return rollup_tokens_and_cost(tr.get_summary())[0]

    text, reason, ok = await planner_queue.run_planned_queue(
        tasks,
        shell_runner=_shell_runner,
        validator_runner=_val_runner,
        get_total_tokens=_tok,
        max_total_tokens=max_tok,
        max_wall_sec=max_wall,
        wall_t0=wall_t0,
    )
    if reason in ("wall_timeout", "token_budget"):
        _round_limit_hit_var.set(True)
    print(text)
    lim = _round_limit_hit_var.get()
    final_ok = ok and reason == "complete" and bool((text or "").strip()) and not lim
    return RunOutcome(text=text or "", main_exit_reason=reason, validation_success=final_ok)


async def run_continuously(max_rounds: int = 20, user_prompt: str = "", system_prompt: str = "", target_url: str = "", sandbox_instance=None) -> RunOutcome:
    """
    PoC 验证主循环（OpenRouter Chat Completions + 工具）。
    max_rounds: 0 表示不限制主工具轮数（仍受 MAX_TOTAL_TOKENS / MAX_WALL_CLOCK_SEC 约束，若配置）。
    入口脚本在 MAX_ROUNDS=0 且未设 ALLOW_UNLIMITED_ROUNDS 时会强制改为 MAX_ROUNDS_HARD_CAP。
    target_url: 写入线程局部量，仅用于用量日志标注。
    validation_success: 主循环正常结束、输出非空、且未触发轮次/token/墙钟上限。
    sandbox_instance 参数已废弃，保留仅为兼容旧调用方；不再创建或销毁沙箱。

    当环境变量 MAIN_STRATEGY=plan_solve 时：先仅允许 read_poc_file（轮数上限见 PLAN_MAX_ROUNDS，默认 8），
    再进入全工具执行阶段；max_rounds 仍仅约束执行阶段。metrics JSON 字段集合与 react 模式一致（main_agent_rounds
    为两阶段主模型调用次数之和，数据仍来自同一 UsageTracker 路径）。

    MAIN_STRATEGY=queue：Planner（JSON tasks）+ TaskQueue 顺序执行 shell/validator/read_file（见 planner_queue）。
    """
    _ = sandbox_instance
    _thread_local.current_target_url = target_url
    _round_limit_hit_var.set(False)

    strategy = _main_strategy()
    if strategy == "queue":
        return await _run_planner_queue_branch(
            max_rounds, user_prompt, system_prompt, target_url
        )

    if strategy != "plan_solve":
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        text, reason = await _chat_tool_agent_loop(
            messages,
            _FULL_MAIN_TOOLS,
            max_rounds,
            usage_agent="main",
            agent_label="poc_validator_main",
        )
        print(text)
        lim = _round_limit_hit_var.get()
        ok = reason == "complete" and bool(text.strip()) and not lim
        return RunOutcome(text=text, main_exit_reason=reason, validation_success=ok)

    # --- plan & solve：两阶段串行，仍使用同一 _chat_tool_agent_loop 与 metrics 管线 ---
    logging.info(
        "Main strategy=plan_solve (plan_max_rounds=%s, solve_max_rounds=%s)",
        _plan_phase_max_rounds(),
        max_rounds,
    )
    messages = [
        {"role": "system", "content": system_prompt + _PLAN_PHASE_SYSTEM_SUFFIX},
        {"role": "user", "content": user_prompt},
    ]
    plan_text, plan_reason = await _chat_tool_agent_loop(
        messages,
        frozenset({"read_poc_file"}),
        _plan_phase_max_rounds(),
        usage_agent="main",
        agent_label="poc_validator_plan",
    )
    lim_after_plan = _round_limit_hit_var.get()
    if plan_reason != "complete" or not (plan_text or "").strip() or lim_after_plan:
        print(plan_text)
        ok = plan_reason == "complete" and bool((plan_text or "").strip()) and not lim_after_plan
        return RunOutcome(
            text=plan_text or "",
            main_exit_reason=plan_reason,
            validation_success=ok,
        )

    messages[0] = {
        "role": "system",
        "content": system_prompt + _EXEC_PHASE_SYSTEM_SUFFIX,
    }
    messages.append({"role": "user", "content": _SOLVE_PHASE_USER})

    _round_limit_hit_var.set(False)
    solve_text, solve_reason = await _chat_tool_agent_loop(
        messages,
        _FULL_MAIN_TOOLS,
        max_rounds,
        usage_agent="main",
        agent_label="poc_validator_main",
    )
    combined = (
        "## 验证计划（阶段一）\n\n"
        + (plan_text or "").strip()
        + "\n\n---\n\n## 验证报告（阶段二）\n\n"
        + (solve_text or "").strip()
    )
    print(combined)
    lim = _round_limit_hit_var.get()
    ok = (
        solve_reason == "complete"
        and bool((solve_text or "").strip())
        and not lim
    )
    return RunOutcome(
        text=combined,
        main_exit_reason=solve_reason,
        validation_success=ok,
    )


def _case_dir_relative_to_root(case_dir: Path) -> str:
    """供提示词与 compose 工具参数：若在 VULHUB_ROOT 下则返回相对路径，否则返回绝对路径字符串。"""
    root = VULHUB_ROOT.resolve()
    c = case_dir.resolve()
    try:
        rel = c.relative_to(root)
        s = rel.as_posix()
        return "" if s == "." else s
    except ValueError:
        return c.as_posix()


async def run_vulhub_case(
    case_dir: Path,
    *,
    max_rounds: int,
    system_prompt: str,
    user_prompt: str,
    sandbox_instance=None,
    auto_compose: bool = True,
) -> RunOutcome:
    """
    Vulhub 单靶场验证：可选在验证前 docker compose pull/up，结束后 down -v（与 LLM 工具互补，保证宿主机一定 teardown）。
    """
    case_dir = case_dir.resolve()
    _thread_local.current_target_url = str(case_dir)
    if auto_compose:
        logging.info("Vulhub: compose pull in %s", case_dir)
        out_pull = await _docker_compose(case_dir, ["pull"])
        logging.info("compose pull output (truncated): %s", out_pull[:500])
        logging.info("Vulhub: compose up -d in %s", case_dir)
        out_up = await _docker_compose(case_dir, ["up", "-d"])
        logging.info("compose up output (truncated): %s", out_up[:500])
    try:
        return await run_continuously(
            max_rounds=max_rounds,
            user_prompt=user_prompt,
            system_prompt=system_prompt,
            target_url=str(case_dir),
            sandbox_instance=sandbox_instance,
        )
    finally:
        if auto_compose:
            logging.info("Vulhub: compose down -v in %s", case_dir)
            out_down = await _docker_compose(case_dir, ["down", "-v"])
            logging.info("compose down output (truncated): %s", out_down[:500])


async def run_single_target_scan(target_url: str, system_prompt: str, base_user_prompt: str, max_rounds: int = 20):
    """
    Run a security scan for a single target URL.
    Returns the scan result and saves it to a file.
    """
    print(f"Starting scan for: {target_url}")

    session_label = f"{_script_parent_dir_name()}-{_slug_from_target_url(target_url)}"
    _cat = os.getenv("INIT_LOG_CATEGORY", "").strip()
    run_dir = ensure_session_log_dir(session_label, subdir=_cat or None)

    # Create usage tracker for this scan
    usage_tracker = UsageTracker()
    set_current_usage_tracker(usage_tracker)

    # Format the user prompt with the target URL
    user_prompt = base_user_prompt.format(target_url=target_url)

    try:
        outcome = await run_continuously(
            user_prompt=user_prompt,
            system_prompt=system_prompt,
            target_url=target_url,
            max_rounds=max_rounds,
            sandbox_instance=None,
        )
        usage_tracker.set_validation_success(outcome.validation_success)
        usage_tracker.set_exit_metadata(outcome)

        result_path = run_dir / "report.md"
        _ensure_session_writable_paths(run_dir, result_path)
        with open(result_path, "w", encoding="utf-8") as f:
            f.write(outcome.text)

        usage_filename, usage_snapshot = usage_tracker.save_to_file(output_dir=run_dir)
        metrics_label = _slug_from_target_url(target_url)
        metrics_path = write_metrics_file(run_dir, metrics_label, usage_snapshot)

        print(f"Scan completed for {target_url} - Results saved to {result_path}")
        print(f"Usage data saved to {usage_filename}")
        print(f"Metrics saved to {metrics_path}")

        return {
            "target": target_url,
            "filename": str(result_path),
            "usage_filename": usage_filename,
            "metrics_file": str(metrics_path),
            "status": "completed",
            "result": outcome.text,
            "usage_summary": usage_snapshot,
        }

    except Exception as e:
        print(f"Error scanning {target_url}: {e}")
        return {
            "target": target_url,
            "filename": None,
            "status": "error",
            "error": str(e),
        }

async def run_parallel_scans(targets: List[str], system_prompt: str, base_user_prompt: str, max_rounds: int = 20):
    """
    Run security scans for multiple targets in parallel.
    """
    print(f"Starting parallel scans for {len(targets)} targets...")
    
    # Create tasks for all targets
    tasks = [
        run_single_target_scan(target, system_prompt, base_user_prompt, max_rounds)
        for target in targets
    ]
    
    # Run all scans in parallel
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    # Process results
    completed = 0
    errors = 0
    
    for result in results:
        if isinstance(result, Exception):
            print(f"Task failed with exception: {result}")
            errors += 1
        elif result.get("status") == "completed":
            completed += 1
        else:
            errors += 1
    
    print(f"\nScan Summary:")
    print(f"Total targets: {len(targets)}")
    print(f"Completed successfully: {completed}")
    print(f"Failed: {errors}")
    
    # Create overall usage summary
    total_main_calls = 0
    total_sandbox_calls = 0
    usage_files = []
    
    for result in results:
        if isinstance(result, dict) and result.get("status") == "completed":
            summary = result.get("usage_summary", {})
            total_main_calls += summary.get("main_agent_calls", 0)
            total_sandbox_calls += summary.get("sandbox_agent_calls", 0)
            if "usage_filename" in result:
                usage_files.append(result["usage_filename"])
    
    print(f"\nUsage Summary:")
    print(f"Total Main Agent API calls: {total_main_calls}")
    print(f"Total nested/local sub-agent API calls: {total_sandbox_calls}")
    print(f"Total API calls: {total_main_calls + total_sandbox_calls}")
    print(f"Usage files created: {len(usage_files)}")
    for uf in usage_files:
        print(f"  - {uf}")
    
    return results


if __name__ == "__main__":
    import sys

    try:
        max_r = int(os.getenv("MAX_ROUNDS", "20"))
    except ValueError:
        max_r = 20
    if max_r == 0 and os.getenv("ALLOW_UNLIMITED_ROUNDS", "").lower() not in (
        "1",
        "true",
        "yes",
    ):
        try:
            cap = int(os.getenv("MAX_ROUNDS_HARD_CAP", "50"))
        except ValueError:
            cap = 50
        max_r = max(1, cap)
        print(
            f"[警告] MAX_ROUNDS=0 且未设置 ALLOW_UNLIMITED_ROUNDS=1，已改用 MAX_ROUNDS={max_r} "
            f"（可通过 MAX_ROUNDS_HARD_CAP 调整上限）。",
            flush=True,
        )
    vulhub_mode = os.getenv("VULHUB_MODE", "1").lower() in ("1", "true", "yes")
    _log_cat = os.getenv("INIT_LOG_CATEGORY", "").strip() or None

    run_dir: Path
    case_dir: Optional[Path] = None
    poc_file: Optional[Path] = None

    if vulhub_mode:
        case_rel = os.getenv("VULHUB_CASE", "").strip()
        if len(sys.argv) > 1:
            case_rel = sys.argv[1].strip()
        case_dir = resolve_vulhub_case_dir(case_rel)
        run_dir = ensure_session_log_dir(session_label_vulhub(case_dir), subdir=_log_cat)
    else:
        poc_arg = Path(sys.argv[1]) if len(sys.argv) > 1 else None
        poc_file = poc_arg or Path(os.getenv("POC_FILE", "poc.md"))
        if not poc_arg and not poc_file.is_absolute():
            poc_file = (Path.cwd() / poc_file).resolve()
        else:
            poc_file = poc_file.resolve()
        run_dir = ensure_session_log_dir(session_label_poc(poc_file), subdir=_log_cat)

    bind_run_output_dir(run_dir)
    session_log_file = run_dir / "session.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[
            logging.FileHandler(session_log_file, encoding="utf-8"),
            logging.StreamHandler(),
        ],
        force=True,
    )

    if not _OPENROUTER_KEY or _OPENROUTER_KEY == "missing-key":
        logging.warning(
            "未检测到 OPENROUTER_API_KEY（或 OPEN_ROUTER_API_KEY），请求将失败；请在 .env 中配置。"
        )

    logging.info("本次运行输出目录: %s", run_dir)

    tracker = UsageTracker()
    set_current_usage_tracker(tracker)

    if vulhub_mode:
        assert case_dir is not None
        readme_name = os.getenv("VULHUB_README", "README.md")
        readme_path = (case_dir / readme_name).resolve()
        port = guess_compose_host_port(case_dir)
        target_base = (os.getenv("VULHUB_TARGET_BASE") or "").strip()
        if not target_base:
            target_base = f"http://127.0.0.1:{port}" if port else "http://127.0.0.1"

        rel_for_tools = _case_dir_relative_to_root(case_dir)

        system_prompt = os.getenv(
            "SYSTEM_PROMPT",
            "你是授权实验环境下的 Vulhub 类靶场漏洞验证助手。每个靶场目录含 docker-compose.yml 与 README.md（PoC/复现步骤通常写在 README）。\n"
            "工具与流程：\n"
            "- vulhub_compose_pull / vulhub_compose_up / vulhub_compose_down：在靶场目录执行 compose；case_dir_relative 为相对 VULHUB_ROOT 的路径片段、或绝对路径；空字符串表示 VULHUB_ROOT。\n"
            "- read_poc_file：读取 README.md 或其它说明文件（相对路径按 cwd 解析）。\n"
            "- validator_agent：在本地宿主环境按 PoC 最小化复现并取证；必要时用 sandbox_agent 做更灵活的命令行探索（均为本地 bash/python3）。\n"
            "凡通过 sandbox_run_command / 子智能体内 shell 做 HTTP 或任意网络探测时：命令正文必须包含工具自带的超时参数（例如 curl 使用 --connect-timeout 与 --max-time；wget 使用 -T；nc 使用 -w），"
            "并同时为 sandbox_run_command 传入合理的 timeout 秒数作为外层兜底；禁止依赖无超时上限的网络命令。\n"
            "宿主程序可能在对话前已自动 pull+up，并在对话结束后自动 down -v；你仍可在需要时自行调用 compose 工具（例如重建环境）。\n"
            "若本机或子进程网络无法直达靶场映射端口，须在报告中说明，并给出应在「与靶场互通的网络环境」下执行的 curl/http 验证命令与关键 payload。\n"
            "最终输出必须包含：执行摘要、验证步骤、证据摘录、明确结论（有效/无效/无法判断）、以及单独小节「关键 payload」（curl/原始 HTTP/可复用利用片段）。",
        )

        default_user = (
            f"当前 Vulhub 靶场绝对路径: {case_dir}\n"
            f"compose 工具参数 case_dir_relative 请使用: {rel_for_tools!r}（相对 VULHUB_ROOT 或绝对路径）\n"
            f"请用 read_poc_file 读取 PoC 文档: {readme_path}\n"
            f"验证目标 HTTP 基址（来自环境或 compose 端口推测）: {target_base}\n\n"
            "按 README 完成验证；优先使用 validator_agent 复现。结束后若容器仍在且需手动清理，调用 vulhub_compose_down(case_dir_relative=上述路径)。\n"
            "输出漏洞验证报告与「关键 payload」。"
        )
        user_prompt = os.getenv("VULHUB_USER_PROMPT", default_user)

        if not readme_path.is_file():
            logging.warning("README 不存在（将依赖模型其它信息）: %s", readme_path)
        compose_file = case_dir / "docker-compose.yml"
        if not compose_file.is_file() and not (case_dir / "docker-compose.yaml").is_file():
            logging.warning("未找到 docker-compose.yml/yaml: %s", case_dir)

        auto_compose = os.getenv("VULHUB_AUTO_COMPOSE", "1").lower() in ("1", "true", "yes")
        print(f"VULHUB_ROOT={VULHUB_ROOT}\nCASE={case_dir}\nAUTO_COMPOSE={auto_compose}")

        outcome = asyncio.run(
            run_vulhub_case(
                case_dir,
                max_rounds=max_r,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                sandbox_instance=None,
                auto_compose=auto_compose,
            )
        )

        tracker.set_validation_success(outcome.validation_success)
        tracker.set_exit_metadata(outcome)
        slug = case_dir.name if case_dir.name else "case"
        rf = (os.getenv("REPORT_FILE") or "").strip()
        if rf and Path(rf).is_absolute():
            report_path = Path(rf)
        else:
            report_path = run_dir / (rf or "report.md")
        _ensure_session_writable_paths(run_dir, report_path)
        report_path.write_text(outcome.text or "", encoding="utf-8")
        usage_path, usage_snapshot = tracker.save_to_file()
        metrics_path = write_metrics_file(run_dir, slug, usage_snapshot)
        print(f"会话目录: {run_dir.resolve()}")
        print(f"会话日志: {session_log_file.resolve()}")
        print(f"报告已写入: {report_path.resolve()}")
        print(f"用量日志: {usage_path}")
        print(f"指标文件: {metrics_path.resolve()}")
        print(f"验证成功(流程): {outcome.validation_success}")
        sys.exit(0)

    # --- 非 Vulhub：单文件 PoC 模式 ---
    assert poc_file is not None
    system_prompt = os.getenv(
        "SYSTEM_PROMPT",
        "你是授权实验环境下的 Web 漏洞 PoC 验证助手。流程：必要时用 read_poc_file 读取 PoC 文件；"
        "用 validator_agent 在本地环境中最小化复现并给出结论；仅在需要更自由命令行探索时使用 sandbox_agent（宿主执行）。"
        "凡 shell 网络验证：命令内须写明 curl/wget/nc 等自带的超时选项，并为 sandbox_run_command 设置合理 timeout。"
        "最终回复须包含：执行摘要、验证步骤、证据（命令/输出片段）、明确结论（有效/无效/无法判断），"
        "以及单独小节「关键 payload」列出 curl、HTTP 原始报文或可复用利用片段。",
    )

    user_tmpl = os.getenv(
        "USER_PROMPT",
        "PoC 文件路径：{poc_path}\n\n请读取并验证该 PoC，输出漏洞验证报告与关键 payload。",
    )
    try:
        user_prompt = user_tmpl.format(poc_path=str(poc_file))
    except KeyError:
        user_prompt = user_tmpl

    if not poc_file.is_file():
        logging.warning("PoC 文件不存在（模型仍可能依赖你在提示中内联的内容）：%s", poc_file)

    outcome = asyncio.run(
        run_continuously(
            max_rounds=max_r,
            user_prompt=user_prompt,
            system_prompt=system_prompt,
            target_url=str(poc_file),
            sandbox_instance=None,
        )
    )

    tracker.set_validation_success(outcome.validation_success)
    tracker.set_exit_metadata(outcome)
    rf = (os.getenv("REPORT_FILE") or "").strip()
    if rf and Path(rf).is_absolute():
        report_path = Path(rf)
    else:
        report_path = run_dir / (rf or "report.md")
    _ensure_session_writable_paths(run_dir, report_path)
    report_path.write_text(outcome.text or "", encoding="utf-8")
    usage_path, usage_snapshot = tracker.save_to_file()
    metrics_label = poc_file.parent.name if poc_file.is_file() else poc_file.name
    metrics_path = write_metrics_file(run_dir, metrics_label, usage_snapshot)
    print(f"会话目录: {run_dir.resolve()}")
    print(f"会话日志: {session_log_file.resolve()}")
    print(f"报告已写入: {report_path.resolve()}")
    print(f"用量日志: {usage_path}")
    print(f"指标文件: {metrics_path.resolve()}")
    print(f"验证成功(流程): {outcome.validation_success}")
    sys.exit(0)
