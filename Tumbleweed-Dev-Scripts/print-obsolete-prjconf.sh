
# Script lists all existing binaries in openSUSE:Factory plus ports
# and compares the prjconf Prefer: entries against this generated list.
# Prefers pointing to non-existing packages are being printed out, as
# they can usually safely be removed.


TMPDIR=$(mktemp -d)

for PORT in "" :ARM :PowerPC :zSystems; do osc ls -b openSUSE:Factory${PORT} -r standard; done | sed 's/\.rpm$//' | sort -u > ${TMPDIR}/all_packages

for pkg in $(for word in $(osc meta prjconf openSUSE:Factory | \
    grep Prefer: | \
    sed "s/Prefer://g"); do echo $word; done | \
    sed "s/^-//g" | \
    sed 's/.*://g' | 
    \sort -u); do 
      grep -q "\\s${pkg}$" ${TMPDIR}/all_packages || echo "$pkg";
     done
