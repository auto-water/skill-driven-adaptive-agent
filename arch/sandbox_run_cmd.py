"""
宿主 shell 执行（供 sandbox_run_command / host_exec 复用）。

超时后除终止 shell 进程外，在 Unix 上向**进程组**发信号，避免
`curl ... | head` 等 pipeline 中子进程在 bash 被杀后仍挂起导致整段卡住。
"""
from __future__ import annotations

import asyncio
import os
import signal
import sys
from typing import Any, Optional


def _env_positive_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        v = int(raw)
        return v if v > 0 else default
    except ValueError:
        return default


def clamp_shell_timeout_sec(timeout: Any) -> int:
    """
    工具参数 timeout 的合法范围：至少 1 秒，至多 SANDBOX_SHELL_TIMEOUT_MAX（默认 600）。
    非法或未解析时使用 SANDBOX_SHELL_TIMEOUT_DEFAULT（默认 120），且不超过上限。
    """
    maximum = max(1, _env_positive_int("SANDBOX_SHELL_TIMEOUT_MAX", 600))
    default = max(1, min(_env_positive_int("SANDBOX_SHELL_TIMEOUT_DEFAULT", 120), maximum))
    try:
        t = int(timeout)
    except (TypeError, ValueError):
        t = default
    return max(1, min(t, maximum))


async def _wait_process(proc: asyncio.subprocess.Process, seconds: float) -> Optional[int]:
    try:
        return await asyncio.wait_for(proc.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        return None


async def _terminate_shell_process_tree(proc: asyncio.subprocess.Process) -> None:
    """超时或取消后：尽量杀掉 shell 及其同一进程组内的子进程（pipeline）。"""
    if proc.returncode is not None:
        return
    if sys.platform != "win32" and proc.pid:
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        code = await _wait_process(proc, 2.0)
        if code is not None:
            return
        try:
            if proc.pid:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await _wait_process(proc, 5.0)
        return
    try:
        proc.kill()
    except ProcessLookupError:
        pass
    await _wait_process(proc, 5.0)


async def run_host_shell(command: str, timeout_sec: int, workdir: str) -> str:
    """
    在 workdir 下以 /bin/bash -c 风格执行 command（与 create_subprocess_shell 一致）。
    timeout_sec 秒后强制结束进程树并返回超时说明文本。
    """
    if timeout_sec < 1:
        timeout_sec = 1
    kw: dict = {
        "command": command,
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
        "cwd": workdir,
        "executable": "/bin/bash",
    }
    if sys.platform != "win32":
        kw["start_new_session"] = True
    proc = await asyncio.create_subprocess_shell(**kw)
    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=float(timeout_sec))
    except asyncio.TimeoutError:
        await _terminate_shell_process_tree(proc)
        return (
            f"Exit code: -1\n\nSTDOUT\n\nSTDERR\n"
            f"Timed out after {timeout_sec}s (local shell; process group terminated)"
        )
    out = out_b.decode(errors="replace") if out_b else ""
    err = err_b.decode(errors="replace") if err_b else ""
    code = proc.returncode if proc.returncode is not None else -1
    return f"Exit code: {code}\n\nSTDOUT\n{out}\n\nSTDERR\n{err}"
