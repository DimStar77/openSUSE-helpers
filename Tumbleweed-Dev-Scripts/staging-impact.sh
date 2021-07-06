IFS=/ read PRJ PKG REPO ARCH dump <<< "$1"
if [ -z "$PRJ" -o -z "$PKG" -o -z "$REPO" -o -z "$ARCH" ]; then
  PRJ=$1
  PKG=$2
  REPO=$3
  ARCH=$4
fi

echo ${PRJ}/${PKG}:
for pkg in $(osc dependson $PRJ $PKG $REPO $ARCH | grep "^   " | awk -F: '{print $1}'); do
    osc api /source/${PRJ}/$pkg?view=info | grep -q "originproject" && echo -n '   ' || echo -n ' * '
    echo ${pkg}
done
