# The sandbox image. Build once, run one container per session epoch.
#
#   docker build -t cloud-agent-sandbox:dev sandbox/
#
# The image holds cursord and the tools the agent is likely to reach for.
# Anything else the agent installs at runtime is rebuildable by definition:
# recovery re-does that work rather than preserving it.

FROM python:3.12-slim

# git is load-bearing — it is the entire durability story for file state.
# The rest is what a coding agent reaches for in the first minute.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        ca-certificates \
        curl \
        ripgrep \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/cursord
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY cursord/ ./cursord/

# The clone lands here. Nothing outside it survives the container.
RUN mkdir -p /workspace
ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/cursord \
    WORKSPACE=/workspace/repo

# No LLM credentials reach this image. The model call happens on the control
# plane; the sandbox only ever sees a tool name and its arguments.

ENTRYPOINT ["python", "-m", "cursord"]
