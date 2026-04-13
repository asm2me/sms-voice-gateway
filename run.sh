#!/usr/bin/env bash
# Quick start for development (without Docker).
# Requires Python 3.10+ and a running Redis instance.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Copy env if not present
if [ ! -f .env ]; then
    cp .env.example .env
    echo "[!] Created .env from .env.example – please edit it before running."
    exit 1
fi

# Create virtual environment if missing
if [ ! -d .venv ]; then
    python3 -m venv .venv
    echo "[+] Created virtualenv"
fi

source .venv/bin/activate
pip install -q --upgrade pip
pip install -q -r requirements.txt

install_pjsua2_from_source() {
    local build_root="${SCRIPT_DIR}/.cache/pjsip"
    local src_dir="${build_root}/pjproject"
    local version="${PJSIP_VERSION:-2.14.1}"
    local archive="${build_root}/pjproject-${version}.tar.gz"
    local url="https://github.com/pjsip/pjproject/archive/refs/tags/${version}.tar.gz"

    mkdir -p "${build_root}"

    if command -v apt-get >/dev/null 2>&1; then
        echo "[+] Installing system packages required for PJSUA2 source builds..."
        apt-get update -y
        apt-get install -y build-essential pkg-config libasound2-dev libssl-dev libopus-dev libvpx-dev libavcodec-dev libavformat-dev libswscale-dev python3-dev python3-setuptools python3-dev swig wget tar make
    fi

    if [ ! -d "${src_dir}" ]; then
        echo "[+] Downloading pjproject ${version}..."
        rm -f "${build_root}"/pjproject-*.tar.gz
        rm -rf "${src_dir}" "${build_root}/pjproject-${version}"
        wget -O "${archive}" "${url}"
        tar -xzf "${archive}" -C "${build_root}"
        mv "${build_root}/pjproject-${version}" "${src_dir}"
    fi

    cd "${src_dir}"
    echo "[+] Building PJSIP/PJSUA2 Python bindings from source..."
    ./configure --prefix="${src_dir}/build"
    make dep
    make -j"$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 1)"
    make install

    cd "${src_dir}/pjsip-apps/src/swig"
    make
    cd "${src_dir}/pjsip-apps/src/swig/python"

    if [ ! -f "pjsua2.py" ] || [ ! -f "pjsua2_wrap.cpp" ]; then
        echo "[!] SWIG wrapper generation failed: missing pjsua2.py or pjsua2_wrap.cpp"
        return 1
    fi

    python setup.py install
    cd "${SCRIPT_DIR}"
}

# Install PJSUA2 for live SIP registration tests.
if ! python -c "import pjsua2" >/dev/null 2>&1; then
    echo "[+] Installing pjsua2..."
    pip install -q pjsua2 || install_pjsua2_from_source || {
        echo "[!] Unable to install pjsua2 automatically."
        echo "[!] Provide a compatible wheel or install from a supported PJSIP source build."
    }
fi

# Ensure audio cache directory exists
mkdir -p audio_cache

echo "[+] Starting SMS Voice Gateway..."
uvicorn app.main:app \
    --host "${HOST:-0.0.0.0}" \
    --port "${PORT:-8000}" \
    --reload \
    --log-level info
