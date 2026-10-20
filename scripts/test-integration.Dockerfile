# Local and CI validation against the deployed HA version.
FROM python:3.14-slim@sha256:a7fb1e634c4a578f9e0bd6327f11a3cde11b7a9395f48e24360c0988bcc5c2bc
RUN python -m pip install --no-cache-dir \
    homeassistant==2026.9.4 paho-mqtt==2.1.0 scapy==2.7.0
RUN python -m pip install --no-cache-dir thinqconnect==1.0.12 xmltodict==0.13.0
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 65534:65534
WORKDIR /tmp
