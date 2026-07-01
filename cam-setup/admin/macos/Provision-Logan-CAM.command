#!/bin/sh
set -eu
umask 077

CAM_ID=''
CUSTOMERS=''
ALLOW_DELIVERY=''
SSH_TARGET=''
OUTPUT_DIR=''
ASSUME_YES=0

usage() {
    printf 'Usage: %s [--cam ID] [--customers CSV] [--allow-delivery true|false] [--ssh-target ALIAS] [--output DIR] [--yes]\n' "$0" >&2
    exit 64
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --cam|--customers|--allow-delivery|--ssh-target|--output)
            [ "$#" -ge 2 ] || usage
            option=$1
            value=$2
            shift 2
            case "$option" in
                --cam) CAM_ID=$value ;;
                --customers) CUSTOMERS=$value ;;
                --allow-delivery) ALLOW_DELIVERY=$value ;;
                --ssh-target) SSH_TARGET=$value ;;
                --output) OUTPUT_DIR=$value ;;
            esac
            ;;
        --yes)
            ASSUME_YES=1
            shift
            ;;
        *) usage ;;
    esac
done

prompt_value() {
    variable_name=$1
    label=$2
    current_value=$3
    if [ -z "$current_value" ]; then
        printf '%s: ' "$label"
        IFS= read -r entered || exit 64
        case "$variable_name" in
            cam) CAM_ID=$entered ;;
            customers) CUSTOMERS=$entered ;;
            delivery) ALLOW_DELIVERY=$entered ;;
            target) SSH_TARGET=$entered ;;
            output) OUTPUT_DIR=$entered ;;
        esac
    fi
}

prompt_value cam 'CAM ID' "$CAM_ID"
prompt_value customers 'Customer numbers (comma-separated)' "$CUSTOMERS"
prompt_value delivery 'Allow report delivery (true or false)' "$ALLOW_DELIVERY"
if [ -z "$SSH_TARGET" ]; then
    printf 'SSH target alias [automation1]: '
    IFS= read -r SSH_TARGET || exit 64
    SSH_TARGET=${SSH_TARGET:-automation1}
fi
if [ -z "$OUTPUT_DIR" ]; then
    default_output="$HOME/logan-cam-bundles"
    printf 'Output directory [%s]: ' "$default_output"
    IFS= read -r OUTPUT_DIR || exit 64
    OUTPUT_DIR=${OUTPUT_DIR:-$default_output}
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

case "$ALLOW_DELIVERY" in
    true|false) ;;
    *)
        printf 'Allow-delivery must be exactly true or false.\n' >&2
        exit 64
        ;;
esac

case "$CUSTOMERS" in
    ''|,*|*,|*,,*)
        printf 'Customers must be a comma-separated list of positive integers.\n' >&2
        exit 64
        ;;
esac
CUSTOMERS_JSON=''
CUSTOMERS_CANONICAL=''
old_ifs=$IFS
IFS=,
set -- $CUSTOMERS
IFS=$old_ifs
for raw_customer in "$@"; do
    trimmed_customer=$(printf '%s' "$raw_customer" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')
    case "$trimmed_customer" in
        ''|*[!0-9]*)
            printf 'Customers must be positive integers.\n' >&2
            exit 64
            ;;
    esac
    customer=$(printf '%s' "$trimmed_customer" | sed 's/^0*//')
    if [ -z "$customer" ]; then
        printf 'Customers must be positive integers.\n' >&2
        exit 64
    fi
    if [ -n "$CUSTOMERS_JSON" ]; then
        CUSTOMERS_JSON="$CUSTOMERS_JSON,$customer"
        CUSTOMERS_CANONICAL="$CUSTOMERS_CANONICAL,$customer"
    else
        CUSTOMERS_JSON=$customer
        CUSTOMERS_CANONICAL=$customer
    fi
done

case "$SSH_TARGET" in
    ''|-*|*[!A-Za-z0-9._@-]*)
        printf 'SSH target must be a safe configured alias.\n' >&2
        exit 64
        ;;
esac

for tool in ssh ssh-keygen osascript sed grep ditto date; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        printf 'Required tool is unavailable: %s\n' "$tool" >&2
        exit 69
    fi
done
if ! ssh -G "$SSH_TARGET" >/dev/null 2>&1; then
    printf 'SSH target cannot be resolved by ssh -G: %s\n' "$SSH_TARGET" >&2
    exit 69
