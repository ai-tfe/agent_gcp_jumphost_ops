#!/bin/sh
set -eu
case "${GOOGLE_CLOUD_PROJECT:-}" in
  ''|[0-9]*) export GOOGLE_CLOUD_PROJECT=svcprjprd ;;
esac
if [ -z "${GOOGLE_CLOUD_LOCATION:-}" ]; then
  export GOOGLE_CLOUD_LOCATION=us-east4
fi
cd /app
exec uvicorn main:app --host 0.0.0.0 --port "${PORT:-8080}"
