#!/bin/sh
# Launch the DVD player from its venv (python-vlc lives there; GTK comes from system site-packages).
cd "$(dirname "$(readlink -f "$0")")" && exec .venv/bin/python -m dvdplayer "$@"
