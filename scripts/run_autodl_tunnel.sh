#!/usr/bin/env bash
set -u

ssh_pid=""

stop_tunnel() {
  if [[ -n "${ssh_pid}" ]]; then
    kill "${ssh_pid}" 2>/dev/null || true
    wait "${ssh_pid}" 2>/dev/null || true
  fi
  exit 0
}

trap stop_tunnel INT TERM

while true; do
  printf '[%s] connecting AutoDL port tunnel\n' "$(date -Iseconds)"
  ssh -N \
    -o BatchMode=yes \
    -o ExitOnForwardFailure=yes \
    -o ConnectTimeout=15 \
    -o ServerAliveInterval=30 \
    -o ServerAliveCountMax=6 \
    -L 127.0.0.1:4173:127.0.0.1:4173 \
    -L 127.0.0.1:6006:127.0.0.1:6006 \
    autodl-minimax &
  ssh_pid=$!
  wait "${ssh_pid}"
  status=$?
  ssh_pid=""
  printf '[%s] tunnel exited with status %s; reconnecting in 5 seconds\n' \
    "$(date -Iseconds)" "${status}"
  sleep 5
done
