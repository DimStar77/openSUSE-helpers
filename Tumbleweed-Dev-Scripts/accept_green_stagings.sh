#!/bin/bash

today=$(date +%Y%m%d)
read snapshot openqa dirty <<< $(osc staging acheck | awk '{print $2" "$5" "$8}' )

if [ $today -le $snapshot ]; then
    echo "openSUSE:Factory has already been accepted today - postponing"
    exit 1
fi


if [ "$snapshot" != "$openqa" -o "$dirty" != "False" ]; then
  echo "openSUSE:Factory is not ready to accept submissions at this moment"
  exit 1
fi


echo Finding acceptable staging projects

for prj in {A..J}; do
  echo Checking project $prj
  if [ $(osc staging check $prj | grep -q "Acceptable staging project"; echo $?) -eq 0 ]; then
    ACCPRJ="$ACCPRJ $prj"
  fi
done

echo "Acceptable projects would be ${ACCPRJ}"
