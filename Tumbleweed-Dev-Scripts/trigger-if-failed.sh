old_IFS=$IFS
IFS=/ read PRJ PKG REPO ARCH dump <<< "$1"
IFS=$old_IFS

if [ -n "$PRJ" -a -n "$PKG" -a -n "$REPO" -a -n "$ARCH" ]; then
  DELAY=$2
else
  PRJ=$1
  PKG=$2
  REPO=$3
  ARCH=$4
  DELAY=$5
fi
retry=0

if [ -z "$PRJ" -o -z "$PKG" -o -z "$REPO" -o -z "$ARCH" ]; then
  echo " USAGE"
  echo " ====="
  echo "$0 needs to know what project/package/repo/arch to test/trigger"
  echo "Call it as $0 <PRJ> <PKG> <REPO> <ARCH> [<DELAY>]"
  echo "or alternatively (to make copy/paste easier)"
  echo "$0 <PRG>/<PKG>/<REPO>/<ARCH> [<DELAY>]"
  echo "The 2nd form allows to copy/paste part of OBS' build log URL"
  exit 1
fi

if [ -z "$DELAY" ]; then
	DELAY=60
fi

while (sleep $DELAY); do

	result=$(osc --no-keyring results --no-multibuild $PRJ $PKG -r $REPO -a $ARCH --csv | awk -F\| '{print $5}' )

	case "$result" in
		"failed")
			if echo $result | grep -i 'failed$' > /dev/null; then
				  echo -n -e "$(date) - attempt\t$[retry +1]: "
				  osc --no-keyring rebuildpac $PRJ $PKG $REPO $ARCH
			fi
			retry=$[$retry + 1]
			;;	
		"succeeded")
			echo "Package $PRJ/$PKG successfully built for $REPO/$ARCH after $retry tries"
			break
			;;
	esac
done
