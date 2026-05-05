#!/usr/bin/env bash
# 依据 data/post_auth/ 下 5 条冒烟 metrics 推导的批量环境变量（2026-05-04 分析）。
#
# 样本统计（validation_success=true）:
#   max total_tokens          89499  (CVE-2021-25646)
#   max total_duration_ms     664010 (~11.1 min 墙钟)
#   max total_llm_rounds      40
#   max main_agent_rounds     9
#   max sandbox_agent_rounds  31（全 run 累计，非单次 nested 上限）
#
# 策略: 在「最长、最耗 token」样本之上留约 2.3× 墙钟、~2.8× token，用于更难靶场与波动；
#       仍能在明显死循环/刷 token 时较早截断。可按预算改大/改小下方数字。
#
# 用法:
#   source "$(dirname "$0")/env_batch_limits_from_smoke.sh"
#   python batch_verify_targets.py --batch-root /path/to/baked_envs/post_auth --copy-session
#
# 或单行:
#   source codes/scripts/env_batch_limits_from_smoke.sh && python codes/scripts/batch_verify_targets.py ...
#
# 注意：本文件设计为 source 执行，不使用 set -e，以免改变调用方 shell 行为。

# --- main.py：主循环与预算 ---
# 冒烟主轮次峰值 9，默认 20 已够；略留余量防更难 README
export MAX_ROUNDS="${MAX_ROUNDS:-22}"

# 累计 token：~2.8 × 冒烟峰值 (89500)，异常膨胀时截断
export MAX_TOTAL_TOKENS="${MAX_TOTAL_TOKENS:-250000}"

# 墙钟（秒）：~2.3 × 最长冒烟 664s，覆盖慢模型/网络
export MAX_WALL_CLOCK_SEC="${MAX_WALL_CLOCK_SEC:-1500}"

# 单次 sandbox_agent / validator_agent 内部轮数上限（与全 run 的 sandbox_agent_rounds 不同）
export MAX_NESTED_ROUNDS="${MAX_NESTED_ROUNDS:-24}"

# 勿默认无限主轮次；若显式要 MAX_ROUNDS=0，需 ALLOW_UNLIMITED_ROUNDS=1
# export ALLOW_UNLIMITED_ROUNDS=1

# 当 MAX_ROUNDS=0 且未允许无限时的兜底（一般不必改）
export MAX_ROUNDS_HARD_CAP="${MAX_ROUNDS_HARD_CAP:-50}"

# --- batch_verify_targets.py：子进程兜底（含 compose 拉镜像等，比 metrics 内 LLM 墙钟更长）---
# 冒烟最长约 11 min；45 min 为单靶场上限，防整条批量卡死
export BATCH_MAIN_TIMEOUT_SEC="${BATCH_MAIN_TIMEOUT_SEC:-2700}"

# 若希望从环境统一传入 timeout（可在包装命令里用）:
#   python batch_verify_targets.py --main-timeout-sec "$BATCH_MAIN_TIMEOUT_SEC"

echo "[env_batch_limits_from_smoke] 已设置: MAX_ROUNDS=$MAX_ROUNDS MAX_TOTAL_TOKENS=$MAX_TOTAL_TOKENS" \
  "MAX_WALL_CLOCK_SEC=$MAX_WALL_CLOCK_SEC MAX_NESTED_ROUNDS=$MAX_NESTED_ROUNDS BATCH_MAIN_TIMEOUT_SEC=$BATCH_MAIN_TIMEOUT_SEC" >&2
