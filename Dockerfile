FROM python:3.12-alpine
WORKDIR /app
COPY beacon.py .
ENV PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8585
HEALTHCHECK --interval=30s --timeout=3s CMD wget -qO- http://127.0.0.1:8585/health || exit 1
ENTRYPOINT ["python3", "beacon.py"]
