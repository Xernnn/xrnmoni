#!/usr/bin/env bash
# Install the xrnmoni agent for the current user on Linux: a venv with its
# requirements, a config file (copied from the example, with a fresh bridge secret,
# if there is none yet) and a systemd user unit that runs it with the graphical
# session. Run it from a checkout; run it again after pulling to update.
#
#   linux/install.sh
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

if [[ -n $secret ]]; then
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
