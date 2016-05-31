#~/bin/sh

if [ -z "$1" ]; then
  echo "No project defined - will not be able to perform the cleanup"
  exit 1
fi

PKGSTOKEEP="Test-DVD-x86_64 Test-DVD-ppc64le bootstrap-copy"

for PERLKEEP in $PKGSTOKEEP; do
  PERLCMD="$PERLCMD -e $PERLKEEP"
done

while [ ! -z "$1" ]; do

  for PRJ in $1 $1:DVD; do
    for pkg in $(osc ls openSUSE:Factory:Staging:$PRJ | grep -v $PERLCMD); do
      osc rdelete openSUSE:Factory:Staging:$PRJ $pkg -m 'auto cleanup'
    done
  done

  shift
done

