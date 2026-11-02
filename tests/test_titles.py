"""Títulos en varios idiomas, fusiones de duplicados y búsqueda de libros ya
conocidos (scripts/titles.py y scripts/transcripts.py).

    python3 -m unittest discover tests
"""

import copy
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import titles  # noqa: E402
from build_site import compact_book  # noqa: E402
from transcripts import add_mentions, find_book, said_in  # noqa: E402


def book(id, title, author="Malcolm Gladwell", **kw):
    return {"id": id, "title": title, "author": author, "themes": [], "episodes": [], **kw}


class Keys(unittest.TestCase):
    def test_all_titles_normalised(self):
        b = book("x", "Fuera de serie", aka=["Outliers"], original_title="Outliers: The Story of Success")
        self.assertEqual(titles.keys(b), {"fuera de serie", "outliers", "outliers the story of success"})

    def test_non_latin_titles_are_ignored(self):
        # Dos originales en cirílico se normalizaban a "" y se fusionaban
        # «Crimen y castigo» con «Los demonios».
        a = book("a", "Crimen y castigo", "Fiódor Dostoievski", original_title="Преступление и наказание")
        b = book("b", "Los demonios", "Fiódor Dostoievski", original_title="Бесы")
        self.assertEqual(titles.keys(a), {"crimen y castigo"})
        pairs, _ = titles.find_duplicates([a, b], set())
        self.assertEqual(pairs, [])

    def test_set_original_ignores_non_latin(self):
        b = book("a", "Los demonios", "Dostoievski")
        titles.set_original(b, "Бесы")
        self.assertNotIn("original_title", b)


class AkaAndOriginal(unittest.TestCase):
    def test_add_aka_skips_known_and_empty(self):
        b = book("x", "Fuera de serie", aka=["Outliers"])
        titles.add_aka(b, "fuera de serie", "OUTLIERS", "", None, "Fueras de serie")
        self.assertEqual(b["aka"], ["Outliers", "Fueras de serie"])

    def test_set_original_moves_it_out_of_aka(self):
        b = book("x", "Fuera de serie", aka=["Outliers"])
        titles.set_original(b, "Outliers")
        self.assertEqual(b["original_title"], "Outliers")
        self.assertNotIn("aka", b)

    def test_set_original_does_not_overwrite(self):
        b = book("x", "Cuentos sin plumas", "Woody Allen", original_title="Without Feathers")
        titles.set_original(b, "Getting Even")
        self.assertEqual(b["original_title"], "Without Feathers")


class FindDuplicates(unittest.TestCase):
    def test_translation_is_merged_into_spanish_title(self):
        es = book("fuera", "Fuera de serie", aka=["Outliers"], original_title="Outliers")
        en = book("outliers", "Outliers", original_title="Outliers", aka=["Fuera de serie"])
        pairs, _ = titles.find_duplicates([en, es], set())
        self.assertEqual([(k["id"], d["id"]) for k, d in pairs], [("fuera", "outliers")])

    def test_needs_same_author(self):
        a = book("a", "Nexus", "Yuval Noah Harari")
        b = book("b", "Nexus", "Henry Miller")
        self.assertEqual(titles.find_duplicates([a, b], set()), ([], []))

    def test_author_stopwords_do_not_count(self):
        a = book("a", "Ética", "José de la Torre")
        b = book("b", "Ética", "Ana de la Fuente")
        self.assertEqual(titles.find_duplicates([a, b], set()), ([], []))

    def test_distinct_pairs_are_never_merged_or_proposed(self):
        a = book("breve", "Breve historia del tiempo", "Stephen Hawking")
        b = book("brevisima", "Brevísima historia del tiempo", "Stephen Hawking", aka=["Breve historia del tiempo"])
        self.assertEqual(titles.find_duplicates([a, b], {frozenset(("breve", "brevisima"))}), ([], []))

    def test_similar_titles_are_only_proposed(self):
        a = book("a", "Elogio del afecto", "Rafael Narbona")
        b = book("b", "Elogio del amor", "Rafael Narbona")
        pairs, maybe = titles.find_duplicates([a, b], set())
        self.assertEqual(pairs, [])
        self.assertEqual([(x["id"], y["id"]) for x, y in maybe], [("a", "b")])

    def test_book_is_merged_only_once(self):
        a = book("a", "Fuera de serie", aka=["Outliers"])
        b = book("b", "Outliers")
        c = book("c", "Outliers", aka=["Fuera de serie"])
        pairs, _ = titles.find_duplicates([a, b, c], set())
        dropped = [d["id"] for _, d in pairs]
        self.assertEqual(len(dropped), len(set(dropped)))
        self.assertEqual(len(dropped), 2)  # de los tres queda uno
        self.assertFalse({k["id"] for k, _ in pairs} & set(dropped))


