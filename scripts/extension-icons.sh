#!/bin/sh
# Renders the extension's icons from the tile: icon.svg is the xrnmoni tile itself, icon-16.svg its
# cut for small sizes (a thinner stripe, a larger screen). Every size a browser may draw, so each
# slot takes the next size up and scales it down: Chromium on Android draws the toolbar button at
# 20 dp, 55 px on a 2.75x phone.
#
#     scripts/extension-icons.sh        (needs rsvg-convert, from librsvg)
set -eu
cd "$(dirname "$0")/../extension/icons"
for s in 16 24; do rsvg-convert -w "$s" -h "$s" icon-16.svg -o "icon$s.png"; done
for s in 32 48 64 96 128 256; do rsvg-convert -w "$s" -h "$s" icon.svg -o "icon$s.png"; done
