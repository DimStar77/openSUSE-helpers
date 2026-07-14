#!/bin/bash

if ! type -p devel_update.sh 2>/dev/null; then
  echo '*****************************************'
  echo 'devel_update.sh script not found in $PATH'
  echo.
  echo 'We cannot update the devel_packages file for'
  echo 'new packages, which results in those packages'
  echo 'not being imported to git'
  echo '*****************************************'
  echo 'For now, this is a warning only'
fi

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

if type -p devel_update.sh 2>/dev/null; then
  MKTEMP=$(mktemp -d)
  pushd $MKTEMP
    git clone gitea@src.opensuse.org:openSUSE/Factory.git
    pushd Factory/pkgs/_meta
      devel_update.sh syncnewpackages
      git commit devel_packages -m "Update devel project for new packages on $today"
      git push
    popd
  popd
  rm -rf "$MKTEMP"
fi

osc staging --wipe-cache list --supersede
osc staging --wipe-cache list --supersede --project openSUSE:Factory:NonFree
osc staging adi
osc staging adi --project openSUSE:Factory:NonFree

# First accept NonFree; after accepting OSS, there is a chance that NonFree stagings already start building again
osc staging accept --project openSUSE:Factory:NonFree
osc staging accept

osc staging unlock