fi

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
BUNDLE_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../../bundle" && pwd -P)
MAC_TEMPLATE="$BUNDLE_ROOT/macos/Install-Logan-MCP.command"
WINDOWS_PS_TEMPLATE="$BUNDLE_ROOT/windows/Install-Logan-MCP.ps1"
WINDOWS_CMD_TEMPLATE="$BUNDLE_ROOT/windows/Double-Click-to-Install.cmd"
README_TEMPLATE="$BUNDLE_ROOT/README.html"
for template in "$MAC_TEMPLATE" "$WINDOWS_PS_TEMPLATE" "$WINDOWS_CMD_TEMPLATE" "$README_TEMPLATE"; do
    if [ ! -f "$template" ]; then
        printf 'Required bundle template is missing: %s\n' "$template" >&2
        exit 66
    fi
done

mkdir -p "$OUTPUT_DIR"
if [ ! -d "$OUTPUT_DIR" ] || [ -L "$OUTPUT_DIR" ]; then
    printf 'Output must be a real directory, not a symbolic link.\n' >&2
    exit 73
fi
OUTPUT_DIR=$(CDPATH= cd -- "$OUTPUT_DIR" && pwd -P)
BUNDLE_NAME="logan-cam-$CAM_ID"
FINAL_DIR="$OUTPUT_DIR/$BUNDLE_NAME"
ARCHIVE_PATH="$OUTPUT_DIR/$BUNDLE_NAME.zip"
if [ -e "$FINAL_DIR" ] || [ -L "$FINAL_DIR" ]; then
    printf 'Refusing to replace existing bundle: %s\n' "$FINAL_DIR" >&2
    exit 73
fi
if [ -e "$ARCHIVE_PATH" ] || [ -L "$ARCHIVE_PATH" ]; then
    printf 'Refusing to replace existing archive: %s\n' "$ARCHIVE_PATH" >&2
    exit 73
fi

if [ "$ASSUME_YES" -eq 0 ]; then
    printf 'Provision %s for customers %s on %s? Type YES: ' "$CAM_ID" "$CUSTOMERS_CANONICAL" "$SSH_TARGET"
    IFS= read -r confirmation || exit 64
    if [ "$confirmation" != 'YES' ]; then
        printf 'Cancelled.\n' >&2
        exit 64
    fi
fi

STAGE_ROOT="$OUTPUT_DIR/.$BUNDLE_NAME.stage.$$"
STAGE_BUNDLE="$STAGE_ROOT/$BUNDLE_NAME"
ARCHIVE_CANDIDATE="$OUTPUT_DIR/.$BUNDLE_NAME.zip.candidate.$$"
JSON_PARSER="$STAGE_ROOT/json-parser.js"
REQUEST_PATH="$STAGE_ROOT/provision.request.json"
RESPONSE_PATH="$STAGE_ROOT/provision.response.json"
ROLLBACK_REQUEST="$STAGE_ROOT/deprovision.request.json"
ROLLBACK_RESPONSE="$STAGE_ROOT/deprovision.response.json"
METADATA_CANDIDATE="$STAGE_ROOT/recovery-metadata.json"
PRIVATE_KEY="$STAGE_BUNDLE/logan-cam.key"
PUBLIC_KEY_FILE="$PRIVATE_KEY.pub"
PROVISION_MAY_HAVE_COMMITTED=0
COMPLETED=0
PUBLISHED=0
ARCHIVE_PUBLISHED=0
RETURNED_FINGERPRINT=''
PROVISION_STATUS=''

early_stage_cleanup() {
    exit_status=$?
    trap - EXIT HUP INT TERM
    rm -rf -- "$STAGE_ROOT"
    rm -f -- "$ARCHIVE_CANDIDATE"
    exit "$exit_status"
}
if [ -e "$STAGE_ROOT" ] || [ -L "$STAGE_ROOT" ] || \
   [ -e "$ARCHIVE_CANDIDATE" ] || [ -L "$ARCHIVE_CANDIDATE" ]; then
    printf 'Refusing to replace an existing staging path.\n' >&2
    exit 73
fi
trap early_stage_cleanup EXIT HUP INT TERM
mkdir "$STAGE_ROOT"
mkdir "$STAGE_BUNDLE"

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

