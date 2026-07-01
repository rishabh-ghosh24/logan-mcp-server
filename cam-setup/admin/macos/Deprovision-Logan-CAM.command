#!/bin/sh
set -eu
umask 077

CAM_ID=''
SSH_TARGET=''
ASSUME_YES=0

usage() {
    printf 'Usage: %s [--cam ID] [--ssh-target ALIAS] [--yes]\n' "$0" >&2
    exit 64
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --cam|--ssh-target)
            [ "$#" -ge 2 ] || usage
            option=$1
            value=$2
            shift 2
            case "$option" in
                --cam) CAM_ID=$value ;;
                --ssh-target) SSH_TARGET=$value ;;
            esac
            ;;
        --yes)
            ASSUME_YES=1
            shift
            ;;
        *) usage ;;
    esac
done

if [ -z "$CAM_ID" ]; then
    printf 'CAM ID: '
    IFS= read -r CAM_ID || exit 64
fi
if [ -z "$SSH_TARGET" ]; then
    printf 'SSH target alias [automation1]: '
    IFS= read -r SSH_TARGET || exit 64
    SSH_TARGET=${SSH_TARGET:-automation1}
fi

CAM_ID=$(printf '%s' "$CAM_ID" | LC_ALL=C tr 'ABCDEFGHIJKLMNOPQRSTUVWXYZ' 'abcdefghijklmnopqrstuvwxyz')

case "$CAM_ID" in
    ''|[!a-z]*|*[!a-z0-9._-]*|*[._-]|*[._-][._-]*)
        printf 'Invalid CAM ID. Use 1-64 lowercase letters/digits with single ., _, or - separators.\n' >&2
        exit 64
        ;;
esac
if [ "${#CAM_ID}" -gt 64 ]; then
    printf 'Invalid CAM ID. Maximum length is 64 characters.\n' >&2
    exit 64
fi
case "$SSH_TARGET" in
    ''|-*|*[!A-Za-z0-9._@-]*)
        printf 'SSH target must be a safe configured alias.\n' >&2
        exit 64
        ;;
esac
for tool in ssh osascript mktemp; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        printf 'Required tool is unavailable: %s\n' "$tool" >&2
        exit 69
    fi
done
if ! ssh -G "$SSH_TARGET" >/dev/null 2>&1; then
    printf 'SSH target cannot be resolved by ssh -G: %s\n' "$SSH_TARGET" >&2
    exit 69
fi

WORK_DIR=$(mktemp -d "${TMPDIR:-/tmp}/logan-cam-deprovision.XXXXXX")
JSON_PARSER="$WORK_DIR/json-parser.js"
SHOW_RESPONSE="$WORK_DIR/show.response.json"
REQUEST_PATH="$WORK_DIR/deprovision.request.json"
RESPONSE_PATH="$WORK_DIR/deprovision.response.json"
cleanup() {
    rm -rf -- "$WORK_DIR"
}
trap cleanup EXIT HUP INT TERM

cat > "$JSON_PARSER" <<'JXA'
ObjC.import('Foundation');
function run(argv) {
    if (argv.length !== 2 && argv.length !== 3) {
        throw new Error('expected JSON path, field path, and optional format');
    }
    var error = Ref();
    var text = $.NSString.stringWithContentsOfFileEncodingError(
        argv[0], $.NSUTF8StringEncoding, error
    );
    if (!text) throw new Error('cannot read JSON response');
    var value = JSON.parse(ObjC.unwrap(text));
    argv[1].split('.').forEach(function (part) {
        if (value === null || typeof value !== 'object' || !(part in value)) {
            throw new Error('missing JSON field: ' + argv[1]);
        }
        value = value[part];
    });
    var field = argv[1];
    var jsonMode = argv.length === 3 && argv[2] === 'json';
    if (field === 'customers') {
        if (!Array.isArray(value) || !value.every(function (item) {
            return typeof item === 'number' && isFinite(item) &&
                Math.floor(item) === item && item > 0;
        })) throw new Error('customers must be positive integers');
        if (!jsonMode) return value.join(',');
    }
    if (field === 'allow_delivery' || field === 'access_revoked') {
        if (typeof value !== 'boolean') throw new Error(field + ' must be boolean');
        if (!jsonMode) return value ? 'true' : 'false';
    }
    if (field === 'shared_account_fallback') {
        if (typeof value !== 'boolean') throw new Error(field + ' must be boolean');
        if (!jsonMode) return value ? 'true' : 'false';
    }
    if (field === 'connection.port') {
        if (typeof value !== 'number' || !isFinite(value) || Math.floor(value) !== value) {
            throw new Error('connection.port must be an integer');
        }
        if (!jsonMode) return String(value);
    }
    if (field === 'failures' || field === 'warnings') {
        if (!Array.isArray(value) || !value.every(function (item) {
            return typeof item === 'string';
        })) throw new Error(field + ' must be an array of strings');
    }
    if (field === 'status' || field === 'cam_id' || field === 'fingerprint' ||
        field === 'backup_dir' || field === 'connection.host' ||
        field === 'connection.remote_user' || field === 'connection.host_public_key') {
        if (typeof value !== 'string') throw new Error(field + ' must be a string');
    }
    if (jsonMode) return JSON.stringify(value);
    if (Array.isArray(value)) throw new Error('unexpected array field');
    if (typeof value === 'string' || typeof value === 'number') return String(value);
    throw new Error('JSON field is not scalar');
}
JXA
chmod 600 "$JSON_PARSER"

json_get() {
    osascript -l JavaScript "$JSON_PARSER" "$1" "$2"
}

json_literal() {
    osascript -l JavaScript "$JSON_PARSER" "$1" "$2" json
}

