Outlining some workflow to survive a metaproject with 500+ submodules
and multiple branches, where the branches drift apart with what
submodules they contain (making git checkout <branch> a real pain)

# gnome-clone.sh
gnome-clone.sh does as the name suggest, clone the entire set of packages
of the GNOME organisation onto the disk. The starting point is _ObsPrj (the
OBS Project definition, refering all the submodules aka packages for the
factory branch

As GNOME lives from multiple branches for GNOME:Factory and GNOME:Next, the
script then intializes a 2nd worktree (GNOME:Next) next to the initial GNOME
checkout, targeting the "next" branch.
The script sets this up using 'worktree' as to not pull the entire thing
twice, saving a good bunch of disk space

# gnome-sync.sh
Checks the GNOME and GNOME:Next directories to see if the submodules
on src.o.o have moved forward and then syncs the modules in need.

This is very similar to 'git pull' in either of the directories, plus 
git submodule update - but it copes perfectly with sbmodules appearing
and disappearing without the need to remember all the parameters

# gnome-audit.sh
Verifies, based on your current state on disk, how far the #factory
and #next branch have drifted. Creates a table of what needs to be updated
in what direction
* BEHIND: #next is <n> commits behind #factory - use gnome-catchup.sh
  to sync the change from #factory into #next
* PENDING PUSH: #next branch contains changes which are not in #factory.
  gnome-promote.sh can create mass-submissions, or you can create a PR
  from #next to #factory
* DIVERGED: commits were added to #next and #factory; before you can merge
  #next into #factory again, you need to pull/rebase #factory into #next

# gnome-catchup.sh
Tries to merge changes from #factory into #next branch

# gnome-promote.sh
Promotes changes from #next branch to #factory branch

# gnome-pool-audit.sh
Shows an overview of what packages in GNOME/ have a diff on their
 factory branch vs the package in pool (i.e what needs to be submitted
 to Factory


