#!/bin/sh

function number_of_tokens() {
  echo $#;
}

if [ -z "$1" ]; then
  echo "You need to specify the package you want to trigger reverse dependencies for"
  exit
fi

PKGLIST=$(osc whatdependson openSUSE:Factory $1 standard x86_64 | grep -v "^$1")

NUM_TRIGGER=$(number_of_tokens $PKGLIST)

if dialog --title "Trigger build of ${NUM_TRIGGER} packages?" --yesno "The package $1 triggers ${NUM_TRIGGER} packages to rebuild. Do you want to start them?" 0 0; then
	echo "You selected to trigger the whole package list... starting."
	i=1
	for PKG in $PKGLIST; do
		echo -n "[$i/${NUM_TRIGGER}] - $PKG: "
		osc rebuildpac openSUSE:Factory $PKG -r standard
		i=$(($i + 1))
	done
else
	echo
	echo 'You decided not to go ahead'
fi

