"""Ekspor kontrak OpenAPI dari kode.

Pakai (dari root repo):
    python tools/export_openapi.py core > docs/openapi-core.yaml
    python tools/export_openapi.py auth > docs/openapi-auth.yaml
"""
import os
import sys
import tempfile

import yaml

which = sys.argv[1] if len(sys.argv) > 1 else "core"
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(root, f"{which}-service"))
os.environ.setdefault("CORE_DB_PATH", os.path.join(tempfile.mkdtemp(), "export.db"))
os.environ.setdefault("AUTH_DB_PATH", os.path.join(tempfile.mkdtemp(), "export-auth.db"))

from app.main import app  # noqa: E402

BASE = "https://donorkita.stei.site"
spec = app.openapi()
spec["servers"] = [{"url": BASE + ("/api" if which == "core" else "/api/auth")}]
schemes = {"BearerJWT": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}}
if which == "core":
    schemes["ApiKey"] = {"type": "apiKey", "in": "header", "name": "X-API-Key"}
spec.setdefault("components", {}).setdefault("securitySchemes", {}).update(schemes)
yaml.safe_dump(spec, sys.stdout, allow_unicode=True, sort_keys=False)
