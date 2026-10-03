# Bash completion for geckopit, geckopit-cli, and geckopit-upgrade
# Supports option completion, profile completion, branch completion,
# and intelligent package completion from local workspace and cache.

_geckopit_get_profiles() {
    python3 -c "
import json, os
p = os.path.expanduser('~/.config/geckopit.json')
if os.path.isfile(p):
    try:
        d = json.load(open(p))
        profs = list(d.get('workspaces', {}).keys()) + list(d.get('profiles', {}).keys())
        print(' '.join(profs))
    except Exception:
        pass
" 2>/dev/null
}

_geckopit_get_packages() {
    python3 -c "
import json, os, glob

pkgs = set()

# 1. From current directory subdirectories with .spec
for s in glob.glob('*/*.spec'):
    pkgs.add(os.path.basename(os.path.dirname(s)))

# 2. From .gitmodules if present
if os.path.isfile('.gitmodules'):
    try:
        import subprocess
        out = subprocess.run(['git', 'config', '--file', '.gitmodules', '--get-regexp', 'path'], capture_output=True, text=True).stdout
        for line in out.splitlines():
            pts = line.split()
            if len(pts) >= 2:
                pkgs.add(pts[1])
    except Exception:
        pass

# 3. From cached package_data
cache_dir = os.path.expanduser('~/.cache/geckopit')
if os.path.isdir(cache_dir):
    for cf in glob.glob(os.path.join(cache_dir, 'cache_*.json')):
        try:
            d = json.load(open(cf))
            for k in d.get('package_data', {}).keys():
                pkgs.add(k)
        except Exception:
            pass

# 4. From active workspace paths in ~/.config/geckopit.json
conf_file = os.path.expanduser('~/.config/geckopit.json')
if os.path.isfile(conf_file):
    try:
        d = json.load(open(conf_file))
        act = d.get('active_workspace') or d.get('active_profile') or ''
        ws = d.get('workspaces', {}).get(act) or d.get('profiles', {}).get(act) or {}
        for p in (ws.get('unstable_path'), ws.get('stable_path')):
            if p and os.path.isdir(p):
                for s in glob.glob(os.path.join(p, '*/*.spec')):
                    pkgs.add(os.path.basename(os.path.dirname(s)))
    except Exception:
        pass

print(' '.join(sorted(pkgs)))
" 2>/dev/null
}

_geckopit_get_upgrade_targets() {
    local cur_pkg="$1"
    python3 -c "
import json, os, sys, glob

pkg = sys.argv[1] if len(sys.argv) > 1 else ''
targets = set(['@PARENT_TAG@'])

cache_dir = os.path.expanduser('~/.cache/geckopit')
if os.path.isdir(cache_dir):
    for cf in glob.glob(os.path.join(cache_dir, 'cache_*.json')):
        try:
            d = json.load(open(cf))
            pkg_data = d.get('package_data', {})
            data = pkg_data.get(pkg) or pkg_data.get(os.path.basename(os.path.abspath(pkg or '.')))
            if data:
                ver = data.get('version', {})
                for k in ('upstream_latest', 'upstream_stable'):
                    v = ver.get(k)
                    if v and v not in ('—', 'N/A'):
                        targets.add(v)
        except Exception:
            pass

print(' '.join(sorted(targets)))
" "$cur_pkg" 2>/dev/null
}

_geckopit_get_prs() {
    local cur_pkg="$1"
    python3 -c "
import json, os, sys, glob

pkg = sys.argv[1] if len(sys.argv) > 1 else ''
prs = set()

if not pkg:
    pkg = os.path.basename(os.getcwd())

cache_dir = os.path.expanduser('~/.cache/geckopit')
if os.path.isdir(cache_dir):
    for cf in glob.glob(os.path.join(cache_dir, 'cache_*.json')):
        try:
            d = json.load(open(cf))
            pkg_data = d.get('package_data', {})
            data = pkg_data.get(pkg)
            if data:
                pr = data.get('pr', {})
                num = pr.get('number')
                if num:
                    prs.add(str(num))
        except Exception:
            pass

print(' '.join(sorted(prs)))
" "$cur_pkg" 2>/dev/null
}

_geckopit_completion() {
    local cur prev words cword
    if type _init_completion >/dev/null 2>&1; then
        _init_completion || return
    else
        COMPREPLY=()
        cur="${COMP_WORDS[COMP_CWORD]}"
        prev="${COMP_WORDS[COMP_CWORD-1]}"
    fi

    local options="--check-patches --list-prs --merge-pr --squash --no-commit --upgrade -u --commit -c --no-edit --audit-deps -d --fix-deps --sync -s --fetch --force -f --jobs -j --version -v --branch -b --profile -p --package --not-in-pool --include-not-in-pool --guide --docs --check-setup --setup --dry-run -n --help -h --bash-completion"
    local branches="factory next stable unstable"

    case "$prev" in
        --merge-pr)
            local cand_pkg=""
            for w in "${COMP_WORDS[@]}"; do
                if [[ "$w" != -* && "$w" != "geckopit"* && "$w" != "$cur" ]]; then
                    cand_pkg="$w"
                    break
                fi
            done
            local pr_targets=$(_geckopit_get_prs "$cand_pkg")
            COMPREPLY=( $(compgen -W "$pr_targets" -- "$cur") )
            return 0
            ;;
        -b|--branch|-v|--version)
            COMPREPLY=( $(compgen -W "$branches" -- "$cur") )
            return 0
            ;;
        -j|--jobs)
            COMPREPLY=( $(compgen -W "4 8 16 32" -- "$cur") )
            return 0
            ;;
        -p|--profile)
            local profiles=$(_geckopit_get_profiles)
            COMPREPLY=( $(compgen -W "$profiles" -- "$cur") )
            return 0
            ;;
        --package)
            local packages=$(_geckopit_get_packages)
            COMPREPLY=( $(compgen -W "$packages" -- "$cur") )
            return 0
            ;;
        -u|--upgrade)
            local cand_pkg=""
            for w in "${COMP_WORDS[@]}"; do
                if [[ "$w" != -* && "$w" != "geckopit"* && "$w" != "$cur" ]]; then
                    cand_pkg="$w"
                    break
                fi
            done
            local up_targets=$(_geckopit_get_upgrade_targets "$cand_pkg")
            COMPREPLY=( $(compgen -W "$up_targets" -- "$cur") )
            return 0
            ;;
    esac

    if [[ "$cur" == -* ]]; then
        COMPREPLY=( $(compgen -W "$options" -- "$cur") )
        return 0
    fi

    local packages=$(_geckopit_get_packages)
    COMPREPLY=( $(compgen -W "$packages $branches" -- "$cur") )
    return 0
}

complete -F _geckopit_completion geckopit-cli
complete -F _geckopit_completion geckopit
complete -F _geckopit_completion geckopit-upgrade
