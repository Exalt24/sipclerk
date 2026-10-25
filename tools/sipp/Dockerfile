# SIPp (GPL-2.0, from the Debian package) as a stand-alone SIP caller that is not our own code.
# Build:  docker build -t sipclerk-sipp tools/sipp
FROM debian:bookworm-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends sip-tester ca-certificates \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /work
ENTRYPOINT ["sipp"]
