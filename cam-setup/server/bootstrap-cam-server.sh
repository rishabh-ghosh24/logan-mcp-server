#!/bin/bash
set -euo pipefail
umask 077

usage() {
    echo "Usage: $0 --repo PATH --config-source PATH --policy-source PATH --public-host HOST [--state-source PATH] [--port N] [--python PATH] [--root-prefix PATH]" >&2
    exit 64
}

ROOT_PREFIX=""
REPO=""
CONFIG_SOURCE=""
POLICY_SOURCE=""
STATE_SOURCE="/home/opc/.oci-logan-mcp"
PUBLIC_HOST=""
PORT=22
PYTHON="python3.11"

while [ "$#" -gt 0 ]; do
    case "$1" in
        --repo)
            [ "$#" -ge 2 ] || usage
            REPO=$2
            shift 2
            ;;
        --config-source)
            [ "$#" -ge 2 ] || usage
            CONFIG_SOURCE=$2
            shift 2
            ;;
        --policy-source)
            [ "$#" -ge 2 ] || usage
            POLICY_SOURCE=$2
            shift 2
            ;;
        --public-host)
            [ "$#" -ge 2 ] || usage
            PUBLIC_HOST=$2
            shift 2
            ;;
        --state-source)
            [ "$#" -ge 2 ] || usage
            STATE_SOURCE=$2
            shift 2
            ;;
        --port)
            [ "$#" -ge 2 ] || usage
            PORT=$2
            shift 2
            ;;
        --python)
            [ "$#" -ge 2 ] || usage
            PYTHON=$2
            shift 2
            ;;
        --root-prefix)
            [ "$#" -ge 2 ] || usage
            ROOT_PREFIX=$2
            shift 2
            ;;
        *)
            usage
            ;;
    esac
done

[ -n "$REPO" ] || usage
[ -n "$CONFIG_SOURCE" ] || usage
[ -n "$POLICY_SOURCE" ] || usage
[ -n "$PUBLIC_HOST" ] || usage