publish_metadata() {
    timestamp=$(date -u '+%Y%m%dT%H%M%S')
    counter=0
    while [ "$counter" -lt 1000 ]; do
        metadata="$OUTPUT_DIR/$BUNDLE_NAME-FAILED-metadata.${timestamp}Z.$$.${counter}.json"
        if [ -e "$metadata" ] || [ -L "$metadata" ]; then
            counter=$((counter + 1))
            continue
        fi
        if ! mv -n "$METADATA_CANDIDATE" "$metadata"; then
            return 1
        fi
        if [ ! -e "$METADATA_CANDIDATE" ]; then
            chmod 600 "$metadata"
            return 0
        fi
        counter=$((counter + 1))
    done
    printf 'Could not publish recovery metadata without replacing an existing path.\n' >&2
    return 1
}

retain_unconfirmed_metadata() {
    {
        printf '{\n'
        printf '  "provision_request": {"cam_id":"%s","customers":[%s],"allow_delivery":%s,"key_fingerprint":"%s"},\n' \
            "$CAM_ID" "$CUSTOMERS_JSON" "$ALLOW_DELIVERY" "$LOCAL_FINGERPRINT"
        printf '  "rollback_request": {"cam_id":"%s","expected_fingerprint":"%s","confirm":true},\n' \
            "$CAM_ID" "$LOCAL_FINGERPRINT"
        printf '  "rollback_response": {"status":"UNCONFIRMED_OR_CONTRACT_MISMATCH"}\n'
        printf '}\n'
    } > "$METADATA_CANDIDATE"
    chmod 600 "$METADATA_CANDIDATE"
    publish_metadata
}

retain_validated_metadata() {
    {
        printf '{\n'
        printf '  "provision_request": {"cam_id":"%s","customers":[%s],"allow_delivery":%s,"key_fingerprint":"%s"},\n' \
            "$CAM_ID" "$CUSTOMERS_JSON" "$ALLOW_DELIVERY" "$LOCAL_FINGERPRINT"
        printf '  "rollback_request": {"cam_id":"%s","expected_fingerprint":"%s","confirm":true},\n' \
            "$CAM_ID" "$LOCAL_FINGERPRINT"
        printf '  "rollback_response": {\n'
        printf '    "status": "%s",\n' "$rollback_status"
        printf '    "cam_id": "%s",\n' "$CAM_ID"
        printf '    "fingerprint": "%s",\n' "$LOCAL_FINGERPRINT"
        printf '    "access_revoked": %s,\n' "$rollback_access_json"
        printf '    "backup_dir": %s,\n' "$rollback_backup_json"
        printf '    "failures": %s,\n' "$rollback_failures_json"
        printf '    "warnings": %s,\n' "$rollback_warnings_json"
        printf '    "shared_account_fallback": %s\n' "$rollback_fallback_json"
        printf '  }\n'
        printf '}\n'
    } > "$METADATA_CANDIDATE"
    chmod 600 "$METADATA_CANDIDATE"
    publish_metadata
}

print_manual_command() {
    manual_request=$(printf '{"cam_id":"%s","expected_fingerprint":"%s","confirm":true}' \
        "$CAM_ID" "$LOCAL_FINGERPRINT")
    printf '%s\n' "Manual fixed command: printf '%s\\n' '$manual_request' | ssh '$SSH_TARGET' 'sudo /opt/logan-mcp/bin/cam-admin deprovision --json'" >&2
}

print_manual_recovery() {
    printf 'HIGH-SEVERITY: automatic access revocation was not confirmed; manual action is required.\n' >&2
    print_manual_command
    return 1
}

report_unconfirmed_rollback() {
    retain_unconfirmed_metadata || true
    print_manual_recovery
}

