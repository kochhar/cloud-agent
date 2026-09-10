# The sandbox image. Build once, run one container per session epoch.
#
#   docker build -t cloud-agent-sandbox:dev agent/
#
# The image holds cursord and the tools the agent is likely to reach for.
# Anything else the agent installs at runtime is rebuildable by definition:
# recovery re-does that work rather than preserving it.

FROM python:3.12-slim

# git is load-bearing — it is the entire durability story for file state.
# openssh-client is what lets it reach a real remote. The rest is what a
# coding agent reaches for in the first minute.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        openssh-client \
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
RUN mkdir -p /workspace /root/.ssh && chmod 700 /root/.ssh
ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/cursord \
    WORKSPACE=/workspace/repo

# Git credentials are mounted at run time, never baked in — a key in a layer
# is a key in the image, and this image runs model-authored commands.
#
# BatchMode is the important flag. Without it a missing key or an unknown host
# turns into an interactive prompt on a terminal nobody is attached to, and
# the clone hangs until the spawn timeout instead of failing with a reason.
ENV GIT_SSH_COMMAND="ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/root/.ssh/known_hosts"

# No LLM credentials reach this image. The model call happens on the control
# plane; the sandbox only ever sees a tool name and its arguments.

ENTRYPOINT ["python", "-m", "cursord"]
