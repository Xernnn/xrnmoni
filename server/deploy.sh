#!/bin/sh
# Installs or updates the xrnmoni hub on a server reached over ssh that runs runit: Termux on an
# old Android phone, or any Linux with runit. Safe to run again: an update swaps in the program and
# its web/ folder together, writes the run file from the settings, restarts it and leaves the data
# alone.
#
#     server/deploy.sh [ssh-host]
#
# The settings come from server/deploy.local, a shell file kept out of git (start from
# deploy.local.example), or from the same names in the environment; a host given here wins.
#     XRNMONI_HOST     the ssh host (or user@host)
#     XRNMONI_DIR      the folder on the server, under its home unless it starts with / (xrnmoni)
#     XRNMONI_PORT     the port on 127.0.0.1 (5240)
#     XRNMONI_PREFIX   the path a reverse proxy publishes it under (/xrnmoni)
#     XRNMONI_USERS    a users file to sign in with, such as one other services share
#                      (default: DIR/data/users; make it with `user add`); ~/ is the server's home
#     XRNMONI_VIEWERS  more users in it who may see the dashboard, space-separated
#     XRNMONI_SERVICE  the runit service's name (xrnmoni)
#     XRNMONI_SVDIR    runit's service folder (default: Termux's, $PREFIX/var/service)
#     XRNMONI_GUARD    a command the run file runs first; the service doesn't start if it fails
#     XRNMONI_SVGUARD  a guard on the server that takes the port and the program, run first the
#                      same way (the home server's sv-guard: ~/.local/bin/sv-guard)
#     XRNMONI_PUBLISH  a command run on the server at the end, to publish it through a proxy
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/.." && pwd)
if [ -f "$HERE/deploy.local" ]; then
    . "$HERE/deploy.local"
fi
HOST=${1:-${XRNMONI_HOST:-}}
DIR=${XRNMONI_DIR:-xrnmoni}
PORT=${XRNMONI_PORT:-5240}
PREFIX_PATH=${XRNMONI_PREFIX:-/xrnmoni}
USERS=${XRNMONI_USERS:-}
VIEWERS=${XRNMONI_VIEWERS:-}
SERVICE=${XRNMONI_SERVICE:-xrnmoni}
SVDIR_SET=${XRNMONI_SVDIR:-}
GUARD=${XRNMONI_GUARD:-}
SVGUARD=${XRNMONI_SVGUARD:-}
PUBLISH=${XRNMONI_PUBLISH:-}

