#!/usr/bin/env python3
"""Construye la web estática en _site/ a partir de site/ y data/.

Copia los ficheros de site/ y genera _site/books.json: los libros de
data/books.json con los datos de sus episodios ya unidos, en el formato
compacto que usa la web.

Si está instalado Pillow, además descarga las portadas y las sirve desde la
propia web como miniaturas WebP (200 y 400 px de ancho) en _site/covers/: pesan
una quinta parte que las originales y no dependen de lo que tarde Open Library.
Las descargas se guardan en .cache/covers/ para no repetirlas en cada build.
Las que fallan se quedan con el enlace original.

    python3 scripts/build_site.py && python3 -m http.server -d _site
"""

import hashlib
import io
import json
import re
import shutil
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "site"
OUT = ROOT / "_site"
CACHE = ROOT / ".cache" / "covers"
COVER_WIDTHS = (200, 400)  # la web elige con srcset según el tamaño en pantalla
USER_AGENT = "lqtd-index/0.1 (web fan no oficial de Lo que tú digas)"


def clean_title(title):
    """Quita el número y la coletilla del podcast: ya se muestran aparte."""
    t = re.sub(r"^#\d+\s*(\([A-Z]\))?\s*:\s*", "", title)
    t = re.sub(r"\s*[-|]\s*#\d+\s*(\([A-Z]\))?\s*$", "", t)
    t = re.sub(r"\s*\|\s*lo que t[uú] digas.*$", "", t, flags=re.I)
    return t.strip(" -|")


def compact_episode(ep):
    out = {
        "n": ep.get("number") if ep.get("kind") == "episodio" else None,
        "title": clean_title(ep["title"]),
        "date": (ep.get("published") or "")[:10],
        "yt": ep.get("youtube_id"),
        "sp": ep.get("spotify_url"),
        "iv": ep.get("ivoox_url"),
        "am": ep.get("amazon_url"),
        "ap": ep.get("apple_url"),
    }
    return {k: v for k, v in out.items() if v not in (None, "")}


def compact_book(book, episodes, ep_index):
    """Libro en formato compacto. Los episodios van aparte (ep_index les da un
    número) y cada libro solo guarda su número y los datos de la mención."""
    mentions = {m["episode"]: m for m in book.get("mentions", [])}
    eps = []
    for e in book["episodes"]:
        if e not in episodes:
            continue
        if e not in ep_index:
            ep_index[e] = len(ep_index)
        ref = {"e": ep_index[e]}
        m = mentions.get(e)
        if m:  # mención encontrada en la transcripción: minuto, quién y cita
            ref.update({k: v for k, v in (("at", m.get("t")), ("by", m.get("by")), ("q", m.get("quote"))) if v})
        eps.append((episodes[e].get("published") or "", ref))
    eps.sort(key=lambda x: x[0], reverse=True)
    out = {
        "id": book["id"],
        "t": book["title"],
        "orig": book.get("original_title"),  # título original (otro idioma)
        "aka": book.get("aka"),  # otras formas del título: también se buscan
        "old": book.get("old_ids"),  # ids de libros fusionados con este
        "a": book.get("author"),
        "th": book.get("themes") or [],
        "y": book.get("year"),
        "pub": book.get("publisher"),
        "isbn": book.get("isbn"),
        "cover": book.get("cover"),
        "buy": book.get("buy_url"),
        # Solo se marca la excepción: por defecto es un libro del invitado.
        "m": 1 if book.get("relation") == "mencionado" else None,
        "date": eps[0][0][:10] if eps else "",
        "eps": [ref for _, ref in eps],
    }
    return {k: v for k, v in out.items() if v not in (None, "", [])}


# --- Portadas ----------------------------------------------------------------

def cover_key(url):
    return hashlib.sha1(url.encode()).hexdigest()[:16]


def make_thumbs(url, Image):
    """Descarga la portada (si no está en caché) y genera sus miniaturas.
    Devuelve la clave de los ficheros, "" si la imagen no sirve (Open Library
    devuelve 1×1 px cuando no tiene la portada) o None si la descarga falló."""
    key = cover_key(url)
    paths = [CACHE / f"{key}-{w}.webp" for w in COVER_WIDTHS]
    if all(p.exists() for p in paths):
        return key
    if (CACHE / f"{key}.none").exists():
        return ""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as r:
            im = Image.open(io.BytesIO(r.read()))
            im.load()
    except Exception:
        return None
    if im.width < 20 or im.height < 20:
        (CACHE / f"{key}.none").touch()
        return ""
    im = im.convert("RGB")
    for w, p in zip(COVER_WIDTHS, paths):
        t = im.resize((w, round(im.height * w / im.width)), Image.LANCZOS) if im.width > w else im
        t.save(p, "WEBP", quality=72, method=6)
    return key


def local_covers(books):
    """Sustituye el enlace de cada portada por la clave de sus miniaturas."""
    try:
        from PIL import Image
    except ImportError:
        print("Pillow no está instalado: las portadas se enlazan a su origen.")
        return
    CACHE.mkdir(parents=True, exist_ok=True)
    urls = sorted({b["cover"] for b in books if b.get("cover")})
    with ThreadPoolExecutor(8) as pool:
        keys = dict(zip(urls, pool.map(lambda u: make_thumbs(u, Image), urls)))
    (OUT / "covers").mkdir()
    for b in books:
        key = keys.get(b.get("cover"))
        if key is None:
            continue  # falló la descarga: se queda el enlace original
        del b["cover"]
        if key:
            b["img"] = key
            for w in COVER_WIDTHS:
                shutil.copyfile(CACHE / f"{key}-{w}.webp", OUT / "covers" / f"{key}-{w}.webp")
    failed = sum(1 for k in keys.values() if k is None)
    print(f"Portadas: {sum(1 for k in keys.values() if k)} en local, {failed} con enlace externo")


def main():
    books = json.loads((ROOT / "data" / "books.json").read_text(encoding="utf-8"))
    episodes = {e["id"]: e for e in json.loads((ROOT / "data" / "episodes.json").read_text(encoding="utf-8"))["episodes"]}
    if OUT.exists():
        shutil.rmtree(OUT)
    shutil.copytree(SRC, OUT)
    ep_index = {}
    compact = [compact_book(b, episodes, ep_index) for b in books["books"]]
    local_covers(compact)
    payload = {
        "updated": books.get("updated_at"),
        "themes": books["themes"],
        "eps": [compact_episode(episodes[e]) for e in ep_index],
        "books": compact,
    }
    (OUT / "books.json").write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"_site/ generado con {len(compact)} libros")


if __name__ == "__main__":
    main()