class KeepFirst(unittest.TestCase):
    def test_prefers_spanish_title(self):
        en = book("en", "Outliers", original_title="Outliers", aka=["Fuera de serie"], cover="x", isbn="1",
                  episodes=["1", "2", "3"])
        es = book("es", "Fuera de serie", original_title="Outliers")
        self.assertEqual(titles.keep_first(en, es)[0]["id"], "es")
        self.assertEqual(titles.keep_first(es, en)[0]["id"], "es")

    def test_prefers_the_title_the_other_gives_as_right(self):
        bad = book("bad", "Homo imperfecto", "Ana", aka=["Homo imperfectus"])
        good = book("good", "Homo imperfectus", "Ana")
        self.assertEqual(titles.keep_first(bad, good)[0]["id"], "good")

    def test_prefers_guest_book_then_complete_one(self):
        mentioned = book("m", "X", relation="mencionado", cover="c", isbn="1")
        own = book("o", "X")
        self.assertEqual(titles.keep_first(mentioned, own)[0]["id"], "o")
        bare = book("bare", "X", review=True)
        full = book("full", "X", cover="c")
        self.assertEqual(titles.keep_first(bare, full)[0]["id"], "full")


class Merge(unittest.TestCase):
    def setUp(self):
        self.keep = book("fuera", "Fuera de serie", themes=["exito"], episodes=["e1", "e2"],
                         mentions=[{"episode": "e2", "t": 10}], relation="mencionado", review=True,
                         original_title="Outliers")
        self.drop = book("outliers", "Outliers", themes=["exito", "psicologia", "ciencia"], episodes=["e2", "e3"],
                         mentions=[{"episode": "e2", "t": 99}, {"episode": "e3", "t": 5}],
                         isbn="978", cover="http://c", old_ids=["outliers-gladwell"], aka=["Outliers (ed. 2008)"])
        titles.merge(self.keep, self.drop)

    def test_episodes_and_mentions_are_joined_without_repeats(self):
        self.assertEqual(self.keep["episodes"], ["e1", "e2", "e3"])
        self.assertEqual([(m["episode"], m["t"]) for m in self.keep["mentions"]], [("e2", 10), ("e3", 5)])

    def test_at_most_two_themes(self):
        self.assertEqual(self.keep["themes"], ["exito", "psicologia"])

    def test_guest_book_and_reviewed_win(self):
        self.assertNotIn("relation", self.keep)
        self.assertNotIn("review", self.keep)

    def test_empty_fields_are_filled_but_title_kept(self):
        self.assertEqual((self.keep["title"], self.keep["isbn"], self.keep["cover"]), ("Fuera de serie", "978", "http://c"))

    def test_old_titles_and_ids_are_kept(self):
        self.assertEqual(self.keep["old_ids"], ["outliers", "outliers-gladwell"])
        self.assertEqual(self.keep["aka"], ["Outliers (ed. 2008)"])  # «Outliers» ya es el original
        self.assertEqual(self.keep["original_title"], "Outliers")

    def test_does_not_overwrite_existing_fields(self):
        keep, drop = book("a", "A", isbn="111"), book("b", "B", isbn="222", relation="mencionado")
        titles.merge(keep, drop)
        self.assertEqual(keep["isbn"], "111")
        self.assertNotIn("relation", keep)


class ForgetTitles(unittest.TestCase):
    def test_removes_each_others_titles(self):
        a = book("cuentos", "Cuentos sin plumas", "Woody Allen", aka=["Cómo acabar definitivamente con la cultura", "Sin plumas"],
                 original_title="Without Feathers")
        b = book("como", "Cómo acabar definitivamente con la cultura", "Woody Allen", original_title="Without Feathers")
        titles.forget_titles([a, b], {frozenset(("cuentos", "como"))})
        self.assertEqual(a["aka"], ["Sin plumas"])
        self.assertNotIn("original_title", a)  # compartido: no se sabe de quién es
        self.assertNotIn("original_title", b)
        self.assertEqual(titles.find_duplicates([a, b], set()), ([], []))

    def test_ignores_missing_ids(self):
        titles.forget_titles([book("a", "A")], {frozenset(("a", "nope"))})


