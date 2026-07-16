#!/bin/bash
set -euo pipefail
umask 077
export LC_ALL=C
export LANG=C

TARGET=""
CUSTOMER=""
CONFIRM_LIVE=""

usage() {
    echo "Usage: $0 --target automation1 --customer POSITIVE_INTEGER --confirm-live CAM-LAYER1-LIVE" >&2
    exit 64
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --target|--customer|--confirm-live)
            [ "$#" -ge 2 ] || usage
            option=$1
            value=$2
            shift 2
            case "$option" in
                --target) TARGET=$value ;;
                --customer) CUSTOMER=$value ;;
                --confirm-live) CONFIRM_LIVE=$value ;;
            esac
            ;;
        *) usage ;;
    esac
done

[ "$TARGET" = "automation1" ] || usage
case "$CUSTOMER" in ''|*[!0-9]*|0) usage ;; esac
[ "$CONFIRM_LIVE" = "CAM-LAYER1-LIVE" ] || usage

for command in ssh ssh-keygen scp sftp python3 mktemp grep; do
    command -v "$command" >/dev/null 2>&1 || {
        echo "FAIL: required command is unavailable: $command" >&2
        exit 69
    }
done

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
PROBE="$SCRIPT_DIR/cam-layer1-live-probe.py"
[ -f "$PROBE" ] || { echo "FAIL: live probe is missing: $PROBE" >&2; exit 66; }

TIMESTAMP=$(date -u '+%Y%m%dT%H%M%SZ' | tr '[:upper:]' '[:lower:]')
CAM_ID="cam_smoke_${TIMESTAMP}_$$"
TMP_DIR=$(mktemp -d "${TMPDIR:-/tmp}/logan-cam-live.XXXXXX")
chmod 700 "$TMP_DIR"
KEY_PATH="$TMP_DIR/logan-cam.key"
KNOWN_HOSTS="$TMP_DIR/known_hosts"
PROVISION_REQUEST="$TMP_DIR/provision.request.json"
PROVISION_RESPONSE="$TMP_DIR/provision.response.json"
DEPROVISION_REQUEST="$TMP_DIR/deprovision.request.json"
DEPROVISION_RESPONSE="$TMP_DIR/deprovision.response.json"
CONNECTION_META="$TMP_DIR/connection.json"
HOLD_PID=""
PROVISION_MAY_HAVE_COMMITTED=0
DEPROVISIONED=0
LOCAL_FINGERPRINT=""
ATTACK_REMOTE_PATH="/tmp/logan-cam-layer1-${CAM_ID}.txt"
ADMIN_SSH_OPTIONS=(
    -o BatchMode=yes
    -o ConnectTimeout=10
    -o ServerAliveInterval=5
    -o ServerAliveCountMax=2
    "$TARGET"
)

pass() {
    printf 'PASS: %s\n' "$1"
}

fail() {
    printf 'FAIL: %s\n' "$1" >&2
    exit 1
}

json_field() {
    python3 - "$1" "$2" <<'PY'
import json
import pathlib
import sys

value = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
for part in sys.argv[2].split("."):
    if not isinstance(value, dict) or part not in value:
        raise SystemExit(f"missing JSON field: {sys.argv[2]}")
    value = value[part]
if isinstance(value, bool):
    print("true" if value else "false")
elif isinstance(value, (str, int)):
    print(value)
else:
    raise SystemExit(f"JSON field is not scalar: {sys.argv[2]}")
PY
}

write_deprovision_request() {
    python3 - "$CAM_ID" "$LOCAL_FINGERPRINT" "$DEPROVISION_REQUEST" <<'PY'
import json
import pathlib
import sys

pathlib.Path(sys.argv[3]).write_text(
    json.dumps({
        "cam_id": sys.argv[1],
        "expected_fingerprint": sys.argv[2],
        "confirm": True,
    }, separators=(",", ":")) + "\n",
    encoding="utf-8",
)
PY
    chmod 600 "$DEPROVISION_REQUEST"
}

