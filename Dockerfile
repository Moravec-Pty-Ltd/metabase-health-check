FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --uid 10001 --create-home monitor \
    && mkdir /data && chown monitor:monitor /data
COPY monitor.py .
USER monitor
VOLUME ["/data"]
HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=3 CMD ["python", "monitor.py", "--healthcheck"]
ENTRYPOINT ["python", "monitor.py"]