class FillAka(unittest.TestCase):
    def run_fill(self, books, answer):
        data = {"books": books}
        with mock.patch.object(titles, "ask_gemini", return_value=(answer, "test")), \
                mock.patch.object(titles.time, "sleep"), mock.patch.object(titles, "log"):
            titles.fill_aka(data, "key", 100)
        return data

    def test_stores_original_and_spanish_title(self):
        data = self.run_fill([book("o", "Outliers")],
                             [{"i": 0, "t": "«Outliers» — Malcolm Gladwell", "es": "Fuera de serie", "orig": "Outliers"}])
        b = data["books"][0]
        self.assertEqual((b["original_title"], b["aka"]), ("Outliers", ["Fuera de serie"]))
        self.assertEqual(data["titles_checked"], ["o"])

    def test_answer_for_another_book_is_ignored(self):
        # Gemini descuadra los números: «t» no es el libro de ese número.
        data = self.run_fill([book("a", "Fuera de serie"), book("b", "Sapiens", "Yuval Noah Harari")],
                             [{"i": 0, "t": "Sapiens", "es": "Sapiens", "orig": "Sapiens: A Brief History of Humankind"}])
        self.assertNotIn("original_title", data["books"][0])

    def test_out_of_range_index_is_ignored(self):
        data = self.run_fill([book("a", "Fuera de serie")], [{"i": 7, "t": "Fuera de serie", "es": "", "orig": "Outliers"}])
        self.assertNotIn("original_title", data["books"][0])

    def test_checked_books_are_skipped(self):
        data = {"books": [book("a", "A")], "titles_checked": ["a"]}
        with mock.patch.object(titles, "ask_gemini") as ask, mock.patch.object(titles, "log"):
            titles.fill_aka(data, "key", 100)
        ask.assert_not_called()


class FindBook(unittest.TestCase):
    books = [book("fuera", "Fuera de serie", original_title="Outliers: The Story of Success", aka=["Outliers"]),
             book("nexus", "Nexus", "Yuval Noah Harari")]

    def test_matches_any_language(self):
        for t in (["Outliers", ""], ["Fuera de serie", "Outliers"], ["Outliers: The Story of Success"]):
            self.assertEqual(find_book(self.books, t, "Malcolm Gladwell")["id"], "fuera", t)

    def test_needs_compatible_author(self):
        self.assertIsNone(find_book(self.books, ["Nexus"], "Henry Miller"))
        self.assertEqual(find_book(self.books, ["Nexus"], "")["id"], "nexus")

    def test_unknown_book(self):
        self.assertIsNone(find_book(self.books, ["Sapiens", None], "Harari"))


class SaidIn(unittest.TestCase):
    def test_title_in_original_language(self):
        text = "y entonces leí outliers de gladwell y me cambió la vida"
        self.assertFalse(said_in("Fuera de serie", text))
        self.assertTrue(said_in("Outliers", text))

    def test_tolerates_transcription_errors(self):
        self.assertTrue(said_in("El hombre en busca de sentido", "el hombre en busca del sentido de frankl"))
        self.assertFalse(said_in("", "lo que sea"))


class AddMentions(unittest.TestCase):
    def test_existing_book_by_original_title_gets_the_mention(self):
        data = {"books": copy.deepcopy(FindBook.books)}
        found = [{"title": "Outliers", "author": "Malcolm Gladwell", "t": "12:34", "by": "Raúl", "quote": "Outliers…"}]
        added = add_mentions(data, {"id": "e562"}, found, verify=False, delay=0)
        self.assertEqual(added, 0)
        b = data["books"][0]
        self.assertEqual(b["episodes"], ["e562"])
        self.assertEqual(b["mentions"][0]["t"], 754)

    def test_new_book_keeps_its_original_title(self):
        data = {"books": []}
        found = [{"title": "El valor de la atención", "original_title": "Stolen Focus", "author": "Johann Hari",
                  "themes": ["salud", "inventado"], "own": False}]
        with mock.patch("transcripts.log"):
            add_mentions(data, {"id": "e1"}, found, verify=False, delay=0)
        b = data["books"][0]
        self.assertEqual((b["original_title"], b["themes"], b["relation"]), ("Stolen Focus", ["salud"], "mencionado"))

    def test_original_equal_to_title_is_not_stored(self):
        data = {"books": []}
        with mock.patch("transcripts.log"):
            add_mentions(data, {"id": "e1"}, [{"title": "Nexus", "original_title": "NEXUS", "author": "Harari"}],
                         verify=False, delay=0)
        self.assertNotIn("original_title", data["books"][0])


class CompactBook(unittest.TestCase):
    def test_web_gets_every_title_and_old_ids(self):
        b = book("fuera", "Fuera de serie", original_title="Outliers", aka=["Fueras de serie"],
                 old_ids=["outliers-malcolm-gladwell"], episodes=["e1"])
        out = compact_book(b, {"e1": {"published": "2026-10-31"}}, {})
        self.assertEqual((out["orig"], out["aka"], out["old"]), ("Outliers", ["Fueras de serie"], ["outliers-malcolm-gladwell"]))

    def test_no_empty_fields(self):
        out = compact_book(book("x", "X"), {}, {})
        self.assertFalse({"orig", "aka", "old"} & out.keys())


if __name__ == "__main__":
    unittest.main()
