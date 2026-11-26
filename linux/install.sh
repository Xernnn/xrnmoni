#!/usr/bin/env bash
# Install the xrnmoni agent for the current user on Linux: a venv with its
# requirements, a config file (copied from the example, with a fresh bridge secret,
# if there is none yet) and a systemd user unit that runs it with the graphical
# session. Run it from a checkout or the unpacked xrnmoni-linux bundle, which it then runs from
# (keep the folder); run it again after pulling or unpacking a newer one to update.
#
#   linux/install.sh [--hub URL] [--token TOKEN] [--device NAME]
#
# --hub, --token and --device go into the config (new or not): the hub's address with its path,
# the device token made for this machine, and the name the dashboard shows. With all three on a
# first install the agent starts at once.
#
# XRNMONI_CONFIG  the config file (default ~/.config/xrnmoni/agent.toml)
# XRNMONI_VENV    the venv (default <checkout>/agent/.venv)
set -euo pipefail

repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
config_home=${XDG_CONFIG_HOME:-$HOME/.config}
config=${XRNMONI_CONFIG:-$config_home/xrnmoni/agent.toml}
venv=${XRNMONI_VENV:-$repo/agent/.venv}
unit=xrnmoni-agent.service
unit_dir=$config_home/systemd/user

settings=()
while [[ $# -gt 0 ]]; do
  case $1 in
    --hub) settings+=(server_url "${2:?--hub needs an address}"); shift 2 ;;
    --token) settings+=(token "${2:?--token needs the device token}"); shift 2 ;;
    --device) settings+=(device "${2:?--device needs a name}"); shift 2 ;;
    *) echo "usage: linux/install.sh [--hub URL] [--token TOKEN] [--device NAME]" >&2; exit 2 ;;
  esac
done

if [[ ! -x $venv/bin/python ]]; then
  python3 -m venv "$venv"
fi
"$venv/bin/pip" install --quiet -r "$repo/agent/requirements.txt"

secret=
if [[ ! -f $config ]]; then
  mkdir -p "$(dirname "$config")"
  secret=$("$venv/bin/python" -c 'import secrets; print(secrets.token_urlsafe(24))')
  (umask 077 && sed "s|^bridge_secret = \"\"|bridge_secret = \"$secret\"|" \
    "$repo/agent/config.example.toml" > "$config")
fi

# The settings given, each as a TOML string in place of its line (or added at the top).
if [[ ${#settings[@]} -gt 0 ]]; then
  "$venv/bin/python" - "$config" "${settings[@]}" <<'PY'
import json, re, sys
path, pairs = sys.argv[1], sys.argv[2:]
with open(path, encoding="utf-8") as f:
    text = f.read()
for key, value in zip(pairs[::2], pairs[1::2]):
    line = f"{key} = {json.dumps(value, ensure_ascii=False)}"
    text, found = re.subn(rf"(?m)^{key}\s*=.*$", lambda m: line, text, count=1)
    if not found:
        text = line + "\n" + text
with open(path, "w", encoding="utf-8") as f:
    f.write(text)
PY
fi

# systemd reads % as a specifier and the paths are quoted in ExecStart
esc() { local s=${1//%/%%}; printf '%s' "${s//\"/\\\"}"; }
template=$(<"$repo/linux/$unit")
template=${template//@PYTHON@/"$(esc "$venv/bin/python")"}
template=${template//@AGENT@/"$(esc "$repo/agent/agent.py")"}
template=${template//@CONFIG@/"$(esc "$config")"}
mkdir -p "$unit_dir"
printf '%s\n' "$template" > "$unit_dir/$unit"

systemctl --user daemon-reload
systemctl --user enable "$unit"

if [[ -n $secret && ${#settings[@]} -eq 6 ]]; then
  systemctl --user restart "$unit"
  cat <<MSG

Wrote $config and started $unit.
In the browser extension's options, set Secret to: $secret
(Firefox: also add the origin it shows to bridge_origins in the config.)
MSG
elif [[ -n $secret ]]; then
  cat <<MSG

Wrote $config.
1. Set server_url, token and device in it.
2. Start the agent:  systemctl --user start $unit
3. In the browser extension's options, set Secret to: $secret
   and add the origin it shows to bridge_origins in the config.
MSG
else
  systemctl --user restart "$unit"
  echo "Restarted $unit with $config"
fi
echo "Log: journalctl --user -u $unit -f"
