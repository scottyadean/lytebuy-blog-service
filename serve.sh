#!/bin/bash
export AWS_PROFILE=dev-lytebuy;
echo "Loading any env vars from .env"
eval "$(
  cat .env | awk '!/^\s*#/' | awk '!/^\s*$/' | while IFS='' read -r line; do
    key=$(echo "$line" | cut -d '=' -f 1)
    value=$(echo "$line" | cut -d '=' -f 2-)
    echo "export $key=\"$value\""
  done
)"

find . -name "*.pyc" -exec rm -f {} \;
sls offline --stage local --noAuth --httpPort 5600 --reloadHandler;
