#!/usr/bin/env python3
"""Títulos en varios idiomas y libros duplicados.

Un mismo libro puede llegar con dos títulos: el original y el de la
traducción («Outliers» / «Fuera de serie») o dos versiones del mismo título
mal transcritas. Este script:

  1. Pregunta a Gemini, por lotes, el título publicado en España y el título
     original de cada libro que aún no esté en `titles_checked`. El original
     va en `original_title` y otras formas del título en español, en `aka`
     (la web también busca por ellos).
  2. Fusiona los libros del mismo autor que comparten algún título: se queda
     el de título en español y le suma episodios, menciones y datos del otro.
     El id del libro fusionado queda en `old_ids` para que sus enlaces
     (/#id) y las listas guardadas sigan funcionando.
  3. Lista como «posibles duplicados» los libros del mismo autor con títulos
     muy parecidos («Rasrás» / «Rastras»), sin fusionarlos: a veces son
     libros distintos («Breve historia del tiempo» / «Brevísima…»).

    python3 scripts/titles.py                   # completa y fusiona
    python3 scripts/titles.py --dry-run         # solo enseña qué haría
    python3 scripts/titles.py --merge ID1 ID2   # fusiona dos libros a mano
    python3 scripts/titles.py --distinct ID1 ID2  # no son el mismo libro

Las parejas marcadas con --distinct se guardan en `not_duplicates` y no se
vuelven a fusionar ni a proponer.
"""

import argparse
import difflib
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from books import BOOKS, load, log, norm, save  # noqa: E402
from transcripts import MODELS, QuotaExhausted, api_key, ask_gemini  # noqa: E402

BATCH = 60

PROMPT = """Eres un documentalista. Para cada libro de la lista (título y autor, a veces \
con errores de transcripción o en otro idioma) di:
- "es": el título con el que se publicó en España, bien escrito. Si no se ha publicado \
en español o no lo sabes con seguridad, cadena vacía.
- "orig": si el libro se escribió en un idioma distinto del español, su título original; \
si se escribió en español, cadena vacía.
No inventes títulos: ante la duda, cadena vacía. Responde con un objeto por libro, con \
su número en "i" y su título tal como aparece en la lista en "t".

{books}
"""

SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {"i": {"type": "INTEGER"}, "t": {"type": "STRING"},
                       "es": {"type": "STRING"}, "orig": {"type": "STRING"}},
        "required": ["i", "t", "es", "orig"],
    },
}

STOP = {"y", "de", "del", "la", "el", "dr", "dra"}


def titles(b):
    return [b["title"], *b.get("aka", []), *([b["original_title"]] if b.get("original_title") else [])]


def keys(b):
    """Títulos normalizados. Los que no tienen letras latinas (un original en
    cirílico) se quedan vacíos y no sirven para comparar."""
    return {norm(t) for t in titles(b)} - {""}


def add_aka(b, *names):
    known = keys(b)
    for n in names:
        n = (n or "").strip()
        if norm(n) and norm(n) not in known:
            b.setdefault("aka", []).append(n)
            known.add(norm(n))


def set_original(b, orig):
    orig = (orig or "").strip()
    if norm(orig) and not b.get("original_title"):
        b["original_title"] = orig
        aka = [t for t in b.get("aka", []) if norm(t) != norm(orig)]
        if aka:
            b["aka"] = aka
        else:
            b.pop("aka", None)


def fill_aka(data, key, limit):
    checked = set(data.setdefault("titles_checked", []))
    pending = [b for b in data["books"] if b["id"] not in checked][:limit]
    log(f"{len(pending)} libros sin títulos alternativos revisados.")
    for start in range(0, len(pending), BATCH):
        batch = pending[start:start + BATCH]
        lines = "\n".join(f"{i}. «{b['title']}» — {b.get('author') or '?'}" for i, b in enumerate(batch))
        try:
            found, model = ask_gemini(PROMPT.format(books=lines), key, MODELS, SCHEMA)
        except QuotaExhausted:
            log("Ningún modelo de Gemini disponible; se seguirá en la próxima ejecución.")
            return
        for f in found:
            b = batch[f["i"]] if 0 <= f.get("i", -1) < len(batch) else None
            # Si Gemini descuadra los números, los títulos irían al libro de al lado.
            if b and (norm(b["title"]) in norm(f.get("t") or "") or similar(f.get("t"), b["title"]) >= 0.8):
                set_original(b, f.get("orig"))
                add_aka(b, f.get("es"))
        data["titles_checked"].extend(b["id"] for b in batch)
        log(f"  {start + len(batch)}/{len(pending)} ({model})")
        time.sleep(4)


def similar(a, b):
    return difflib.SequenceMatcher(None, norm(a or ""), norm(b or "")).ratio()


def same_author(a, b):
    ta = set(norm(a.get("author") or "").split()) - STOP
    tb = set(norm(b.get("author") or "").split()) - STOP
    return bool(ta & tb)


