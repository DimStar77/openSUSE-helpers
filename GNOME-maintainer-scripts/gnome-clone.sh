#!/bin/bash

git clone gitea@src.opensuse.org:/GNOME/_ObsPrj GNOME || exit 1
cd GNOME
git submodule update --init --recursive --jobs 10
git worktree add ../GNOME:Next next
cd ../GNOME:Next
git submodule update --init --recursive --jobs 10
cd ..

