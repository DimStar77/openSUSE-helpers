#!/usr/bin/env python3
"""
openSUSE Maintainer Environment Readiness Auditor & Onboarding Assistant.
Checks and configures the 4 core developer pillars required for packaging:
1. Git Identity (user.name & user.email)
2. SSH Authentication (keys & connection to gitea@src.opensuse.org)
3. OBS Credentials (osc configuration in ~/.config/osc/oscrc)
4. Gitea API Access (tea configuration in ~/.config/tea/config.yml)
"""

import os
import sys
import re
import shutil
import subprocess
import configparser
from typing import Dict, List, Tuple, Optional

GITEA_SSH_HOST = "src.opensuse.org"
GITEA_SSH_USER = "gitea"
GITEA_WEB_URL = "https://src.opensuse.org"
OBS_API_URL = "https://api.opensuse.org"


def check_git_identity() -> Dict[str, any]:
    """Checks whether Git user.name and user.email are configured."""
    name = ""
    email = ""

    # 1. Try standard git config
    try:
        proc_name = subprocess.run(["git", "config", "user.name"], capture_output=True, text=True)
        name = proc_name.stdout.strip()
        proc_email = subprocess.run(["git", "config", "user.email"], capture_output=True, text=True)
        email = proc_email.stdout.strip()
    except Exception:
        pass

    # 2. Check ~/.gitconfig directly if subshell environment overrode GIT_CONFIG_GLOBAL
    gitconfig_path = os.path.expanduser("~/.gitconfig")
    if (not name or not email) and os.path.isfile(gitconfig_path):
        try:
            cfg = configparser.ConfigParser()
            cfg.read(gitconfig_path)
            if "user" in cfg:
                if not name and "name" in cfg["user"]:
                    name = cfg["user"]["name"].strip()
                if not email and "email" in cfg["user"]:
                    email = cfg["user"]["email"].strip()
        except Exception:
            pass

    return {
        "configured": bool(name and email),
        "name": name,
        "email": email,
    }


def check_ssh_readiness(test_connection: bool = True) -> Dict[str, any]:
    """
    Checks SSH keys in ~/.ssh and tests SSH authentication against gitea@src.opensuse.org.
    Accepts host key to seed known_hosts automatically.
    """
    ssh_dir = os.path.expanduser("~/.ssh")
    pub_keys = []
    if os.path.isdir(ssh_dir):
        try:
            for f in os.listdir(ssh_dir):
                if f.startswith("id_") and f.endswith(".pub"):
                    pub_keys.append(f)
        except Exception:
            pass

    authenticated = False
    username = None
    msg = ""

    if test_connection:
        try:
            proc = subprocess.run(
                [
                    "ssh",
                    "-o", "BatchMode=yes",
                    "-o", "StrictHostKeyChecking=accept-new",
                    "-o", "ConnectTimeout=6",
                    "-T", f"{GITEA_SSH_USER}@{GITEA_SSH_HOST}"
                ],
                capture_output=True,
                text=True
            )
            out = (proc.stdout or "") + (proc.stderr or "")
            msg = out.strip()
            # Standard Gitea banner: "Hi there, <username>! You've successfully authenticated..."
            m = re.search(r"Hi there, ([^!]+)!", out)
            if m:
                authenticated = True
                username = m.group(1).strip()
        except Exception as e:
            msg = str(e)

    return {
        "has_keys": bool(pub_keys),
        "keys": sorted(pub_keys),
        "authenticated": authenticated,
        "username": username,
        "message": msg,
    }


def check_obs_credentials() -> Dict[str, any]:
    """Checks whether ~/.config/osc/oscrc or ~/.oscrc is configured with OBS API credentials."""
    paths = [
        os.path.expanduser("~/.config/osc/oscrc"),
        os.path.expanduser("~/.oscrc"),
    ]

    found_path = None
    user = None
    apiurl = None

    for p in paths:
        if os.path.isfile(p):
            try:
                cfg = configparser.ConfigParser()
                cfg.read(p)
                for sec in cfg.sections():
                    if "user" in cfg[sec] and cfg[sec]["user"].strip():
                        found_path = p
                        user = cfg[sec]["user"].strip()
                        apiurl = sec
                        if "api.opensuse.org" in sec:
                            break
                if found_path:
                    break
            except Exception:
                pass

    return {
        "configured": bool(user),
        "user": user,
        "apiurl": apiurl,
        "path": found_path,
    }


