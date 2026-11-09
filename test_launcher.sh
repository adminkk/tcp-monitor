#!/usr/bin/env bash
# Exercise launcher arguments and process lifecycle using a harmless fake collector.
# Git Bash runs syntax and invalid-argument checks; lifecycle checks require Linux.
set -euo pipefail
SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
TEST_BASE="$(cd -- "${TMPDIR:-/tmp}" && pwd -P)"
FIXTURE="$(mktemp -d "$TEST_BASE/tcp-launcher-test.XXXXXX")"
HOST_SYSTEM="$(uname -s)"
cleanup() {
  bash "$FIXTURE/tcp-monitor.sh" stop --output-dir "$FIXTURE/data with spaces" >/dev/null 2>&1 || true
  # Resolve and check the exact temporary target before recursive cleanup.
  if [[ -d $FIXTURE && $FIXTURE == "$TEST_BASE"/tcp-launcher-test.* &&
        $(cd -- "$FIXTURE" && pwd -P) == "$FIXTURE" ]]; then
    rm -rf -- "$FIXTURE"
  fi
}
trap cleanup EXIT
mkdir -p "$FIXTURE/bin"
cp "$SOURCE_DIR/tcp-monitor.sh" "$FIXTURE/tcp-monitor.sh"
touch "$FIXTURE/index.html"
cat > "$FIXTURE/bin/uname" <<'SH'
#!/usr/bin/env bash
echo Linux
SH
for command in ss flock; do
  printf '#!/usr/bin/env bash\nexit 0\n' > "$FIXTURE/bin/$command"
done
cat > "$FIXTURE/bin/fake-python" <<'SH'
#!/usr/bin/env bash
if [[ ${1:-} == -c ]]; then exit 0; fi
shift # -u
exec bash "$@"
SH
cat > "$FIXTURE/tcp_monitor.py" <<'SH'
#!/usr/bin/env bash
here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
printf '%s\n' "$@" > "$here/received-args"
for arg in "$@"; do if [[ $arg == fail_initialization ]]; then exit 7; fi; done
printf 'TCP state=established test collector\n'
trap 'exit 0' TERM INT
while :; do sleep 0.1; done
SH
chmod +x "$FIXTURE/bin/"*
export PATH="$FIXTURE/bin:$PATH"
run() { bash "$FIXTURE/tcp-monitor.sh" "$@" --output-dir "$FIXTURE/data with spaces" --python "$FIXTURE/bin/fake-python"; }
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
reject() {
  local code=0
  run start "$@" > "$FIXTURE/rejected.log" 2>&1 || code=$?
  [[ $code == 2 ]] || fail "Invalid arguments were not rejected: $* (code $code)"
}

bash -n "$FIXTURE/tcp-monitor.sh"
reject --days 0
reject --ports 40002,65536
reject --http-port -1
reject --state wrong
reject --unknown value
reject --ports
reject --port 40002

if [[ $HOST_SYSTEM != Linux ]]; then
  # Verify exact argument forwarding even on Git Bash, using a collector that
  # records its argv and exits immediately rather than relying on Windows PIDs.
  code=0
  TCP_MONITOR_PORTS=9999 run start --days 30 --ports 40002,40000 \
    --bind fail_initialization --http-port 0 --log-file "$FIXTURE/start log.txt" \
    > "$FIXTURE/forwarding.log" 2>&1 || code=$?
  [[ $code == 1 ]] || fail 'Expected fake collector initialization failure'
  for ((attempt=0; attempt<20; attempt++)); do
    [[ -f $FIXTURE/received-args ]] && break
    sleep 0.1
  done
  mapfile -t arguments < "$FIXTURE/received-args"
  expected=(--days 30 --port 40002 --port 40000 --bind fail_initialization --http-port 0 --state established --output-dir "$FIXTURE/data with spaces")
  [[ ${#arguments[@]} == ${#expected[@]} ]] || fail 'Wrong forwarded argument count'
  for ((i=0; i<${#expected[@]}; i++)); do
    [[ ${arguments[i]} == "${expected[i]}" ]] || fail "Forwarded argument $i was not preserved"
  done
  printf 'Syntax, 7 invalid-argument checks, and argument forwarding passed. SKIP lifecycle checks: Linux /proc required.\n'
  exit 0
fi

TCP_MONITOR_PORTS=9999 run start --days 30 --ports 40002,40000 --bind 0.0.0.0 --http-port 0 --log-file "$FIXTURE/start log.txt"
mapfile -t arguments < "$FIXTURE/received-args"
expected=(--days 30 --port 40002 --port 40000 --bind 0.0.0.0 --http-port 0 --state established --output-dir "$FIXTURE/data with spaces")
[[ ${#arguments[@]} == ${#expected[@]} ]] || fail 'Wrong argument count'
for ((i=0; i<${#expected[@]}; i++)); do
  [[ ${arguments[i]} == "${expected[i]}" ]] || fail "Argument $i was not preserved"
done
run status
read -r first_pid _ < "$FIXTURE/data with spaces/tcp-monitor.pid"
run start --days 90
read -r same_pid _ < "$FIXTURE/data with spaces/tcp-monitor.pid"
[[ $first_pid == "$same_pid" ]] || fail 'Repeated start created a second process'
run restart --days 5 --ports 11000,40002 --http-port 0
read -r second_pid _ < "$FIXTURE/data with spaces/tcp-monitor.pid"
[[ $first_pid != "$second_pid" ]] || fail 'Restart did not replace the process'
grep -qx 11000 "$FIXTURE/received-args" || fail '--ports was not expanded'
run stop
run stop
code=0; run status || code=$?
[[ $code == 3 ]] || fail 'Stopped status should return 3'

# A PID file pointing at this unrelated test process must never signal it.
stat="$(cat "/proc/$$/stat")"; rest="${stat##*) }"; read -r -a fields <<< "$rest"
printf '%s %s\n' "$$" "${fields[19]}" > "$FIXTURE/data with spaces/tcp-monitor.pid"
run stop
[[ ! -e "$FIXTURE/data with spaces/tcp-monitor.pid" ]] || fail 'Stale PID file was not removed'

code=0; run start --bind fail_initialization --http-port 0 > "$FIXTURE/failure.log" 2>&1 || code=$?
[[ $code == 1 ]] || fail 'Failed initialization was reported as successful'
[[ ! -e "$FIXTURE/data with spaces/tcp-monitor.pid" ]] || fail 'Failed startup left a PID file'
printf 'Launcher checks passed (arguments, defaults, lifecycle, stale PID, initialization failure).\n'
