#!/bin/sh
set -eu
umask 077
if [ ! -f "${KAPONIR_HOME:-/data}/vault.key" ]; then
    kaponir init
fi
exec kaponir "$@"
