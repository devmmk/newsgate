#!/bin/sh
set -eu

chown -R appuser:appuser /app/data /app/sessions

exec gosu appuser "$@"
