#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

root=/srv/plotter
state=$root/shared
profile=/etc/plotter/machine-profile.json
nginx_include=/etc/nginx/plotter/upstream.conf
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

fail() { printf '%s\n' "$*" >&2; exit 1; }

usage() {
    cat <<'EOF'
Usage:
  sudo bash deploy/release.sh stage VERSION SOURCE_TREE
  sudo bash deploy/release.sh activate VERSION
  sudo bash deploy/release.sh rollback
EOF
}

valid_version() { [[ $1 =~ ^[0-9][a-zA-Z0-9._-]*$ ]]; }

slot_port() {
    case $1 in
        blue) printf '%s\n' "$BLUE_PORT" ;;
        green) printf '%s\n' "$GREEN_PORT" ;;
        *) return 1 ;;
    esac
}

active_slot() {
    sed -n '1s/^# plotter-slot: \(blue\|green\|none\)$/\1/p' "$nginx_include"
}

set_link() {
    local target=$1 link=$2 tmp
    tmp=$(mktemp "${link}.XXXXXXXX")
    rm -- "$tmp"
    ln -s -- "$target" "$tmp"
    mv -Tf -- "$tmp" "$link"
}

render_proxy() {
    local slot=$1 temp
    temp=$(mktemp "${nginx_include}.XXXXXXXX")
    printf '# plotter-slot: %s\nproxy_pass http://127.0.0.1:%s;\nadd_header X-Plotter-Slot %s always;\n' \
        "$slot" "$(slot_port "$slot")" "$slot" > "$temp"
    chmod 0644 -- "$temp"
    mv -f -- "$temp" "$nginx_include"
}

worker_pids() {
    local master
    master=$(systemctl show nginx --property=MainPID --value)
    [[ $master =~ ^[1-9][0-9]*$ ]] || return 1
    ps --ppid "$master" -o pid= -o args= | awk '/nginx: worker process/ {print $1}'
}

wait_workers() {
    local workers=$1 elapsed=0 pid busy
    while (( elapsed < DRAIN_SECONDS )); do
        busy=0
        for pid in $workers; do
            if kill -0 "$pid" 2>/dev/null; then busy=1; fi
        done
        if (( ! busy )); then return 0; fi
        sleep 1
        (( elapsed += 1 ))
    done
    return 1
}

check_http() {
    local address=$1
    shift
    curl -fsS --noproxy '*' --max-time 5 "$@" "$address/api/status" |
        python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(d.get("centralMode") is not True)' &&
        curl -fsS --noproxy '*' --max-time 10 "$@" "$address/api/workspace" |
        python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(type(d.get("revision")) is not int or "workspace" not in d or d.get("recovered") is True)'
}

wait_http() {
    local attempt
    for (( attempt = 0; attempt < 25; attempt++ )); do
        if check_http "$@" 2>/dev/null; then return 0; fi
        sleep 1
    done
    check_http "$@"
}

copy_state() {
    local dest=$1 file
    # The app locks this stable sibling while publishing the backup and primary.
    (
        flock -s 8
        for file in workspace.json workspace.json.bak cut-journal.json; do
            if [[ -f $state/$file ]]; then cp -- "$state/$file" "$dest/$file"; fi
        done
    ) 8<"$state/workspace.json.lock"
}

check_state() {
    local release=$1 copy=$2
    "$release/.venv/bin/python" -B "$script_dir/check-state.py" "$release/web-plotter" "$copy"
}

