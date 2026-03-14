FROM python:3.12-slim-bookworm

ENV INSTALL_DIR=/opt/pymc_repeater \
    CONFIG_DIR=/etc/pymc_repeater \
    DATA_DIR=/var/lib/pymc_repeater \
    PYTHONUNBUFFERED=1 \
    SETUPTOOLS_SCM_PRETEND_VERSION_FOR_PYMC_REPEATER=1.0.5

# Install runtime dependencies only
RUN apt-get update && apt-get install -y \
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

# Create runtime directories
RUN mkdir -p ${INSTALL_DIR} ${CONFIG_DIR} ${DATA_DIR}

WORKDIR ${INSTALL_DIR}

# Copy source
COPY repeater ./repeater
COPY pyproject.toml .
COPY radio-presets.json .
COPY radio-settings.json .

# Install package
RUN pip install --no-cache-dir .

EXPOSE 8000

ENTRYPOINT ["python3", "-m", "repeater.main", "--config", "/etc/pymc_repeater/config.yaml"]
