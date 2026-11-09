#!/usr/bin/env bash
# Linux launcher. Run with bash; no executable permission is required.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
SCRIPT_FILE="$SCRIPT_DIR/tcp_monitor.py"

# Defaults can also be set with environment variables or command-line options.
DAYS="${TCP_MONITOR_DAYS:-30}"
PORTS="${TCP_MONITOR_PORTS:-40002}"
BIND="${TCP_MONITOR_BIND:-0.0.0.0}"
HTTP_PORT="${TCP_MONITOR_HTTP_PORT:-8765}"
STATE="${TCP_MONITOR_STATE:-established}"
OUTPUT_DIR="${TCP_MONITOR_OUTPUT_DIR:-$SCRIPT_DIR/data}"
LOG_FILE="${TCP_MONITOR_LOG_FILE:-$SCRIPT_DIR/monitor.log}"
PYTHON_BIN="${TCP_MONITOR_PYTHON:-python3}"

usage() {
  cat <<HELP
Usage: bash tcp-monitor.sh [start|stop|restart|status] [options]

Actions: start (default), stop, restart, status.
Options:
  --days N               Retain N days of samples (default: $DAYS)
  --ports PORT1,PORT2     Local TCP ports (default: ${PORTS:-all}); 'all' means whole host
  --bind ADDRESS         HTTP bind address (default: $BIND)
  --http-port PORT        HTTP port (default: $HTTP_PORT); 0 disables HTTP
  --state STATE          established (default), all, time-wait, close-wait,
                         syn-recv, syn-sent
  --output-dir DIR       Sample and PID directory (default: ./data beside script)
  --log-file FILE        Startup/error log (default: monitor.log beside script)
  --python PATH          Python 3.8+ executable (default: python3)
  -h, --help             Show this help

Examples:
  bash tcp-monitor.sh start --days 30 --ports 40002,40000,11000 --bind 0.0.0.0 --http-port 8765
  bash tcp-monitor.sh restart --days 7 --ports 40002,40000
  bash tcp-monitor.sh status
  bash tcp-monitor.sh stop

For a custom --output-dir, use the SAME directory for stop/status/restart.
CLI ports override TCP_MONITOR_PORTS. Other TCP_MONITOR_* variables override defaults.
Restart uses the options/defaults supplied to that command; it does not reuse old CLI options.
HELP
}

