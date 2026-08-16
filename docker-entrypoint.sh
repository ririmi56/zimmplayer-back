#!/bin/sh
# Applique les migrations avant de demarrer l'API : sur une cible airgap on ne
# veut pas d'etape manuelle supplementaire lors d'une mise a jour.
set -e

echo "Application des migrations..."
alembic upgrade head

exec "$@"
