#!/usr/bin/env bash
# 对 baked_envs 下各分类批量跑 batch_verify_targets.py，通过参数指定要测试的 main.py（如 init / arch 等不同实现）。
#
# 用法:
#   ./test_all.sh codes/init/main.py
#   ./test_all.sh init/main.py --copy-session
#   ./test_all.sh --main-py arch/main.py --recursive
#   ./test_all.sh --dry-run codes/arch_modified/main.py
#
# 第一个非选项参数或 --main-py 的值为 main.py 路径；可为相对 codes/ 的路径或任意存在的绝对路径。
# 其余参数原样传给 batch_verify_targets.py（勿传 --batch-root、勿重复传 --main-py）。

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CODES_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
WORKBENCH="$(cd "$CODES_DIR/.." && pwd)"
BAKED_ENVS="${BAKED_ENVS:-$WORKBENCH/baked_envs}"
BATCH_PY="$SCRIPT_DIR/batch_verify_targets.py"

DRY_RUN=0
MAIN_PY=""
PASS_THROUGH=()

usage() {
  cat <<'EOF'
用法:
  test_all.sh [选项] <main.py路径> [batch_verify_targets.py 的参数...]

选项:
  --main-py PATH   显式指定 main.py（可与位置参数二选一，以先出现的为准）
  --dry-run        只打印将执行的命令
  -h, --help       显示本说明

示例:
  test_all.sh codes/init/main.py --copy-session
  test_all.sh init/main.py
  test_all.sh --main-py /abs/path/to/codes/arch/main.py --recursive
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --main-py)
      if [[ $# -lt 2 ]]; then
        echo "错误: --main-py 需要路径参数" >&2
        exit 2
      fi
      MAIN_PY="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      if [[ -z "$MAIN_PY" ]]; then
        MAIN_PY="$1"
        shift
      else
        PASS_THROUGH+=("$1")
        shift
      fi
      ;;
  esac
done

if [[ -z "$MAIN_PY" ]]; then
  echo "错误: 必须指定 main.py 路径（见 --help）" >&2
  usage >&2
  exit 2
fi

_resolve_main_py() {
  local p="$1"
  if [[ -f "$p" ]]; then
    printf '%s\n' "$(cd "$(dirname "$p")" && pwd)/$(basename "$p")"
    return 0
  fi
  # 相对 codes/，如 init/main.py、arch/main.py
  if [[ -f "$CODES_DIR/$p" ]]; then
    local full="$CODES_DIR/$p"
    printf '%s\n' "$(cd "$(dirname "$full")" && pwd)/$(basename "$full")"
    return 0
  fi
  # 相对仓库根，如 codes/init/main.py
  if [[ -f "$WORKBENCH/$p" ]]; then
    local full="$WORKBENCH/$p"
    printf '%s\n' "$(cd "$(dirname "$full")" && pwd)/$(basename "$full")"
    return 0
  fi
  return 1
}

if ! MAIN_PY_RESOLVED="$(_resolve_main_py "$MAIN_PY")"; then
  echo "错误: 找不到 main.py: $MAIN_PY（已尝试: 当前路径、\$CODES_DIR/$MAIN_PY、\$WORKBENCH/$MAIN_PY）" >&2
  exit 2
fi
MAIN_PY="$MAIN_PY_RESOLVED"

# ========== 冒烟推导 limit（与 run_baked_envs 批量脚本一致，始终 export）==========
export MAX_ROUNDS="${MAX_ROUNDS:-22}"
export MAX_TOTAL_TOKENS="${MAX_TOTAL_TOKENS:-250000}"
export MAX_WALL_CLOCK_SEC="${MAX_WALL_CLOCK_SEC:-900}"
export MAX_NESTED_ROUNDS="${MAX_NESTED_ROUNDS:-24}"
export MAX_ROUNDS_HARD_CAP="${MAX_ROUNDS_HARD_CAP:-50}"
export BATCH_MAIN_TIMEOUT_SEC="${BATCH_MAIN_TIMEOUT_SEC:-900}"

echo "[test_all] main.py=$MAIN_PY" >&2
echo "[test_all] MAX_ROUNDS=$MAX_ROUNDS MAX_TOTAL_TOKENS=$MAX_TOTAL_TOKENS MAX_WALL_CLOCK_SEC=$MAX_WALL_CLOCK_SEC" \
  "MAX_NESTED_ROUNDS=$MAX_NESTED_ROUNDS BATCH_MAIN_TIMEOUT_SEC=$BATCH_MAIN_TIMEOUT_SEC (单靶场默认 15min)" >&2

if [[ ! -d "$BAKED_ENVS" ]]; then
  echo "错误: 找不到 baked_envs 目录: $BAKED_ENVS" >&2
  exit 2
fi

if [[ ! -f "$BATCH_PY" ]]; then
  echo "错误: 找不到 batch_verify_targets.py: $BATCH_PY" >&2
  exit 2
fi

PYTHON_EXE="${PYTHON:-${PYTHON_EXE:-python3}}"

DIRS=()
shopt -s nullglob
for d in "$BAKED_ENVS"/*/; do
  [[ -d "$d" ]] || continue
  DIRS+=("$(basename "$d")")
done
shopt -u nullglob

if [[ ${#DIRS[@]} -eq 0 ]]; then
  echo "警告: $BAKED_ENVS 下没有子目录。" >&2
  exit 0
fi

mapfile -t DIRS < <(printf '%s\n' "${DIRS[@]}" | sort)

failed_batches=0
ran=0

_pass_has_main_timeout() {
  local i
  for i in "${!PASS_THROUGH[@]}"; do
    if [[ "${PASS_THROUGH[$i]}" == --main-timeout-sec ]]; then
      return 0
    fi
  done
  return 1
}

run_batch_verify() {
  local root="$1"
  shift
  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "$PYTHON_EXE" "$BATCH_PY" --main-py "$MAIN_PY" --batch-root "$root" "$@" "${PASS_THROUGH[@]}"
    return 0
  fi
  if ! "$PYTHON_EXE" "$BATCH_PY" --main-py "$MAIN_PY" --batch-root "$root" "$@" "${PASS_THROUGH[@]}"; then
    return 1
  fi
  return 0
}

EXTRA_TIMEOUT=()
if ! _pass_has_main_timeout; then
  EXTRA_TIMEOUT=(--main-timeout-sec "$BATCH_MAIN_TIMEOUT_SEC")
fi

for name in "${DIRS[@]}"; do
  root="$BAKED_ENVS/$name"
  [[ -d "$root" ]] || continue

  echo ""
  echo "========== 分类: $name =========="
  echo "  --main-py $MAIN_PY"
  echo "  --batch-root $root"

  if ! run_batch_verify "$root" "${EXTRA_TIMEOUT[@]}"; then
    echo "[批次失败] $name" >&2
    ((failed_batches++)) || true
  fi
  ((ran++)) || true
done

echo ""
echo "========== 汇总 =========="
echo "已处理分类数: $ran"
echo "失败批次数: $failed_batches"

if [[ "$failed_batches" -gt 0 ]]; then
  exit 1
fi
exit 0