cleanup() {
    exit_status=$?
    trap - EXIT HUP INT TERM
    if [ -n "$HOLD_PID" ] && kill -0 "$HOLD_PID" 2>/dev/null; then
        kill "$HOLD_PID" 2>/dev/null || true
        wait "$HOLD_PID" 2>/dev/null || true
    fi
    if [ "$PROVISION_MAY_HAVE_COMMITTED" -eq 1 ] && [ "$DEPROVISIONED" -eq 0 ] && [ -n "$LOCAL_FINGERPRINT" ]; then
        write_deprovision_request
        cleanup_response="$TMP_DIR/cleanup.response.json"
        cleanup_rc=0
        ssh "${ADMIN_SSH_OPTIONS[@]}" 'sudo /opt/logan-mcp/bin/cam-admin deprovision --json' \
            < "$DEPROVISION_REQUEST" > "$cleanup_response" || cleanup_rc=$?
        cleanup_verified=0
        if [ -s "$cleanup_response" ]; then
            if python3 - "$cleanup_response" "$CAM_ID" "$LOCAL_FINGERPRINT" "$cleanup_rc" <<'PY'
import json
import pathlib
import sys

payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
status = payload.get("status")
expected_exit = 0 if status == "SUCCESS" else 1
ok = (
    status in {"SUCCESS", "FAILED_ACCESS_REVOKED_CLEANUP_REQUIRED"}
    and int(sys.argv[4]) == expected_exit
    and payload.get("cam_id") == sys.argv[2]
    and payload.get("fingerprint") == sys.argv[3]
    and payload.get("access_revoked") is True
)
raise SystemExit(0 if ok else 1)
PY
            then
                cleanup_verified=1
            fi
        fi
        if [ "$cleanup_verified" -ne 1 ]; then
            printf 'FAIL: automatic cleanup was not verified. Run exactly:\n' >&2
            printf "printf '%%s\\n' '%s' | ssh '%s' 'sudo /opt/logan-mcp/bin/cam-admin deprovision --json'\n" \
                "$(tr -d '\n' < "$DEPROVISION_REQUEST")" "$TARGET" >&2
        fi
    fi
    ssh "${ADMIN_SSH_OPTIONS[@]}" "sudo /bin/rm -f -- '$ATTACK_REMOTE_PATH'" >/dev/null 2>&1 || true
    rm -rf -- "$TMP_DIR"
    exit "$exit_status"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

run_bounded() {
    seconds=$1
    stdout_path=$2
    stderr_path=$3
    shift 3
    python3 - "$seconds" "$stdout_path" "$stderr_path" "$@" <<'PY'
import pathlib
import subprocess
import sys

seconds = int(sys.argv[1])
stdout_path = pathlib.Path(sys.argv[2])
stderr_path = pathlib.Path(sys.argv[3])
command = sys.argv[4:]

def as_text(value):
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value

try:
    result = subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=seconds,
    )
except subprocess.TimeoutExpired as exc:
    stdout_path.write_text(as_text(exc.stdout), encoding="utf-8")
    stderr_path.write_text(as_text(exc.stderr), encoding="utf-8")
    raise SystemExit(124)
stdout_path.write_text(result.stdout, encoding="utf-8")
stderr_path.write_text(result.stderr, encoding="utf-8")
raise SystemExit(result.returncode if result.returncode >= 0 else 128 - result.returncode)
PY
}

run_no_marker_attack() {
    label=$1
    shift
    stdout_path="$TMP_DIR/$label.stdout"
    stderr_path="$TMP_DIR/$label.stderr"
    attack_rc=0
    run_bounded 10 "$stdout_path" "$stderr_path" "$@" || attack_rc=$?
    [ -f "$stdout_path" ] && [ -f "$stderr_path" ] || fail "$label runner did not capture output"
    if grep -F "$ATTACK_MARKER" "$stdout_path" "$stderr_path" >/dev/null 2>&1; then
        fail "$label printed the command-override marker (exit $attack_rc)"
    fi
    pass "$label"
}

run_refusal_attack() {
    label=$1
    reject_timeout=$2
    shift 2
    stdout_path="$TMP_DIR/$label.stdout"
    stderr_path="$TMP_DIR/$label.stderr"
    attack_rc=0
    run_bounded 10 "$stdout_path" "$stderr_path" "$@" || attack_rc=$?
    [ -f "$stdout_path" ] && [ -f "$stderr_path" ] || fail "$label runner did not capture output"
    [ "$attack_rc" -ne 0 ] || fail "$label unexpectedly succeeded"
    if [ "$reject_timeout" = "true" ] && [ "$attack_rc" -eq 124 ]; then
        fail "$label remained active instead of being refused"
    fi
    if grep -F "$ATTACK_MARKER" "$stdout_path" "$stderr_path" >/dev/null 2>&1; then
        fail "$label printed the command-override marker"
    fi
    pass "$label"
}

