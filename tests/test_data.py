"""Coherencia de data/books.json: ids, fusiones, títulos y búsqueda.

    python3 -m unittest discover tests
"""

import json
import sys
import unicodedata
import unittest
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import titles  # noqa: E402
from books import norm  # noqa: E402
from build_site import compact_book  # noqa: E402

DATA = json.loads((ROOT / "data" / "books.json").read_text(encoding="utf-8"))
BOOKS = DATA["books"]
IDS = {b["id"] for b in BOOKS}
EPISODES = {e["id"]: e for e in json.loads((ROOT / "data" / "episodes.json").read_text(encoding="utf-8"))["episodes"]}


def web_norm(s):
    """La misma normalización que la búsqueda de site/app.js."""
    s = unicodedata.normalize("NFD", s or "")
    return "".join(c for c in s if not "̀" <= c <= "ͯ").lower()


class Ids(unittest.TestCase):
    def test_unique(self):
        self.assertEqual([i for i, n in Counter(b["id"] for b in BOOKS).items() if n > 1], [])

    def test_old_ids_point_to_a_single_book(self):
        # Un id fusionado no puede seguir existiendo ni apuntar a dos libros:
        # el enlace /#id antiguo abriría otro libro.
        old = Counter(o for b in BOOKS for o in b.get("old_ids", []))
        self.assertEqual(sorted(old.keys() & IDS), [])
        self.assertEqual([o for o, n in old.items() if n > 1], [])

    def test_known_distinct_books_survive(self):
        # Fusiones equivocadas que ya pasaron una vez (originales en cirílico,
        # títulos de Gemini cruzados): estos libros tienen que seguir aparte.
        keep = {"crimen-y-castigo-fiodor-dostoyevski", "los-demonios-fiodor-dostoyevski",
                "dinero-facil-jens-lapidus", "una-vida-de-lujo-jens-lapidus",
                "breve-historia-del-tiempo-stephen-hawking", "brevisima-historia-del-tiempo-stephen-hawking",
                "critica-de-la-razon-pura-immanuel-kant", "critica-de-la-razon-practica-immanuel-kant"}
        self.assertEqual(sorted(keep - IDS), [])

    def test_lists_reference_existing_books(self):
        for pair in DATA.get("not_duplicates", []):
            self.assertEqual(len(pair), 2, pair)
            self.assertLessEqual(set(pair), IDS, f"not_duplicates con un libro que ya no existe: {pair}")


class Titles(unittest.TestCase):
    def test_aka_are_other_titles(self):
        for b in BOOKS:
            aka = [norm(t) for t in b.get("aka", [])]
            with self.subTest(b["id"]):
                self.assertTrue(all(aka), "aka vacío o sin letras latinas")
                self.assertEqual(len(aka), len(set(aka)), "aka repetido")
                self.assertNotIn(norm(b["title"]), aka)
                self.assertNotIn(norm(b.get("original_title") or "-"), aka)
                self.assertNotEqual(b.get("aka"), [], "aka vacío: hay que quitar el campo")

    def test_original_title_is_text(self):
        for b in BOOKS:
            if "original_title" in b:
                self.assertTrue(b["original_title"].strip(), b["id"])

    def test_no_pending_duplicates(self):
        # Dos libros del mismo autor con un título en común son el mismo libro:
        # titles.py los fusiona, o se marcan con --distinct.
        distinct = {frozenset(p) for p in DATA.get("not_duplicates", [])}
        pairs, _ = titles.find_duplicates(BOOKS, distinct)
        self.assertEqual([(k["id"], d["id"]) for k, d in pairs], [])

    def test_distinct_books_do_not_share_titles(self):
        by_id = {b["id"]: b for b in BOOKS}
        for a, b in DATA.get("not_duplicates", []):
            with self.subTest((a, b)):
                self.assertEqual(titles.keys(by_id[a]) & titles.keys(by_id[b]), set())


class Search(unittest.TestCase):
    """Cada libro se encuentra por todos sus títulos en la web."""

    def test_every_title_reaches_the_web(self):
        for b in BOOKS:
            out = compact_book(b, EPISODES, {})
            hay = web_norm(" ".join([out["t"], out.get("orig") or "", *out.get("aka", [])]))
            for t in titles.titles(b):
                with self.subTest(b["id"], title=t):
                    self.assertIn(web_norm(t), hay)

    def test_known_translations(self):
        by_title = {}
        for b in BOOKS:
            for t in titles.titles(b):
                by_title.setdefault(web_norm(t), set()).add(b["id"])
        for es, en in [("Fuera de serie", "Outliers"), ("Pensar rápido, pensar despacio", "Thinking, Fast and Slow")]:
            with self.subTest(es):
                self.assertEqual(by_title.get(web_norm(es)), by_title.get(web_norm(en)))
                self.assertEqual(len(by_title[web_norm(es)]), 1)


if __name__ == "__main__":
    unittest.main()
