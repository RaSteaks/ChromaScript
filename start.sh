#!/bin/bash
# Reuse the installed environment; starting the app never downloads dependencies.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
workspace="$ROOT/workspace"
serve_args=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --help|-h)
            echo "用法：./start.sh [--workspace 路径] [--port 8765] [--export-root 路径]"
            echo "默认数据目录为仓库内的 workspace；按 Ctrl+C 停止服务。"
            exit 0
            ;;
        --workspace|-w)
            if [[ $# -lt 2 || -z "$2" || "$2" == -* ]]; then
                echo "$1 需要一个工作区路径。" >&2
                exit 2
            fi
            workspace="$2"
            shift 2
            ;;
        --workspace=*)
            workspace="${1#*=}"
            if [[ -z "$workspace" ]]; then
                echo "--workspace 需要一个工作区路径。" >&2
                exit 2
            fi
            shift
            ;;
        *)
            serve_args+=("$1")
            shift
            ;;
    esac
done

if [[ ! -x "$ROOT/.venv/bin/python" || ! -f "$ROOT/.venv/bin/chromascript" || ! -f "$ROOT/frontend/dist/index.html" ]]; then
    echo "安装不完整，请先运行 $ROOT/install.sh。" >&2
    exit 1
fi

# Keep the caller's cwd for explicit relative paths, but anchor the default workspace
# to the checkout. exec forwards Ctrl+C and the server's exit status directly.
# Guard an empty array for macOS Bash 3.2, where nounset rejects a plain expansion.
exec "$ROOT/.venv/bin/python" -m chromascript.cli --workspace "$workspace" serve "${serve_args[@]+"${serve_args[@]}"}"
