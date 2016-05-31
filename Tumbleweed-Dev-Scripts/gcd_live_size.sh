
IMG=$1

if [ -z "$IMG" ]; then
  echo "No paramter specified, assuming gnome"
  IMG=gnome
fi

osc rbl openSUSE:Factory:Live kiwi-image-livecd-$IMG standard x86_64 | awk -F= '/\+ size=/ {print $2}'
