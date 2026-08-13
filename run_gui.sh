#!/usr/bin/env bash
set -euo pipefail

# Always run from the script's directory, no matter where it is called from
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

UV_EXE=""

find_uv() {
    if command -v uv >/dev/null 2>&1; then
        UV_EXE="$(command -v uv)"
        return 0
    fi
    if [[ -x "${HOME}/.local/bin/uv" ]]; then
        UV_EXE="${HOME}/.local/bin/uv"
        export PATH="${HOME}/.local/bin:${PATH}"
        return 0
    fi
    if [[ -x "${HOME}/.cargo/bin/uv" ]]; then
        UV_EXE="${HOME}/.cargo/bin/uv"
        export PATH="${HOME}/.cargo/bin:${PATH}"
        return 0
    fi
    return 1
}

if ! find_uv; then
    echo "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="${HOME}/.local/bin:${PATH}"
    if [[ -x "${HOME}/.local/bin/uv" ]]; then
        UV_EXE="${HOME}/.local/bin/uv"
    elif [[ -x "${HOME}/.cargo/bin/uv" ]]; then
        UV_EXE="${HOME}/.cargo/bin/uv"
    elif command -v uv >/dev/null 2>&1; then
        UV_EXE="$(command -v uv)"
    else
        echo "uv was not found after installation." >&2
        exit 1
    fi
fi

if [[ ! -x ".venv/bin/python" ]]; then
    echo "Creating virtual environment..."
    "$UV_EXE" sync
fi

exec "$UV_EXE" run suno_download_gui.py
