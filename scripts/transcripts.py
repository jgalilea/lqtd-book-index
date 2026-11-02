#!/usr/bin/env python3
"""Busca libros mencionados en las conversaciones (no solo en la descripción).

Para cada episodio con vídeo de YouTube que aún no esté en
`transcripts_checked` de data/books.json:

  1. Descarga los subtítulos automáticos en español con yt-dlp y los guarda
     en .cache/transcripts/ (ignorado por git: el texto de los episodios no
     se publica, solo lo que se extrae de él).
  2. Se los pasa a Gemini (capa gratuita, clave en GEMINI_API_KEY o en .env)
     para que liste los libros mencionados: título, autor, quién lo menciona,
     minuto y una cita breve. Gemini corrige de paso los nombres mal
     transcritos y propone temáticas.
  3. Comprueba cada título en Open Library. Los que no aparecen quedan con
     `review: true` para revisarlos a mano.
  4. Los añade a data/books.json (o une la mención a un libro que ya estaba)
     con `mentions: [{episode, t, by, quote}]`.

Es reanudable: guarda tras cada episodio y, si se agota la cuota diaria de
Gemini, para y sigue donde lo dejó en la siguiente ejecución.
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from books import (BOOKS, EPISODES, ROOT, THEMES, book_id, load, log,  # noqa: E402
                   lookup_openlibrary, norm, save)

CACHE = ROOT / ".cache" / "transcripts"
# En orden de preferencia; si uno está saturado o sin cuota, se pasa al siguiente.
# Flash-Lite primero: responde en segundos y, con el filtro de títulos y Open
# Library, la calidad basta. gemini-3.8-flash queda fuera: en octubre de 2026
# aceptaba las peticiones pero tardaba minutos en responder (o no respondía).
MODELS = ["gemini-3.5-flash-lite", "gemini-3.6-flash", "gemini-3.5-flash"]
API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


class QuotaExhausted(Exception):
    pass


def api_key():
    if os.environ.get("GEMINI_API_KEY"):
        return os.environ["GEMINI_API_KEY"]
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("GEMINI_API_KEY="):
                return line.split("=", 1)[1].strip()
    return None


# --- 1. Subtítulos -----------------------------------------------------------

def parse_vtt(path):
    """VTT de subtítulos automáticos -> [(segundo, texto)] sin repeticiones.

    Los automáticos de YouTube repiten cada línea en el cue siguiente; se
    queda solo el texto nuevo de cada uno."""
    cues, start, prev = [], None, ""
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"(\d+):(\d+):(\d+)\.\d+ -->", line)
        if m:
            h, mi, s = map(int, m.groups())
            start = h * 3600 + mi * 60 + s
            continue
        text = re.sub(r"<[^>]+>", "", line).strip()
        if not text or start is None or text.startswith(("WEBVTT", "Kind:", "Language:")):
            continue
        if text == prev or prev.endswith(text):
            continue
        cues.append((start, text))
        prev = text
    return cues


def fetch_transcript(video_id):
    """Texto con marcas de tiempo cada ~30 s, o None si no hay subtítulos."""
    out = CACHE / f"{video_id}.txt"
    if out.exists():
        return out.read_text(encoding="utf-8") or None
    import yt_dlp

    CACHE.mkdir(parents=True, exist_ok=True)
    opts = {
        "skip_download": True,
        "writeautomaticsub": True,
        "subtitleslangs": ["es-orig", "es"],
        "subtitlesformat": "vtt",
        "outtmpl": str(CACHE / "%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "js_runtimes": {"node": {}},
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([f"https://www.youtube.com/watch?v={video_id}"])
    vtts = sorted(CACHE.glob(f"{video_id}.*.vtt"), key=lambda p: "orig" not in p.name)
    if not vtts:
        out.write_text("", encoding="utf-8")  # sin subtítulos: no reintentar
        return None
    lines, last_mark = [], -999
    for sec, text in parse_vtt(vtts[0]):
        if sec - last_mark >= 30:
            lines.append(f"\n[{sec // 60}:{sec % 60:02d}] ")
            last_mark = sec
        lines.append(text + " ")
    for v in vtts:
        v.unlink()
    text = "".join(lines).strip()
    out.write_text(text, encoding="utf-8")
    return text


# --- 2. Gemini ---------------------------------------------------------------

PROMPT = """Eres un documentalista. Abajo tienes la transcripción automática (con errores) \
de un episodio del podcast «Lo que tú digas», de Álex Fidalgo. Invitado/s: {guest}.
Título del episodio: {title}

Lista TODOS los libros que se mencionan en la conversación: el del invitado, los que \
recomiendan, los que citan o comentan, aunque sea de pasada.