show_ssh_rc=0
ssh "$SSH_TARGET" "sudo /opt/logan-mcp/bin/cam-admin show --cam $CAM_ID --json" \
    > "$SHOW_RESPONSE" || show_ssh_rc=$?
if [ ! -s "$SHOW_RESPONSE" ]; then
    printf 'Could not retrieve CAM assignment JSON.\n' >&2
    exit 70
fi
SHOW_STATUS=$(json_get "$SHOW_RESPONSE" status) || {
    printf 'Show response is not valid contract JSON.\n' >&2
    exit 70
}
if [ "$SHOW_STATUS" != 'SUCCESS' ]; then
    printf 'Could not retrieve CAM assignment: %s\n' "$SHOW_STATUS" >&2
    exit 70
fi
SHOWN_CAM=$(json_get "$SHOW_RESPONSE" cam_id)
SHOWN_CUSTOMERS=$(json_get "$SHOW_RESPONSE" customers)
SHOWN_DELIVERY=$(json_get "$SHOW_RESPONSE" allow_delivery)
FINGERPRINT=$(json_get "$SHOW_RESPONSE" fingerprint)
if [ "$SHOWN_CAM" != "$CAM_ID" ]; then
    printf 'Show response CAM ID does not match the request.\n' >&2
    exit 70
fi
case "$FINGERPRINT" in SHA256:*) ;; *) exit 70 ;; esac
if [ "$show_ssh_rc" -ne 0 ]; then
    printf 'Show returned SUCCESS JSON with invalid SSH exit %s; refusing to continue.\n' "$show_ssh_rc" >&2
    exit 70
fi

printf 'CAM ID: %s\nCustomers: %s\nAllow delivery: %s\nFingerprint: %s\n' \
    "$CAM_ID" "$SHOWN_CUSTOMERS" "$SHOWN_DELIVERY" "$FINGERPRINT"
if [ "$ASSUME_YES" -eq 0 ]; then
    printf 'Type REVOKE %s to continue: ' "$CAM_ID"
    IFS= read -r confirmation || exit 64
    if [ "$confirmation" != "REVOKE $CAM_ID" ]; then
        printf 'Confirmation did not match; no revocation was requested.\n' >&2
        exit 64
    fi
fi

printf '{"cam_id":"%s","expected_fingerprint":"%s","confirm":true}\n' \
    "$CAM_ID" "$FINGERPRINT" > "$REQUEST_PATH"
deprovision_ssh_rc=0
ssh "$SSH_TARGET" 'sudo /opt/logan-mcp/bin/cam-admin deprovision --json' \
    < "$REQUEST_PATH" > "$RESPONSE_PATH" || deprovision_ssh_rc=$?
if [ ! -s "$RESPONSE_PATH" ]; then
    printf 'HIGH-SEVERITY: no revocation JSON was returned; access status is unconfirmed.\n' >&2
    exit 1
fi

STATUS=$(json_get "$RESPONSE_PATH" status) || {
    printf 'HIGH-SEVERITY: revocation response is invalid; access status is unconfirmed.\n' >&2
    exit 1
}
RETURNED_CAM=$(json_get "$RESPONSE_PATH" cam_id 2>/dev/null || printf '')
RETURNED_FINGERPRINT=$(json_get "$RESPONSE_PATH" fingerprint 2>/dev/null || printf '')
ACCESS_REVOKED=$(json_get "$RESPONSE_PATH" access_revoked 2>/dev/null || printf 'false')
if [ "$RETURNED_CAM" != "$CAM_ID" ] || [ "$RETURNED_FINGERPRINT" != "$FINGERPRINT" ]; then
    printf 'HIGH-SEVERITY: revocation response identity does not match; access status is unconfirmed.\n' >&2
    exit 1
fi

case "$STATUS" in
    SUCCESS)
        if [ "$deprovision_ssh_rc" -ne 0 ]; then
            printf 'HIGH-SEVERITY: SUCCESS requires SSH exit 0, received %s.\n' "$deprovision_ssh_rc" >&2
            exit 1
        fi
        if [ "$ACCESS_REVOKED" != 'true' ]; then
            printf 'HIGH-SEVERITY: SUCCESS did not confirm access_revoked=true.\n' >&2
            exit 1
        fi
        printf 'Access revoked successfully for %s.\n' "$CAM_ID"
        exit 0
        ;;
    FAILED_ACCESS_REVOKED_CLEANUP_REQUIRED)
        if [ "$deprovision_ssh_rc" -ne 1 ]; then
            printf 'HIGH-SEVERITY: cleanup-required status requires SSH exit 1, received %s.\n' "$deprovision_ssh_rc" >&2
            exit 1
        fi
        if [ "$ACCESS_REVOKED" = 'true' ]; then
            printf 'Access is revoked for %s, but server cleanup remains required.\n' "$CAM_ID" >&2
            exit 1
        fi
        printf 'HIGH-SEVERITY: cleanup status did not confirm access revocation.\n' >&2
        exit 1
        ;;
    FAILED_REVOCATION_UNCONFIRMED)
        if [ "$deprovision_ssh_rc" -ne 1 ]; then
            printf 'HIGH-SEVERITY: revocation-unconfirmed status requires SSH exit 1, received %s.\n' "$deprovision_ssh_rc" >&2
            exit 1
        fi
        printf 'HIGH-SEVERITY: revocation is unconfirmed; treat the credential as still active.\n' >&2
        exit 1
        ;;
    *)
        printf 'HIGH-SEVERITY: unknown revocation status %s; access status is unconfirmed.\n' "$STATUS" >&2
        exit 1
        ;;
esac
