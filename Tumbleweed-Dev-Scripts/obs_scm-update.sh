

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

OLD_REV=$(awk '/commit:/ {print $2}' *.obsinfo 2>/dev/null)
PKGNAME=$(grep '<param name="url">.*</param>' _service | sed -e 's/<.*">//' -e's/<.*>//' -e 's/ //g' -e 's/\.git$//' | awk -F/ '{print $NF}')

rm *.obscpio 2> /dev/null

sed -i "s|<param name=\"revision\">.*</param>|<param name=\"revision\">${REV}</param>|" _service
osc service mr

VERSION=$(awk '/version:/ {print $2}' *.obsinfo)
NEW_REV=$(awk '/commit:/ {print $2}' *.obsinfo 2>/dev/null)

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

