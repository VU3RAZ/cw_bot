#!/bin/sh
# Build the FT8 decoder on first run, then start the web server.
cd "$(dirname "$0")"
if [ ! -x vendor/ft8_lib/decode_ft8 ]; then
  [ -d vendor/ft8_lib ] || git clone --depth 1 https://github.com/kgoba/ft8_lib.git vendor/ft8_lib
  make -C vendor/ft8_lib -j4 decode_ft8 || exit 1
fi
exec python3 server.py "$@"
