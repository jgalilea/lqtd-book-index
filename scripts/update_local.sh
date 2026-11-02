#!/usr/bin/env bash
# Busca los libros mencionados en las conversaciones de los episodios nuevos
# y lo sube al repo. Se ejecuta en local porque YouTube bloquea la descarga
# de subtítulos desde los servidores de GitHub Actions.
#
#   ./scripts/update_local.sh            # episodios pendientes (hasta 40)
#   ./scripts/update_local.sh --limit 5  # opciones extra para transcripts.py
#   NO_PUSH=1 ./scripts/update_local.sh  # sin commit ni push
#
# Necesita Python 3, Node.js (yt-dlp lo usa para YouTube) y una clave
# gratuita de Gemini en .env (GEMINI_API_KEY=...). Ver README.
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ ! -f .env ]] || ! grep -q '^GEMINI_API_KEY=.' .env; then
  echo "Falta la clave de Gemini: crea .env con GEMINI_API_KEY=tu_clave (ver README)." >&2
  exit 1
fi
command -v node >/dev/null || { echo "Falta Node.js (lo necesita yt-dlp para YouTube)." >&2; exit 1; }

# Entorno virtual propio con las dependencias (solo la primera vez).
if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi
.venv/bin/pip install -q -r requirements.txt

# Trae los episodios nuevos que haya añadido la Action.
git pull --rebase --autostash -q

.venv/bin/python scripts/transcripts.py "$@"
.venv/bin/python scripts/titles.py              # títulos en otros idiomas y duplicados
.venv/bin/python scripts/books.py --no-detect   # fichas y portadas de los libros nuevos

if git diff --quiet -- data/; then
  echo "Sin libros nuevos."
  exit 0
fi
git --no-pager diff --stat -- data/
# Si una fusión o un título ha dejado los datos incoherentes, no se sube nada.
.venv/bin/python -m unittest discover -q tests || { echo "Fallan los tests: revisa data/books.json antes de subirlo." >&2; exit 1; }
if [[ -n "${NO_PUSH:-}" ]]; then
  echo "NO_PUSH: cambios sin subir."
  exit 0
fi
git add data/
git commit -q -m "Libros mencionados en episodios nuevos ($(date +%F))"
git push -q
echo "Subido. La Action publicará la web en un minuto."
