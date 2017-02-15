#!/bin/bash

today=$(date +%Y%m%d)

stdver=$(osc api /source/openSUSE:Factory/_product:openSUSE-release/openSUSE-release.spec | awk '/^Version/ {print $2}')

if [ $today -le $stdver ]; then
    echo "openSUSE:Factory has already been accepted today - latest snapshot build: $stdver"
    exit 1
fi

read snapshot openqa dirty <<< $(osc staging acheck | awk '{print $2" "$5" "$8}' )

if [ "$snapshot" != "$openqa" -o "$dirty" != "False" ]; then
  echo "openSUSE:Factory is not ready to accept submissions at this moment"
  exit 1
fi

osc staging list --supersede

echo Finding acceptable staging projects

for prj in {A..J}; do
  echo Checking project $prj
  if [ $(osc staging check $prj | grep -q "Acceptable staging project"; echo $?) -eq 0 ]; then
    ACCPRJ="$ACCPRJ $prj"
  fi
done

if [ -z "$ACCPRJ" ]; then
  echo "No staging project to accept - skipping non-ring-only accept run"
  exit 1
fi

echo "Acceptable projects${ACCPRJ}"

osc staging adi

osc staging accept $ACCPRJ