def check_gitea_tea() -> Dict[str, any]:
    """Checks whether ~/.config/tea/config.yml is configured with src.opensuse.org token."""
    tea_bin = shutil.which("tea")
    tea_conf_path = os.path.expanduser("~/.config/tea/config.yml")
    configured = False
    user = None
    url = None

    if os.path.isfile(tea_conf_path):
        try:
            with open(tea_conf_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            m_url = re.search(r"url:\s*(\S+)", content)
            m_user = re.search(r"user:\s*(\S+)", content)
            if m_url and "src.opensuse.org" in m_url.group(1):
                configured = True
                url = m_url.group(1).strip()
                if m_user:
                    user = m_user.group(1).strip()
        except Exception:
            pass

    return {
        "has_binary": bool(tea_bin),
        "configured": configured,
        "user": user,
        "url": url,
        "path": tea_conf_path if os.path.isfile(tea_conf_path) else None,
    }


def check_all_readiness(test_ssh: bool = True) -> Dict[str, any]:
    """Performs a complete readiness check across all 4 developer pillars."""
    git_status = check_git_identity()
    ssh_status = check_ssh_readiness(test_connection=test_ssh)
    obs_status = check_obs_credentials()
    tea_status = check_gitea_tea()

    all_ready = (
        git_status["configured"]
        and ssh_status["authenticated"]
        and obs_status["configured"]
        and tea_status["configured"]
    )

    return {
        "all_ready": all_ready,
        "git": git_status,
        "ssh": ssh_status,
        "obs": obs_status,
        "tea": tea_status,
    }


def configure_git_identity(name: str, email: str, gitconfig_path: Optional[str] = None) -> Tuple[bool, str]:
    """Sets user.name and user.email in Git config."""
    name = (name or "").strip()
    email = (email or "").strip()
    if not name or not email:
        return False, "Both name and email are required."

    target_cfg = gitconfig_path or os.path.expanduser("~/.gitconfig")
    try:
        cmd = ["git", "config", "--file", target_cfg]
        res1 = subprocess.run(cmd + ["user.name", name], capture_output=True, text=True)
        res2 = subprocess.run(cmd + ["user.email", email], capture_output=True, text=True)
        if res1.returncode == 0 and res2.returncode == 0:
            return True, f"Configured Git identity as '{name} <{email}>'"
        return False, res1.stderr or res2.stderr or "Failed to set Git config."
    except Exception as e:
        return False, str(e)


def generate_ssh_key(comment: Optional[str] = None, key_path: Optional[str] = None) -> Tuple[bool, str, str]:
    """
    Generates a secure ed25519 SSH keypair if one does not exist.
    Returns (success, public_key_content, message).
    """
    ssh_dir = os.path.expanduser("~/.ssh")
    os.makedirs(ssh_dir, mode=0o700, exist_ok=True)

    target_key = key_path or os.path.join(ssh_dir, "id_ed25519")
    pub_key_path = f"{target_key}.pub"

    if os.path.isfile(target_key):
        if os.path.isfile(pub_key_path):
            with open(pub_key_path, "r", encoding="utf-8") as f:
                return True, f.read().strip(), f"Using existing SSH key: {pub_key_path}"
        return False, "", f"Private key {target_key} exists but public key is missing."

    cmd = [
        "ssh-keygen",
        "-t", "ed25519",
        "-N", "",
        "-f", target_key
    ]
    if comment:
        cmd.extend(["-C", comment])

    try:
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode == 0 and os.path.isfile(pub_key_path):
            os.chmod(target_key, 0o600)
            with open(pub_key_path, "r", encoding="utf-8") as f:
                pub_content = f.read().strip()
            return True, pub_content, f"Generated new SSH key: {pub_key_path}"
        return False, "", res.stderr or "ssh-keygen failed."
    except Exception as e:
        return False, "", str(e)


def configure_osc_credentials(user: str, token_or_pass: str, apiurl: str = OBS_API_URL, oscrc_path: Optional[str] = None) -> Tuple[bool, str]:
    """
    Writes or updates ~/.config/osc/oscrc with credentials for the specified apiurl.
    Strictly secures file permissions to 0600.
    """
    user = (user or "").strip()
    token_or_pass = (token_or_pass or "").strip()
    apiurl = (apiurl or OBS_API_URL).strip().rstrip("/")
    if not user or not token_or_pass:
        return False, "Both username and token/password are required."

    target_path = oscrc_path or os.path.expanduser("~/.config/osc/oscrc")
    os.makedirs(os.path.dirname(target_path), mode=0o700, exist_ok=True)

    cfg = configparser.ConfigParser()
    if os.path.isfile(target_path):
        try:
            cfg.read(target_path)
        except Exception:
            pass

    if "general" not in cfg:
        cfg["general"] = {}
    cfg["general"]["apiurl"] = apiurl

    if apiurl not in cfg:
        cfg[apiurl] = {}
    cfg[apiurl]["user"] = user
    cfg[apiurl]["pass"] = token_or_pass

    try:
        with open(target_path, "w", encoding="utf-8") as f:
            cfg.write(f)
        os.chmod(target_path, 0o600)
        return True, f"Saved OBS credentials in {target_path}"
    except Exception as e:
        return False, str(e)


def configure_gitea_login(token: str, name: str = "opensuse", url: str = GITEA_WEB_URL, config_path: Optional[str] = None) -> Tuple[bool, str]:
    """
    Configures Gitea API authentication in ~/.config/tea/config.yml or via the 'tea' CLI.
    Strictly secures file permissions to 0600.
    """
    token = (token or "").strip()
    name = (name or "opensuse").strip()
    url = (url or GITEA_WEB_URL).strip().rstrip("/")
    if not token:
        return False, "Gitea API token is required."

    tea_bin = shutil.which("tea")
    if tea_bin:
        try:
            cmd = [tea_bin, "login", "add", "--name", name, "--url", url, "--token", token]
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode == 0:
                return True, f"Configured Gitea login '{name}' via tea CLI."
        except Exception:
            pass

    # Fallback to direct YAML configuration
    target_path = config_path or os.path.expanduser("~/.config/tea/config.yml")
    os.makedirs(os.path.dirname(target_path), mode=0o700, exist_ok=True)

    entry = f"""logins:
  - name: {name}
    url: {url}
    token: {token}
    default: true
    insecure: false
"""
    try:
        with open(target_path, "w", encoding="utf-8") as f:
            f.write(entry)
        os.chmod(target_path, 0o600)
        return True, f"Saved Gitea configuration in {target_path}"
    except Exception as e:
        return False, str(e)


def format_readiness_cli_report(readiness: Dict[str, any]) -> str:
    """Formats environment readiness check results into an ANSI-colored status report."""
    git_st = readiness["git"]
    ssh_st = readiness["ssh"]
    obs_st = readiness["obs"]
    tea_st = readiness["tea"]

    lines = ["\x1b[1m=> openSUSE Maintainer Environment Readiness:\x1b[0m\n"]

    # 1. Git Identity
    if git_st["configured"]:
        lines.append(f"  \x1b[1;32m[✓]\x1b[0m \x1b[1mGit Identity:\x1b[0m {git_st['name']} <{git_st['email']}>")
    else:
        lines.append("  \x1b[1;31m[✗]\x1b[0m \x1b[1mGit Identity:\x1b[0m Not configured (user.name or user.email missing)")

    # 2. SSH Authentication
    if ssh_st["authenticated"]:
        user_info = f"as '{ssh_st['username']}'" if ssh_st['username'] else "successfully"
        lines.append(f"  \x1b[1;32m[✓]\x1b[0m \x1b[1mSSH Authentication:\x1b[0m Connected to {GITEA_SSH_HOST} {user_info}")
    elif ssh_st["has_keys"]:
        lines.append(f"  \x1b[1;33m[!]\x1b[0m \x1b[1mSSH Authentication:\x1b[0m Local keys found ({', '.join(ssh_st['keys'])}), but authentication to {GITEA_SSH_HOST} failed")
    else:
        lines.append(f"  \x1b[1;31m[✗]\x1b[0m \x1b[1mSSH Authentication:\x1b[0m No SSH keys found in ~/.ssh/")

    # 3. OBS Credentials
    if obs_st["configured"]:
        lines.append(f"  \x1b[1;32m[✓]\x1b[0m \x1b[1mOBS Credentials:\x1b[0m Configured as '{obs_st['user']}' ({obs_st['apiurl']})")
    else:
        lines.append("  \x1b[1;31m[✗]\x1b[0m \x1b[1mOBS Credentials:\x1b[0m Missing (~/.config/osc/oscrc not found or empty)")

    # 4. Gitea CLI (tea)
    if tea_st["configured"]:
        lines.append(f"  \x1b[1;32m[✓]\x1b[0m \x1b[1mGitea API (tea):\x1b[0m Configured for {tea_st.get('url') or GITEA_WEB_URL}")
    else:
        lines.append(f"  \x1b[1;31m[✗]\x1b[0m \x1b[1mGitea API (tea):\x1b[0m Not configured (~/.config/tea/config.yml missing)")

    if readiness["all_ready"]:
        lines.append("\n\x1b[1;32m🎉 Your packaging environment is fully configured and ready!\x1b[0m")
    else:
        lines.append(f"\n\x1b[1;33m💡 Run 'geckopit-cli --setup' to interactively configure missing items.\x1b[0m")

    return "\n".join(lines)
