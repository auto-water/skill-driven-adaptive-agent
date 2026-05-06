#!/usr/bin/env python3
"""
批量驱动 codes/init/main.py，对某一目录下所有靶场（含 docker-compose 的子目录）依次验证，
并将各靶场的 metrics JSON 复制到指定目录（默认：仓库根目录下的 data/）。
若指定的 --output-dir 无法创建或不可写（如无权限的 /data），会自动回退到上述默认目录并打印提示。

示例：
  python batch_verify_targets.py \\
    --batch-root /path/to/baked_envs/post_auth

  # 指定其它输出目录 + 归档完整会话（含 report、usage、session.log）
  python batch_verify_targets.py \\
    --batch-root /path/to/baked_envs/some_category \\
    --copy-session

  # 与旧版一致：会话不分子目录（codes/logs/init/<init>-<slug>/）
  python batch_verify_targets.py \\
    --batch-root /path/to/baked_envs/post_auth \\
    --flat-init-logs

默认向子进程设置 INIT_LOG_CATEGORY=<batch-root 目录名>，使日志落在 codes/logs/init/<类别>/<init>-<slug>/，
与 data/<类别>/ 下的 metrics 汇总一致。

传入子进程的环境变量：VULHUB_ROOT、VULHUB_CASE、VULHUB_MODE、INIT_LOG_CATEGORY（除非 --flat-init-logs）。

推荐与批量测试一起在 shell 或 --env 中设置的 main.py 变量（详见 codes/init/main.py）：
  MAX_ROUNDS              主智能体工具往返轮数上限（默认 20；0 需 ALLOW_UNLIMITED_ROUNDS=1，否则用 MAX_ROUNDS_HARD_CAP）
  MAX_TOTAL_TOKENS        单靶场累计 token 上限，超出则结束（0 或未设表示不限制）
  MAX_WALL_CLOCK_SEC      单靶场墙钟秒数上限（0 或未设表示不限制）
  MAX_NESTED_ROUNDS       sandbox_agent / validator_agent 内部轮数上限（默认 20）
  OPENROUTER_* / MODEL    模型与密钥

本脚本 CLI：
  --main-timeout-sec      单靶场子进程最长等待（默认 5400；0 为不限制）
  --copy-session          将会话目录复制到 data/<batch>/_sessions/<靶场>/
  --flat-init-logs        不设置 INIT_LOG_CATEGORY

post_auth 下可为指向 vulhub 的符号链接：本脚本对「靶场路径」不做 .resolve()；
main.py 内部仍会对 (VULHUB_ROOT/VULHUB_CASE) 做 resolve。

单靶场是否「验证通过」以 metrics JSON 的 validation_success 为准；main.py 退出码仅作提示。
metrics 另含 main_exit_reason、limit_hit_*（token / 墙钟 / 轮数）字段便于分析中断原因。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple

# 默认：<workbench>/data（与 codes/ 同级），例如 /home/.../Projects/workbench/data
_DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent.parent.parent / "data"
# main.py 单靶场默认墙钟上限（秒）；0 表示不限制子进程等待时间
_DEFAULT_MAIN_TIMEOUT_SEC = 5400


def _sanitize_init_log_category(raw: str) -> str:
    """与 main.py 一致，用于拼接 codes/logs/init/<category>/。"""
    s = raw.strip()
    if not s:
        return ""
    s = s.replace("..", "_").replace("/", "_").replace("\\", "_")
    return s.strip("_") or "_"


def _try_make_writable_dir(path: Path) -> Path:
    """创建目录并探测可写；失败则抛出 PermissionError / OSError。"""
    p = path.expanduser().resolve()
    p.mkdir(parents=True, exist_ok=True)
    test = p / ".write_test_batch_verify"
    try:
        test.write_text("")
        test.unlink(missing_ok=True)
    except OSError as e:
        raise PermissionError(f"目录不可写: {p} ({e})") from e
    return p


def _ensure_output_root(requested: Path) -> Path:
    """
    优先使用用户指定的输出目录；若无法创建或不可写，且与默认不同，
    则自动回退到仓库根下的 data/，保证在无 root 权限时仍能跑通。
    """
    req = requested.expanduser().resolve()
    default = _DEFAULT_OUTPUT_DIR.resolve()
    chain = [req] if req == default else [req, default]
    last_err: BaseException | None = None
    for i, cand in enumerate(chain):
        try:
            return _try_make_writable_dir(cand)
        except (PermissionError, OSError) as e:
            last_err = e
            if i == 0 and len(chain) > 1:
                print(
                    f"[提示] 输出目录不可用「{cand}」: {e}\n"
                    f"[提示] 已自动改用「{chain[1]}」。若需固定使用原路径，请 "
                    "sudo mkdir/chown 或换用可写目录。",
                    file=sys.stderr,
                )
    assert last_err is not None
    print(
        f"无法在「{req}」与默认「{default}」创建或写入输出目录。\n"
        "若曾使用 /data，需 root 创建并 chown："
        "sudo mkdir -p /data && sudo chown \"$USER:$USER\" /data\n"
        f"最后一次错误: {last_err}",
        file=sys.stderr,
    )
    raise SystemExit(2) from last_err


def _has_compose(d: Path) -> bool:
    return (d / "docker-compose.yml").is_file() or (d / "docker-compose.yaml").is_file()


def discover_cases(batch_root: Path, recursive: bool) -> List[Path]:
    batch_root = batch_root.resolve()
    if not batch_root.is_dir():
        raise FileNotFoundError(f"batch-root 不是目录: {batch_root}")
    found: List[Path] = []
    if recursive:
        seen = set()
        for name in ("docker-compose.yml", "docker-compose.yaml"):
            for p in batch_root.rglob(name):
                # 不 resolve parent：避免符号链接解析到 vulhub 外路径后 relative_to 失败
                parent = p.parent
                try:
                    parent.relative_to(batch_root)
                except ValueError:
                    continue
                if parent not in seen:
                    seen.add(parent)
                    found.append(parent)
        return sorted(found, key=lambda x: x.as_posix())
    for p in sorted(batch_root.iterdir()):
        if p.is_dir() and _has_compose(p):
            found.append(p)
    return found


def _session_log_root(main_py: Path, init_log_category: Optional[str] = None) -> Path:
    """codes/logs/init[/INIT_LOG_CATEGORY]"""
    base = main_py.resolve().parent.parent / "logs" / "init"
    if init_log_category and init_log_category.strip():
        return base / _sanitize_init_log_category(init_log_category)
    return base


def resolved_vulhub_case_dir(batch_root: Path, case_dir: Path) -> Path:
    """与 main.py 中 resolve_vulhub_case_dir(VULHUB_CASE) 解析结果一致（含跟随符号链接）。"""
    rel = _case_rel_posix(batch_root, case_dir)
    return (batch_root.resolve() / rel).resolve()


def session_label_for_case(batch_root: Path, case_dir: Path, main_py: Path) -> str:
    """与 main.py 的 session_label_vulhub(case_dir) 一致：{main 父目录名}-{resolved_case.name}。"""
    rc = resolved_vulhub_case_dir(batch_root, case_dir)
    prefix = main_py.resolve().parent.name
    return f"{prefix}-{rc.name}"


def find_metrics_src(
    batch_root: Path,
    case_dir: Path,
    main_py: Path,
    *,
    init_log_category: Optional[str] = None,
    min_mtime: float = 0.0,
) -> Optional[Path]:
    """
    main.py 在 SESSION_LOG_ROOT [/INIT_LOG_CATEGORY] / session_label / metrics+{resolved}.json 写入指标。
    init_log_category 与传给子进程的 INIT_LOG_CATEGORY 一致（默认 batch_root 名）。
    """
    rc = resolved_vulhub_case_dir(batch_root, case_dir)
    session_dir = _session_log_root(main_py, init_log_category) / session_label_for_case(
        batch_root, case_dir, main_py
    )
    primary = session_dir / f"metrics+{rc.name}.json"
    skew = 5.0

    def is_fresh(p: Path) -> bool:
        return min_mtime <= 0 or p.stat().st_mtime >= min_mtime - skew

    if primary.is_file() and is_fresh(primary):
        return primary
    if session_dir.is_dir():
        for p in sorted(
            session_dir.glob("metrics*.json"),
            key=lambda x: x.stat().st_mtime,
            reverse=True,
        ):
            if p.is_file() and is_fresh(p):
                return p
    if primary.is_file():
        return primary
    if session_dir.is_dir():
        globs = sorted(
            [p for p in session_dir.glob("metrics*.json") if p.is_file()],
            key=lambda x: x.stat().st_mtime,
            reverse=True,
        )
        if globs:
            return globs[0]
    return None


def read_validation_success(metrics_path: Path) -> Tuple[Optional[bool], str]:
    """从 metrics JSON 读取 validation_success；无法解析时返回 (None, 原因)。"""
    try:
        data = json.loads(metrics_path.read_text(encoding="utf-8"))
    except Exception as e:
        return None, f"JSON 解析失败: {e}"
    vs = data.get("validation_success")
    if isinstance(vs, bool):
        return vs, ""
    return None, "缺少或类型非法的 validation_success 字段"


def _case_rel_posix(batch_root: Path, case_dir: Path) -> str:
    """逻辑相对路径（不跟随符号链接）。"""
    return case_dir.relative_to(batch_root.resolve()).as_posix()


def output_metrics_path(output_dir: Path, batch_root: Path, case_dir: Path) -> Path:
    """避免不同批次同名叶子目录冲突：output_dir / <batch名> / <相对路径扁平化>.json"""
    safe = _case_rel_posix(batch_root, case_dir).replace("/", "__")
    out_sub = output_dir.resolve() / batch_root.name
    out_sub.mkdir(parents=True, exist_ok=True)
    return out_sub / f"{safe}.json"


def run_one_case(
    *,
    python_exe: str,
    main_py: Path,
    batch_root: Path,
    case_dir: Path,
    output_dir: Path,
    extra_env: dict,
    init_log_category: Optional[str],
    main_timeout_sec: Optional[float],
    copy_session: bool,
) -> int:
    rel = _case_rel_posix(batch_root, case_dir)
    env = os.environ.copy()
    env.update(
        {
            "VULHUB_ROOT": str(batch_root.resolve()),
            "VULHUB_CASE": rel,
            "VULHUB_MODE": "1",
        }
    )
    if init_log_category:
        env["INIT_LOG_CATEGORY"] = _sanitize_init_log_category(init_log_category)
    env.update(extra_env)

    print(f"\n=== 靶场: {rel} ===", flush=True)
    run_started = time.time()
    r: Optional[subprocess.CompletedProcess[str]] = None
    try:
        r = subprocess.run(
            [python_exe, str(main_py)],
            cwd=str(main_py.parent),
            env=env,
            timeout=main_timeout_sec,
        )
    except subprocess.TimeoutExpired:
        print(
            f"[错误] main.py 超过 {main_timeout_sec}s 未退出，已终止子进程。"
            " 可增大 --main-timeout-sec 或检查是否死循环。",
            flush=True,
        )
    if r is not None and r.returncode != 0:
        print(
            f"[注意] main.py 退出码 {r.returncode}（批量结果以 metrics 中 validation_success 为准）",
            flush=True,
        )

    src = find_metrics_src(
        batch_root,
        case_dir,
        main_py,
        init_log_category=init_log_category,
        min_mtime=run_started,
    )
    dst = output_metrics_path(output_dir, batch_root, case_dir)
    if src is None or not src.is_file():
        label = session_label_for_case(batch_root, case_dir, main_py)
        rc = resolved_vulhub_case_dir(batch_root, case_dir)
        cat = _sanitize_init_log_category(init_log_category) if init_log_category else ""
        sub = f"init/{cat}/{label}" if cat else f"init/{label}"
        print(
            f"[错误] 未找到本次运行产生的 metrics（会话目录: {sub}，期望 metrics+{rc.name}.json）",
            flush=True,
        )
        return 1

    shutil.copy2(src, dst)
    print(f"[已复制] {src} -> {dst}", flush=True)

    if copy_session:
        sess = src.parent
        safe = _case_rel_posix(batch_root, case_dir).replace("/", "__")
        arch = output_dir.resolve() / batch_root.name / "_sessions" / safe
        try:
            if arch.exists():
                shutil.rmtree(arch)
            shutil.copytree(sess, arch)
            print(f"[已归档会话] {sess} -> {arch}", flush=True)
        except OSError as e:
            print(f"[警告] 归档会话失败: {e}", flush=True)

    ok, err = read_validation_success(src)
    if ok is None:
        print(f"[错误] 无法读取验证结论: {err}", flush=True)
        return 1
    if ok:
        print(f"[验证结论] validation_success=true（来源: {src.name}）", flush=True)
        return 0
    print(f"[验证结论] validation_success=false（来源: {src.name}）", flush=True)
    return 1


def parse_env_pairs(pairs: List[str]) -> dict:
    out = {}
    for item in pairs:
        if "=" not in item:
            raise ValueError(f"无效 KEY=VAL: {item}")
        k, _, v = item.partition("=")
        out[k.strip()] = v
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="批量运行 init/main.py 并汇总 metrics")
    ap.add_argument(
        "--batch-root",
        type=Path,
        required=True,
        help="靶场集合根目录，例如 baked_envs/post_auth（其下每个含 compose 的子目录为一靶场）",
    )
    ap.add_argument(
        "--output-dir",
        type=Path,
        default=_DEFAULT_OUTPUT_DIR,
        help=(
            "metrics 复制目标根目录（其下会再建 batch-root 目录名子文件夹）。"
            f"默认: 仓库根下的 data/（当前解析为 {_DEFAULT_OUTPUT_DIR}）。"
        ),
    )
    ap.add_argument(
        "--main-py",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "init" / "main.py",
        help="main.py 路径，默认 codes/init/main.py",
    )
    ap.add_argument(
        "--python",
        default=sys.executable,
        help="解释器，默认当前 python",
    )
    ap.add_argument(
        "--recursive",
        action="store_true",
        help="递归查找所有含 docker-compose.yml(yaml) 的目录（相对 batch-root 的子路径作为 VULHUB_CASE）",
    )
    ap.add_argument(
        "--env",
        action="append",
        default=[],
        metavar="KEY=VAL",
        help="额外传入子进程的环境变量，可多次指定，例如 --env MAX_ROUNDS=20",
    )
    ap.add_argument(
        "--flat-init-logs",
        action="store_true",
        help="不传 INIT_LOG_CATEGORY，会话仍写入 codes/logs/init/<init>-<slug>/（与旧行为一致）",
    )
    ap.add_argument(
        "--main-timeout-sec",
        type=int,
        default=_DEFAULT_MAIN_TIMEOUT_SEC,
        help=(
            "单靶场 main.py 子进程最长等待秒数，超时则终止并继续下一靶场；"
            f"默认 {_DEFAULT_MAIN_TIMEOUT_SEC}；设为 0 表示不限制"
        ),
    )
    ap.add_argument(
        "--copy-session",
        action="store_true",
        help="除 metrics 外，将会话目录整份复制到 <output>/<batch-root名>/_sessions/<靶场相对路径>/",
    )
    ap.add_argument(
        "--only",
        action="append",
        default=None,
        metavar="DIR_NAME",
        help=(
            "仅运行 batch-root 下子目录名为 DIR_NAME 的靶场（与 discover 结果中的目录名一致，可重复）。"
            "未指定则运行该 batch-root 下全部靶场。"
        ),
    )
    args = ap.parse_args()

    output_root = _ensure_output_root(args.output_dir)

    batch_root = args.batch_root
    main_py = args.main_py.resolve()
    if not main_py.is_file():
        print(f"找不到 main.py: {main_py}", file=sys.stderr)
        return 2

    try:
        cases = discover_cases(batch_root, args.recursive)
    except FileNotFoundError as e:
        print(e, file=sys.stderr)
        return 2

    if not cases:
        print(
            f"未在 {batch_root.resolve()} 下发现含 docker-compose.yml/yaml 的子目录。"
            " 若目录尚未创建，请先添加靶场；需要多层目录可加 --recursive。",
            file=sys.stderr,
        )
        return 1

    if args.only:
        only_set = set(args.only)
        discovered = {c.name for c in cases}
        missing = sorted(only_set - discovered)
        if missing:
            print(
                f"[警告] --only 中有未在 batch-root 下发现的目录名: {missing}",
                file=sys.stderr,
            )
        cases = [c for c in cases if c.name in only_set]
        if not cases:
            print(
                "筛选后没有待运行的靶场（请核对 --only 与 batch-root 下子目录名是否一致）。",
                file=sys.stderr,
            )
            return 1

    extra_env = parse_env_pairs(args.env)
    log_cat: Optional[str] = None if args.flat_init_logs else batch_root.resolve().name
    timeout_sec: Optional[float] = (
        None if args.main_timeout_sec == 0 else float(args.main_timeout_sec)
    )
    failed = 0
    for case_dir in cases:
        code = run_one_case(
            python_exe=args.python,
            main_py=main_py,
            batch_root=batch_root.resolve(),
            case_dir=case_dir,
            output_dir=output_root,
            extra_env=extra_env,
            init_log_category=log_cat,
            main_timeout_sec=timeout_sec,
            copy_session=args.copy_session,
        )
        if code != 0:
            failed += 1

    print(
        f"\n完成: {len(cases)} 个靶场; 未通过或缺 metrics 的计数: {failed} "
        f"（依据各靶场 metrics 内 validation_success）。",
        flush=True,
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
