#!/usr/bin/env bash
# 对 workbench/baked_envs/ 下除 post_auth 外的每个子目录依次执行 batch_verify_targets.py。
#
# 用法:
#   ./run_baked_envs_except_post_auth.sh
#   ./run_baked_envs_except_post_auth.sh --source-smoke-limits --copy-session
#   ./run_baked_envs_except_post_auth.sh --recursive --main-timeout-sec 3600
#
# 说明:
#   - 每个分类目录单独一条 batch，metrics 默认写入 workbench/data/<分类名>/
#   - 传入 batch_verify 的参数请放在本脚本之后（勿再传 --batch-root，由脚本按目录填入）
#   - --source-smoke-limits 会 source 同目录下的 env_batch_limits_from_smoke.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKBENCH="$(cd "$SCRIPT_DIR/../.." && pwd)"
BAKED_ENVS="${BAKED_ENVS:-$WORKBENCH/baked_envs}"
BATCH_PY="$SCRIPT_DIR/batch_verify_targets.py"
EXCLUDE_NAME="post_auth"

SOURCE_SMOKE=0
DRY_RUN=0
PASS_THROUGH=()

usage() {
  sed -n '1,20p' "$0" | tail -n +2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source-smoke-limits)
      SOURCE_SMOKE=1
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      PASS_THROUGH+=("$1")
      shift
      ;;
  esac
done

if [[ ! -d "$BAKED_ENVS" ]]; then
  echo "错误: 找不到 baked_envs 目录: $BAKED_ENVS" >&2
  exit 2
fi

if [[ ! -f "$BATCH_PY" ]]; then
  echo "错误: 找不到 batch_verify_targets.py: $BATCH_PY" >&2
  exit 2
fi

if [[ "$SOURCE_SMOKE" -eq 1 ]]; then
  # shellcheck source=/dev/null
  source "$SCRIPT_DIR/env_batch_limits_from_smoke.sh"
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

for name in "${DIRS[@]}"; do
  [[ "$name" == "$EXCLUDE_NAME" ]] && continue
  root="$BAKED_ENVS/$name"
  [[ -d "$root" ]] || continue

  echo ""
  echo "========== 分类: $name =========="
  echo "  --batch-root $root"

  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "$PYTHON_EXE" "$BATCH_PY" --batch-root "$root" "${PASS_THROUGH[@]}"
    ((ran++)) || true
    continue
  fi

  if ! "$PYTHON_EXE" "$BATCH_PY" --batch-root "$root" "${PASS_THROUGH[@]}"; then
    echo "[批次失败] $name" >&2
    ((failed_batches++)) || true
  fi
  ((ran++)) || true
done

echo ""
echo "========== 汇总 =========="
echo "已处理分类数（不含 $EXCLUDE_NAME）: $ran"
echo "失败批次数: $failed_batches"

if [[ "$failed_batches" -gt 0 ]]; then
  exit 1
fi
exit 0
