#!/bin/bash

if [ -z "$1" ]; then
  PROJECT=openSUSE:Factory
else
  PROJECT=$1
fi

for prj in {A..J}; do
  osc rebuildpac -f ${PROJECT}:Staging:${prj}
  osc rebuildpac -f ${PROJECT}:Staging:${prj}:DVD
done

