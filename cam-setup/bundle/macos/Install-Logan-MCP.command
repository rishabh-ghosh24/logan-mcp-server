#!/bin/sh
set -eu
umask 077

CAM_HOST='@@CAM_HOST@@'
CAM_PORT='@@CAM_PORT@@'
CAM_REMOTE_USER='@@CAM_REMOTE_USER@@'
CAM_HOST_PUBLIC_KEY='@@CAM_HOST_PUBLIC_KEY@@'

NON_INTERACTIVE=0
if [ "${1:-}" = "--non-interactive" ]; then
    NON_INTERACTIVE=1
    shift
fi
if [ "$#" -ne 0 ]; then
    printf 'Usage: %s [--non-interactive]\n' "$0" >&2
    exit 64
fi

CAM_TOKEN_PREFIX='@@''CAM_'
case "$CAM_HOST$CAM_PORT$CAM_REMOTE_USER$CAM_HOST_PUBLIC_KEY" in
    *"$CAM_TOKEN_PREFIX"*)
        printf 'Installer metadata is incomplete. Contact your administrator.\n' >&2
        exit 65
        ;;
esac
case "$CAM_HOST" in
    ''|*[!A-Za-z0-9._-]*)
        printf 'Installer contains an invalid server host.\n' >&2
        exit 65
        ;;
esac
case "$CAM_REMOTE_USER" in
    cam) ;;
    *)
        printf 'Installer contains an invalid restricted account.\n' >&2
        exit 65
        ;;
esac
case "$CAM_PORT" in
    ''|*[!0-9]*)
        printf 'Installer contains an invalid server port.\n' >&2
        exit 65
        ;;
esac
if [ "$CAM_PORT" -lt 1 ] || [ "$CAM_PORT" -gt 65535 ]; then
    printf 'Installer contains an invalid server port.\n' >&2
    exit 65
fi

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
SOURCE_KEY="$SCRIPT_DIR/logan-cam.key"
if [ ! -f "$SOURCE_KEY" ] || [ -L "$SOURCE_KEY" ]; then
    printf 'Missing private key beside installer: %s\n' "$SOURCE_KEY" >&2
    exit 66
fi

INSTALL_DIR="$HOME/.logan-mcp"
KEY_PATH="$INSTALL_DIR/logan-cam.key"
KNOWN_HOSTS_PATH="$INSTALL_DIR/known_hosts"
CODEX_DIR="$HOME/.codex"
CONFIG_PATH="$CODEX_DIR/config.toml"

reject_newline() {
    case "$1" in
        *'
'*) return 1 ;;
    esac
    return 0
}

for value in "$HOME" "$CAM_HOST" "$CAM_PORT" "$CAM_REMOTE_USER" "$CAM_HOST_PUBLIC_KEY"; do
    if ! reject_newline "$value"; then
        printf 'Installer metadata or path contains a newline.\n' >&2
        exit 65
    fi
done

HOST_KEY=$(printf '%s\n' "$CAM_HOST_PUBLIC_KEY" | awk '
    NF >= 2 && $1 ~ /^ssh-/ && $2 ~ /^[A-Za-z0-9+\/=]+$/ {
        print $1 " " $2
        found = 1
    }
    END { if (!found) exit 1 }
') || {
    printf 'Installer contains an invalid pinned server key.\n' >&2
    exit 65
}
if [ "$(printf '%s\n' "$HOST_KEY" | wc -l | tr -d ' ')" -ne 1 ]; then
    printf 'Installer contains an invalid pinned server key.\n' >&2
    exit 65
fi

if [ "$CAM_PORT" = "22" ]; then
    KNOWN_HOST="$CAM_HOST"
else
    KNOWN_HOST="[$CAM_HOST]:$CAM_PORT"
fi

