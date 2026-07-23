#!/usr/bin/env bash
# Install the two least-privilege Alpha POS SSH forwarding accounts.
#
# Run as root from this deploy directory. The private client keys are never
# copied to the relay. Existing SSH files are backed up before replacement,
# sshd validates the complete effective configuration before reload, and a
# failed install restores the previous SSH configuration.
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
    echo "install_support_relay.sh must run as root" >&2
    exit 1
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SSHD_BIN="$(command -v sshd)"
NOLOGIN_SHELL="$(command -v nologin || true)"
if [[ -z "${NOLOGIN_SHELL}" ]]; then
    NOLOGIN_SHELL=/usr/sbin/nologin
fi
if [[ ! -x "${NOLOGIN_SHELL}" ]]; then
    echo "nologin shell is unavailable" >&2
    exit 1
fi

required=(
    99-alphapos-support.conf
    99-alphapos-inspector.conf
    alphapos-support.authorized_keys
    alphapos-inspector.authorized_keys
)
for name in "${required[@]}"; do
    if [[ ! -f "${SCRIPT_DIR}/${name}" ]]; then
        echo "missing deployment input: ${SCRIPT_DIR}/${name}" >&2
        exit 1
    fi
done

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_DIR="/root/alphapos-ssh-backups/${STAMP}"
install -d -m 700 "${BACKUP_DIR}"
CHANGED=0
SUCCEEDED=0

backup_file() {
    local source=$1 label=$2
    if [[ -e "${source}" ]]; then
        cp -a -- "${source}" "${BACKUP_DIR}/${label}"
    else
        : >"${BACKUP_DIR}/${label}.missing"
    fi
}

restore_file() {
    local target=$1 label=$2
    if [[ -e "${BACKUP_DIR}/${label}" ]]; then
        cp -a -- "${BACKUP_DIR}/${label}" "${target}"
    elif [[ -e "${BACKUP_DIR}/${label}.missing" ]]; then
        rm -f -- "${target}"
    fi
}

reload_sshd() {
    if systemctl reload ssh 2>/dev/null; then
        return
    fi
    systemctl reload sshd
}

rollback_on_error() {
    local status=$?
    if [[ ${status} -ne 0 && ${CHANGED} -eq 1 && ${SUCCEEDED} -eq 0 ]]; then
        echo "Install failed; restoring SSH files from ${BACKUP_DIR}" >&2
        restore_file /etc/ssh/sshd_config.d/99-alphapos-support.conf support.conf
        restore_file /etc/ssh/sshd_config.d/99-alphapos-inspector.conf inspector.conf
        restore_file /home/alphapos-support/.ssh/authorized_keys support.authorized_keys
        restore_file /home/alphapos-inspector/.ssh/authorized_keys inspector.authorized_keys
        "${SSHD_BIN}" -t || true
        reload_sshd || true
    fi
    exit "${status}"
}
trap rollback_on_error EXIT

backup_file /etc/ssh/sshd_config.d/99-alphapos-support.conf support.conf
backup_file /etc/ssh/sshd_config.d/99-alphapos-inspector.conf inspector.conf
backup_file /home/alphapos-support/.ssh/authorized_keys support.authorized_keys
backup_file /home/alphapos-inspector/.ssh/authorized_keys inspector.authorized_keys

install -d -o root -g root -m 755 /etc/ssh/sshd_config.d
install -o root -g root -m 600 \
    "${SCRIPT_DIR}/99-alphapos-support.conf" \
    /etc/ssh/sshd_config.d/99-alphapos-support.conf
install -o root -g root -m 600 \
    "${SCRIPT_DIR}/99-alphapos-inspector.conf" \
    /etc/ssh/sshd_config.d/99-alphapos-inspector.conf
CHANGED=1

# Validate the actual sshd include graph before touching accounts or reloading.
"${SSHD_BIN}" -t

install_account() {
    local account=$1 key_source=$2
    if ! id "${account}" >/dev/null 2>&1; then
        useradd --create-home --home-dir "/home/${account}" \
            --shell "${NOLOGIN_SHELL}" --user-group "${account}"
    fi
    usermod --shell "${NOLOGIN_SHELL}" "${account}"
    passwd --lock "${account}" >/dev/null
    install -d -o "${account}" -g "${account}" -m 700 "/home/${account}"
    install -d -o "${account}" -g "${account}" -m 700 "/home/${account}/.ssh"
    install -o "${account}" -g "${account}" -m 600 \
        "${key_source}" "/home/${account}/.ssh/authorized_keys"
}

install_account alphapos-support "${SCRIPT_DIR}/alphapos-support.authorized_keys"
install_account alphapos-inspector "${SCRIPT_DIR}/alphapos-inspector.authorized_keys"

"${SSHD_BIN}" -t

require_policy() {
    local policy=$1 expected=$2
    if ! grep -Fxq -- "${expected}" <<<"${policy}"; then
        echo "effective sshd policy is missing: ${expected}" >&2
        return 1
    fi
}

support_policy="$(
    "${SSHD_BIN}" -T \
        -C user=alphapos-support,host=relay,addr=127.0.0.1
)"
inspector_policy="$(
    "${SSHD_BIN}" -T \
        -C user=alphapos-inspector,host=relay,addr=127.0.0.1
)"

common_policy=(
    "authenticationmethods publickey"
    "pubkeyauthentication yes"
    "passwordauthentication no"
    "kbdinteractiveauthentication no"
    "allowstreamlocalforwarding no"
    "gatewayports no"
    "permittty no"
    "permittunnel no"
    "permituserrc no"
    "x11forwarding no"
    "allowagentforwarding no"
    "maxsessions 0"
)
for expected in "${common_policy[@]}"; do
    require_policy "${support_policy}" "${expected}"
    require_policy "${inspector_policy}" "${expected}"
done
require_policy "${support_policy}" "allowtcpforwarding remote"
require_policy "${support_policy}" "permitopen none"
require_policy \
    "${support_policy}" \
    "permitlisten 127.0.0.1:15433 127.0.0.1:18000"
require_policy "${inspector_policy}" "allowtcpforwarding local"
require_policy \
    "${inspector_policy}" \
    "permitopen 127.0.0.1:15433 127.0.0.1:18000"
require_policy "${inspector_policy}" "permitlisten none"

for account in alphapos-support alphapos-inspector; do
    passwd -S "${account}" | grep -Eq "^[^ ]+ L "
    [[ "$(getent passwd "${account}" | cut -d: -f7)" == "${NOLOGIN_SHELL}" ]]
    [[ "$(stat -c %a "/home/${account}")" == 700 ]]
    [[ "$(stat -c %a "/home/${account}/.ssh")" == 700 ]]
    [[ "$(stat -c %a "/home/${account}/.ssh/authorized_keys")" == 600 ]]
done

# Reload only after the full effective-policy and account-state contract passes.
reload_sshd
SUCCEEDED=1

echo "Alpha POS relay SSH policy installed and asserted."
echo "Rollback backup: ${BACKUP_DIR}"
echo "Support policy:"
grep -E '^(authenticationmethods|allowtcpforwarding|allowstreamlocalforwarding|gatewayports|permitlisten|permitopen|permittty|permittunnel|permituserrc|maxsessions) ' \
    <<<"${support_policy}"
echo "Inspector policy:"
grep -E '^(authenticationmethods|allowtcpforwarding|allowstreamlocalforwarding|gatewayports|permitlisten|permitopen|permittty|permittunnel|permituserrc|maxsessions) ' \
    <<<"${inspector_policy}"
