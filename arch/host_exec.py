"""宿主 shell 执行（供 sandbox_run_command 与 planner 队列复用）。"""
from __future__ import annotations

from sandbox_run_cmd import run_host_shell


async def local_run_shell(command: str, timeout: int, workdir: str) -> str:
    """在 workdir 下以 bash 执行 command；超时后终止整组子进程（见 sandbox_run_cmd）。"""
    return await run_host_shell(command, timeout, workdir)