run_local_forwarding_denial() {
    port=$1
    stdout_path="$TMP_DIR/local-forwarding.stdout"
    stderr_path="$TMP_DIR/local-forwarding.stderr"
    if ! python3 - "$port" "$stdout_path" "$stderr_path" \
        ssh "${SSH_OPTIONS[@]}" -o ExitOnForwardFailure=yes -N \
        -L "127.0.0.1:${port}:127.0.0.1:22" "$CAM_DEST" <<'PY'
import pathlib
import socket
import subprocess
import sys
import time

port = int(sys.argv[1])
stdout_path = pathlib.Path(sys.argv[2])
stderr_path = pathlib.Path(sys.argv[3])
command = sys.argv[4:]
process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
result = ""
try:
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if process.poll() is not None:
            result = "forwarding process exited before opening a listener"
            break
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5) as client:
                client.settimeout(3)
                response = client.recv(64)
            if response.startswith(b"SSH-"):
                raise SystemExit("local forwarding reached the remote SSH service")
            result = "forwarding channel was denied"
            break
        except (ConnectionRefusedError, TimeoutError, socket.timeout):
            time.sleep(0.1)
    else:
        raise SystemExit("local forwarding listener did not become testable")
finally:
    if process.poll() is None:
        process.terminate()
    try:
        stdout, stderr = process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate()
    stdout_path.write_bytes(stdout)
    stderr_path.write_bytes(stderr)

if not result:
    raise SystemExit("local forwarding result was not determined")
PY
    then
        fail "local-forwarding reached the remote SSH service"
    fi
    pass "local-forwarding channel denial"
}

# 1. Bootstrap boundary.
BOOTSTRAP_RESPONSE="$TMP_DIR/bootstrap.response.json"
if ! ssh "${ADMIN_SSH_OPTIONS[@]}" 'sudo /opt/logan-mcp/bin/cam-admin bootstrap-check --json' > "$BOOTSTRAP_RESPONSE"; then
    fail "bootstrap-check SSH call failed"
fi
python3 - "$BOOTSTRAP_RESPONSE" <<'PY' || fail "bootstrap-check did not return all true checks"
import json
import pathlib
import sys

payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
checks = payload.get("checks")
if payload.get("status") != "SUCCESS" or not isinstance(checks, dict) or not checks:
    raise SystemExit(1)
if any(value is not True for value in checks.values()):
    raise SystemExit(1)
PY
pass "bootstrap-check"

# 2. Dedicated key and strict public-only provision request.
ssh-keygen -q -t ed25519 -N '' -C "logan-cam:$CAM_ID" -f "$KEY_PATH"
chmod 600 "$KEY_PATH"
PUBLIC_KEY=$(tr -d '\n' < "$KEY_PATH.pub")
LOCAL_FINGERPRINT=$(ssh-keygen -lf "$KEY_PATH.pub" -E sha256 | awk 'NR == 1 { print $2 }')
case "$LOCAL_FINGERPRINT" in SHA256:*) ;; *) fail "local fingerprint is invalid" ;; esac
python3 - "$CAM_ID" "$CUSTOMER" "$PUBLIC_KEY" "$PROVISION_REQUEST" <<'PY'
import json
import pathlib
import sys

pathlib.Path(sys.argv[4]).write_text(
    json.dumps({
        "cam_id": sys.argv[1],
        "customers": [int(sys.argv[2])],
        "allow_delivery": False,
        "public_key": sys.argv[3],
    }, separators=(",", ":")) + "\n",
    encoding="utf-8",
)
PY
chmod 600 "$PROVISION_REQUEST"
PROVISION_MAY_HAVE_COMMITTED=1
provision_rc=0
ssh "${ADMIN_SSH_OPTIONS[@]}" 'sudo /opt/logan-mcp/bin/cam-admin provision --json' \
    < "$PROVISION_REQUEST" > "$PROVISION_RESPONSE" || provision_rc=$?
