FROM python:3.11-slim
WORKDIR /app
COPY engine.py llm.py server.py ./
COPY web ./web
COPY apps/insurance_claims/fixtures ./apps/insurance_claims/fixtures
ENV PORT=8080 PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["python", "server.py"]
