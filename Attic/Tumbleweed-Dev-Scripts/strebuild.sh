#!/bin/bash

if [ -z "$1" ]; then
  PROJECT=openSUSE:Factory
else
  PROJECT=$1
fi

for prj in {A..J}; do
  for sub in {"",:DVD}; do
    osc rebuildpac -f ${PROJECT}:Staging:${prj}${sub}
  done
done