[ "$provision_rc" -eq 0 ] || fail "provision returned SSH exit $provision_rc"
python3 - "$PROVISION_RESPONSE" "$CAM_ID" "$CUSTOMER" "$LOCAL_FINGERPRINT" "$CONNECTION_META" <<'PY' || fail "provision response did not match the local request"
import json
import pathlib
import re
import sys

payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
connection = payload.get("connection")
if not isinstance(connection, dict):
    raise SystemExit(1)
host = connection.get("host")
port = connection.get("port")
remote_user = connection.get("remote_user")
host_key = connection.get("host_public_key")
key_parts = host_key.split() if isinstance(host_key, str) else []
ok = (
    payload.get("status") == "SUCCESS"
    and payload.get("cam_id") == sys.argv[2]
    and payload.get("customers") == [int(sys.argv[3])]
    and payload.get("allow_delivery") is False
    and payload.get("fingerprint") == sys.argv[4]
    and isinstance(host, str)
    and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.:-]{0,252}", host)
    and type(port) is int and 1 <= port <= 65535
    and remote_user == "cam"
    and len(key_parts) in (2, 3)
    and key_parts[0] == "ssh-ed25519"
    and re.fullmatch(r"[A-Za-z0-9+/=]+", key_parts[1])
)
if not ok:
    raise SystemExit(1)
pathlib.Path(sys.argv[5]).write_text(json.dumps({
    "host": host,
    "port": port,
    "remote_user": remote_user,
    "host_public_key": " ".join(key_parts[:2]),
}, separators=(",", ":")) + "\n", encoding="utf-8")
PY
HOST=$(json_field "$CONNECTION_META" host)
PORT=$(json_field "$CONNECTION_META" port)
REMOTE_USER=$(json_field "$CONNECTION_META" remote_user)
HOST_PUBLIC_KEY=$(json_field "$CONNECTION_META" host_public_key)
[ "$REMOTE_USER" = "cam" ] || fail "provision returned an unexpected remote user"
if [ "$PORT" = "22" ]; then KNOWN_HOST_TOKEN=$HOST; else KNOWN_HOST_TOKEN="[$HOST]:$PORT"; fi
printf '%s %s\n' "$KNOWN_HOST_TOKEN" "$HOST_PUBLIC_KEY" > "$KNOWN_HOSTS"
chmod 600 "$KNOWN_HOSTS"
SSH_OPTIONS=(
    -i "$KEY_PATH"
    -o BatchMode=yes
    -o IdentitiesOnly=yes
    -o StrictHostKeyChecking=yes
    -o "UserKnownHostsFile=$KNOWN_HOSTS"
    -o ConnectTimeout=8
    -o ServerAliveInterval=2
    -o ServerAliveCountMax=2
    -p "$PORT"
)
CAM_DEST="$REMOTE_USER@$HOST"
pass "provision"

# 3-4. Pinned MCP scope and retained learned-query artifact.
INITIAL_TAG="${CAM_ID}_initial"
INITIAL_QUERY="layer1_retention_${INITIAL_TAG}"
INITIAL_PROBE_OUT="$TMP_DIR/initial-probe.json"
python3 "$PROBE" \
    --host "$HOST" --port "$PORT" --key "$KEY_PATH" \
    --known-hosts "$KNOWN_HOSTS" --customer "$CUSTOMER" \
    --retention-tag "$INITIAL_TAG" > "$INITIAL_PROBE_OUT"
[ "$(json_field "$INITIAL_PROBE_OUT" status)" = "PASS" ] || fail "initial MCP scope probe failed"
pass "customer scope, blocked-tool suppression, and initial retention artifact"

# 5. Forced-command attack matrix.
ATTACK_MARKER="CAM_LAYER1_OVERRIDE_${CAM_ID}"
REMOTE_MARKER_COMMAND="printf '%s\\n' '$ATTACK_MARKER'"
run_no_marker_attack command-override \
    ssh "${SSH_OPTIONS[@]}" "$CAM_DEST" "$REMOTE_MARKER_COMMAND"