stage() (
    local version=$1 source=$2 final=$root/releases/$1 preview= preview_started=0 ready=0
    valid_version "$version" || fail "Invalid version label."
    [[ -f $source/web-plotter/server.py && -f $source/web-plotter/requirements.txt ]] || fail "Source tree lacks web-plotter files."
    [[ ! -e $final ]] || fail "Release directory already exists. Use a new version label."
    cleanup_stage() {
        if (( preview_started )); then systemctl stop plotter-preview.service >/dev/null 2>&1 || true; fi
        if [[ -n $preview ]]; then rm -rf -- "$preview"; fi
        if (( ! ready )); then rm -rf -- "$final"; fi
    }
    trap cleanup_stage EXIT
    # Virtual environments embed absolute paths. Build in the release's final directory.
    install -d -m 0755 "$final/web-plotter/static"
    install -m 0644 "$source"/web-plotter/*.py "$source/web-plotter/requirements.txt" "$final/web-plotter/"
    install -m 0644 "$source/VERSION" "$final/VERSION"
    install -m 0644 "$source"/web-plotter/static/{app.js,index.html,simulation.js,style.css} "$final/web-plotter/static/"
    install -D -m 0644 "$source/research/machine-profile.example.json" "$final/research/machine-profile.example.json"
    python3 -m venv "$final/.venv"
    "$final/.venv/bin/python" -m pip install -r "$final/web-plotter/requirements.txt"
    # A service account must be able to traverse the immutable release and its venv.
    chmod -R a+rX "$final"
    preview=$(mktemp -d "$root/preview.XXXXXXXX")
    copy_state "$preview"
    check_state "$final" "$preview"
    (cd "$final/web-plotter" && "$final/.venv/bin/python" -B -m unittest discover -q)
    chown -R plotter:plotter "$preview"
    systemctl is-active --quiet plotter-preview.service && fail "Plotter preview service is already running."
    preview_started=1
    systemd-run --quiet --collect --unit=plotter-preview --service-type=exec \
        --uid=plotter --gid=plotter --working-directory="$final/web-plotter" \
        "$final/.venv/bin/python" -B server.py --central --port "$PREVIEW_PORT" --profile "$profile" \
        --workspace "$preview/workspace.json" --public-origin "$origin"
    wait_http "http://127.0.0.1:$PREVIEW_PORT" || fail "Candidate failed isolated health checks."
    systemctl stop plotter-preview.service
    preview_started=0
    touch "$final/.staged"
    ready=1
    printf 'Staged and checked release %s using an isolated workspace.\n' "$version"
)

switch_release() (
    local release=$1 slot old_slot old_release= workers= recovery_workers= backup attempt
    local started=0 config_changed=0 committed=0
    local curl_tls=(--connect-to "$PUBLIC_HOST:$PUBLIC_PORT:127.0.0.1:$PUBLIC_PORT")
    [[ -f $release/.staged ]] || fail "Release must pass stage first."
    old_slot=$(active_slot)
    case $old_slot in
        none|green) slot=blue ;;
        blue) slot=green ;;
        *) fail "Nginx include is missing its Plotter slot marker." ;;
    esac
    if [[ $old_slot != none ]]; then
        old_release=$(readlink -f -- "$root/slots/$old_slot")
        [[ -d $old_release && -f $old_release/.staged ]] || fail "Active release is unknown."
        [[ $old_release != "$release" ]] || fail "Release is already live."
        systemctl is-active --quiet "plotter@$old_slot.service" || fail "Active Plotter service is stopped."
    fi
    if systemctl is-active --quiet "plotter@$slot.service"; then
        fail "Alternate slot still running. Finish draining it before retrying."
    fi

    backup=$(mktemp -d "$root/shared/backups/switch.XXXXXXXX")
    cleanup() {
        local result=$?
        if (( ! committed )); then
            if (( config_changed )); then
                recovery_workers=$(worker_pids || true)
                if install -m 0644 "$backup/nginx-before.conf" "${nginx_include}.restore" &&
                    mv -f -- "${nginx_include}.restore" "$nginx_include" &&
                    nginx -t && systemctl reload nginx; then
                    if (( started )); then systemctl disable "plotter@$slot.service" >/dev/null || true; fi
                    if (( started )) && wait_workers "$recovery_workers"; then
                        systemctl stop "plotter@$slot.service" || true
                    fi
                else
                    printf 'Could not restore the previous Nginx configuration. Both services were left running.\n' >&2
                fi
            elif (( started )); then
                systemctl disable --now "plotter@$slot.service" >/dev/null || true
            fi
        fi
        return "$result"
    }
    trap cleanup EXIT
    copy_state "$backup"
    cp -- "$nginx_include" "$backup/nginx-before.conf"
    check_state "$release" "$backup"
    set_link "$release" "$root/slots/$slot"
    started=1
    systemctl start "plotter@$slot.service"
    wait_http "http://127.0.0.1:$(slot_port "$slot")" || fail "Candidate failed live-port health checks."
    # Keep the old backend until Nginx's prior worker generation has drained.
    workers=$(worker_pids)
    [[ -n $workers ]] || fail "No Nginx worker processes found."
    render_proxy "$slot"
    config_changed=1
    nginx -t
    systemctl reload nginx
    if [[ -n ${TLS_CA_FILE:-} ]]; then curl_tls+=(--cacert "$TLS_CA_FILE"); fi
    wait_http "$origin" "${curl_tls[@]}" ||
        fail "Plotter HTTPS health check failed after switch."
    for (( attempt = 0; attempt < 15; attempt++ )); do
        if curl -fsS --noproxy '*' --max-time 5 "${curl_tls[@]}" \
            -D - -o /dev/null "$origin/api/status" | tr -d '\r' | grep -Eqi "^X-Plotter-Slot: $slot$"; then
            break
        fi
        sleep 1
    done
    (( attempt < 15 )) || fail "HTTPS did not switch to the candidate slot."
    systemctl enable "plotter@$slot.service" >/dev/null
    if [[ -n $old_release ]]; then
        set_link "$old_release" "$root/previous"
    fi
    committed=1
    if [[ $old_slot != none ]]; then
        systemctl disable "plotter@$old_slot.service" >/dev/null
        if wait_workers "$workers"; then
            systemctl stop "plotter@$old_slot.service"
        else
            printf 'Old slot %s is still running for draining requests. Check workers before stopping it.\n' "$old_slot" >&2
        fi
    fi
    printf 'Active release: %s (%s). Retained backup: %s\n' "${release##*/}" "$slot" "$backup"
)