rollback_provision() {
    printf '{"cam_id":"%s","expected_fingerprint":"%s","confirm":true}\n' \
        "$CAM_ID" "$LOCAL_FINGERPRINT" > "$ROLLBACK_REQUEST"
    rollback_ssh_rc=0
    ssh "$SSH_TARGET" 'sudo /opt/logan-mcp/bin/cam-admin deprovision --json' \
        < "$ROLLBACK_REQUEST" > "$ROLLBACK_RESPONSE" || rollback_ssh_rc=$?
    if [ ! -s "$ROLLBACK_RESPONSE" ]; then
        report_unconfirmed_rollback
        return 1
    fi
    if ! rollback_status=$(json_get "$ROLLBACK_RESPONSE" status 2>/dev/null); then
        report_unconfirmed_rollback
        return 1
    fi
    if ! rollback_cam=$(json_get "$ROLLBACK_RESPONSE" cam_id 2>/dev/null) || \
       ! rollback_fingerprint=$(json_get "$ROLLBACK_RESPONSE" fingerprint 2>/dev/null) || \
       ! rollback_access=$(json_get "$ROLLBACK_RESPONSE" access_revoked 2>/dev/null); then
        report_unconfirmed_rollback
        return 1
    fi
    if [ "$rollback_cam" != "$CAM_ID" ] || \
       [ "$rollback_fingerprint" != "$LOCAL_FINGERPRINT" ]; then
        report_unconfirmed_rollback
        return 1
    fi
    case "$rollback_status" in
        SUCCESS)
            if [ "$rollback_ssh_rc" -eq 0 ] && [ "$rollback_access" = 'true' ]; then
                PROVISION_MAY_HAVE_COMMITTED=0
                printf 'Server provision was rolled back after local publishing failed.\n' >&2
                return 0
            fi
            ;;
        FAILED_ACCESS_REVOKED_CLEANUP_REQUIRED)
            if [ "$rollback_ssh_rc" -eq 1 ] && [ "$rollback_access" = 'true' ] && \
               rollback_access_json=$(json_literal "$ROLLBACK_RESPONSE" access_revoked 2>/dev/null) && \
               rollback_backup_json=$(json_literal "$ROLLBACK_RESPONSE" backup_dir 2>/dev/null) && \
               rollback_failures_json=$(json_literal "$ROLLBACK_RESPONSE" failures 2>/dev/null) && \
               rollback_warnings_json=$(json_literal "$ROLLBACK_RESPONSE" warnings 2>/dev/null) && \
                rollback_fallback_json=$(json_literal "$ROLLBACK_RESPONSE" shared_account_fallback 2>/dev/null); then
                PROVISION_MAY_HAVE_COMMITTED=0
                if retain_validated_metadata; then
                    printf 'Access is revoked, but server cleanup remains incomplete; recovery metadata was retained at %s.\n' \
                        "$metadata" >&2
                else
                    printf 'HIGH-SEVERITY: access is revoked, but recovery metadata retention failed.\n' >&2
                    printf 'Recovery metadata path: NOT PUBLISHED; the staging candidate will be removed.\n' >&2
                    print_manual_command
                fi
                return 1
            fi
            ;;
        FAILED_REVOCATION_UNCONFIRMED)
            if [ "$rollback_ssh_rc" -eq 1 ] && \
               rollback_access_json=$(json_literal "$ROLLBACK_RESPONSE" access_revoked 2>/dev/null) && \
               rollback_backup_json=$(json_literal "$ROLLBACK_RESPONSE" backup_dir 2>/dev/null) && \
               rollback_failures_json=$(json_literal "$ROLLBACK_RESPONSE" failures 2>/dev/null) && \
               rollback_warnings_json=$(json_literal "$ROLLBACK_RESPONSE" warnings 2>/dev/null) && \
               rollback_fallback_json=$(json_literal "$ROLLBACK_RESPONSE" shared_account_fallback 2>/dev/null); then
                retain_validated_metadata || true
                print_manual_recovery
                return 1
            fi
            ;;
        *) ;;
    esac
    report_unconfirmed_rollback
    return 1
}

on_exit() {
    exit_status=$?
    trap - EXIT HUP INT TERM
    if [ "$PROVISION_MAY_HAVE_COMMITTED" -eq 1 ] && [ "$COMPLETED" -eq 0 ]; then
        rollback_provision || true
    fi
    if [ "$COMPLETED" -eq 0 ]; then
        if [ "$PUBLISHED" -eq 1 ]; then
            rm -rf -- "$FINAL_DIR"
        fi
        if [ "$ARCHIVE_PUBLISHED" -eq 1 ]; then
            rm -f -- "$ARCHIVE_PATH"
        fi
    fi
    rm -rf -- "$STAGE_ROOT"
    rm -f -- "$ARCHIVE_CANDIDATE"
    exit "$exit_status"
}
trap - EXIT HUP INT TERM
trap on_exit EXIT HUP INT TERM

ssh-keygen -q -t ed25519 -N '' -C "logan-cam:$CAM_ID" -f "$PRIVATE_KEY"
chmod 600 "$PRIVATE_KEY"
chmod 644 "$PUBLIC_KEY_FILE"
PUBLIC_KEY=$(sed -n '1p' "$PUBLIC_KEY_FILE")
if [ "$(wc -l < "$PUBLIC_KEY_FILE" | tr -d ' ')" -ne 1 ]; then
    printf 'Generated public key is not exactly one line.\n' >&2
    exit 70
