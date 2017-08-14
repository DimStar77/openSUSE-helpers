#!/bin/bash

# These are the versions defined by TTM - so that's what SHOULD be in place
totest=$(osc cat openSUSE:Factory:Staging/dashboard/version_totest)
snapshot=$(osc cat openSUSE:Factory:Staging/dashboard/version_snapshot)

# These are the version extracted from OBS directly
prjsnapshot=$(osc ls -b openSUSE:Factory _product:openSUSE-release snapshot x86_64 | awk -F- '/src.rpm/ {print $3}')
prjtotest=$(osc ls -b openSUSE:Factory _product:openSUSE-release totest x86_64 | awk -F- '/src.rpm/ {print $3}')

if [ $snapshot -gt $prjsnapshot -a $snapshot -eq $prjtotest ]; then
  # TTM triggered a publisher run of :ToTest - let's sync up /snapshot from /totest
  echo In need to sync /snapshot
  #obs_admin  --clone-repository openSUSE:Factory totest openSUSE:Factory snapshot
fi

if [ $totest -gt $prjtotest ]; then
  # TTM synced openSUSE:Factory/standard to openSUSE:Factory:ToTest
  # Let's initiate the sync from /standard to /totest
  echo in need to sync /totest
  #obs_admin  --clone-repository openSUSE:Factory standard openSUSE:Factory totest
fi