if [[ ${1:-} == --help || ${1:-} == -h ]]; then usage; exit 0; fi
case ${1:-} in
    stage) [[ $# == 3 ]] || { usage >&2; exit 2; } ;;
    activate) [[ $# == 2 ]] || { usage >&2; exit 2; } ;;
    rollback) [[ $# == 1 ]] || { usage >&2; exit 2; } ;;
    *) usage >&2; exit 2 ;;
esac
[[ $EUID == 0 ]] || fail "Run this script as root on the Plotter host."
[[ -r /etc/plotter/deploy.conf && -r $profile ]] || fail "Install /etc/plotter configuration first."
# shellcheck source=/dev/null
source /etc/plotter/deploy.conf
[[ ${PUBLIC_HOST:-} =~ ^[a-zA-Z0-9][a-zA-Z0-9.-]+$ ]] || fail "Invalid PUBLIC_HOST."
PUBLIC_PORT=${PUBLIC_PORT:-443}
[[ $PUBLIC_PORT =~ ^[1-9][0-9]{0,4}$ ]] && (( PUBLIC_PORT <= 65535 )) || fail "Invalid PUBLIC_PORT."
for port in "${BLUE_PORT:-}" "${GREEN_PORT:-}" "${PREVIEW_PORT:-}"; do
    [[ $port =~ ^[1-9][0-9]{3,4}$ ]] && (( port >= 1024 && port <= 65535 )) || fail "Invalid loopback port."
done
[[ $BLUE_PORT != "$GREEN_PORT" && $BLUE_PORT != "$PREVIEW_PORT" && $GREEN_PORT != "$PREVIEW_PORT" ]] || fail "Loopback ports must differ."
DRAIN_SECONDS=${DRAIN_SECONDS:-30}
[[ $DRAIN_SECONDS =~ ^[1-9][0-9]{0,2}$ ]] || fail "Invalid DRAIN_SECONDS."
origin=https://$PUBLIC_HOST
if [[ $PUBLIC_PORT != 443 ]]; then origin+=:$PUBLIC_PORT; fi
[[ -d $root/slots && -d $state/backups && -f $state/workspace.json.lock && -f $nginx_include ]] ||
    fail "Install Plotter directories, state lock and Nginx include first."
exec 9>"$root/.release.lock"
flock -n 9 || fail "Another Plotter release command is running."

case $1 in
    stage) stage "$2" "$(realpath -- "$3")" ;;
    activate)
        valid_version "$2" || fail "Invalid version label."
        switch_release "$root/releases/$2"
        ;;
    rollback)
        [[ -L $root/previous ]] || fail "No previous release is recorded."
        previous=$(readlink -f -- "$root/previous")
        [[ $previous == "$root/releases/"* ]] || fail "Previous release is outside the release directory."
        switch_release "$previous"
        ;;
esac
