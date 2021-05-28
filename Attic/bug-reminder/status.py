import json
import time
import osc
import osc.core
import osc.conf
import xml.etree.ElementTree as ET
import urllib2
import smtplib
from email.mime.text import MIMEText
import email.utils


osc.conf.get_config()
apiurl = osc.conf.config['apiurl']

project="openSUSE:Factory"
URL="https://build.opensuse.org/project/status?&project=%s&ignore_pending=true&limit_to_fails=true&include_versions=false&format=json" % project


seconds_to_remember = 7 * 86400
now = int(time.time())

class RemindedPackage(object):
    def __init__(self,firstfail,reminded,remindCount,bug):
        self.firstfail=firstfail
        self.reminded=reminded
        self.bug=bug
        self.remindCount=remindCount

def jdefault(o):
        return o.__dict__


def sendmail(to, fullname, subject, message):
    msg = MIMEText(message, _charset="UTF-8")
    msg['Subject'] = subject
    msg['To'] = email.utils.formataddr((fullname, to))
    msg['From'] = 'DimStar / Dominique Leuenberger <dimstar@opensuse.org>'
    msg['Date'] = email.utils.formatdate()
    msg.add_header('Precedence', 'bulk')
    msg.add_header('X-Mailer','openSUSE Tumbleweed - Failure Notification')
    try:
        s = smtplib.SMTP('ani.leuenberger.net')
        s.sendmail(msg['From'], {msg['To'], "Dominique Leuenberger <dimstar@opensuse.org>" }, msg.as_string())
        s.quit()
    except:
        print("Failed to send an email to %s (%s)" % (fullname, to))
        pass

json_data = urllib2.urlopen(URL)
data = json.load(json_data)
json_data.close()

try:
    with open('openSUSE:Factory.reminded.json') as json_data:
        RemindedLoaded = json.load(json_data)
    json_data.close()
except:
    RemindedLoaded = {}
    pass

Reminded = {}
Person = {}

EMAIL1 = u"""
Dear %s

Please be informed that the package '%s' in openSUSE Tumbleweed has
not had a successful build since %s (See project openSUSE:Factory).

This can be due to an error in your package directly or could be caused
by a package you depend on to build. In any case, please do your utmost to
get the status back to building.

You will get another reminder in a week if the package still fails by then.

*** NOTE: This is an attempt to raise awareness of the maintainers about broken
          builds in Tumbleweed. You receive this mail because you are marked
          as maintainer for the above mentioned package (or project maintainer
          if the package has no explicit maintainer assigned)

Kind regards,
Dominique Leuenberger a.k.a DimStar
"""

EMAIL2 = u"""
Dear %s

Despite the reminder of one week ago, we have to inform you that the package
'%s' is still failing in openSUSE Tumbleweed (see project openSUSE:Factory).

It has been failing to build since %s.

Please find the time to fix the build of this package. If needed, also reach out
to the broader community, trying to find somebody to help you fix this package.

*** NOTE: This is an attempt to raise awareness of the maintainers about broken
          builds in Tumbleweed. You receive this mail because you are marked
          as maintainer for the above mentioned package (or project maintainer
          if the package has no explicit maintainer assigned)

Kind regards,
Dominique Leuenberger a.k.a DimStar
"""


# Go through all the failed packages and update the reminder
for package in data:
    # Only consider packages that failed for > seconds_to_remember days (7 days)
    if package["firstfail"] < now - seconds_to_remember:
        if not package["name"]  in RemindedLoaded.keys():
            # This is the first time we see this package failing for > 7 days
            reminded = now
            bug=""
            remindCount = 1
        else:
            if RemindedLoaded[package["name"]]["reminded"] < now - seconds_to_remember:
                # We had seen this package in the last run - special treatment
                reminded = now
                bug="boo#123"
                remindCount = RemindedLoaded[package["name"]]["remindCount"] + 1
            else:
                reminded = RemindedLoaded[package["name"]]["reminded"]
                remindCount = RemindedLoaded[package["name"]]["remindCount"]
                bug = RemindedLoaded[package["name"]]["bug"]
        Reminded[package["name"]] = RemindedPackage(package["firstfail"], reminded, remindCount, bug)

with open('openSUSE:Factory.reminded.json', 'w') as json_result:
    json.dump(Reminded, json_result, default=jdefault)

for package in Reminded:
    # Now we check on all the packages if we have to perform any reminder actions...
    if Reminded[package].reminded == now:
	# find the maintainers, try to not hammer the server too much
	maintainers = osc.core.owner(apiurl, package, project='openSUSE:Factory')
	for maintainer in maintainers[0]:
            if not maintainer.attrib["name"] in Person.keys() and maintainer.tag == "person":
                Person[maintainer.attrib["name"]] = osc.core.get_user_data(apiurl, maintainer.attrib["name"], 'login', 'realname', 'email')
        if Reminded[package].remindCount == 1:
            for maintainer in maintainers[0]:
                if maintainer.tag == "person":
                    sendmail(Person[maintainer.attrib["name"]][2], Person[maintainer.attrib["name"]][1], 'openSUSE Tumbleweed - %s - Build fail notification' % package,
                             EMAIL1 % (Person[maintainer.attrib["name"]][1], package, time.ctime(Reminded[package].firstfail)))
        elif Reminded[package].remindCount == 2:
            for maintainer in maintainers[0]:
                if maintainer.tag == "person":
                    sendmail(Person[maintainer.attrib["name"]][2], Person[maintainer.attrib["name"]][1], 'openSUSE Tumbleweed - %s - Build fail notification - reminder' % package,
                             EMAIL2 % (Person[maintainer.attrib["name"]][1], package, time.ctime(Reminded[package].firstfail)))
        elif Reminded[package].remindCount == 3:
            print( "Package '%s' has been failing for three weeks - let's create a bug report" % package)
        else:
            print( "Package '%s' is no longer maintained - send a mail to factory maintainers..." % package)

