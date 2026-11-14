#!/bin/sh
# Minimal FFmpeg for the Windows build: only what the app runs (see domain/rip.py and dvdplayer/localmedia.py).
#   DVD title -> MKV (stream copy), WAV -> FLAC with a cover, FLAC -> Opus with an ffmetadata file,
#   ffprobe tags of flac/opus/ogg/mp3/m4a/wav, the cover picture pulled out of a track.
# A full build is ~200 MB of DLLs; this one is ~10 MB.
# Usage: build-ffmpeg.sh <source dir> <install prefix> [extra configure args...]
# LOCAL_TEST=1 drops the external libraries (libdvdnav/libdvdread/libopus) to check the list on a dev machine.
set -eu
src=$1 prefix=$2
shift 2

list() { echo "$*" | tr ' ' ','; }

PROTOCOLS="file pipe"
# mpegps finds out what its video stream is by probing it, which needs the raw mpegvideo demuxer.
DEMUXERS="dvdvideo mpegps mpegvideo wav flac ogg mp3 mov matroska image2 image_jpeg_pipe image_png_pipe ffmetadata"
MUXERS="matroska flac ogg opus image2 null"
# Decoders are only used to probe stream parameters (rips copy video/DVD audio) and to read FLAC for Opus.
DECODERS="mpeg1video mpeg2video ac3 eac3 dca mp1 mp2 mp3 pcm_dvd pcm_s16le pcm_s16be pcm_s24le dvdsub
  flac opus vorbis aac alac mjpeg png"
ENCODERS="flac"
PARSERS="mpegvideo ac3 dca mpegaudio dvdsub dvd_nav flac opus vorbis aac mjpeg png"
FILTERS="aresample aformat anull atrim format null trim"
EXTERNAL="--enable-libdvdnav --enable-libdvdread --enable-libopus"
if [ "${LOCAL_TEST:-}" = 1 ]; then
  DEMUXERS=$(echo "$DEMUXERS" | sed 's/dvdvideo //')
  EXTERNAL=""
else
  ENCODERS="$ENCODERS libopus"
fi

# The ffmpeg program needs threads, and --disable-autodetect would otherwise leave the choice to luck.
case "$(uname -s)" in
  MINGW* | MSYS*) THREADS="--enable-w32threads" ;;
  *) THREADS="--enable-pthreads" ;;
esac

cd "$src"
# shellcheck disable=SC2086
./configure \
  --prefix="$prefix" \
  --enable-gpl --enable-shared --disable-static \
  --disable-everything --disable-autodetect --disable-network \
  --disable-doc --disable-debug --disable-avdevice --disable-ffplay \
  --enable-zlib $THREADS $EXTERNAL \
  --enable-protocol="$(list $PROTOCOLS)" \
  --enable-demuxer="$(list $DEMUXERS)" \
  --enable-muxer="$(list $MUXERS)" \
  --enable-decoder="$(list $DECODERS)" \
  --enable-encoder="$(list $ENCODERS)" \
  --enable-parser="$(list $PARSERS)" \
  --enable-filter="$(list $FILTERS)" \
  "$@"
make -j"$(nproc)"
make install
