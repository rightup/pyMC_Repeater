FROM python:3.12-slim-bookworm

ARG PACKAGE_VERSION=1.0.5
ARG USER=repeater
ARG GROUP=repeater
ARG PUID=15888
ARG PGID=15888
ARG PYMC_CORE_REF=dev
ARG PYMC_CORE_SHA
ARG TARGETARCH
ARG YQ_VERSION=v4.40.5

ENV INSTALL_DIR=/opt/pymc_repeater \
    CONFIG_DIR=/etc/pymc_repeater \
    DATA_DIR=/var/lib/pymc_repeater \
    HOME_DIR=/home/${USER} \
    PATH=/home/${USER}/.local/bin:${PATH} \
    PYTHONUNBUFFERED=1 \
    SETUPTOOLS_SCM_PRETEND_VERSION_FOR_PYMC_REPEATER=${PACKAGE_VERSION} \
    PUID=${PUID} \
    PGID=${PGID}

# Install runtime dependencies only
RUN DEBIAN_FRONTEND=noninteractive apt-get update && apt-get install -y \
    libffi-dev \
    librrd-dev \
    pkg-config \
    jq \
    wget \
    libusb-1.0-0 \
    swig \
    git \
    build-essential \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

RUN arch="${TARGETARCH:-}" \
    && if [ -z "${arch}" ]; then arch="$(uname -m)"; fi \
    && case "${arch}" in \
        amd64|x86_64) YQ_BINARY="yq_linux_amd64" ;; \
        arm64|aarch64) YQ_BINARY="yq_linux_arm64" ;; \
        arm|armv7|armv7l) YQ_BINARY="yq_linux_arm" ;; \
        *) echo "Unsupported architecture for yq: ${arch}" >&2; exit 1 ;; \
    esac \
    && wget -qO /usr/local/bin/yq "https://github.com/mikefarah/yq/releases/download/${YQ_VERSION}/${YQ_BINARY}" \
    && chmod +x /usr/local/bin/yq

# Create the group and user in order to run without root privileges
RUN groupadd --gid "$PGID" "$GROUP" \
    && useradd --uid "$PUID" --gid "$PGID" --home-dir "$HOME_DIR" --create-home --shell /usr/bin/bash "$USER"

# Create runtime directories
RUN mkdir -p ${INSTALL_DIR} ${CONFIG_DIR} ${DATA_DIR} \
    && chown -R "$USER":"$GROUP" ${INSTALL_DIR} ${CONFIG_DIR} ${DATA_DIR} ${HOME_DIR}

WORKDIR ${INSTALL_DIR}

# Copy source
COPY repeater ./repeater
COPY pyproject.toml .
COPY config.yaml.example .
COPY radio-presets.json .
COPY radio-settings.json .
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

# Switch to the unprivileged runtime user
USER ${USER}

# Install the matching pyMC_core ref by resolved commit SHA, then install repeater
# without re-resolving dependencies back to a cached branch tip.
RUN test -n "${PYMC_CORE_SHA}" \
    && echo "Installing pyMC_core ${PYMC_CORE_REF} @ ${PYMC_CORE_SHA}" \
    && pip install --no-cache-dir "pymc_core[hardware] @ git+https://github.com/pyMC-dev/pyMC_core.git@${PYMC_CORE_SHA}" \
    && python -c "import pathlib, tomllib; data = tomllib.loads(pathlib.Path('pyproject.toml').read_text()); deps = [d for d in data['project']['dependencies'] if not d.startswith('pymc_core')]; deps += data['project']['optional-dependencies']['rrd']; pathlib.Path('/tmp/pymc_repeater_requirements.txt').write_text('\n'.join(deps) + '\n')" \
    && pip install --no-cache-dir -r /tmp/pymc_repeater_requirements.txt \
    && pip install --no-cache-dir --no-deps .

USER root

RUN chmod +x /usr/local/bin/docker-entrypoint.sh

USER ${USER}

EXPOSE 8000

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
