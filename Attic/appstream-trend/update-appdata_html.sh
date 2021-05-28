#!/bin/bash

pushd $(dirname $0)

#APPDATA="http://download.opensuse.org/tumbleweed/repo/oss/suse/setup/descr/appdata.html.xz"

#rm appdata.html appdata.html.xz
#curl $APPDATA > appdata.html.xz
#unxz appdata.html.xza

# We already downloaded the .xz file earlier and decompressed it
git commit appdata.html data.txt appdata-failed.html -m "appdata.html: $1"
