FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app
WORKDIR /app
COPY pyproject.toml requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
COPY apps/__init__.py apps/__init__.py
COPY apps/policyguard apps/policyguard
COPY apps/sas_mock apps/sas_mock
COPY shared shared
RUN useradd --create-home app && chown -R app:app /app
USER app
CMD ["uvicorn", "apps.policyguard.main:app", "--host", "0.0.0.0", "--port", "8000"]
