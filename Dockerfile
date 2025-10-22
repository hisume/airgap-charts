# syntax=docker/dockerfile:1.6
FROM python:3.11-slim AS runtime

ARG TARGETOS=linux
ARG TARGETARCH=amd64
ENV DEBIAN_FRONTEND=noninteractive

# Tool versions
ENV HELM_VERSION=3.14.4 \
    YQ_VERSION=4.44.3 \
    CRANE_VERSION=0.19.1

# Base utilities
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl unzip tar bash git \
  && rm -rf /var/lib/apt/lists/*

# awscli v2 (arch-aware)
RUN set -eux; \
  case "$TARGETARCH" in \
    amd64) AWS_ARCH=x86_64 ;; \
    arm64) AWS_ARCH=aarch64 ;; \
    *) echo "Unsupported TARGETARCH: $TARGETARCH" && exit 1 ;; \
  esac; \
  curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-${AWS_ARCH}.zip" -o /tmp/awscliv2.zip; \
  unzip -q /tmp/awscliv2.zip -d /tmp; \
  /tmp/aws/install -i /usr/local/aws-cli -b /usr/local/bin; \
  rm -rf /tmp/aws /tmp/awscliv2.zip

# Helm (OCI)
RUN curl -fsSL "https://get.helm.sh/helm-v${HELM_VERSION}-linux-${TARGETARCH}.tar.gz" -o /tmp/helm.tgz \
  && tar -zx -C /tmp -f /tmp/helm.tgz \
  && mv "/tmp/linux-${TARGETARCH}/helm" /usr/local/bin/helm \
  && rm -rf /tmp/helm.tgz "/tmp/linux-${TARGETARCH}"

# yq (Mike Farah)
RUN curl -fsSL "https://github.com/mikefarah/yq/releases/download/v${YQ_VERSION}/yq_linux_${TARGETARCH}" -o /usr/local/bin/yq \
  && chmod +x /usr/local/bin/yq

# crane (go-containerregistry)
# Download the go-containerregistry tarball and extract the 'crane' binary
RUN set -eux; \
  case "$TARGETARCH" in \
    amd64) GC_ARCH=x86_64 ;; \
    arm64) GC_ARCH=aarch64 ;; \
    *) echo "Unsupported TARGETARCH: $TARGETARCH" && exit 1 ;; \
  esac; \
  curl -fsSL "https://github.com/google/go-containerregistry/releases/download/v${CRANE_VERSION}/go-containerregistry_Linux_${GC_ARCH}.tar.gz" -o /tmp/gcr.tgz; \
  tar -xzf /tmp/gcr.tgz -C /usr/local/bin crane; \
  chmod +x /usr/local/bin/crane; \
  rm -f /tmp/gcr.tgz

# Optional: enable OCI for older helm flows; Python unbuffered logs
ENV HELM_EXPERIMENTAL_OCI=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install Python dependencies first for better layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy project
COPY . .

# Default entrypoint – override with flags at runtime
ENTRYPOINT ["/bin/bash", "-lc"]
CMD ["bash -i"]
# Example default CMD (commented; provide your args at 'docker run' time)
# CMD ["--values", "./values.yaml", "--scan-only"]
