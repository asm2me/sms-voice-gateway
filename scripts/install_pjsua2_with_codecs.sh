#!/usr/bin/env bash
set -euo pipefail

# Installs/builds PJPROJECT + Python pjsua2 bindings with external G.729 support
# on Debian/Ubuntu systems. G.723.1 is only enabled when an external codec
# implementation is available on the system at build time.
#
# Usage:
#   sudo bash scripts/install_pjsua2_with_codecs.sh
#
# Optional environment variables:
#   PJPROJECT_VERSION=2.14.1
#   PREFIX=/usr/local
#   WITH_G729=1
#   WITH_G7231=0
#   PYTHON_BIN=python3
#   JOBS=4

PJPROJECT_VERSION="${PJPROJECT_VERSION:-2.14.1}"
PREFIX="${PREFIX:-/usr/local}"
WITH_G729="${WITH_G729:-1}"
WITH_G7231="${WITH_G7231:-0}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
JOBS="${JOBS:-$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 2)}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this script as root (sudo)." >&2
  exit 1
fi

if ! command -v apt-get >/dev/null 2>&1; then
  echo "This script currently supports Debian/Ubuntu systems with apt-get." >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive

apt-get update
apt-get install -y --no-install-recommends \
  build-essential \
  ca-certificates \
  curl \
  git \
  pkg-config \
  autoconf \
  automake \
  libtool \
  make \
  cmake \
  libasound2-dev \
  libssl-dev \
  libopus-dev \
  libspeex-dev \
  libspeexdsp-dev \
  libgsm1-dev \
  python3-dev \
  python3-pip \
  python3-setuptools \
  python3-wheel \
  libsox-dev || true

if apt-cache show libilbc-dev >/dev/null 2>&1; then
  apt-get install -y --no-install-recommends libilbc-dev
elif apt-cache show libilbc2 >/dev/null 2>&1; then
  apt-get install -y --no-install-recommends libilbc2
else
  echo "libilbc package not available in this distribution; continuing without it."
fi

WORKDIR="/usr/local/src/pjsip-build"
rm -rf "${WORKDIR}"
mkdir -p "${WORKDIR}"
cd "${WORKDIR}"

if [[ "${WITH_G729}" == "1" ]]; then
  if [[ ! -d bcg729 ]]; then
    git clone --depth 1 https://github.com/BelledonneCommunications/bcg729.git
  fi
  cmake -S bcg729 -B bcg729/build -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="${PREFIX}"
  cmake --build bcg729/build -j"${JOBS}"
  cmake --install bcg729/build
  ldconfig
fi

if [[ ! -d pjproject ]]; then
  curl -fsSL "https://github.com/pjsip/pjproject/archive/refs/tags/${PJPROJECT_VERSION}.tar.gz" -o pjproject.tar.gz
  tar -xzf pjproject.tar.gz
  mv "pjproject-${PJPROJECT_VERSION}" pjproject
fi

cd pjproject

cat > user.mak <<EOF
export CFLAGS += -fPIC
export CXXFLAGS += -fPIC
export LDFLAGS +=
export PJ_AUTOCONF=1
export PJSUA_HAS_VIDEO=0
export PJMEDIA_HAS_SPEEX_AEC=1
export PJMEDIA_HAS_G711_CODEC=1
export PJMEDIA_HAS_GSM_CODEC=1
export PJMEDIA_HAS_ILBC_CODEC=1
export PJMEDIA_HAS_SPEEX_CODEC=1
export PJMEDIA_HAS_OPUS_CODEC=1
export PJMEDIA_CODEC_MAX_SILENCE_PERIOD=-1
EOF

if [[ "${WITH_G729}" == "1" ]]; then
  cat >> user.mak <<EOF
export BCG729_PREFIX=${PREFIX}
export PJMEDIA_HAS_BCG729=1
EOF
fi

if [[ "${WITH_G7231}" == "1" ]]; then
  cat >> user.mak <<EOF
export PJMEDIA_HAS_G723_1_CODEC=1
EOF
fi

./configure --prefix="${PREFIX}" --enable-shared
make dep
make -j"${JOBS}"
make install
ldconfig

PYTHON_INCLUDE="$(${PYTHON_BIN} - <<'PY'
import sysconfig
print(sysconfig.get_paths()["include"])
PY
)"

PYTHON_LIBDIR="$(${PYTHON_BIN} - <<'PY'
import sysconfig
print(sysconfig.get_config_var("LIBDIR") or "")
PY
)"

make -C pjsip-apps/src/swig python \
  PYTHON="${PYTHON_BIN}" \
  PYTHON_INCLUDE="${PYTHON_INCLUDE}" \
  PYTHON_LIBDIR="${PYTHON_LIBDIR}" \
  USE_PYTHON=1

if [[ -d pjsip-apps/src/swig/python ]]; then
  cd pjsip-apps/src/swig/python
  "${PYTHON_BIN}" setup.py build
  "${PYTHON_BIN}" setup.py install
fi

ldconfig

echo
echo "Installed pjproject ${PJPROJECT_VERSION} with Python pjsua2 bindings."
echo "Requested codec flags:"
echo "  G729  : ${WITH_G729}"
echo "  G723.1: ${WITH_G7231}"
echo
echo "Verify with:"
echo "  ${PYTHON_BIN} -c \"import pjsua2 as pj; print('pjsua2 ok', pj)\""
echo
echo "Then restart the gateway service/container and inspect codecEnum2() / SIP SDP again."
echo "The installer intentionally builds only the Python SWIG target and skips Java/JDK-dependent targets."
echo
echo "Note: G.723.1 support depends on an available codec implementation in the build environment."
echo "If your pjproject build still does not expose G723.1, you need a compatible external codec library/toolchain."
