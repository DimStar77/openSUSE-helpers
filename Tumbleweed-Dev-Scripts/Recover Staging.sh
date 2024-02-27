#!/bin/sh
# Recovering a staging project after wiping all binaries:

if [ -z "$1" ]; then
  echo "Call as $0 <LETTER> - e.g $0 L to recover openSUSE:Factory:Staging:L"
  exit 1
fi

osc copypac -e openSUSE:Factory:Rings:0-Bootstrap rpmlint-mini-AGGR openSUSE:Factory:Staging:$1
osc aggregatepac openSUSE:Factory polkit-default-privs openSUSE:Factory:Staging:$1
osc aggregatepac openSUSE:Factory ovmf openSUSE:Factory:Staging:$1

# bootstrapping rust versions
for RUST in $(osc ls openSUSE:Factory:Rings:1-MinimalX | grep -P 'rust1.\d+$'); do
  osc aggregatepac openSUSE:Factory $RUST openSUSE:Factory:Staging:$1 $RUST-x86_64
  osc aggregatepac openSUSE:Factory:LegacyX86 $RUST openSUSE:Factory:Staging:$1 $RUST-i586
done

# bootstrapping java versions
for JAVA in $(osc ls openSUSE:Factory:Rings:1-MinimalX | grep -P 'java.*openjdk$'); do
  osc aggregatepac openSUSE:Factory $JAVA openSUSE:Factory:Staging:$1 $JAVA-x86_64
  osc aggregatepac openSUSE:Factory:LegacyX86 $JAVA openSUSE:Factory:Staging:$1 $JAVA-i586
done

