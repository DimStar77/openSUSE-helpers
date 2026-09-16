#!/bin/bash

IMG=$1

if [ -z "$IMG" ]; then
  echo "No paramter specified, assuming gnome"
  IMG=gnome
fi

osc api /build/openSUSE:Factory:Live/images/x86_64/livecd-tumbleweed-$IMG

