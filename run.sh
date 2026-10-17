#!/usr/bin/env bash
# 一键启动：
#
#   ./run.sh                 起网页版（锻造蓝图 → 审阅 → 对谈）
#   ./run.sh --mock          起网页版，页面默认勾上模拟模式（不花钱）
#   ./run.sh --port 9000     起网页版，换端口
#
#   ./run.sh forge ...       透传给命令行：duo-forge forge ...
#   ./run.sh run ...         透传给命令行：duo-forge run ...
#   ./run.sh show ...        透传给命令行：duo-forge show ...
#
# 不管在哪个目录下调用都没问题，脚本会自己切回项目根。
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")"

# --- 前置检查 ---------------------------------------------------------------

if ! command -v uv >/dev/null 2>&1; then
  cat >&2 <<'MSG'
✗ 没找到 uv，装一下：

    curl -LsSf https://astral.sh/uv/install.sh | sh

装完重开一个终端（或 source ~/.bashrc）再试。
MSG
  exit 1
fi

has_key() { grep -qE '^DEEPSEEK_API_KEY=.+' .env 2>/dev/null; }
has_mock() {
  local arg
  for arg in "$@"; do
    [[ "$arg" == "--mock" ]] && return 0
  done
  return 1
}

# --- CLI 模式 ---------------------------------------------------------------
# 第一个参数是子命令时透传。show 只读 JSON，永远不需要 key；
# forge / run 在非 mock 时需要 key，缺了就当场说清楚，别等模型调用失败。

if [[ $# -gt 0 && ( "$1" == "forge" || "$1" == "run" || "$1" == "show" ) ]]; then
  if [[ "$1" != "show" ]] && ! has_mock "$@" && ! has_key; then
    cat >&2 <<'MSG'
✗ 没有找到 DEEPSEEK_API_KEY。

  1) cp .env.example .env
  2) 把 DeepSeek 的 key（sk- 开头）填进 .env

想先不花钱跑通流程：./run.sh --mock（网页版）
或者：            ./run.sh forge --purpose "..." --mock
MSG
    exit 1
  fi
  exec uv run duo-forge "$@"
fi

# --- 网页版 -----------------------------------------------------------------
# 没有 key 也照常起：用户还能在页面里勾「模拟模式」。只提醒一句。

if ! has_key; then
  echo "提示：.env 里没有 DEEPSEEK_API_KEY。可以先勾「模拟模式」试跑；" >&2
  echo "      要生成真实内容时，按 .env.example 填好 key 再重启。" >&2
  echo >&2
fi

exec uv run python launcher.py "$@"