die() { printf 'tcp-monitor: %s\n' "$*" >&2; exit 2; }
need_value() { [[ $# -ge 2 && $2 != --* ]] || die "Missing value for $1"; }
valid_port() { [[ $1 =~ ^[0-9]{1,5}$ ]] && ((10#$1 >= $2 && 10#$1 <= 65535)); }

ACTION=start
if [[ $# -gt 0 ]]; then
  case "$1" in start|stop|restart|status) ACTION="$1"; shift ;; esac
fi
while [[ $# -gt 0 ]]; do
  case "$1" in
    --days) need_value "$@"; DAYS="$2"; shift 2 ;;
    --ports) need_value "$@"; PORTS="$2"; shift 2 ;;
    --bind) need_value "$@"; BIND="$2"; shift 2 ;;
    --http-port) need_value "$@"; HTTP_PORT="$2"; shift 2 ;;
    --state) need_value "$@"; STATE="$2"; shift 2 ;;
    --output-dir) need_value "$@"; OUTPUT_DIR="$2"; shift 2 ;;
    --log-file) need_value "$@"; LOG_FILE="$2"; shift 2 ;;
    --python) need_value "$@"; PYTHON_BIN="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown argument: $1 (use --help)" ;;
  esac
done

[[ $DAYS =~ ^[1-9][0-9]*$ ]] || die "--days must be a positive integer"
valid_port "$HTTP_PORT" 0 || die "--http-port must be 0..65535"
HTTP_PORT=$((10#$HTTP_PORT))
[[ -n $BIND && -n $OUTPUT_DIR && -n $LOG_FILE ]] || die "Addresses and paths must not be empty"
case "$STATE" in established|all|time-wait|close-wait|syn-recv|syn-sent) ;; *) die "Invalid --state: $STATE" ;; esac

PORT_ARGS=()
if [[ -n $PORTS && $PORTS != all ]]; then
  PORTS="${PORTS//,/ }"
  read -r -a PORT_LIST <<< "$PORTS"
  for port in "${PORT_LIST[@]}"; do
    valid_port "$port" 1 || die "Invalid local port: $port (expected 1..65535)"
    PORT_ARGS+=(--port "$((10#$port))")
  done
fi

[[ $(uname -s) == Linux ]] || die "This launcher requires Linux"
command -v flock >/dev/null 2>&1 || die "flock not found; install util-linux"
mkdir -p -- "$OUTPUT_DIR"
OUTPUT_DIR="$(cd -- "$OUTPUT_DIR" && pwd -P)"
PID_FILE="$OUTPUT_DIR/tcp-monitor.pid"
# Serialize launcher operations; do not let the background collector inherit this lock.
exec 9>"$OUTPUT_DIR/.tcp-launcher.lock"
flock -n 9 || die "Another launcher command is using this output directory"

# /proc start time prevents a stale PID file from targeting a reused process ID.
process_start() {
  local pid="$1" stat rest
  local -a fields
  [[ -r /proc/$pid/stat ]] || return 1
  stat="$(cat "/proc/$pid/stat" 2>/dev/null)" || return 1
  rest="${stat##*) }"
  read -r -a fields <<< "$rest"
  [[ ${fields[0]:-Z} != Z && -n ${fields[19]:-} ]] || return 1
  printf '%s' "${fields[19]}"
}

read_running() {
  local current arg found=false
  RUNNING_PID=""; RUNNING_START=""
  [[ -f $PID_FILE ]] || return 1
  read -r RUNNING_PID RUNNING_START < "$PID_FILE" || return 1
  [[ $RUNNING_PID =~ ^[1-9][0-9]*$ && $RUNNING_START =~ ^[0-9]+$ ]] || return 1
  kill -0 "$RUNNING_PID" 2>/dev/null || return 1
  current="$(process_start "$RUNNING_PID")" || return 1
  [[ $current == "$RUNNING_START" && -r /proc/$RUNNING_PID/cmdline ]] || return 1
  while IFS= read -r -d '' arg; do
    if [[ $arg == "$SCRIPT_FILE" ]]; then found=true; fi
  done < "/proc/$RUNNING_PID/cmdline"
  [[ $found == true ]]
}

preflight() {
  [[ -f $SCRIPT_FILE ]] || die "Missing $SCRIPT_FILE"
  if (( HTTP_PORT != 0 )); then [[ -f $SCRIPT_DIR/index.html ]] || die "Missing index.html"; fi
  command -v "$PYTHON_BIN" >/dev/null 2>&1 || die "Python executable not found: $PYTHON_BIN"
  "$PYTHON_BIN" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' || die "Python 3.8+ is required"
  command -v ss >/dev/null 2>&1 || die "ss not found; install iproute2 / iproute"
  mkdir -p -- "$(dirname -- "$LOG_FILE")"
  LOG_FILE="$(cd -- "$(dirname -- "$LOG_FILE")" && pwd -P)/$(basename -- "$LOG_FILE")"
  local protected
  for protected in "$SCRIPT_FILE" "$SCRIPT_DIR/index.html" "$SCRIPT_DIR/tcp-monitor.sh" \
    "$OUTPUT_DIR/tcp_connections.txt" "$PID_FILE" "$OUTPUT_DIR/.tcp-monitor.lock" \
    "$OUTPUT_DIR/.tcp-launcher.lock" "$OUTPUT_DIR/.tcp_connections.tmp"; do
    if [[ $LOG_FILE == "$protected" || ( -e $LOG_FILE && $LOG_FILE -ef $protected ) ]]; then
      die "Log path conflicts with a program or data file: $protected"
    fi
  done
}

start_monitor() {
  if read_running; then
    printf 'Already running: PID %s. Use restart to change options.\n' "$RUNNING_PID"
    return 0
  fi
  local pid started attempt
  # Replace the startup log on each launch so repeated restarts do not grow it forever.
  nohup "$PYTHON_BIN" -u "$SCRIPT_FILE" --days "$DAYS" "${PORT_ARGS[@]}" \
    --bind "$BIND" --http-port "$HTTP_PORT" --state "$STATE" --output-dir "$OUTPUT_DIR" \
    9>&- </dev/null >"$LOG_FILE" 2>&1 &
  pid=$!
  if ! started="$(process_start "$pid")"; then
    if kill -0 "$pid" 2>/dev/null; then
      printf 'Cannot verify PID %s through /proc; inspect %s. No managed PID file was written.\n' "$pid" "$LOG_FILE" >&2
    else
      wait "$pid" || true
      cat -- "$LOG_FILE" >&2
    fi
    return 1
  fi
  printf '%s %s\n' "$pid" "$started" > "$PID_FILE"
  # Wait for the collector's initialization marker, rather than declaring success
  # while Python is still checking history, its data lock or the HTTP bind.
  for ((attempt=0; attempt<100; attempt++)); do
    # The new child can briefly still be in nohup/exec before Python appears in
    # cmdline. Check its known birth time here, not the fully initialized identity.
    if [[ $(process_start "$pid" || true) != "$started" ]]; then
      rm -f -- "$PID_FILE"
      wait "$pid" || true
      printf 'Startup failed; see %s\n' "$LOG_FILE" >&2
      cat -- "$LOG_FILE" >&2
      return 1
    fi
    if grep -q '^TCP state=' "$LOG_FILE" && read_running; then
      printf 'Started: PID %s\nData: %s/tcp_connections.txt\nLog: %s\n' "$pid" "$OUTPUT_DIR" "$LOG_FILE"
      if (( HTTP_PORT != 0 )); then printf 'HTTP: http://%s:%s/\n' "$BIND" "$HTTP_PORT"; else printf 'HTTP disabled\n'; fi
      return 0
    fi
    sleep 0.1
  done
  printf 'Process is still initializing: PID %s. Check %s and run status later.\n' "$pid" "$LOG_FILE" >&2
  return 1
}

stop_monitor() {
  if ! read_running; then
    printf 'Not running; no process was signaled.\n'
    rm -f -- "$PID_FILE"
    return 0
  fi
  local pid="$RUNNING_PID" attempt
  kill -TERM "$pid"
  for ((attempt=0; attempt<30; attempt++)); do
    if ! read_running; then
      rm -f -- "$PID_FILE"
      printf 'Stopped: PID %s\n' "$pid"
      return 0
    fi
    sleep 1
  done
  printf 'Still stopping: PID %s; check status later. No SIGKILL was sent.\n' "$pid" >&2
  return 1
}

case "$ACTION" in
  start) preflight; start_monitor ;;
  restart) preflight; stop_monitor; start_monitor ;;
  stop) stop_monitor ;;
  status)
    if read_running; then
      printf 'Running: PID %s\nData: %s/tcp_connections.txt\n' "$RUNNING_PID" "$OUTPUT_DIR"
    else
      printf 'Not running\n'; exit 3
    fi ;;
esac