def keep_first(a, b):
    """Orden del libro que se queda: el de título en español (no el original
    en otro idioma), el de título bien escrito (el que Gemini da como título
    del otro), el del invitado, el más completo, el más citado."""
    def score(x, other):
        # Título original en otro idioma, habiendo uno en español.
        foreign = norm(x["title"]) == norm(x.get("original_title") or "") and bool(x.get("aka"))
        right = norm(x["title"]) in {norm(t) for t in other.get("aka", [])}
        return (not foreign, right, x.get("relation") != "mencionado", not x.get("review"), bool(x.get("cover")),
                bool(x.get("isbn")), len(x.get("episodes", [])))
    return (a, b) if score(a, b) >= score(b, a) else (b, a)


def merge(keep, drop):
    for e in drop.get("episodes", []):
        if e not in keep["episodes"]:
            keep["episodes"].append(e)
    have = {m["episode"] for m in keep.get("mentions", [])}
    for m in drop.get("mentions", []):
        if m["episode"] not in have:
            keep.setdefault("mentions", []).append(m)
    for t in drop.get("themes", []):
        if t not in keep["themes"] and len(keep["themes"]) < 2:
            keep["themes"].append(t)
    if drop.get("relation") != "mencionado":
        keep.pop("relation", None)  # en uno de los episodios es el libro del invitado
    for k, v in drop.items():
        if k not in ("id", "title", "aka", "original_title", "old_ids", "episodes", "mentions", "themes", "relation", "review") and v and not keep.get(k):
            keep[k] = v
    if not drop.get("review"):
        keep.pop("review", None)
    set_original(keep, drop.get("original_title"))
    add_aka(keep, *titles(drop))
    keep["old_ids"] = sorted({*keep.get("old_ids", []), drop["id"], *drop.get("old_ids", [])})


def forget_titles(books, distinct):
    """En las parejas que no son el mismo libro, quita a cada uno el título del
    otro: si Gemini lo puso en `aka`, la búsqueda los confundiría."""
    by_id = {b["id"]: b for b in books}
    for pair in distinct:
        a, b = (by_id.get(i) for i in pair)
        if not (a and b):
            continue
        # El mismo título original para dos libros distintos: uno está mal y no
        # se sabe cuál, así que se quita de los dos.
        if norm(a.get("original_title") or "") and norm(a.get("original_title")) == norm(b.get("original_title") or ""):
            del a["original_title"], b["original_title"]
        for x, y in ((a, b), (b, a)):
            if x.get("aka"):
                x["aka"] = [t for t in x["aka"] if norm(t) not in keys(y)]
                if not x["aka"]:
                    del x["aka"]


def find_duplicates(books, distinct):
    """Parejas (se queda, se va) de libros del mismo autor con un título en
    común, y las de títulos muy parecidos (posibles duplicados)."""
    pairs, maybe, gone = [], [], set()
    for i, a in enumerate(books):
        for b in books[i + 1:]:
            if a["id"] in gone or b["id"] in gone or not same_author(a, b):
                continue
            if frozenset((a["id"], b["id"])) in distinct:
                continue
            if keys(a) & keys(b):
                keep, drop = keep_first(a, b)
                pairs.append((keep, drop))
                gone.add(drop["id"])
            elif max(similar(x, y) for x in titles(a) for y in titles(b)) >= 0.8:
                maybe.append((a, b))
    return pairs, maybe


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--limit", type=int, default=2000, help="libros máximos a revisar por ejecución")
    ap.add_argument("--dry-run", action="store_true", help="no guarda nada")
    ap.add_argument("--merge", nargs=2, metavar="ID", help="fusiona estos dos libros (se queda el primero)")
    ap.add_argument("--distinct", nargs=2, metavar="ID", help="estos dos libros no son el mismo")
    args = ap.parse_args()

    data = load(BOOKS, None)
    by_id = {b["id"]: b for b in data["books"]}
    for ids in filter(None, (args.merge, args.distinct)):
        if missing := [i for i in ids if i not in by_id]:
            sys.exit(f"No existe: {', '.join(missing)}")
    if args.distinct:
        data.setdefault("not_duplicates", []).append(sorted(args.distinct))
    distinct = {frozenset(p) for p in data.get("not_duplicates", [])}
    forget_titles(data["books"], distinct)

    if args.merge:
        pairs = [(by_id[args.merge[0]], by_id[args.merge[1]])]
    elif args.distinct:
        pairs = []
    else:
        key = api_key()
        if key:
            fill_aka(data, key, args.limit)
        else:
            log("Falta GEMINI_API_KEY: solo se fusionan duplicados con los títulos que ya hay.")
        pairs, maybe = find_duplicates(data["books"], distinct)
        for a, b in maybe:
            log(f"  ? «{a['title']}» / «{b['title']}» ({a.get('author')}): --merge {a['id']} {b['id']}  o  --distinct …")
        if maybe:
            log(f"{len(maybe)} posibles duplicados para revisar a mano.")
    for keep, drop in pairs:
        log(f"  = «{keep['title']}» ← «{drop['title']}» ({keep.get('author')})")
        merge(keep, drop)
    gone = {drop["id"] for _, drop in pairs}
    data["books"] = [b for b in data["books"] if b["id"] not in gone]
    log(f"{len(pairs)} duplicados fusionados.")
    if not args.dry_run:
        save(BOOKS, data)


if __name__ == "__main__":
    main()
