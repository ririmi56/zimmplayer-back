FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# ffmpeg n'est requis que par la sortie Snapcast, qui decode les fichiers du
# bucket en PCM cote serveur. Il pese lourd (~250 Mo) mais le mode s'active a
# chaud depuis l'interface : il doit donc etre present dans l'image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Les dependances d'abord : cette couche est reutilisee tant que pyproject
# ne bouge pas, ce qui accelere nettement les reconstructions.
COPY pyproject.toml ./
RUN pip install --no-cache-dir .

COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app

COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 8000
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
