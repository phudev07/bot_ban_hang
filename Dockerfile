FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml ./

RUN python - <<'PY'
import tomllib

with open("pyproject.toml", "rb") as source:
    dependencies = tomllib.load(source)["project"]["dependencies"]
with open("/tmp/requirements.txt", "w", encoding="utf-8") as target:
    target.write("\n".join(dependencies) + "\n")
PY

RUN python -m pip install --no-cache-dir --upgrade "pip>=26.1.2,<27" \
    && pip install --no-cache-dir -r /tmp/requirements.txt

COPY README.md ./
COPY app ./app

CMD ["python", "-m", "app.main"]
