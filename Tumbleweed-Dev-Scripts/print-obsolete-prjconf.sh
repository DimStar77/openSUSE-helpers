
# Script lists all existing binaries in openSUSE:Factory plus ports
# and compares the prjconf Prefer: entries against this generated list.
# Prefers pointing to non-existing packages are being printed out, as
# they can usually safely be removed.


TMPDIR=$(mktemp -d)

for PORT in "" :ARM :PowerPC :zSystems; do osc ls -b openSUSE:Factory${PORT} -r standard; done | sed 's/\.rpm$//' | sort -u > ${TMPDIR}/all_packages

echo "### Prefer: packages to verify - the package names listed here do not exist"

for pkg in $(for word in $(osc meta prjconf openSUSE:Factory | \
    grep Prefer: | \
    sed "s/Prefer://g"); do echo $word; done | \
    sed "s/^-//g" | \
    sed 's/.*://g' | \
    sort -u); do 
      grep -q "\\s${pkg}$" ${TMPDIR}/all_packages || echo "$pkg";
     done

# Checking onlybuild entries
for prj in openSUSE:Factory openSUSE:Factory:NonFree; do
  osc prjresults --show-excluded ${prj} -a x86_64 -r standard -V | awk '/^[.FUf%bsSx]/ {print $2}' >> ${TMPDIR}/all_sources
done

echo using ${TMPDIR}/all_packages

echo
echo "### onlybuild|excludebuid: packages to verify - the package names listed here do not exist"
echo

for pkg in $(osc meta prjconf openSUSE:Factory | grep "BuildFlags.*build:" | sed -e 's/BuildFlags: //' -e 's/onlybuild://' -e 's/excludebuild://'); do
      grep -q "^${pkg}$" ${TMPDIR}/all_sources || echo "Drop only|exclude build for $pkg";
  done
