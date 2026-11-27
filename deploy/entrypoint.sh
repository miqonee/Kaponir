#!/bin/sh
set -eu
umask 077
data_dir="${KAPONIR_HOME:-/data}"
if [ ! -f "$data_dir/vault.db" ] || [ ! -f "$data_dir/config.toml" ]; then
    if [ "${KAPONIR_DATASET_ENABLED:-}" = false ]; then
        kaponir init --without-dataset
    else
        kaponir init
    fi
fi
exec kaponir "$@"
