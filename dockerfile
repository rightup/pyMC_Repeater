FROM python:3.12-slim-bookworm

ARG PACKAGE_VERSION=1.0.5
ARG USER=repeater
ARG GROUP=repeater
ARG PUID=15888
ARG PGID=15888
ARG DIALOUT_GID=20
ARG GPIO_GID=986
ARG SPI_GID=989
ARG TARGETARCH
ARG YQ_VERSION=v4.40.5
ARG PYMC_CONSOLE_REPO=Treehouse-00/pymc_console-dist
ARG PYMC_CONSOLE_VERSION=latest
ARG PYMC_CONSOLE_CACHE_BUST=default

ENV INSTALL_DIR=/opt/openhop_repeater \
    CONFIG_DIR=/etc/openhop_repeater \
    DATA_DIR=/var/lib/openhop_repeater \
    PYMC_CONSOLE_WEB_DIR=/opt/pymc_console/web/html \
    HOME_DIR=/home/${USER} \
    PATH=/home/${USER}/.local/bin:${PATH} \
    PYTHONUNBUFFERED=1 \
    SETUPTOOLS_SCM_PRETEND_VERSION_FOR_OPENHOP_REPEATER=${PACKAGE_VERSION} \
    PUID=${PUID} \
    PGID=${PGID} \
    DIALOUT_GID=${DIALOUT_GID} \
    GPIO_GID=${GPIO_GID} \
    SPI_GID=${SPI_GID}

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

# Bundle the optional PyMC Console frontend so the web UI can select it without
# requiring a host bind mount. Use PYMC_CONSOLE_VERSION=latest to pull the
# newest release at image build time, or pin a tag such as v0.9.329.
RUN set -eux; \
    echo "Bundling PyMC Console cache key: ${PYMC_CONSOLE_CACHE_BUST}"; \
    mkdir -p "${PYMC_CONSOLE_WEB_DIR}"; \
    if [ "${PYMC_CONSOLE_VERSION}" = "latest" ]; then \
        console_url="https://github.com/${PYMC_CONSOLE_REPO}/releases/latest/download/pymc-ui-latest.tar.gz"; \
    else \
        console_url="https://github.com/${PYMC_CONSOLE_REPO}/releases/download/${PYMC_CONSOLE_VERSION}/pymc-ui-${PYMC_CONSOLE_VERSION}.tar.gz"; \
    fi; \
    wget -qO /tmp/pymc-console.tar.gz "${console_url}"; \
    tar -xzf /tmp/pymc-console.tar.gz -C "${PYMC_CONSOLE_WEB_DIR}"; \
    rm /tmp/pymc-console.tar.gz; \
    test -f "${PYMC_CONSOLE_WEB_DIR}/index.html"

# Create the group and user in order to run without root privileges
RUN groupadd --gid "$PGID" "$GROUP" \
    && (getent group dialout >/dev/null || groupadd --gid "$DIALOUT_GID" dialout) \
    && groupadd --gid "$GPIO_GID" gpio \
    && groupadd --gid "$SPI_GID" spi \
    && useradd --uid "$PUID" --gid "$PGID" --home-dir "$HOME_DIR" --create-home --shell /usr/bin/bash "$USER" \
    && usermod -a -G dialout,gpio,spi "$USER"

# Create runtime directories
RUN mkdir -p ${INSTALL_DIR} ${CONFIG_DIR} ${DATA_DIR} \
    && chown -R "$USER":"$GROUP" ${INSTALL_DIR} ${CONFIG_DIR} ${DATA_DIR} ${HOME_DIR} /opt/pymc_console

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

# Install package
RUN pip install --no-cache-dir ".[rrd]"

USER root

RUN chmod +x /usr/local/bin/docker-entrypoint.sh

USER ${USER}

EXPOSE 8000

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
