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

openqa=$(osc  meta attribute openSUSE:Factory -a  OSRT:ToTestManagerStatus | awk '/testing/ {print $2}' | tr -d \')

if [ "$stdver" -ne "$openqa" ]; then
    echo "$stdver did not yet move over to openQA - please wait..."
    osc staging unlock
    exit 1
fi

osc staging --wipe-cache list --supersede
osc staging --wipe-cache list --supersede --project openSUSE:Factory:NonFree
osc staging adi
osc staging adi --project openSUSE:Factory:NonFree

# echo Finding acceptable staging projects

# ACCPRJS now including adi stagings
# ACCPRJS=$(/usr/bin/osc api /staging/openSUSE:Factory/staging_projects?status=1 | egrep 'staging_project name="openSUSE:Factory:Staging:.*" state="acceptable"' | egrep -o 'name="openSUSE:Factory:Staging:[^"]+' | sed 's/name="openSUSE:Factory:Staging://')

#for prj in $ACCPRJS; do
#  ACCPRJ="$ACCPRJ $prj"
#done
#
#if [ -z "$ACCPRJ" ]; then
#  echo "No staging project to accept - skipping non-ring-only accept run"
#  osc staging unlock
#  exit 1
#fi

# echo "Acceptable projects${ACCPRJ}"

# First accept NonFree; after accepting OSS, there is a chance that NonFree stagings already start building again
osc staging accept --project openSUSE:Factory:NonFree
osc staging accept

osc staging unlock