ENV_TAG="${CAM_ID}_env"
ENV_PROBE_OUT="$TMP_DIR/environment-injection.json"
python3 "$PROBE" \
    --host "$HOST" --port "$PORT" --key "$KEY_PATH" \
    --known-hosts "$KNOWN_HOSTS" --customer "$CUSTOMER" \
    --retention-tag "$ENV_TAG" --attempt-env-injection > "$ENV_PROBE_OUT"
[ "$(json_field "$ENV_PROBE_OUT" status)" = "PASS" ] || \
    fail "environment-injection changed the effective CAM scope"
pass "environment-injection"
run_no_marker_attack pty-refusal \
    ssh "${SSH_OPTIONS[@]}" -tt "$CAM_DEST" "$REMOTE_MARKER_COMMAND"
LOCAL_FORWARD_PORT=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')
run_local_forwarding_denial "$LOCAL_FORWARD_PORT"
run_no_marker_attack agent-forwarding \
    ssh "${SSH_OPTIONS[@]}" -A "$CAM_DEST" "$REMOTE_MARKER_COMMAND"
run_no_marker_attack x11-forwarding \
    ssh "${SSH_OPTIONS[@]}" -X "$CAM_DEST" "$REMOTE_MARKER_COMMAND"
printf '%s\n' "$ATTACK_MARKER" > "$TMP_DIR/scp-source.txt"
if [[ "$HOST" == *:* ]]; then SCP_DEST="${REMOTE_USER}@[${HOST}]:${ATTACK_REMOTE_PATH}"; else SCP_DEST="${REMOTE_USER}@${HOST}:${ATTACK_REMOTE_PATH}"; fi
run_refusal_attack scp-refusal false \
    scp -i "$KEY_PATH" -P "$PORT" -o BatchMode=yes -o IdentitiesOnly=yes \
    -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=$KNOWN_HOSTS" \
    "$TMP_DIR/scp-source.txt" "$SCP_DEST"
printf 'put %s %s\n' "$TMP_DIR/scp-source.txt" "$ATTACK_REMOTE_PATH" > "$TMP_DIR/sftp.batch"
run_refusal_attack sftp-refusal false \
    sftp -b "$TMP_DIR/sftp.batch" -i "$KEY_PATH" -P "$PORT" \
    -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes \
    -o "UserKnownHostsFile=$KNOWN_HOSTS" "$CAM_DEST"

# 6. Hold one initialized session, revoke it, and require disconnection.
HOLD_TAG="${CAM_ID}_hold"
HOLD_QUERY="layer1_retention_${HOLD_TAG}"
HOLD_READY="$TMP_DIR/hold.ready.json"
HOLD_OUT="$TMP_DIR/hold.stdout"
HOLD_ERR="$TMP_DIR/hold.stderr"
HOLD_READY_TIMEOUT_SECONDS=120
python3 "$PROBE" \
    --host "$HOST" --port "$PORT" --key "$KEY_PATH" \
    --known-hosts "$KNOWN_HOSTS" --customer "$CUSTOMER" \
    --retention-tag "$HOLD_TAG" --hold-seconds 120 \
    --ready-file "$HOLD_READY" > "$HOLD_OUT" 2> "$HOLD_ERR" &
HOLD_PID=$!
ready=0
for _ in $(seq 1 "$HOLD_READY_TIMEOUT_SECONDS"); do
    if [ -s "$HOLD_READY" ]; then ready=1; break; fi
    kill -0 "$HOLD_PID" 2>/dev/null || fail "held MCP probe exited before initialization"
    sleep 1
done
if [ "$ready" -ne 1 ]; then
    [ -f "$HOLD_ERR" ] && cat "$HOLD_ERR" >&2
    fail "held MCP probe did not initialize within ${HOLD_READY_TIMEOUT_SECONDS} seconds"
fi
pass "held MCP session initialized"

write_deprovision_request
deprovision_rc=0
ssh "${ADMIN_SSH_OPTIONS[@]}" 'sudo /opt/logan-mcp/bin/cam-admin deprovision --json' \
    < "$DEPROVISION_REQUEST" > "$DEPROVISION_RESPONSE" || deprovision_rc=$?
python3 - "$DEPROVISION_RESPONSE" "$CAM_ID" "$LOCAL_FINGERPRINT" "$deprovision_rc" <<'PY' || fail "deprovision did not return verified SUCCESS"
import json
import pathlib
import sys

payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
ok = (
    int(sys.argv[4]) == 0
    and payload.get("status") == "SUCCESS"
    and payload.get("cam_id") == sys.argv[2]
    and payload.get("fingerprint") == sys.argv[3]
    and payload.get("access_revoked") is True
)
raise SystemExit(0 if ok else 1)
PY
DEPROVISIONED=1
PROVISION_MAY_HAVE_COMMITTED=0
pass "deprovision"

disconnected=0
for _ in $(seq 1 30); do
    if ! kill -0 "$HOLD_PID" 2>/dev/null; then disconnected=1; break; fi
    sleep 1
done
if [ "$disconnected" -ne 1 ]; then
    kill "$HOLD_PID" 2>/dev/null || true
    fail "held CAM session remained alive after deprovision"
fi
hold_rc=0
wait "$HOLD_PID" || hold_rc=$?
HOLD_PID=""
[ "$hold_rc" -ne 0 ] || fail "held MCP probe completed normally after deprovision"
pass "active CAM session termination"

# 7. The revoked key must fail public-key authentication, not merely MCP startup.
REAUTH_OUT="$TMP_DIR/reauth.stdout"
REAUTH_ERR="$TMP_DIR/reauth.stderr"
reauth_rc=0
run_bounded 15 "$REAUTH_OUT" "$REAUTH_ERR" \
    ssh "${SSH_OPTIONS[@]}" -T "$CAM_DEST" || reauth_rc=$?
[ "$reauth_rc" -ne 0 ] || fail "deprovisioned key unexpectedly reconnected"
grep -Eiq 'permission denied|authentication failed' "$REAUTH_ERR" || \
    fail "reconnect failed for a reason other than public-key rejection"
pass "deprovisioned key authentication rejection"

# 8. Verify live policy/key absence and retained learning/audit state as root.
VERIFY_SCRIPT="$TMP_DIR/verify-retained-state.py"
cat > "$VERIFY_SCRIPT" <<'PY'
import json
import pathlib
import sys

import yaml

cam_id, initial_query, hold_query, attack_path = sys.argv[1:]
base = pathlib.Path("/home/cam/.oci-logan-mcp")
user_dir = base / "users" / cam_id
learned_path = user_dir / "learned_queries.yaml"
policy = yaml.safe_load(pathlib.Path("/etc/logan-mcp/access_control.yaml").read_text(encoding="utf-8")) or {}
authorized = pathlib.Path("/home/cam/.ssh/authorized_keys").read_text(encoding="utf-8")
audit_path = pathlib.Path("/var/log/logan-cam-admin.jsonl")
audit_records = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines() if line]
learned = learned_path.read_text(encoding="utf-8")
ok = (
    user_dir.is_dir()
    and initial_query in learned
    and hold_query in learned
    and cam_id not in (policy.get("cams") or {})
    and f"logan-cam:{cam_id}" not in authorized
    and f"cam-launch {cam_id}" not in authorized
    and any(item.get("cam_id") == cam_id and item.get("operation") == "provision" for item in audit_records)
    and any(item.get("cam_id") == cam_id and item.get("operation") == "deprovision" for item in audit_records)
    and not pathlib.Path(attack_path).exists()
)
if not ok:
    raise SystemExit("retained-state verification failed")
print(json.dumps({"status": "PASS", "user_dir": str(user_dir)}, sort_keys=True))
PY
if ! ssh "${ADMIN_SSH_OPTIONS[@]}" \
    "sudo /opt/logan-mcp/venv/bin/python - '$CAM_ID' '$INITIAL_QUERY' '$HOLD_QUERY' '$ATTACK_REMOTE_PATH'" \
    < "$VERIFY_SCRIPT" > "$TMP_DIR/retained-state.json"; then
    fail "retained learning/audit or policy/key absence verification failed"
fi
[ "$(json_field "$TMP_DIR/retained-state.json" status)" = "PASS" ] || \
    fail "retained-state verifier did not report PASS"
pass "policy/key absence with retained learning and audit state"

printf '{"cam_id":"%s","customer":%s,"status":"PASS"}\n' "$CAM_ID" "$CUSTOMER"
