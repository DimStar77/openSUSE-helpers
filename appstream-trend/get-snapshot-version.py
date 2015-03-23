#!/usr/bin/python

import urllib2
import re

CONTENT="http://download.opensuse.org/tumbleweed/repo/oss/content"
APPDATA="http://download.opensuse.org/tumbleweed/repo/oss/suse/setup/descr/appdata.html"

CNT = urllib2.urlopen(CONTENT).read()
DISTRO = re.findall("DISTRO.*", CNT)

V1 = (DISTRO[0].split(':')[4]).split(',')[0]
SNAPDATE=V1[:4] + '-' + V1[4:6] + '-' + V1[6:]

print (SNAPDATE)
