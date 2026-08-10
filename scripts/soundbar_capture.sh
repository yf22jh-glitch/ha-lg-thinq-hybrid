#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
TARGET_IP="${LG_SOUNDBAR_IP:-}"
AP_HOST="${LG_SOUNDBAR_AP_HOST:-ap}"
ROUTER_HOST="${LG_SOUNDBAR_ROUTER_HOST:-router}"
AP_INTERFACE="${LG_SOUNDBAR_AP_INTERFACE:-br-lan}"
CAPTURE_DIR="${LG_SOUNDBAR_CAPTURE_DIR:-${XDG_STATE_HOME:-${TMPDIR:-/tmp}}/lg-soundbar-captures}"
STATE_DIR="${LG_SOUNDBAR_STATE_DIR:-${XDG_RUNTIME_DIR:-${TMPDIR:-/tmp}}/lg-soundbar-capture}"

REMOTE_TCPDUMP="/tmp/lg-soundbar-tcpdump"
REMOTE_LIBPCAP="/tmp/lg-soundbar-libpcap.so.1"
REMOTE_LIBPCAP_LINK="/tmp/libpcap.so.1"
PID_FILE="${STATE_DIR}/capture.pid"
PATH_FILE="${STATE_DIR}/capture.path"
LOG_PATH_FILE="${STATE_DIR}/capture.log.path"

usage() {
  printf 'Usage: LG_SOUNDBAR_IP=192.0.2.10 %s {start|stop|status|latest|decode [pcap]}\n' "$0" >&2
}

require_target_ip() {
  if [[ ! "$TARGET_IP" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
    printf 'LG_SOUNDBAR_IP must be set to the target IPv4 address.\n' >&2
    return 1
  fi
}

is_running() {
  [[ -f "$PID_FILE" ]] || return 1
  local pid
  pid="$(<"$PID_FILE")"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  kill -0 "$pid" 2>/dev/null
}

stage_ap_tcpdump() {
  if ssh "$AP_HOST" \
    "test -x '$REMOTE_TCPDUMP' && test -f '$REMOTE_LIBPCAP'"; then
    ssh "$AP_HOST" "ln -sf '$REMOTE_LIBPCAP' '$REMOTE_LIBPCAP_LINK'"
    return
  fi

  local stage_dir
  stage_dir="$(mktemp -d "${TMPDIR:-/tmp}/lg-soundbar-tcpdump.XXXXXX")"
  local tcpdump_local="${stage_dir}/lg-soundbar-tcpdump"
  local libpcap_local="${stage_dir}/lg-soundbar-libpcap.so.1"

  scp -O -q "${ROUTER_HOST}:/usr/bin/tcpdump" "$tcpdump_local"
  scp -O -q "${ROUTER_HOST}:/usr/lib/libpcap.so.1.10.6" "$libpcap_local"
  scp -O -q "$tcpdump_local" "$libpcap_local" "${AP_HOST}:/tmp/"
  ssh "$AP_HOST" \
    "chmod 0755 '$REMOTE_TCPDUMP' && ln -sf '$REMOTE_LIBPCAP' '$REMOTE_LIBPCAP_LINK'"

  rm -f "$tcpdump_local" "$libpcap_local"
  rmdir "$stage_dir"
}

start_capture() {
  require_target_ip
  mkdir -p "$CAPTURE_DIR" "$STATE_DIR"
  if is_running; then
    printf 'Capture is already running (PID %s): %s\n' \
      "$(<"$PID_FILE")" "$(<"$PATH_FILE")"
    return
  fi

  stage_ap_tcpdump

  local stamp capture_path log_path remote_command
  stamp="$(date '+%Y%m%d-%H%M%S')"
  capture_path="${CAPTURE_DIR}/lg-soundbar-${stamp}.pcap"
  log_path="${CAPTURE_DIR}/lg-soundbar-${stamp}.tcpdump.log"
  remote_command="LD_LIBRARY_PATH=/tmp exec ${REMOTE_TCPDUMP} -U -n -i ${AP_INTERFACE} -s 0 -w - 'host ${TARGET_IP} and tcp port 9741'"

  nohup ssh \
    -o ServerAliveInterval=15 \
    -o ServerAliveCountMax=4 \
    "$AP_HOST" "$remote_command" \
    >"$capture_path" 2>"$log_path" </dev/null &
  local pid=$!

  printf '%s\n' "$pid" >"$PID_FILE"
  printf '%s\n' "$capture_path" >"$PATH_FILE"
  printf '%s\n' "$log_path" >"$LOG_PATH_FILE"

  sleep 1
  if ! kill -0 "$pid" 2>/dev/null; then
    printf 'Capture failed to start. Log: %s\n' "$log_path" >&2
    sed -n '1,80p' "$log_path" >&2
    return 1
  fi

  printf 'Capture started (PID %s): %s\n' "$pid" "$capture_path"
}

stop_capture() {
  if ! is_running; then
    printf 'Capture is not running.\n'
    [[ -f "$PATH_FILE" ]] && printf 'Latest capture: %s\n' "$(<"$PATH_FILE")"
    return
  fi

  local pid capture_path
  pid="$(<"$PID_FILE")"
  capture_path="$(<"$PATH_FILE")"
  kill -INT "$pid"

  local attempt
  for attempt in 1 2 3 4 5; do
    kill -0 "$pid" 2>/dev/null || break
    sleep 1
  done
  if kill -0 "$pid" 2>/dev/null; then
    kill -TERM "$pid"
  fi

  printf 'Capture stopped: %s\n' "$capture_path"
  if [[ -f "$capture_path" ]]; then
    printf 'Size: %s bytes\n' "$(wc -c <"$capture_path")"
  fi
}

show_status() {
  if is_running; then
    local capture_path
    capture_path="$(<"$PATH_FILE")"
    printf 'running PID=%s file=%s size=%s\n' \
      "$(<"$PID_FILE")" "$capture_path" "$(wc -c <"$capture_path")"
    return
  fi
  printf 'stopped\n'
  [[ -f "$PATH_FILE" ]] && printf 'latest=%s\n' "$(<"$PATH_FILE")"
}

show_latest() {
  if [[ ! -f "$PATH_FILE" ]]; then
    printf 'No capture has been recorded yet.\n' >&2
    return 1
  fi
  printf '%s\n' "$(<"$PATH_FILE")"
}

decode_capture() {
  require_target_ip
  local capture_path="${1:-}"
  if [[ -z "$capture_path" ]]; then
    if [[ ! -f "$PATH_FILE" ]]; then
      printf 'No capture has been recorded yet.\n' >&2
      return 1
    fi
    capture_path="$(<"$PATH_FILE")"
  fi
  "${SCRIPT_DIR}/decode_lg_soundbar_pcap.py" \
    --target-ip "$TARGET_IP" "$capture_path"
}

case "${1:-}" in
  start) start_capture ;;
  stop) stop_capture ;;
  status) show_status ;;
  latest) show_latest ;;
  decode) decode_capture "${2:-}" ;;
  *) usage; exit 2 ;;
esac
