FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY elearn_mcp.py remote_auth.py ./

# Run as a normal user, not root
RUN useradd --create-home app
USER app

EXPOSE 8080
CMD ["python", "elearn_mcp.py", "serve"]
