FROM python:3.12-slim
WORKDIR /opt/invest
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir -e ".[app]"
COPY db ./db
COPY fixtures ./fixtures
USER nobody
