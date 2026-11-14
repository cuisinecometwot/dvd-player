#!/bin/sh
# Removes VLC plugins the app never loads. It plays local discs and files through libvlc only: no VLC interface,
# no network or capture inputs, no streaming or encoding. Cuts the VLC folder from ~149 MB to ~65 MB.
# Usage: trim-vlc.sh <vlc plugins dir>
set -eu
plugins=$1

# Whole categories: VLC's own interfaces, stream output/encoding, network discovery, visualisations, scripts.
for dir in gui access_output stream_out mux visualization services_discovery lua video_splitter \
  stream_extractor keystore meta_engine; do
  rm -rf "${plugins:?}/$dir"
done

# Inputs: keep local discs and files only (this drops network protocols, Blu-ray, webcams, DVB, screen capture).
KEEP_ACCESS="filesystem dvdread dvdnav cdda vcd attachment idummy imem access_imem access_concat"
for f in "$plugins"/access/*.dll; do
  name=$(basename "$f" .dll)
  name=${name#lib}
  name=${name%_plugin}
  case " $KEEP_ACCESS " in
    *" $name "*) ;;
    *) rm -f "$f" ;;
  esac
done

rm -f "$plugins"/access/*.jar  # Blu-ray menu (Java) support, removed with its plugin above

# Encoders, broadcast/teletext/MIDI/game-music decoders, TLS, and other single plugins nothing here uses.
for name in \
  codec/x264 codec/x26410b codec/x265 codec/aom codec/qsv codec/twolame codec/zvbi codec/aribsub \
  codec/fluidsynth codec/crystalhd codec/scte18 codec/scte27 codec/t140 codec/rtpvideo \
  misc/gnutls misc/audioscrobbler misc/export misc/fingerprinter misc/vod_rtsp misc/addonsfsstorage \
  misc/addonsvorepository \
  demux/adaptive demux/sid demux/gme demux/mod demux/demux_chromecast demux/nsc demux/demuxdump \
  text_renderer/sapi; do
  rm -f "$plugins/$(dirname "$name")/lib$(basename "$name")_plugin.dll"
done
