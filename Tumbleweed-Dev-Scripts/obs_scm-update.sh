

if [ ! -d .osc ]; then
  echo "This seems not to be an osc checkout - aborting"
  exit 1
fi

if [ -z "$1" ]; then
  REV="@PARENT_TAG@"
else
  REV="$1"
fi

if [ ! -f _service ]; then
  echo "Package has not been converted to a _service managed package"
  exit 3
fi

PKGNAME=$(awk -F'[<>]' '/<param name="url">/ { gsub(/\.git$/, "", $3); n=split($3,a,"/"); print a[n]; exit }' _service)
OLD_REV=$(awk '/commit:/ {print $2}' ${PKGNAME}.obsinfo 2>/dev/null)

rm *.obscpio 2> /dev/null

sed -i '1,/<param name="revision">/s|<param name="revision">[^<]*</param>|<param name="revision">'"$REV"'</param>|' _service
osc service mr

VERSION=$(awk '/version:/ {print $2}' ${PKGNAME}.obsinfo)
NEW_REV=$(awk '/commit:/ {print $2}' ${PKGNAME}.obsinfo 2>/dev/null)

if [ ! -z "$OLD_REV" ]; then
  pushd $PKGNAME
  git diff $OLD_REV..HEAD -- NEWS > ../osc-collab.NEWS
  git diff $OLD_REV..HEAD -- meson.build > ../osc-collab.meson
  git diff $OLD_REV..HEAD -- meson_options.txt > ../osc-collab.meson_options
popd
fi

if [ "$OLD_REV" != "$NEW_REV" ]; then
  echo "- Update to version $VERSION:" > .NEWS
  grep "^+" osc-collab.NEWS | sed 's/^+//g' >> .NEWS
  osc vc -F .NEWS
  rm .NEWS
fi

osc ar

