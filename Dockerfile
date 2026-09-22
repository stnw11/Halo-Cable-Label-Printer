FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# The label font travels with the project so output does not depend on
# which fonts happen to be installed. See assets/fonts/README.md.
COPY assets/ ./assets/
COPY protocol/ ./protocol/
COPY src/ ./src/
COPY tools/ ./tools/

RUN useradd --create-home --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /app

USER appuser

CMD ["python", "-m", "src.main"]
