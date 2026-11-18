#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Builds sigrok-cli with libsigrokdecode at the commit that flex/PREREG.md section 2 pins, into PREFIX ($1).
# The decoders that sigrok-cli runs are then those of that commit (PREFIX/share/libsigrokdecode/decoders), not the
# ones of the distribution package (libsigrokdecode 0.5.3, 2020). libsigrok (the VCD input) is the Ubuntu package.
# Use: flex/sigrok_build.sh PREFIX; then run PREFIX/bin/sigrok-cli with LD_LIBRARY_PATH=PREFIX/lib.
set -euo pipefail
PREFIX=${1:-$HOME/sigrok}
LSD=71f451443029322d57376214c330b518efd84f88   # libsigrokdecode, flex/PREREG.md section 2
CLI=b584f959edb788f1731d5a304badf241ac21bf65   # sigrok-cli tag sigrok-cli-0.7.2
sudo apt-get update -y
sudo apt-get install -y build-essential autoconf autoconf-archive automake libtool pkg-config libglib2.0-dev \
  python3-dev libsigrok-dev git
W=$(mktemp -d)
git clone -q https://github.com/sigrokproject/libsigrokdecode.git "$W/lsd"
git -C "$W/lsd" checkout -q "$LSD"
(cd "$W/lsd" && ./autogen.sh && ./configure --prefix="$PREFIX" --disable-static && make -j"$(nproc)" && make install)
git clone -q https://github.com/sigrokproject/sigrok-cli.git "$W/cli"
git -C "$W/cli" checkout -q "$CLI"
(cd "$W/cli" && ./autogen.sh && PKG_CONFIG_PATH="$PREFIX/lib/pkgconfig" ./configure --prefix="$PREFIX" \
  && make -j"$(nproc)" && make install)
printf 'libsigrokdecode %s\nsigrok-cli %s\n' "$LSD" "$CLI" > "$PREFIX/PINS"
cat "$PREFIX/PINS"