# Paths on the server. An unquoted ~/ in deploy.local was turned into this machine's home when the
# shell read it here; the server's home is another folder.
case $USERS in "$HOME"/*) USERS="~/${USERS#"$HOME"/}" ;; esac
case $SVGUARD in "$HOME"/*) SVGUARD="~/${SVGUARD#"$HOME"/}" ;; esac

if [ -z "$HOST" ]; then
    echo "Which server? Give its ssh host: server/deploy.sh HOST, or set XRNMONI_HOST in server/deploy.local." >&2
    exit 2
fi
case $PORT in *[!0-9]* | "") echo "XRNMONI_PORT must be a number" >&2; exit 2 ;; esac
case $SERVICE in *[!A-Za-z0-9._-]* | "" | .*) echo "XRNMONI_SERVICE must be a plain name" >&2; exit 2 ;; esac

# One word for the remote shell, whatever is in it.
q() {
    printf "'%s'" "$(printf %s "$1" | sed "s/'/'\\\\''/g")"
}

echo "Testing first..."
if ! (cd "$REPO" && PYTHONDONTWRITEBYTECODE=1 python3 -m unittest server/test_xrnmoni.py >/dev/null 2>&1); then
    echo "The server tests failed, so nothing was changed. Run them for details:" >&2
    echo "    python3 -m unittest server/test_xrnmoni.py" >&2
    exit 1
fi

echo "Copying it to $HOST..."
# The program and its page go in together: unpacked beside them first, then swapped in.
tar czf - --exclude=__pycache__ -C "$HERE" xrnmoni.py web | ssh "$HOST" "sh -c $(q '
    set -e
    D=$1
    case $D in /*) ;; *) D=$HOME/$D ;; esac
    mkdir -p "$D/data/downloads"
    chmod 700 "$D" "$D/data" "$D/data/downloads"
    cd "$D"
    rm -rf .incoming web.old
    mkdir .incoming
    tar xzf - -C .incoming
    test -s .incoming/xrnmoni.py
    test -s .incoming/web/index.html
    if [ -d web ]; then mv web web.old; fi
    mv .incoming/web web
    mv .incoming/xrnmoni.py xrnmoni.py
    chmod 755 xrnmoni.py
    rm -rf .incoming web.old
') sh $(q "$DIR")"

echo "Setting up the service..."
ssh "$HOST" "sh -s -- $(q "$DIR") $(q "$PORT") $(q "$PREFIX_PATH") $(q "$USERS") $(q "$VIEWERS") $(q "$SERVICE") $(q "$SVDIR_SET") $(q "$GUARD") $(q "$SVGUARD")" <<'EOS'
set -e
DIR=$1 PORT=$2 PREFIX_PATH=$3 USERS=$4 VIEWERS=$5 SERVICE=$6 SV=$7 GUARD=$8 SVGUARD=$9
case $DIR in /*) ;; *) DIR=$HOME/$DIR ;; esac
case $USERS in "~/"*) USERS=$HOME/${USERS#"~/"} ;; esac
case $SVGUARD in "~/"*) SVGUARD=$HOME/${SVGUARD#"~/"} ;; esac
if [ -n "$SVGUARD" ] && [ ! -x "$SVGUARD" ]; then
    echo "No $SVGUARD on the server; put it there first (the home-server repo's phone/install-sv-guard)." >&2
    exit 1
fi
S=${SV:-${PREFIX:-}/var/service}
if [ ! -d "$S" ]; then
    echo "No runit service folder at $S; set XRNMONI_SVDIR." >&2
    exit 1
fi
PY=$(command -v python3 || command -v python) || { echo "No Python on the server." >&2; exit 1; }
SH=$(command -v sh)
"$PY" -c "import bcrypt" 2>/dev/null || {
    echo "bcrypt is missing on the server: pkg install python-bcrypt (Termux), or pip install bcrypt." >&2
    exit 1
}
# Termux's Python has no zone database; without one, CSV times come out in the server's zone.
"$PY" -c "import zoneinfo; zoneinfo.ZoneInfo('Europe/Paris')" 2>/dev/null ||
    "$PY" -m pip install --quiet --disable-pip-version-check tzdata 2>/dev/null ||
    echo "No zone database: CSV times come out in the server's zone."

ARGS="--port $PORT --data \"$DIR/data\" --prefix \"$PREFIX_PATH\""
if [ -n "$USERS" ]; then ARGS="$ARGS --users \"$USERS\""; fi
for v in $VIEWERS; do ARGS="$ARGS --viewer \"$v\""; done
if [ -n "${PREFIX:-}" ]; then LOG=$PREFIX/var/log/sv/$SERVICE; else LOG=/var/log/sv/$SERVICE; fi

write_run() {
    {
        echo "#!$SH"
        echo "# The xrnmoni hub, written by server/deploy.sh from its settings: change them there."
        if [ -n "$GUARD" ]; then echo "$GUARD || exit 1"; fi
        if [ -n "$SVGUARD" ]; then echo "\"$SVGUARD\" $PORT \"$DIR/xrnmoni.py\" || exit 1"; fi
        echo "exec \"$PY\" \"$DIR/xrnmoni.py\" $ARGS 2>&1"
    } > "$1/run.new"
    chmod 755 "$1/run.new"
    mv "$1/run.new" "$1/run"
    printf '#!%s\nmkdir -p "%s"\nexec svlogd -tt "%s"\n' "$SH" "$LOG" "$LOG" > "$1/log/run.new"
    chmod 755 "$1/log/run.new"
    mv "$1/log/run.new" "$1/log/run"
}

export SVDIR=$S
if [ -d "$S/$SERVICE" ]; then
    write_run "$S/$SERVICE"
    sv restart "$SERVICE"
    exit 0
fi
# Built under a dot name, which runit ignores, so it never starts half written.
T=$S/.$SERVICE.new
rm -rf "$T"
mkdir -p "$T/log"
write_run "$T"
chmod 700 "$T" "$T/log"
mv "$T" "$S/$SERVICE"
EOS

echo "Waiting for it to answer..."
tries=0
until [ "$(ssh "$HOST" "curl -s -m 3 http://127.0.0.1:$PORT/health" || true)" = ok ]; do
    tries=$((tries + 1))
    if [ "$tries" -ge 8 ]; then
        echo "It did not start. Its log:" >&2
        ssh "$HOST" "tail -20 \"\${PREFIX:+\$PREFIX}/var/log/sv/$SERVICE/current\"" >&2 || true
        exit 1
    fi
    sleep 2
done

if [ -n "$PUBLISH" ]; then
    echo "Publishing it..."
    ssh "$HOST" "$PUBLISH"
fi

# The first time, no one can sign in and no agent can post yet: say how.
if [ -z "$USERS" ] && ! ssh "$HOST" "sh -c $(q 'D=$1; case $D in /*) ;; *) D=$HOME/$D ;; esac; test -s "$D/data/users"') sh $(q "$DIR")"; then
    echo "No one can sign in yet. Make a login, and a token for each device:"
    echo "    ssh -t $HOST 'python3 $DIR/xrnmoni.py user add NAME --data $DIR/data'"
    echo "    ssh $HOST 'python3 $DIR/xrnmoni.py token add laptop --data $DIR/data'"
fi
echo "Done."
