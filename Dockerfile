FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
# google-api-python-client arrives as a firebase-admin dependency and ships
# ~100MB of pre-generated discovery documents for every Google API. Nothing
# here uses it (firebase-admin verifies tokens via google-auth), so drop it.
RUN pip install --no-cache-dir -r requirements.txt \
    && rm -rf /usr/local/lib/python3.12/site-packages/googleapiclient/discovery_cache/documents

COPY . .

RUN mkdir -p uploads && chmod +x docker-entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["./docker-entrypoint.sh"]
