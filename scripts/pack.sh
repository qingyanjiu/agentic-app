#!/usr/bin/env bash
# 打包项目源码：不含 .git、BGE 模型、缓存和运行时产物。
#
# 用法：
#   ./scripts/pack.sh                 # 输出到项目根 agentic-app-src-<时间>.zip
#   ./scripts/pack.sh /tmp/app.zip    # 指定输出路径
#
# 为什么排除模型（92M）：
#   agent/intent/models/BAAI_bge-small-zh-v1.5/ 是意图分类用的 BGE 权重，
#   占仓库八成体积，而且是二进制、几乎压不动（91M 压完还是 91M）。
#   目标机器上按下面任一方式补：
#     1) 从原环境拷：
#        scp -r agent/intent/models/BAAI_bge-small-zh-v1.5 \
#              目标机:项目路径/agent/intent/models/
#     2) 让 sentence-transformers 自动下载（目标机能访问 huggingface）
#        代码里的模型名是 "BAAI/bge-small-zh-v1.5"，
#        本地目录不存在时首次加载会走网络。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAMP="$(date +%Y%m%d-%H%M)"
OUT="${1:-$ROOT/agentic-app-src-$STAMP.zip}"

# 相对路径转绝对路径（避免 cd 之后写错位置）
case "$OUT" in
    /*) ;;
    *) OUT="$(pwd)/$OUT" ;;
esac

cd "$ROOT"

command -v zip >/dev/null 2>&1 || {
    echo "缺少 zip 命令。macOS 自带；Debian/Ubuntu 用 apt install zip" >&2
    exit 1
}

# 排除清单。注意 models/、memory/ 里现在是 Python 代码（models/llm.py、memory/session_state.py），
# 要保留；要排除的只有 agent/intent/models/ 这个权重目录和运行时产物。
EXCLUDES=(
    ".git/*"                     # 版本库（含 68M 历史）
    "agent/intent/models/*"      # BGE 权重 92M
    "*/__pycache__/*"
    "__pycache__/*"
    "*.pyc"
    "*.pyo"
    ".pytest_cache/*"
    ".DS_Store"
    ".claude/*"
    ".tmp_stubs/*"
    ".vscode/*"
    "exports/*"                  # 运行产物（导出的 docx 等）
    "uploads/*"                  # 图片上传已移除；留规则防止日后又生成运行时目录
    "reports/*"                  # 报告下载已移除，同上
    # memory/ 里只有 session_state.py 和 chat_store.py 是代码；
    # 对话历史落在项目根的 chat_history.db（SQLite），属于运行时数据
    "chat_history.db*"
    "memory_store.db"
    "memory_store.json"
    "app.log"
    ".env"                       # 密钥
    ".env.*"
    "global_config.json"
    "*.zip"
)

ARGS=()
for pattern in "${EXCLUDES[@]}"; do
    ARGS+=(-x "$pattern")
done

rm -f "$OUT"
zip -r -q "$OUT" . "${ARGS[@]}"

echo "已生成：$OUT"
echo "大小：  $(du -h "$OUT" | cut -f1)"
echo "文件数：$(unzip -Z1 "$OUT" | grep -vc '/$')"

# 自检：确认敏感文件和大目录没有混进去
echo
echo "自检："
failed=0
for probe in ".git/" "agent/intent/models/" ".env" "global_config.json" \
    "memory_store.db" "exports/"; do
    if unzip -Z1 "$OUT" | grep -q "^$probe"; then
        echo "  [x] 仍包含 $probe"
        failed=1
    else
        echo "  [ok] 已排除 $probe"
    fi
done

# 反向自检：必需的代码包必须在包里（部署后启动即 import，缺一个就崩）。
# 踩过的坑："memory/*" 曾被当成运行时数据整目录排除，线上直接
# ModuleNotFoundError: No module named 'memory'。
LISTING="$(unzip -Z1 "$OUT" || true)"
for need in "memory/session_state.py" "memory/chat_store.py" \
    "agent/loop/agent.py" "agent/loop/ws_bridge.py" \
    "mcp_client/knowledge_http_client.py" "mcp_client/knowledge_mcp_client.py" \
    "models/llm.py" "app.py"; do
    if printf '%s\n' "$LISTING" | grep -qx "$need"; then
        echo "  [ok] 包含 $need"
    else
        echo "  [x] 缺少 $need（检查 EXCLUDES 是否误排了代码目录）"
        failed=1
    fi
done

# 模型不在包里是预期行为，提示一下
echo
if unzip -Z1 "$OUT" | grep -q "^agent/intent/models/"; then
    echo "警告：模型被打进去了，检查排除规则" >&2
    failed=1
else
    echo "提示：BGE 模型未打包，部署时需单独放置或让它自动下载（见脚本开头注释）"
fi

exit "$failed"
