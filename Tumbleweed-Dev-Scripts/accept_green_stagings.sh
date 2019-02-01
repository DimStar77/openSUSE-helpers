#!/bin/bash

if ! osc staging lock -m "Factory accept bot"; then
   echo "Cannot aquire lock - please try again later"
   exit 1
fi

today=$(date +%Y%m%d)
REGEXP="(<value>)\K.*(?=</value>)"

stdver=$(osc api /source/openSUSE:Factory/_attribute/OSRT:ProductVersion | grep -Po $REGEXP)

if [ $today -le $stdver ]; then
    echo "openSUSE:Factory has already been accepted today - current snapshot build: $stdver"
    osc staging unlock
    exit 1
fi

read snapshot openqa dirty <<< $(osc staging acheck | awk '{print $2" "$5" "$8}' )

if [ "$stdver" -ne "$openqa" ]; then
    echo "$stdver did not yet move over to openQA - please wait..."
    osc staging unlock
    exit 1
fi

if [ "$snapshot" != "$openqa" -o "$dirty" != "False" ]; then
  echo "openSUSE:Factory is not ready to accept submissions at this moment"
  exit 1
fi

osc check_dups openSUSE:Factory
osc staging --wipe-cache list --supersede
osc staging adi

echo Finding acceptable staging projects

ACCPRJS=$(/usr/bin/osc api /project/staging_projects/openSUSE:Factory?format=json  | jq '.[] | select(.overall_state=="acceptable") | select( .name | contains(":adi:") | not).name' | sed -e 's/"//g' -e 's/openSUSE:Factory:Staging://')

for prj in $ACCPRJS; do
  ACCPRJ="$ACCPRJ $prj"
done

if [ -z "$ACCPRJ" ]; then
  echo "No staging project to accept - skipping non-ring-only accept run"
  osc staging unlock
  exit 1
fi

echo "Acceptable projects${ACCPRJ}"

# we --force accept, as we only accept stagings that were green before
# it frequently happens that 'rings change' (think delete requests) and
# the scheduler marks a staging as 'dirty'/building, failing the original
# accept command
osc staging accept --force $ACCPRJ

osc staging unlock
