FROM python:3.12-slim-bookworm

ARG USER=repeater
ARG GROUP=repeater
ARG PUID=15888
ARG PGID=15888

ENV INSTALL_DIR=/opt/pymc_repeater \
    CONFIG_DIR=/etc/pymc_repeater \
    DATA_DIR=/var/lib/pymc_repeater \
    HOME_DIR=/home/"$USER" \
    PYTHONUNBUFFERED=1 \
    SETUPTOOLS_SCM_PRETEND_VERSION_FOR_PYMC_REPEATER=1.0.5 \
    PUID="$PUID" \
    PGID="$PGID" 

# Install runtime dependencies only
RUN DEBIAN_FRONTEND=noninteractive apt-get update && apt-get install -y \
    libffi-dev \
    python3-rrdtool \
    jq \
    wget \
    libusb-1.0-0 \
    swig \
    git \
    build-essential \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

# Create the group and user in order to run without root privileges
RUN groupadd --gid "$PGID" "$GROUP" \
    && useradd --uid "$PUID" --gid "$PGID" --shell /usr/bin/bash "$USER"

# Create runtime directories
RUN mkdir -p ${INSTALL_DIR} ${CONFIG_DIR} ${DATA_DIR} ${HOME_DIR}

WORKDIR ${INSTALL_DIR}

# Copy source
COPY repeater ./repeater
COPY pyproject.toml .
COPY radio-presets.json .
COPY radio-settings.json .
RUN chown -R "$USER":"$GROUP" ${INSTALL_DIR} ${CONFIG_DIR} ${DATA_DIR} ${HOME_DIR}

# Switch to $USER
USER ${USER}

# Install package
RUN pip install --no-cache-dir .

EXPOSE 8000

ENTRYPOINT ["python3", "-m", "repeater.main", "--config", "/etc/pymc_repeater/config.yaml"]
