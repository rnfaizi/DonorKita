# Image python:3.12-slim tersedia untuk amd64 dan arm64, jadi bisa di-build langsung di STB.
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
RUN useradd --system --uid 10001 appuser && mkdir /data && chown appuser /data
USER appuser
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5).status==200 else 1)"
# --root-path /api karena gateway Nginx meneruskan /api/ ke service ini.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", \
     "--root-path", "/api", "--proxy-headers", "--forwarded-allow-ips", "*"]
