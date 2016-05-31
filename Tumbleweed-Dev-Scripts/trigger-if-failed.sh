
PRJ=$1
PKG=$2
REPO=$3
ARCH=$4
DELAY=$5
retry=0

if [ -z "$DELAY" ]; then
	DELAY=60
fi

while (sleep $DELAY); do

	result=$(osc --no-keyring results $PRJ $PKG -r $REPO -a $ARCH --csv | awk -F\| '{print $5}' )

	case "$result" in
		"failed")
			if echo $result | grep -i 'failed$' > /dev/null; then
				  echo -n -e "$(date) - attempt\t$[retry +1]: "
				  osc --no-keyring rebuildpac $PRJ $PKG $REPO $ARCH
			fi
			retry=$[$retry + 1]
			;;	
		"succeeded")
			echo "Package $PRJ/$PKG successfully built for $REPO/$ARCJ after $retry tries"
			break
			;;
	esac
done
