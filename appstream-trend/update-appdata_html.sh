#!/bin/bash

pushd $(dirname $0)

APPDATA="http://download.opensuse.org/tumbleweed/repo/oss/suse/setup/descr/appdata.html.xz"

rm appdata.html appdata.html.xz
curl $APPDATA > appdata.html.xz
unxz appdata.html.xz
git commit appdata.html data.txt -m "appdata.html: $1"
