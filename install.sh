#!/bin/bash
# Install into the checkout without changing system packages or workspace data.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
    echo "用法：./install.sh（安装或更新依赖并构建网页）"
    exit 0
fi
if [[ $# -ne 0 ]]; then
    echo "安装脚本不接受参数；使用 ./install.sh --help 查看用法。" >&2
    exit 2
fi

trap 'echo "安装未完成，请解决上方错误后重新运行 ./install.sh。" >&2' ERR
for tool in python3.12 node npm ffmpeg ffprobe; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "缺少 ${tool}，请先安装 README 中的系统依赖并确保其位于 PATH。" >&2
        exit 1
    fi
done
if ! python3.12 -c 'import sys; sys.exit(sys.version_info[:2] != (3, 12))'; then
    echo "需要 Python 3.12，请检查 python3.12 命令。" >&2
    exit 1
fi
if ! node -e 'const [major, minor] = process.versions.node.split(".").map(Number); process.exit(major > 22 || (major === 22 && minor >= 12) ? 0 : 1)'; then
    echo "需要 Node.js 22.12 或更新版本。" >&2
    exit 1
fi

cd -- "$ROOT"
# Refuse to overwrite a foreign/broken environment; it may contain user data.
if [[ -e .venv || -L .venv ]]; then
    if [[ ! -x .venv/bin/python ]] || ! .venv/bin/python -c 'import sys; sys.exit(not (sys.version_info[:2] == (3, 12) and sys.prefix != sys.base_prefix))'; then
        echo ".venv 不是可用的 Python 3.12 虚拟环境；请先将其移走后重试。" >&2
        exit 1
    fi
else
    python3.12 -m venv .venv
fi

# npm's token-generation step must use the same Python as the backend.
export PATH="$ROOT/.venv/bin:$PATH"
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install -e . --no-deps
npm --prefix frontend ci --include=dev
npm --prefix frontend run build
echo "安装完成。运行 ./start.sh 启动 ChromaScript。"
