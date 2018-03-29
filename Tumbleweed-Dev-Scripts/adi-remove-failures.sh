#!/bin/bash

# Scans an adi Staging and unselects everything currently failing or unresolvable, leaving only 'successful' packages
# in the staging. This can help for adi that are completely reviewed, but blocked by some failing package
# The failed ones will move to a random new adi staging

if [ -z "$1" ]; then
	echo "Need to be called as $0 <n> - with <n> referencing the number of the adi project to process"
	exit
fi

osc prjresults openSUSE:Factory:Staging:adi:$1 -V | awk '/^[FU] / {print $NF}' | xargs -r osc staging adi --move
