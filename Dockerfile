FROM ros:jazzy-ros-base-noble@sha256:31daab66eef9139933379fb67159449944f4e2dcf2e22c2d12cc715f29873e0f

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN apt-get update && apt-get install -y --no-install-recommends \
      python3-venv python3-opencv \
      ros-jazzy-cv-bridge \
      ros-jazzy-rosbag2-py \
      ros-jazzy-rosbag2-storage-mcap \
      ros-jazzy-sensor-msgs \
      ros-jazzy-std-msgs \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv --system-site-packages /opt/hri-venv
ENV PATH="/opt/hri-venv/bin:${PATH}"
WORKDIR /app
COPY pyproject.toml README.md PLAN.md ./
COPY hri_curator ./hri_curator
RUN pip install --no-cache-dir .
RUN dpkg-query -W -f='${Package}=${Version}\n' \
      python3-venv python3-opencv ros-jazzy-cv-bridge ros-jazzy-rosbag2-py \
      ros-jazzy-rosbag2-storage-mcap ros-jazzy-sensor-msgs ros-jazzy-std-msgs \
      > /app/ros-packages.lock

ENV PYTHONDONTWRITEBYTECODE=1

EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=2)" || exit 1
ENTRYPOINT ["/ros_entrypoint.sh"]
CMD ["hri-curator", "review", "--root", "/data/subject", "--host", "0.0.0.0", "--port", "8000"]
