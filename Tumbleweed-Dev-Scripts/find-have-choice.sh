#!/bin/sh

for pkg in $(osc results openSUSE:Factory -a x86_64 -r standard -V -s U | awk '/^U/ {print $2}'); do
       	osc r -v openSUSE:Factory -r standard -a x86_64 $pkg | grep -q "have choice" && echo "Check unresolvable./have choice for $pkg";
done