TEST_MODE=${CAM_BOOTSTRAP_TEST_MODE:-0}
if [ -n "$ROOT_PREFIX" ]; then
    [ "$TEST_MODE" = "1" ] || usage
    case "$ROOT_PREFIX" in
        /*) ;;
        *) usage ;;
    esac
    [ "$ROOT_PREFIX" != "/" ] || usage
elif [ "$TEST_MODE" = "1" ]; then
    usage
fi
if [ "$TEST_MODE" != "1" ] && [ "$EUID" -ne 0 ]; then
    echo "bootstrap-cam-server.sh must run as root" >&2
    exit 1
fi
if [ "$TEST_MODE" != "1" ]; then
    PATH="/usr/sbin:/usr/bin:/sbin:/bin"
    export PATH
fi

if ! [[ "$PORT" =~ ^[0-9]+$ ]] || [ "$PORT" -lt 1 ] || [ "$PORT" -gt 65535 ]; then
    usage
fi
if [ "${#PUBLIC_HOST}" -gt 253 ] || ! [[ "$PUBLIC_HOST" =~ ^[A-Za-z0-9][A-Za-z0-9.:-]*$ ]]; then
    usage
fi

OPT_DIR="${ROOT_PREFIX}/opt/logan-mcp"
ETC_DIR="${ROOT_PREFIX}/etc/logan-mcp"
CAM_HOME="${ROOT_PREFIX}/home/cam"
STATE_DIR="${ROOT_PREFIX}/home/cam/.oci-logan-mcp"
SSH_DIR="${ROOT_PREFIX}/home/cam/.ssh"
SSHD_CONFIG="${ROOT_PREFIX}/etc/ssh/sshd_config"
SSHD_DROPIN="${ROOT_PREFIX}/etc/ssh/sshd_config.d/90-logan-cam.conf"
BACKUP_ROOT="${ROOT_PREFIX}/var/lib/logan-cam-admin/backups"
AUDIT_PATH="${ROOT_PREFIX}/var/log/logan-cam-admin.jsonl"
HOST_KEY_PATH="${ROOT_PREFIX}/etc/ssh/ssh_host_ed25519_key.pub"
AUTHORIZED_KEYS="${SSH_DIR}/authorized_keys"

[ -d "$REPO" ] || usage
[ -f "$REPO/pyproject.toml" ] || usage
[ -f "$REPO/cam-setup/server/cam-launch" ] || usage
[ -f "$REPO/cam-setup/server/cam-admin" ] || usage
[ -f "$CONFIG_SOURCE" ] || usage
[ -f "$POLICY_SOURCE" ] || usage
[ -f "$HOST_KEY_PATH" ] || usage
[ -f "$SSHD_CONFIG" ] || usage
command -v "$PYTHON" >/dev/null 2>&1 || usage
command -v sshd >/dev/null 2>&1 || usage
command -v systemctl >/dev/null 2>&1 || usage

for controlled_path in \
    "$OPT_DIR" \
    "$ETC_DIR" \
    "$ETC_DIR/config.yaml" \
    "$ETC_DIR/access_control.yaml" \
    "$CAM_HOME" \
    "$STATE_DIR" \
    "$SSH_DIR" \
    "$SSHD_CONFIG" \
    "$AUTHORIZED_KEYS" \
    "$SSHD_DROPIN" \
    "$BACKUP_ROOT" \
    "$AUDIT_PATH"; do
    if [ -L "$controlled_path" ]; then
        echo "Refusing symbolic link at controlled path: $controlled_path" >&2
        exit 1
    fi
done

HOST_PUBLIC_KEY=$(/usr/bin/head -n 1 "$HOST_KEY_PATH")
if ! [[ "$HOST_PUBLIC_KEY" =~ ^ssh-ed25519[[:space:]][A-Za-z0-9+/=]+([[:space:]][^[:space:]]+)?$ ]]; then
    echo "Host ed25519 public key is invalid" >&2
    exit 1
fi
if ! "$PYTHON" - "$HOST_PUBLIC_KEY" <<'PY'
import base64
import binascii
import struct
import sys

parts = sys.argv[1].split()
if len(parts) not in (2, 3) or parts[0] != "ssh-ed25519":
    raise SystemExit(1)
try:
    blob = base64.b64decode(parts[1], validate=True)
except (binascii.Error, ValueError):
    raise SystemExit(1)

def read_string(offset):
    if offset + 4 > len(blob):
        raise SystemExit(1)
    length = struct.unpack(">I", blob[offset:offset + 4])[0]
    start = offset + 4
    end = start + length
    if end > len(blob):
        raise SystemExit(1)
    return blob[start:end], end

algorithm, offset = read_string(0)
key, offset = read_string(offset)
if algorithm != b"ssh-ed25519" or len(key) != 32 or offset != len(blob):
    raise SystemExit(1)
PY
then
    echo "Host ed25519 public key wire data is invalid" >&2
    exit 1
fi

TIMESTAMP=$(/bin/date -u +%Y%m%dT%H%M%SZ)
BACKUP_DIR="${BACKUP_ROOT}/bootstrap-${TIMESTAMP}-$$"
/usr/bin/install -d -m 0700 "$BACKUP_DIR"

backup_file() {
    local source=$1
    local name=$2
    if [ -e "$source" ]; then
        /bin/cp -p "$source" "$BACKUP_DIR/$name"
    fi
}

backup_file "$ETC_DIR/config.yaml" config.yaml
backup_file "$ETC_DIR/access_control.yaml" access_control.yaml
backup_file "$AUTHORIZED_KEYS" authorized_keys
backup_file "$SSHD_CONFIG" sshd_config
backup_file "$SSHD_DROPIN" 90-logan-cam.conf
if [ -d "$STATE_DIR" ]; then
    /usr/bin/tar -C "$STATE_DIR" -cf "$BACKUP_DIR/state.tar" .
fi

INITIAL_CONFIG=0
VALIDATION_CONFIG="$ETC_DIR/config.yaml"
if [ ! -f "$VALIDATION_CONFIG" ]; then
    INITIAL_CONFIG=1
    VALIDATION_CONFIG="$CONFIG_SOURCE"
fi
INITIAL_POLICY=0
VALIDATION_POLICY="$ETC_DIR/access_control.yaml"
if [ ! -f "$VALIDATION_POLICY" ]; then
    INITIAL_POLICY=1
    VALIDATION_POLICY="$POLICY_SOURCE"
fi

if [ "$TEST_MODE" != "1" ]; then
    if ! getent group cam >/dev/null 2>&1; then
        groupadd --system cam
    fi
    if ! id -u cam >/dev/null 2>&1; then
        useradd --system --gid cam --home-dir /home/cam --create-home --shell /bin/sh cam
    else
        usermod -g cam -G '' -d /home/cam -s /bin/sh cam
    fi
    passwd -l cam >/dev/null
fi

/usr/bin/install -d -m 0755 "$OPT_DIR" "$OPT_DIR/bin"
if [ "$TEST_MODE" = "1" ]; then
    /usr/bin/install -d -m 0755 "$OPT_DIR/venv/bin"
    /bin/ln -sfn "$(command -v "$PYTHON")" "$OPT_DIR/venv/bin/python"
else
    if [ ! -x "$OPT_DIR/venv/bin/python" ]; then
        "$PYTHON" -m venv "$OPT_DIR/venv"
    fi
    "$OPT_DIR/venv/bin/python" -m pip install --upgrade "$REPO"
fi

SOURCE_VALIDATOR="$BACKUP_DIR/validate_sources.py"
cat > "$SOURCE_VALIDATOR" <<'PY'
import pathlib
import sys

import yaml

from oci_logan_mcp.access_control import load_access_config
from oci_logan_mcp.config import _parse_config

config_path = pathlib.Path(sys.argv[1])
policy_path = pathlib.Path(sys.argv[2])
initial_policy = sys.argv[3] == "1"
raw_config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
if not isinstance(raw_config, dict):
    raise SystemExit("config.yaml must contain a mapping")
settings = _parse_config(raw_config)
if settings.oci.auth_type != "instance_principal":
    raise SystemExit("config.yaml must use instance_principal authentication")
policy = load_access_config(policy_path)
if initial_policy and policy.cams:
    raise SystemExit(
        "initial access_control.yaml must not define CAMs; use cam-admin provision"
    )
PY
if [ "$TEST_MODE" = "1" ]; then
    PYTHONPATH="$REPO/src" "$PYTHON" "$SOURCE_VALIDATOR" \
        "$VALIDATION_CONFIG" "$VALIDATION_POLICY" "$INITIAL_POLICY"
else
    "$OPT_DIR/venv/bin/python" -I "$SOURCE_VALIDATOR" \
        "$VALIDATION_CONFIG" "$VALIDATION_POLICY" "$INITIAL_POLICY"
fi
/bin/rm -f "$SOURCE_VALIDATOR"

/usr/bin/install -m 0755 "$REPO/cam-setup/server/cam-launch" "$OPT_DIR/bin/cam-launch"
/usr/bin/install -m 0755 "$REPO/cam-setup/server/cam-admin" "$OPT_DIR/bin/cam-admin"
/bin/chmod -R a+rX,go-w "$OPT_DIR"

/usr/bin/install -d -m 0750 "$ETC_DIR" "$CAM_HOME" "$SSH_DIR"
/usr/bin/install -d -m 0700 "$STATE_DIR" "$BACKUP_ROOT"
AUDIT_PARENT=$(/usr/bin/dirname "$AUDIT_PATH")
if [ ! -d "$AUDIT_PARENT" ]; then
    /usr/bin/install -d -m 0755 "$AUDIT_PARENT"
fi
if [ "$TEST_MODE" != "1" ]; then
    /bin/chown root:cam "$ETC_DIR" "$CAM_HOME" "$SSH_DIR"
    /bin/chown -R cam:cam "$STATE_DIR"
    if [ -e "$AUTHORIZED_KEYS" ]; then
        /bin/chown root:cam "$AUTHORIZED_KEYS"
        /bin/chmod 0640 "$AUTHORIZED_KEYS"
    fi
fi

CONFIG_CANDIDATE="$ETC_DIR/.config.yaml.$$"
CONFIG_NORMALIZER_PYTHON="$PYTHON"
if [ "$TEST_MODE" != "1" ]; then
    CONFIG_NORMALIZER_PYTHON="$OPT_DIR/venv/bin/python"
fi
CONFIG_CHANGED=$("$CONFIG_NORMALIZER_PYTHON" - "$VALIDATION_CONFIG" "$CONFIG_CANDIDATE" "$STATE_DIR" <<'PY'
import pathlib
import sys

import yaml

source, candidate, state_dir = map(pathlib.Path, sys.argv[1:])
raw = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
if not isinstance(raw, dict):
    raise SystemExit("config.yaml must contain a mapping")

desired_paths = (
    (raw.get("logging"), "log_path", state_dir / "logs"),
    (raw.get("report_delivery"), "artifact_dir", state_dir / "reports"),
)
changed = False
for section, key, desired in desired_paths:
    if isinstance(section, dict) and section.get(key) != str(desired):
        section[key] = str(desired)
        changed = True
if raw.get("transcript_dir") != str(state_dir / "transcripts") and "transcript_dir" in raw:
    raw["transcript_dir"] = str(state_dir / "transcripts")
    changed = True

if changed:
    candidate.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
print("true" if changed else "false")
PY
)
if [ "$INITIAL_CONFIG" -eq 1 ]; then
    /usr/bin/install -m 0640 "$VALIDATION_CONFIG" "$ETC_DIR/config.yaml"
fi
if [ "$CONFIG_CHANGED" = "true" ]; then
    /bin/chmod 0640 "$CONFIG_CANDIDATE"
    /bin/mv -f "$CONFIG_CANDIDATE" "$ETC_DIR/config.yaml"
fi
/bin/rm -f "$CONFIG_CANDIDATE"
if [ "$INITIAL_POLICY" -eq 1 ]; then
    /usr/bin/install -m 0640 "$POLICY_SOURCE" "$ETC_DIR/access_control.yaml"
fi

MIGRATION_MARKER="$STATE_DIR/.legacy-state-migrated"
if [ -d "$STATE_SOURCE" ] && [ ! -e "$MIGRATION_MARKER" ]; then
    /usr/bin/tar -C "$STATE_SOURCE" \
        --exclude=config.yaml --exclude=access_control.yaml \
        -cf - . | /usr/bin/tar -C "$STATE_DIR" -xf -
    : > "$MIGRATION_MARKER"
fi
/bin/rm -f "$STATE_DIR/config.yaml" "$STATE_DIR/access_control.yaml"

if [ ! -e "$AUTHORIZED_KEYS" ]; then
    /usr/bin/install -m 0640 /dev/null "$AUTHORIZED_KEYS"
fi

CONNECTION_TMP="$ETC_DIR/.connection.json.$$"
"$PYTHON" - "$PUBLIC_HOST" "$PORT" "$HOST_PUBLIC_KEY" "$CONNECTION_TMP" <<'PY'
import json
import pathlib
import sys

host, port, host_public_key, destination = sys.argv[1:]
payload = {
    "host": host,
    "port": int(port),
    "remote_user": "cam",
    "host_public_key": host_public_key,
}
pathlib.Path(destination).write_text(
    json.dumps(payload, sort_keys=True) + "\n",
    encoding="utf-8",
)
PY
/bin/chmod 0640 "$CONNECTION_TMP"
/bin/mv -f "$CONNECTION_TMP" "$ETC_DIR/connection.json"

/bin/chmod 0755 "$OPT_DIR" "$OPT_DIR/bin"
/bin/chmod 0755 "$OPT_DIR/bin/cam-launch" "$OPT_DIR/bin/cam-admin"
/bin/chmod 0750 "$ETC_DIR" "$CAM_HOME" "$SSH_DIR"
/bin/chmod 0640 "$ETC_DIR/config.yaml" "$ETC_DIR/access_control.yaml" "$ETC_DIR/connection.json" "$AUTHORIZED_KEYS"
/bin/chmod 0700 "$STATE_DIR" "$BACKUP_ROOT" "$BACKUP_DIR"
if [ ! -e "$AUDIT_PATH" ]; then
    : > "$AUDIT_PATH"
fi
/bin/chmod 0600 "$AUDIT_PATH"

if [ "$TEST_MODE" != "1" ]; then
    /bin/chown -R root:root "$OPT_DIR"
    /bin/chown root:cam "$ETC_DIR" "$ETC_DIR/config.yaml" "$ETC_DIR/access_control.yaml" "$ETC_DIR/connection.json"
    /bin/chown root:cam "$CAM_HOME" "$SSH_DIR" "$AUTHORIZED_KEYS"
    /bin/chown -R cam:cam "$STATE_DIR"
    /bin/chown -R root:root "$BACKUP_ROOT"
    /bin/chown root:root "$AUDIT_PATH"
fi

SSHD_DIR=$(/usr/bin/dirname "$SSHD_DROPIN")
/usr/bin/install -d -m 0755 "$SSHD_DIR"
SSHD_CONFIG_CHANGED=0
if ! /usr/bin/grep -Eq \
    '^[[:space:]]*Include[[:space:]]+/etc/ssh/sshd_config\.d/\*\.conf([[:space:]]|$)' \
    "$SSHD_CONFIG"; then
    SSHD_CONFIG_CANDIDATE="${SSHD_CONFIG}.logan-cam.$$"
    /usr/bin/awk '
        BEGIN { inserted = 0 }
        tolower($1) == "match" && !inserted {
            print "Include /etc/ssh/sshd_config.d/*.conf"
            inserted = 1
        }
        { print }
        END {
            if (!inserted) {
                print "Include /etc/ssh/sshd_config.d/*.conf"
            }
        }
    ' "$SSHD_CONFIG" > "$SSHD_CONFIG_CANDIDATE"
    /bin/chmod 0600 "$SSHD_CONFIG_CANDIDATE"
    /bin/mv -f "$SSHD_CONFIG_CANDIDATE" "$SSHD_CONFIG"
    SSHD_CONFIG_CHANGED=1
fi
SSHD_CANDIDATE="$SSHD_DIR/.90-logan-cam.conf.$$"
cat > "$SSHD_CANDIDATE" <<'EOF'
Match User cam
    AuthenticationMethods publickey
    PasswordAuthentication no
    KbdInteractiveAuthentication no
    # PermitUserEnvironment is global-only on OpenSSH; bootstrap-check requires
    # the effective global setting to be no before this boundary can activate.
    PermitUserRC no
    AllowAgentForwarding no
    AllowTcpForwarding no
    GatewayPorts no
    X11Forwarding no
    PermitTunnel no
    PermitTTY no
EOF
/bin/chmod 0644 "$SSHD_CANDIDATE"

HAD_DROPIN=0
if [ -e "$SSHD_DROPIN" ]; then
    HAD_DROPIN=1
fi

restore_dropin() {
    if [ "$HAD_DROPIN" -eq 1 ]; then
        /usr/bin/install -m 0644 "$BACKUP_DIR/90-logan-cam.conf" "$SSHD_DROPIN"
    else
        /bin/rm -f "$SSHD_DROPIN"
    fi
    if [ "$SSHD_CONFIG_CHANGED" -eq 1 ]; then
        /bin/cp -p "$BACKUP_DIR/sshd_config" "$SSHD_CONFIG"
    fi
}

/bin/mv -f "$SSHD_CANDIDATE" "$SSHD_DROPIN"
if ! sshd -t; then
    restore_dropin
    echo "sshd configuration validation failed; previous drop-in restored" >&2
    exit 1
fi

if [ "$TEST_MODE" = "1" ]; then
    BOOTSTRAP_OUTPUT='{"status":"SUCCESS","checks":{"test_mode":true}}'
else
    if ! BOOTSTRAP_OUTPUT=$("$OPT_DIR/bin/cam-admin" bootstrap-check --json); then
        restore_dropin
        echo "CAM bootstrap check failed; previous drop-in restored" >&2
        exit 1
    fi
fi

if ! "$PYTHON" -c 'import json,sys; data=json.load(sys.stdin); raise SystemExit(0 if data.get("status") == "SUCCESS" else 1)' <<< "$BOOTSTRAP_OUTPUT"; then
    restore_dropin
    echo "CAM bootstrap check did not report success; previous drop-in restored" >&2
    exit 1
fi

if ! systemctl reload sshd; then
    restore_dropin
    sshd -t && systemctl reload sshd >/dev/null 2>&1 || true
    echo "sshd reload failed; previous drop-in restored" >&2
    exit 1
fi

printf '%s\n' "$BOOTSTRAP_OUTPUT"
