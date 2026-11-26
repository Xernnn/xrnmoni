#!/bin/sh
# Puts files in the hub's downloads folder, which its Get page lists: the Windows package, the Linux
# bundle, the extension (.zip and .xpi) and the phone app (.apk). The settings are deploy.sh's
# (server/deploy.local, or XRNMONI_HOST and XRNMONI_DIR in the environment); a host given wins.
#
#     server/put-downloads.sh [-H ssh-host] FILE...
#
# Each file goes in whole or not at all (sent beside it under a dot name, then renamed), so the Get
# page never offers half of one. The page shows the newest file of each kind; older ones stay in
# the folder until removed by hand.
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
if [ -f "$HERE/deploy.local" ]; then
    . "$HERE/deploy.local"
fi
HOST=${XRNMONI_HOST:-}
DIR=${XRNMONI_DIR:-xrnmoni}
if [ "${1:-}" = -H ]; then
    HOST=${2:-}
    shift 2
fi
if [ -z "$HOST" ]; then
    echo "Which server? Give its ssh host: server/put-downloads.sh -H HOST FILE..., or set XRNMONI_HOST in server/deploy.local." >&2
    exit 2
fi
if [ $# -eq 0 ]; then
    echo "Give the files: server/put-downloads.sh FILE..." >&2
    exit 2
fi

# One word for the remote shell, whatever is in it.
q() {
    printf "'%s'" "$(printf %s "$1" | sed "s/'/'\\\\''/g")"
}

for f; do
    name=${f##*/}
    case $name in
        "" | [!A-Za-z0-9]* | *[!A-Za-z0-9._-]*)
            echo "$name: a name of letters, digits, . _ and - only, as the hub serves" >&2
            exit 2 ;;
    esac
    case $name in
        *.apk | *.zip | *.xpi | *.tar.gz | *.exe) ;;
        *) echo "$name: the Get page offers .apk, .zip, .xpi, .tar.gz and .exe" >&2; exit 2 ;;
    esac
    if [ ! -f "$f" ] || [ ! -s "$f" ]; then
        echo "$f: not a file, or empty" >&2
        exit 2
    fi
done

for f; do
    name=${f##*/}
    echo "Sending $name..."
    ssh "$HOST" "sh -c $(q '
        set -e
        D=$1
        case $D in /*) ;; *) D=$HOME/$D ;; esac
        mkdir -p "$D/data/downloads"
        chmod 700 "$D/data/downloads"
        cat > "$D/data/downloads/.$2.part"
        chmod 600 "$D/data/downloads/.$2.part"
        mv "$D/data/downloads/.$2.part" "$D/data/downloads/$2"
    ') sh $(q "$DIR") $(q "$name")" < "$f"
done
echo "The downloads folder now holds:"
ssh "$HOST" "sh -c $(q 'D=$1; case $D in /*) ;; *) D=$HOME/$D ;; esac; ls -l "$D/data/downloads"') sh $(q "$DIR")"