fi
case "$PUBLIC_KEY" in
    "ssh-ed25519 "*" logan-cam:$CAM_ID") ;;
    *)
        printf 'Generated public key has an unexpected format or comment.\n' >&2
        exit 70
        ;;
esac
LOCAL_FINGERPRINT=$(ssh-keygen -lf "$PUBLIC_KEY_FILE" -E sha256 | awk 'NR == 1 { print $2 }')
case "$LOCAL_FINGERPRINT" in
    SHA256:*) ;;
    *)
        printf 'Could not determine the local key fingerprint.\n' >&2
        exit 70
        ;;
esac

printf '{"cam_id":"%s","customers":[%s],"allow_delivery":%s,"public_key":"%s"}\n' \
    "$CAM_ID" "$CUSTOMERS_JSON" "$ALLOW_DELIVERY" "$PUBLIC_KEY" > "$REQUEST_PATH"

provision_ssh_rc=0
PROVISION_MAY_HAVE_COMMITTED=1
ssh "$SSH_TARGET" 'sudo /opt/logan-mcp/bin/cam-admin provision --json' \
    < "$REQUEST_PATH" > "$RESPONSE_PATH" || provision_ssh_rc=$?
if [ ! -s "$RESPONSE_PATH" ]; then
    printf 'Provisioning returned no JSON response.\n' >&2
    exit 70
fi
PROVISION_STATUS=$(json_get "$RESPONSE_PATH" status) || {
    printf 'Provisioning response is not valid contract JSON.\n' >&2
    exit 70
}
if [ "$PROVISION_STATUS" != 'SUCCESS' ]; then
    printf 'Server provisioning failed with status: %s\n' "$PROVISION_STATUS" >&2
    exit 70
fi
RETURNED_FINGERPRINT=$(json_get "$RESPONSE_PATH" fingerprint)
RETURNED_CAM=$(json_get "$RESPONSE_PATH" cam_id)
RETURNED_CUSTOMERS=$(json_get "$RESPONSE_PATH" customers)
RETURNED_DELIVERY=$(json_get "$RESPONSE_PATH" allow_delivery)
CAM_HOST=$(json_get "$RESPONSE_PATH" connection.host)
CAM_PORT=$(json_get "$RESPONSE_PATH" connection.port)
CAM_REMOTE_USER=$(json_get "$RESPONSE_PATH" connection.remote_user)
CAM_HOST_PUBLIC_KEY=$(json_get "$RESPONSE_PATH" connection.host_public_key)

if [ "$provision_ssh_rc" -ne 0 ]; then
    printf 'Provisioning returned SUCCESS with a nonzero SSH exit; refusing local publish.\n' >&2
    exit 70
fi
if [ "$RETURNED_CAM" != "$CAM_ID" ] || \
   [ "$RETURNED_CUSTOMERS" != "$CUSTOMERS_CANONICAL" ] || \
   [ "$RETURNED_DELIVERY" != "$ALLOW_DELIVERY" ] || \
   [ "$RETURNED_FINGERPRINT" != "$LOCAL_FINGERPRINT" ]; then
    printf 'Provisioning response does not exactly match the requested CAM assignment or local key.\n' >&2
    exit 70
fi
case "$RETURNED_FINGERPRINT" in SHA256:*) ;; *) exit 70 ;; esac
case "$CAM_HOST" in ''|*[!A-Za-z0-9._-]*) exit 70 ;; esac
case "$CAM_PORT" in ''|*[!0-9]*) exit 70 ;; esac
if [ "$CAM_PORT" -lt 1 ] || [ "$CAM_PORT" -gt 65535 ]; then exit 70; fi
if [ "$CAM_REMOTE_USER" != 'cam' ]; then exit 70; fi
case "$CAM_HOST_PUBLIC_KEY" in 'ssh-ed25519 '*) ;; *) exit 70 ;; esac

reject_template_value() {
    if printf '%s' "$1" | LC_ALL=C grep -q '[[:cntrl:]]'; then
        return 1
    fi
    case "$1" in
        *'|'*|*"'"*) return 1 ;;
    esac
    return 0
}
for value in "$CAM_HOST" "$CAM_PORT" "$CAM_REMOTE_USER" "$CAM_HOST_PUBLIC_KEY"; do
    if ! reject_template_value "$value"; then
        printf 'Server returned an unsafe template token value.\n' >&2
        exit 70
    fi
done

