FROM python:3.12-slim

WORKDIR /app
COPY . .
RUN pip install --no-cache-dir .[all]

ENV BRAIN_DB=/data/brain.db
VOLUME /data

CMD ["brainctl-mcp"]