Reglas:
- Solo libros con un título concreto. No incluyas películas, series, discos, podcasts, \
artículos, estudios científicos ni menciones vagas («un libro sobre el estrés»). Si \
hablan de la película o la serie basada en un libro, no la incluyas; solo si hablan del libro.
- Título y autor reales: nunca «Desconocido», «Varios» ni «Anónimo» salvo en clásicos anónimos.
- La transcripción tiene errores en los nombres propios: corrige título y autor a como \
se conocen en España (por ejemplo «Jonathan Heide» -> Jonathan Haidt). Si un libro \
extranjero tiene título en español, usa el español aunque lo digan en el idioma original.
- "original_title": si el libro se escribió en otro idioma, su título original \
(«Outliers» para «Fuera de serie»); si se escribió en español, cadena vacía.
- No inventes: si no sabes con seguridad qué libro es, no lo incluyas.
- "t" es la marca [m:ss] más cercana a donde se menciona por primera vez.
- "by": quién lo menciona: "Álex", "invitado" o el nombre de la persona.
- "own": true si el autor es el propio invitado.
- "quote": frase breve (máximo 25 palabras) de la transcripción donde se menciona, \
con la ortografía arreglada.
- "themes": una o dos de estas temáticas: {themes}

Transcripción:
{transcript}
"""

SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "title": {"type": "STRING"},
            "original_title": {"type": "STRING"},
            "author": {"type": "STRING"},
            "t": {"type": "STRING"},
            "by": {"type": "STRING"},
            "own": {"type": "BOOLEAN"},
            "quote": {"type": "STRING"},
            "themes": {"type": "ARRAY", "items": {"type": "STRING", "enum": list(THEMES)}},
        },
        "required": ["title", "author", "t", "by", "own", "quote", "themes"],
    },
}


def ask_gemini(prompt, key, models, schema=SCHEMA):
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "responseSchema": schema,
                             "temperature": 0.1},
    }).encode()
    for model in models:
        for attempt in range(2):
            req = urllib.request.Request(API.format(model=model), data=body, headers={
                "Content-Type": "application/json", "x-goog-api-key": key})
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    data = json.load(resp)
                text = data["candidates"][0]["content"]["parts"][0]["text"]
                return json.loads(text), model
            except urllib.error.HTTPError as e:
                msg = e.read().decode(errors="replace")
                if e.code == 429 and ("PerDay" in msg or "per day" in msg.lower()):
                    log(f"  cuota diaria agotada en {model}")
                    break  # siguiente modelo
                if e.code == 503:  # «high demand»: mejor probar otro modelo ya
                    log(f"  {model} saturado")
                    break
                if e.code in (429, 500):
                    time.sleep(20 * (attempt + 1))
                    continue
                raise RuntimeError(f"{model}: HTTP {e.code} {msg[:300]}")
            except (urllib.error.URLError, TimeoutError, KeyError, json.JSONDecodeError) as e:
                log(f"  {model}: {e}")
                break
    raise QuotaExhausted("ningún modelo disponible")


# --- 3 y 4. Verificar y guardar ------------------------------------------------

def guest_names(episode):
    t = re.sub(r"\s*-\s*#\d+.*$", "", episode["title"])
    t = re.sub(r"^#\d+\s*(\([A-Z]\))?\s*:\s*", "", t)
    return t


def to_seconds(t):
    m = re.match(r"\s*\[?(\d+):(\d{2})", t or "")
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def find_book(books, titles, author):
    """El libro que ya está en books con alguno de esos títulos (en cualquier
    idioma: se compara también con `aka` y `original_title`) y un autor compatible."""
    nts, na = [norm(t) for t in titles if t], norm(author).split()
    for b in books:
        for bt in [norm(t) for t in [b["title"], *b.get("aka", []), b.get("original_title")] if t]:
            if any(bt == nt or (len(nt) > 6 and (nt in bt or bt in nt)) for nt in nts):
                if not na or not norm(b.get("author", "")) or set(na) & set(norm(b["author"]).split()):
                    return b
    return None


def said_in(title, transcript):
    """¿Se dice el título en la conversación? Filtra libros inventados y
    menciones en las que solo se nombra al autor («como Tolkien»). Basta con
    el 60 % de las palabras significativas, por los errores de transcripción."""
    words = [w for w in norm(title).split() if len(w) > 3] or norm(title).split()
    text = norm(transcript)
    return bool(words) and sum(1 for w in words if f" {w} " in f" {text} ") / len(words) >= 0.6


def add_mentions(data, episode, found, verify, delay):
    added = 0
    for f in found:
        title, author = f["title"].strip(), f["author"].strip()
        original = (f.get("original_title") or "").strip()
        if not title:
            continue
        book = find_book(data["books"], [title, original], author)
        if book is None:
            book = {"id": book_id(title, author), "title": title, "author": author,
                    "themes": [t for t in f.get("themes", []) if t in THEMES][:2],
                    "episodes": [], "source": "transcripcion"}
            if not f.get("own"):
                book["relation"] = "mencionado"
            if verify:
                try:
                    info = lookup_openlibrary(book)
                except (urllib.error.URLError, TimeoutError) as e:
                    # Open Library a veces rechaza conexiones: sin `lookup`,
                    # books.py lo completará en otra ejecución.
                    log(f"   Open Library no responde ({e}); ficha pendiente")
                    info = None
                else:
                    book["lookup"] = "openlibrary" if info else "none"
                    if not info:
                        book["review"] = True  # Open Library no lo conoce: revisar
                time.sleep(delay)
                for k, v in (info or {}).items():
                    if v:
                        book[k] = v
            if original and norm(original) != norm(title):
                book["original_title"] = original
            data["books"].append(book)
            added += 1
            log(f"  + «{title}» ({author}){' — revisar' if book.get('review') else ''}")
        if episode["id"] not in book["episodes"]:
            book["episodes"].append(episode["id"])
        mentions = book.setdefault("mentions", [])
        if not any(m["episode"] == episode["id"] for m in mentions):
            mentions.append({"episode": episode["id"], "t": to_seconds(f.get("t")),
                             "by": f.get("by"), "quote": (f.get("quote") or "")[:220]})
    return added


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--limit", type=int, default=40, help="episodios máximos por ejecución")
    ap.add_argument("--delay", type=float, default=6.0, help="pausa entre llamadas a Gemini (s)")
    ap.add_argument("--model", action="append", help="modelo(s) de Gemini, en orden de preferencia")
    ap.add_argument("--episode", action="append", help="procesa solo estos números de episodio")
    ap.add_argument("--dry-run", action="store_true", help="muestra lo encontrado sin guardar")
    ap.add_argument("--no-verify", action="store_true", help="no consulta Open Library")
    args = ap.parse_args()

    key = api_key()
    if not key:
        log("Falta GEMINI_API_KEY (variable de entorno o .env); no se buscan libros en transcripciones.")
        return
    data = load(BOOKS, None)
    checked = set(data.setdefault("transcripts_checked", []))
    episodes = sorted(load(EPISODES, {"episodes": []})["episodes"],
                      key=lambda e: e.get("published") or "", reverse=True)
    pending = [e for e in episodes if e.get("youtube_id") and e["id"] not in checked
               and (e.get("duration_seconds") or 0) >= 20 * 60]
    if args.episode:
        pending = [e for e in episodes if str(e.get("number")) in args.episode and e.get("youtube_id")]
    log(f"{len(pending)} episodios por revisar; esta ejecución, hasta {args.limit}.")

    total = 0
    for ep in pending[:args.limit]:
        label = f"#{ep['number']}" if ep.get("number") else ep["title"][:40]
        try:
            text = fetch_transcript(ep["youtube_id"])
        except Exception as e:  # YouTube puede bloquear; se reintenta otro día
            log(f"{label}: sin subtítulos por ahora ({str(e)[:120]})")
            continue
        if not text:
            log(f"{label}: el vídeo no tiene subtítulos")
            if not args.dry_run:
                data["transcripts_checked"].append(ep["id"])
                save(BOOKS, data)
            continue
        prompt = PROMPT.format(guest=guest_names(ep), title=ep["title"],
                               themes=", ".join(THEMES), transcript=text)
        try:
            found, model = ask_gemini(prompt, key, args.model or MODELS)
        except QuotaExhausted:
            log("Ningún modelo de Gemini disponible (sin cuota o saturados); se seguirá en la próxima ejecución.")
            break
        # Vale cualquiera de los dos títulos: dicen «Outliers» y Gemini lo
        # devuelve como «Fuera de serie».
        kept = [f for f in found if said_in(f["title"], text) or said_in(f.get("original_title") or "", text)]
        for f in found:
            if f not in kept:
                log(f"   descartado (no se dice el título): «{f['title']}» — {f['author']}")
        found = kept
        log(f"{label}: {len(found)} libros ({model})")
        if args.dry_run:
            for f in found:
                log(f"   [{f['t']}] «{f['title']}» — {f['author']} · {f['by']}{' · propio' if f['own'] else ''} · {f['themes']}\n      “{f['quote']}”")
        else:
            total += add_mentions(data, ep, found, not args.no_verify, 1.0)
            data["transcripts_checked"].append(ep["id"])
            data["books"].sort(key=lambda b: b["id"])
            save(BOOKS, data)
        time.sleep(args.delay)
    log(f"Listo: {total} libros nuevos.")


if __name__ == "__main__":
    main()