sed_escape() {
    printf '%s' "$1" | sed 's/[\\&|]/\\&/g'
}
render_connection_template() {
    source_path=$1
    destination_path=$2
    safe_host=$(sed_escape "$CAM_HOST")
    safe_port=$(sed_escape "$CAM_PORT")
    safe_user=$(sed_escape "$CAM_REMOTE_USER")
    safe_host_key=$(sed_escape "$CAM_HOST_PUBLIC_KEY")
    sed \
        -e "s|@@CAM_HOST@@|$safe_host|g" \
        -e "s|@@CAM_PORT@@|$safe_port|g" \
        -e "s|@@CAM_REMOTE_USER@@|$safe_user|g" \
        -e "s|@@CAM_HOST_PUBLIC_KEY@@|$safe_host_key|g" \
        "$source_path" > "$destination_path"
}

html_escape() {
    printf '%s' "$1" | sed \
        -e 's/&/\&amp;/g' \
        -e 's/</\&lt;/g' \
        -e 's/>/\&gt;/g' \
        -e 's/"/\&quot;/g' \
        -e "s/'/\\&#39;/g"
}

render_connection_template "$MAC_TEMPLATE" "$STAGE_BUNDLE/Install-Logan-MCP.command"
render_connection_template "$WINDOWS_PS_TEMPLATE" "$STAGE_BUNDLE/Install-Logan-MCP.ps1"
cp "$WINDOWS_CMD_TEMPLATE" "$STAGE_BUNDLE/Double-Click-to-Install.cmd"

README_CAM=$(sed_escape "$(html_escape "$CAM_ID")")
README_SERVER=$(sed_escape "$(html_escape "$SSH_TARGET")")
README_CREATED=$(sed_escape "$(html_escape "$(date -u '+%Y-%m-%dT%H:%M:%SZ')")")
README_FINGERPRINT=$(sed_escape "$(html_escape "$RETURNED_FINGERPRINT")")
sed \
    -e "s|@@CAM_ID@@|$README_CAM|g" \
    -e "s|@@CAM_SERVER_NAME@@|$README_SERVER|g" \
    -e "s|@@CAM_CREATED_AT@@|$README_CREATED|g" \
    -e "s|@@CAM_FINGERPRINT@@|$README_FINGERPRINT|g" \
    "$README_TEMPLATE" > "$STAGE_BUNDLE/README.html"

for rendered in \
    "$STAGE_BUNDLE/Install-Logan-MCP.command" \
    "$STAGE_BUNDLE/Install-Logan-MCP.ps1" \
    "$STAGE_BUNDLE/Double-Click-to-Install.cmd" \
    "$STAGE_BUNDLE/README.html"
do
    if grep -q '@@CAM_' "$rendered"; then
        printf 'Unrendered CAM token remains in %s.\n' "$rendered" >&2
        exit 70
    fi
done
chmod 700 "$STAGE_BUNDLE" "$STAGE_BUNDLE/Install-Logan-MCP.command" "$STAGE_BUNDLE/Double-Click-to-Install.cmd"
chmod 600 "$STAGE_BUNDLE/Install-Logan-MCP.ps1" "$STAGE_BUNDLE/README.html" "$PRIVATE_KEY"
rm -f "$PUBLIC_KEY_FILE"

ditto -c -k --sequesterRsrc --keepParent "$STAGE_BUNDLE" "$ARCHIVE_CANDIDATE"
chmod 600 "$ARCHIVE_CANDIDATE"
if [ -e "$FINAL_DIR" ] || [ -e "$ARCHIVE_PATH" ]; then
    printf 'A bundle or archive appeared while provisioning; refusing to replace it.\n' >&2
    exit 73
fi
mv -n "$STAGE_BUNDLE" "$FINAL_DIR"
if [ -e "$STAGE_BUNDLE" ]; then
    printf 'Concurrent bundle path detected; refusing to overwrite it.\n' >&2
    exit 73
fi
PUBLISHED=1
mv -n "$ARCHIVE_CANDIDATE" "$ARCHIVE_PATH"
if [ -e "$ARCHIVE_CANDIDATE" ]; then
    printf 'Concurrent archive path detected; refusing to overwrite it.\n' >&2
    exit 73
fi
ARCHIVE_PUBLISHED=1
chmod 600 "$ARCHIVE_PATH"
COMPLETED=1

printf 'Provisioning succeeded.\nBundle: %s\nArchive: %s\n' "$FINAL_DIR" "$ARCHIVE_PATH"
printf 'Security notice: this plain ZIP is not encryption. Deliver it only through an approved secure channel.\n'