toml_quote() {
    escaped=$(printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g')
    printf '"%s"' "$escaped"
}

validate_and_filter() {
    input=$1
    output=$2
    awk '
        BEGIN { current_top_level = 1 }
        function flush_pending(    i) {
            for (i = 1; i <= pending_count; i++) {
                print pending[i]
                delete pending[i]
            }
            pending_count = 0
        }
        function parse_header(line,    i, length_line, array_header, char,
                              quote, value, start, escaped) {
            parsed_count = 0
            parsed_first = ""
            parsed_second = ""
            length_line = length(line)
            i = 1
            while (i <= length_line && substr(line, i, 1) ~ /[[:space:]]/) i++
            if (substr(line, i, 1) != "[") return 0
            i++
            array_header = 0
            if (substr(line, i, 1) == "[") {
                array_header = 1
                i++
            }
            while (1) {
                while (i <= length_line && substr(line, i, 1) ~ /[[:space:]]/) i++
                char = substr(line, i, 1)
                value = ""
                if (char == "\"" || char == "\047") {
                    quote = char
                    i++
                    while (i <= length_line) {
                        char = substr(line, i, 1)
                        if (char == quote) {
                            i++
                            break
                        }
                        if (quote == "\"" && char == "\\") {
                            return 0
                        } else {
                            value = value char
                            i++
                        }
                    }
                    if (char != quote) return 0
                } else {
                    start = i
                    while (i <= length_line &&
                           substr(line, i, 1) ~ /^[A-Za-z0-9_-]$/) i++
                    if (i == start) return 0
                    value = substr(line, start, i - start)
                }
                parsed_count++
                if (parsed_count == 1) parsed_first = value
                if (parsed_count == 2) parsed_second = value
                while (i <= length_line && substr(line, i, 1) ~ /[[:space:]]/) i++
                if (substr(line, i, 1) == ".") {
                    i++
                    continue
                }
                if (array_header) {
                    if (substr(line, i, 2) != "]]" ) return 0
                    i += 2
                } else {
                    if (substr(line, i, 1) != "]") return 0
                    i++
                }
                while (i <= length_line && substr(line, i, 1) ~ /[[:space:]]/) i++
                if (i <= length_line && substr(line, i, 1) != "#") return 0
                return 1
            }
        }
        {
            if (index($0, "\"\"\"") || index($0, "\047\047\047")) {
                print "Multiline TOML strings are unsupported" > "/dev/stderr"
                exit 42
            }
            if ($0 ~ /^[[:space:]]*$/) {
                if (!skip) pending[++pending_count] = $0
                next
            }
            trimmed = $0
            sub(/^[[:space:]]*/, "", trimmed)
            if (substr(trimmed, 1, 1) == "[") {
                if (!parse_header($0)) {
                    print "Malformed TOML table header on line " NR > "/dev/stderr"
                    exit 40
                }
                root = (parsed_count == 2 &&
                        parsed_first == "mcp_servers" &&
                        parsed_second == "assurance-logan")
                nested = (parsed_count > 2 &&
                          parsed_first == "mcp_servers" &&
                          parsed_second == "assurance-logan")
                current_top_level = 0
                inside_mcp_servers = (parsed_count == 1 &&
                                      parsed_first == "mcp_servers")
                if (root) {
                    roots++
                    if (roots > 1) {
                        print "Duplicate Assurance Logan MCP root table" > "/dev/stderr"
                        exit 41
                    }
                    if (pending_count > 0) {
                        delete pending[pending_count]
                        pending_count--
                    }
                }
                if (!root && !nested) flush_pending()
                if (root) flush_pending()
                skip = (root || nested)
            } else {
                compact = trimmed
                gsub(/[[:space:]]/, "", compact)
                dotted_remainder = ""
                if (index(compact, "mcp_servers.") == 1) {
                    dotted_remainder = substr(compact, length("mcp_servers.") + 1)
                } else if (index(compact, "\"mcp_servers\".") == 1) {
                    dotted_remainder = substr(compact, length("\"mcp_servers\".") + 1)
                } else if (index(compact, "\047mcp_servers\047.") == 1) {
                    dotted_remainder = substr(compact, length("\047mcp_servers\047.") + 1)
                }
                dotted_target = (dotted_remainder ~ /^("assurance-logan"|assurance-logan)=/ ||
                                 index(dotted_remainder, "\047assurance-logan\047=") == 1)
                inline_mcp_servers = (compact ~ /^("mcp_servers"|mcp_servers)=/ ||
                                      index(compact, "\047mcp_servers\047=") == 1)
                nested_target_key = (compact ~ /^("assurance-logan"|assurance-logan)=/ ||
                                     index(compact, "\047assurance-logan\047=") == 1)
                if ((current_top_level && (dotted_target || inline_mcp_servers)) ||
                    (inside_mcp_servers && nested_target_key)) {
                    print "Unsupported TOML declaration may alias Assurance Logan MCP" > "/dev/stderr"
                    exit 43
                }
            }
            if (!skip) {
                flush_pending()
                print $0
            }
        }
        END { if (!skip) flush_pending() }
    ' "$input" > "$output"
}

if ! command -v mktemp >/dev/null 2>&1; then
    printf 'Required tool is unavailable: mktemp\n' >&2
    exit 69
fi

mkdir -p "$CODEX_DIR"
chmod 700 "$CODEX_DIR"
if [ -L "$CONFIG_PATH" ] || { [ -e "$CONFIG_PATH" ] && [ ! -f "$CONFIG_PATH" ]; }; then
    printf 'Refusing non-regular Codex config path: %s\n' "$CONFIG_PATH" >&2
    exit 73
fi

mkdir -p "$INSTALL_DIR"
chmod 700 "$INSTALL_DIR"
for installed_path in "$KEY_PATH" "$KNOWN_HOSTS_PATH"; do
    if [ -L "$installed_path" ] || { [ -e "$installed_path" ] && [ ! -f "$installed_path" ]; }; then
        printf 'Refusing non-regular path in secure install directory: %s\n' "$installed_path" >&2
        exit 73
    fi
done

CANDIDATE=''
VALIDATED=''
KEY_CANDIDATE=''
HOST_CANDIDATE=''
KEY_BACKUP=''
HOST_BACKUP=''
KEY_EXISTED=0
HOST_EXISTED=0
KEY_PUBLISHED=0
HOST_PUBLISHED=0
INSTALL_COMPLETE=0

require_candidate() {
    if [ ! -f "$1" ] || [ -L "$1" ]; then
        printf 'Refusing unsafe installer candidate: %s\n' "$1" >&2
        return 1
    fi
    return 0
}

secure_mktemp() {
    created=$(mktemp "$1") || return 1
    if ! require_candidate "$created"; then
        return 1
    fi
    chmod 600 "$created"
    printf '%s\n' "$created"
}

cleanup() {
    exit_status=$?
    trap - EXIT HUP INT TERM
    if [ "$INSTALL_COMPLETE" -eq 0 ]; then
        if [ "$KEY_PUBLISHED" -eq 1 ]; then
            if [ "$KEY_EXISTED" -eq 1 ] && require_candidate "$KEY_BACKUP"; then
                mv -f "$KEY_BACKUP" "$KEY_PATH" || true
            else
                rm -f "$KEY_PATH"
            fi
        fi
        if [ "$HOST_PUBLISHED" -eq 1 ]; then
            if [ "$HOST_EXISTED" -eq 1 ] && require_candidate "$HOST_BACKUP"; then
                mv -f "$HOST_BACKUP" "$KNOWN_HOSTS_PATH" || true
            else
                rm -f "$KNOWN_HOSTS_PATH"
            fi
        fi
    fi
    for temporary in "$CANDIDATE" "$VALIDATED" "$KEY_CANDIDATE" \
        "$HOST_CANDIDATE" "$KEY_BACKUP" "$HOST_BACKUP"
    do
        if [ -n "$temporary" ]; then rm -f "$temporary"; fi
    done
    exit "$exit_status"
}
trap cleanup EXIT HUP INT TERM

CANDIDATE=$(secure_mktemp "$CODEX_DIR/.config.toml.candidate.XXXXXX")
VALIDATED=$(secure_mktemp "$CODEX_DIR/.config.toml.validated.XXXXXX")
KEY_CANDIDATE=$(secure_mktemp "$INSTALL_DIR/.logan-cam.key.candidate.XXXXXX")
HOST_CANDIDATE=$(secure_mktemp "$INSTALL_DIR/.known-hosts.candidate.XXXXXX")
KEY_BACKUP=$(secure_mktemp "$INSTALL_DIR/.logan-cam.key.backup.XXXXXX")
HOST_BACKUP=$(secure_mktemp "$INSTALL_DIR/.known-hosts.backup.XXXXXX")

require_candidate "$CANDIDATE"
require_candidate "$VALIDATED"
if [ -f "$CONFIG_PATH" ]; then
    validate_and_filter "$CONFIG_PATH" "$CANDIDATE" || {
        printf 'Codex config was not changed. Fix the reported TOML safety error first.\n' >&2
        exit 65
    }
else
    : > "$CANDIDATE"
fi
require_candidate "$CANDIDATE"

if [ -s "$CANDIDATE" ]; then
    printf '\n' >> "$CANDIDATE"
fi
{
    printf '[mcp_servers.assurance-logan]\n'
    printf 'command = "ssh"\n'
    printf 'args = ['
    separator=''
    for argument in \
        -i "$KEY_PATH" \
        -o BatchMode=yes \
        -o IdentitiesOnly=yes \
        -o StrictHostKeyChecking=yes \
        -o "UserKnownHostsFile=$KNOWN_HOSTS_PATH" \
        -o ServerAliveInterval=60 \
        -o ServerAliveCountMax=3 \
        -p "$CAM_PORT" \
        "$CAM_REMOTE_USER@$CAM_HOST"
    do
        printf '%s' "$separator"
        toml_quote "$argument"
        separator=', '
    done
    printf ']\n'
} >> "$CANDIDATE"
chmod 600 "$CANDIDATE"

require_candidate "$VALIDATED"
validate_and_filter "$CANDIDATE" "$VALIDATED" || {
    printf 'Generated Codex config failed validation; original was not changed.\n' >&2
    exit 70
}
require_candidate "$CANDIDATE"
require_candidate "$VALIDATED"
if [ "$(grep -c '^\[mcp_servers\.assurance-logan\]$' "$CANDIDATE")" -ne 1 ]; then
    printf 'Generated Codex config failed validation; original was not changed.\n' >&2
    exit 70
fi

require_candidate "$KEY_CANDIDATE"
cp "$SOURCE_KEY" "$KEY_CANDIDATE"
chmod 600 "$KEY_CANDIDATE"
require_candidate "$HOST_CANDIDATE"
printf '%s %s\n' "$KNOWN_HOST" "$HOST_KEY" > "$HOST_CANDIDATE"
chmod 600 "$HOST_CANDIDATE"
require_candidate "$KEY_CANDIDATE"
require_candidate "$HOST_CANDIDATE"

if [ -f "$KEY_PATH" ]; then
    require_candidate "$KEY_BACKUP"
    cp -p "$KEY_PATH" "$KEY_BACKUP"
    KEY_EXISTED=1
fi
if [ -f "$KNOWN_HOSTS_PATH" ]; then
    require_candidate "$HOST_BACKUP"
    cp -p "$KNOWN_HOSTS_PATH" "$HOST_BACKUP"
    HOST_EXISTED=1
fi
require_candidate "$KEY_BACKUP"
require_candidate "$HOST_BACKUP"

if [ -f "$CONFIG_PATH" ]; then
    timestamp=$(date -u '+%Y%m%dT%H%M%S')
    backup="$CONFIG_PATH.backup-${timestamp}Z"
    counter=0
    while [ -e "$backup" ] || [ -L "$backup" ]; do
        counter=$((counter + 1))
        backup="$CONFIG_PATH.backup-${timestamp}.${counter}Z"
    done
    cp "$CONFIG_PATH" "$backup"
    chmod 600 "$backup"
fi

require_candidate "$KEY_CANDIDATE"
mv -f "$KEY_CANDIDATE" "$KEY_PATH"
KEY_PUBLISHED=1
require_candidate "$HOST_CANDIDATE"
mv -f "$HOST_CANDIDATE" "$KNOWN_HOSTS_PATH"
HOST_PUBLISHED=1
require_candidate "$CANDIDATE"
if ! mv -f "$CANDIDATE" "$CONFIG_PATH"; then
    printf 'Codex config publication failed; restoring installed SSH artifacts.\n' >&2
    exit 70
fi
INSTALL_COMPLETE=1

printf 'Logan MCP is installed for Codex. Restart Codex before using it.\n'
if [ "$NON_INTERACTIVE" -eq 0 ]; then
    printf 'Press Return to close this window: '
    IFS= read -r _answer || true
fi
