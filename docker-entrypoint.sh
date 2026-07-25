#!/bin/sh
set -eu

INSTALL_DIR="${INSTALL_DIR:-/opt/openhop_repeater}"
CONFIG_DIR="${CONFIG_DIR:-/etc/openhop_repeater}"
CONFIG_PATH="${OPENHOP_REPEATER_CONFIG:-${PYMC_REPEATER_CONFIG:-${CONFIG_DIR}/config.yaml}}"
EXAMPLE_PATH="${CONFIG_DIR}/config.yaml.example"
BUNDLED_EXAMPLE_PATH="${INSTALL_DIR}/config.yaml.example"
RUNTIME_USER="${USER:-repeater}"
RUNTIME_UID="${PUID:-unknown}"
RUNTIME_GID="${PGID:-unknown}"
YQ_CMD="${YQ_CMD:-/usr/local/bin/yq}"

mkdir -p "${CONFIG_DIR}"

print_permission_help() {
    echo "If you are bind-mounting ./config or ./data, ensure the host paths are writable by ${RUNTIME_USER} (${RUNTIME_UID}:${RUNTIME_GID})." >&2
    echo "For the default image user, run: sudo chown -R ${RUNTIME_UID}:${RUNTIME_GID} ./config ./data" >&2
}

fail_bad_config_mount() {
    echo "Invalid Docker config mount: ${CONFIG_PATH} is a directory, but it must be the config file." >&2
    echo "This usually happens when ./config.yaml is bind-mounted before that host file exists." >&2
    echo "Use the supported folder mount instead:" >&2
    echo "  - ./config:/etc/openhop_repeater" >&2
    echo "Then place the config at ./config/config.yaml." >&2
    print_permission_help
    exit 1
}

copy_or_die() {
    src="$1"
    dest="$2"
    if ! cp "${src}" "${dest}"; then
        echo "Failed to initialize ${dest} from ${src}." >&2
        print_permission_help
        exit 1
    fi
}

merge_config_from_example() {
    config_path="$1"

    if [ ! -f "${config_path}" ] || [ ! -f "${EXAMPLE_PATH}" ]; then
        return 0
    fi

    if [ ! -x "${YQ_CMD}" ] || ! "${YQ_CMD}" --version 2>&1 | grep -q "mikefarah/yq"; then
        echo "Skipping config merge: mikefarah yq is not available at ${YQ_CMD}." >&2
        return 0
    fi

    tmpdir="$(mktemp -d)"
    stripped_user="${tmpdir}/config.stripped.yaml"
    merged_config="${tmpdir}/config.merged.yaml"

    cleanup_merge() {
        rm -rf "${tmpdir}"
    }
    trap cleanup_merge EXIT HUP INT TERM

    # Keep only the example's comments to avoid comment duplication across upgrades.
    "${YQ_CMD}" eval '... comments=""' "${config_path}" > "${stripped_user}" 2>/dev/null || cp "${config_path}" "${stripped_user}"

    if ! "${YQ_CMD}" eval-all '. as $item ireduce ({}; . * $item)' "${EXAMPLE_PATH}" "${stripped_user}" > "${merged_config}" 2>/dev/null; then
        echo "Failed to merge ${config_path} with ${EXAMPLE_PATH}; keeping the existing config." >&2
        cleanup_merge
        trap - EXIT HUP INT TERM
        return 0
    fi

    if ! "${YQ_CMD}" eval '.' "${merged_config}" >/dev/null 2>&1; then
        echo "Merged config for ${config_path} is invalid; keeping the existing config." >&2
        cleanup_merge
        trap - EXIT HUP INT TERM
        return 0
    fi

    if ! cmp -s "${config_path}" "${merged_config}"; then
        if ! cp "${merged_config}" "${config_path}"; then
            echo "Failed to update ${config_path} from merged config; the bind-mounted config is not writable." >&2
            echo "Keeping the durable config path; security credentials must never be generated in a temporary config." >&2
            print_permission_help
        fi
    fi

    cleanup_merge
    trap - EXIT HUP INT TERM
}

if [ -d "${CONFIG_PATH}" ] && [ "$(basename "${CONFIG_PATH}")" = "config.yaml" ]; then
    fail_bad_config_mount
fi

if [ ! -f "${EXAMPLE_PATH}" ] && [ -f "${BUNDLED_EXAMPLE_PATH}" ]; then
    if ! cp "${BUNDLED_EXAMPLE_PATH}" "${EXAMPLE_PATH}"; then
        echo "Could not copy bundled example config to ${EXAMPLE_PATH}; using bundled example for config merge only." >&2
        print_permission_help
        EXAMPLE_PATH="${BUNDLED_EXAMPLE_PATH}"
    fi
fi

if [ -d "${CONFIG_PATH}" ]; then
    if [ ! -s "${CONFIG_PATH}/config.yaml" ] && [ -f "${EXAMPLE_PATH}" ]; then
        copy_or_die "${EXAMPLE_PATH}" "${CONFIG_PATH}/config.yaml"
    fi
    CONFIG_PATH="${CONFIG_PATH}/config.yaml"
elif [ ! -s "${CONFIG_PATH}" ] && [ -f "${EXAMPLE_PATH}" ]; then
    copy_or_die "${EXAMPLE_PATH}" "${CONFIG_PATH}"
fi

merge_config_from_example "${CONFIG_PATH}"

exec python3 -m repeater.main --config "${CONFIG_PATH}"
