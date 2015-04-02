# Copyright (C) 2015 SUSE Linux Products GmbH
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.

from __future__ import print_function

from osc import cmdln

@cmdln.option('--push',action='store_true',
              help="Push changed packages to their parents")
def do_pcheck(self, subcmd, opts, project):
    """${cmd_name}: Show changed packages (packages that have a diff)

    Examples:
    osc cpkgs <prj>        # shows changed packages etc. for <prj>

    --push      Create submit requests for packages with a diff (if none exists yet)

    """
    apiurl = self.get_api_url()
    sinfos = get_project_sourceinfo(apiurl, project, True)
    todo = {}
    errors = {}
    md5s = {}
    pmap = {}
    changed = []
    changeSRed = {}
    api = oscapi(apiurl)
    for pkg, sinfo in sinfos.iteritems():
        if sinfo.find('error'):
            errors[pkg] = sinfo.find('error').text
            continue
        elif sinfo.find('linked') is not None:
            elm = sinfo.find('linked')
            key = '%s/%s' % (elm.get('project'), elm.get('package'))
            pmap.setdefault(key, []).append(pkg)
            todo.setdefault(elm.get('project'), []).append(elm.get('package'))
        md5s[pkg] = sinfo.get('verifymd5')
    for prj, pkgs in todo.iteritems():
        sinfos = get_project_sourceinfo(apiurl, prj, True, *pkgs)
        for pkg, sinfo in sinfos.iteritems():
            key = '%s/%s' % (prj, pkg)
            for p in pmap[key]:
                vmd5 = md5s.pop(p)
                if vmd5 != sinfo.get('verifymd5'):
                    # Is there already an SR outgoing for this package?
                    SRid = api.sr_for_package(project, p)
                    if SRid > 0:
                        changeSRed[p] = SRid
                    else:
                        changed.append(p)
                        if opts.push:
                            api.create(project=project, package=p, target=prj)
                            
    overview = 'Overview of project {}'.format(project)
    print()
    print(overview)
    print('=' * len(overview))
    print('Changed & unsubmitted packages: %d' % len(changed))
    print(', '.join(changed))
    print() 
    print('Changed & submitted packages: %d' % len(changeSRed.keys()))
    print(', '.join(['%s(%s)' % (pkg, SR) for pkg, SR in changeSRed.iteritems()]))
    print()
    print('Packages without link: %d' % len(md5s.keys()))
    print(', '.join(md5s.keys()))
    print()
    print('Packages with errors: %d' % len(errors.keys()))
    print('\n'.join(['%s: %s' % (p, err) for p, err in errors.iteritems()]))

class oscapi:
    def __init__(self, apiurl):
        self.apiurl = apiurl

    def sr_for_package(self, project, package):
        query = "(state/@name='new' or state/@name='review') and (action/source/@project='{project}' or submit/source/@project='{project}') and (action/source/@package='{package}' or submit/source/@package='Packafe')".format(project=project, package=package)

        result = search(self.apiurl, request=query)
        collection = result['request']
        requests = []
        for root in collection.findall('request'):
            return root.get('id')

    def get_rev(self, project, package):
        return 0

    def create(self, project, package, target):
        currev = get_source_rev(self.apiurl, project, package)['rev']
        print ("Creating a request from {project}/{package}".format(project=project,package=package))
        query = {'cmd'    : 'create' }
        url = makeurl(self.apiurl, ['request'], query=query)

        data = '<request type="submit"><submit><source project="{project}" package="{package}" rev="{rev}"/><target project="{target}" package="{package}"  /></submit><state name="new"/><description>Scripted push of project {project}</description></request>'.format(project=project, package=package, target=target, rev=currev)
        f = http_POST(url, data=data)

