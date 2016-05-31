#!/bin/bash

for prj in {A..J}; do
  osc rebuildpac -f openSUSE:Factory:Staging:${prj}
  osc rebuildpac -f openSUSE:Factory:Staging:${prj}:DVD
done

