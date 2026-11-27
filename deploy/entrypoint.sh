#!/bin/sh
set -eu
umask 077
if [ ! -f "${KAPONIR_HOME:-/data}/vault.key" ]; then
    if [ "${KAPONIR_DATASET_ENABLED:-}" = false ]; then
        kaponir init --without-dataset
    else
        kaponir init
    fi
fi
exec kaponir "$@"
