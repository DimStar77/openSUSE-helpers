#!/usr/bin/python

import urllib2
import re

CONTENT="http://download.opensuse.org/tumbleweed/repo/oss/content"
APPDATA="appdata.html"

CNT = urllib2.urlopen(CONTENT).read()
DISTRO = re.findall("DISTRO.*", CNT)

APPCNT = open(APPDATA, 'r').read()
DATA = re.findall('<tr><td class="alt">Keywords</td><td>(\d+)/(\d+)</td><td class="thin">.*</td></tr>', APPCNT)

V1 = (DISTRO[0].split(':')[4]).split(',')[0]
SNAPDATE=V1[:4] + '-' + V1[4:6] + '-' + V1[6:]
print ("%s:%s" % (SNAPDATE, DATA[0][1]))

