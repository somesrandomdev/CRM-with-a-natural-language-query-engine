FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv

COPY pyproject.toml ./
# Install dependencies first so source edits don't bust the layer cache.
RUN mkdir -p app nlquery pipeline scoring llm seed evals \
    && for d in app nlquery pipeline scoring llm seed evals; do touch $d/__init__.py; done \
    && pip install --no-cache-dir -e . \
    && rm -rf app/__init__.py

COPY . .
RUN useradd --create-home clearpipe && chown -R clearpipe /srv
USER clearpipe

EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
