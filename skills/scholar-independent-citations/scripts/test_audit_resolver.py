"""Fictional fixtures only; no user archive, credentials or live requests."""
import json
from pathlib import Path
import tempfile
import unittest
from audit_resolver import (Resolver, canonical_name, classify_pair, compatible_name,
                             doi_from_url, norm, preserve_scholar_ids, visible_authors, visible_covered)
from author_audit import analyze, known_ids


class AuthorshipResolutionTests(unittest.TestCase):
    def test_surname_first_full_name_is_shared(self):
        self.assertEqual(canonical_name("Fixture, Bo"), "Bo Fixture")
        self.assertEqual(classify_pair(["Bo Fixture"], [{"name": "Fixture, Bo"}], {})[0], "non_independent")

    def test_surname_first_middle_names_are_preserved(self):
        self.assertEqual(canonical_name("Lexample, Wren Sol"), "Wren Sol Lexample")
        self.assertEqual(classify_pair(["Wren Sol Lexample"], [{"name": "Lexample, Wren Sol"}], {})[0], "non_independent")

    def test_distinct_full_given_names_are_not_initials(self):
        for other in ("Benwick Fixture", "Bellin Fixture", "Bex Fixture"):
            self.assertFalse(compatible_name("Bo Fixture", other))
            self.assertEqual(classify_pair(["Bo Fixture"], [{"name": other}], {})[0], "independent")

    def test_initials_are_alignment_not_identity(self):
        self.assertTrue(compatible_name("Bo Fixture", "B Fixture"))
        self.assertEqual(classify_pair(["Bo Fixture"], [{"name": "B Fixture"}], {})[0], "unresolved")

    def test_distinct_spelled_out_given_names(self):
        self.assertEqual(classify_pair(["Fran Reader"], [{"name": "Reader, Feng"}], {})[0], "independent")

    def test_four_initials_align_complete_names(self):
        self.assertTrue(compatible_name("MSMS Aexample", "Meralin Sondel Marven Selrin Aexample"))
        self.assertTrue(compatible_name("KMNK Kexample", "Kerin Marven Neralin Kaven Kexample"))
        self.assertEqual(classify_pair(["Meralin Sondel Marven Selrin Aexample"], [{"name": "MSMS Aexample"}], {})[0], "unresolved")

    def test_transliteration_aligns_but_does_not_prove_identity(self):
        self.assertTrue(compatible_name("J Öxample", "Jorin Oexample"))
        self.assertEqual(classify_pair(["Jorin Öxample"], [{"name": "Jorin Oexample"}], {})[0], "unresolved")

    def test_summary_authors_not_lost(self):
        authors, cut = visible_authors({"publication_info": {"summary": "A Example, F Reader - Journal, 2000",
                                    "authors": [{"name": "A Example", "author_id": "OTHER"}]}})
        self.assertEqual([a["name"] for a in authors], ["A Example", "F Reader"])
        self.assertFalse(cut)

    def test_explicit_truncation_and_et_al(self):
        for text in ("A Example…", "A Example...", "A Example et al."):
            self.assertTrue(visible_authors({"publication_info": {"summary": text + " - Journal"}})[1])

    def test_venue_ellipsis_is_not_author_truncation(self):
        self.assertFalse(visible_authors({"publication_info": {"summary": "A Example - Conference…"}})[1])

    def test_cannot_fit_two_visible_authors_into_one_slot(self):
        self.assertFalse(visible_covered([{"name": "B Fixture"}, {"name": "B Fixture"}], [{"name": "Bo Fixture"}]))
        self.assertTrue(visible_covered([{"name": "B Fixture"}, {"name": "B Fixture"}], [{"name": "Bo Fixture"}, {"name": "Benwick Fixture"}]))

    def test_supplement_must_cover_visible_authors(self):
        self.assertFalse(visible_covered([{"name": "F Reader"}, {"name": "A Example"}], [{"name": "Feng Reader"}]))

    def test_ids_survive_name_expansion(self):
        authors, conflicts = preserve_scholar_ids([{"name": "F Reader", "author_id": "OUTSIDER"}], [{"name": "Reader, Feng"}])
        self.assertFalse(conflicts)
        self.assertEqual(authors[0]["author_id"], "OUTSIDER")

    def test_ambiguous_id_assignment_is_not_guessed(self):
        authors, conflicts = preserve_scholar_ids([{"name": "B Fixture", "author_id": "B"}], [{"name": "Bo Fixture"}, {"name": "Benwick Fixture"}])
        self.assertTrue(all(not a.get("author_id") for a in authors))

    def test_conflicting_ids_are_exposed(self):
        _, conflicts = preserve_scholar_ids([{"name": "F Reader", "author_id": "A"}, {"name": "F Reader", "author_id": "B"}], [{"name": "Fran Reader"}])
        self.assertTrue(conflicts)

    def test_matching_id_proves_overlap_despite_truncation(self):
        self.assertEqual(classify_pair(["Fran Reader"], [{"name": "F Reader", "author_id": "OWNER"}], {"franreader": "OWNER"}, True)[0], "non_independent")

    def test_distinct_known_ids_precede_matching_names(self):
        self.assertEqual(classify_pair(["Fran Reader"], [{"name": "Fran Reader", "author_id": "OTHER"}], {"franreader": "OWNER"})[0], "independent")

    def test_owner_id_cannot_be_overwritten(self):
        ids = known_ids({"author": {"name": "Fran Reader"}, "author_id": "OWNER"}, {"co_authors": [{"name": "Fran Reader", "author_id": "OTHER"}]})
        self.assertEqual(ids["franreader"], "OWNER")

    def test_title_numbers_are_not_removed(self):
        self.assertNotEqual(norm("Evaluation of Model 1"), norm("Evaluation of Model 2"))

    def test_near_title_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            resolver = Resolver(Path(folder), None)
            self.assertIsNotNone(resolver.validate({"title": "Evaluation of Model 1", "publication_info": {}},
                 {"title": "Evaluation of Model 2", "authors": [{"name": "Alice Example"}], "source": "https://example.org"}))

    def test_wrong_doi_rejected_even_with_exact_title(self):
        with tempfile.TemporaryDirectory() as folder:
            resolver = Resolver(Path(folder), None)
            self.assertEqual(resolver.validate({"title": "Same title", "link": "https://doi.org/10.1234/first", "publication_info": {}},
                {"title": "Same title", "doi": "10.1234/second", "source": "https://doi.org/10.1234/second", "authors": [{"name": "Alice Example"}]}), "Conflicting explicit DOI")

    def test_springer_download_suffix_is_not_part_of_doi(self):
        self.assertEqual(doi_from_url("https://link.springer.com/content/pdf/10.1007/978-0-000-00000-0_01.pdf"), "10.1007/978-0-000-00000-0_01")

    def test_mit_download_path_is_not_part_of_doi(self):
        self.assertEqual(doi_from_url("https://direct.mit.edu/tacl/article-pdf/doi/10.1162/TACL.a.00001/123456/tacl.a.00001.pdf"), "10.1162/tacl.a.00001")

    def test_same_arxiv_record_can_change_title(self):
        with tempfile.TemporaryDirectory() as folder:
            resolver = Resolver(Path(folder), None)
            candidate = {"title": "Updated preprint title", "source": "https://arxiv.org/abs/2001.00001", "authors": [{"name": "Alice Example"}]}
            self.assertIsNone(resolver.validate({"title": "Old preprint title", "link": "https://arxiv.org/pdf/2001.00001", "publication_info": {"summary": "A Example"}}, candidate))
            self.assertEqual(candidate["record_match"], "explicit_arxiv_record")

    def test_missing_primary_and_resource_links_are_allowed(self):
        class Unavailable:
            def json(self, url): raise RuntimeError("Unavailable test metadata")
            def get(self, url): raise RuntimeError("Unavailable test metadata")
        with tempfile.TemporaryDirectory() as folder:
            result = Resolver(Path(folder), Unavailable()).resolve({"title": "Missing links", "result_id": "NONE", "link": None,
                      "resources": [{"link": None}], "publication_info": {"summary": "A Example… - Journal"}})
            self.assertFalse(result["complete"])

    def test_changed_input_does_not_reuse_old_author_resolution(self):
        class Unavailable:
            def json(self, url): raise RuntimeError("Unavailable test metadata")
            def get(self, url): raise RuntimeError("Unavailable test metadata")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            resolver = Resolver(root, Unavailable())
            item = {"title": "Same record", "result_id": "C", "publication_info": {"summary": "A Example… - Journal"}}
            first = resolver.resolve(item)
            second = resolver.resolve(dict(item, publication_info={"summary": "B Sample… - Journal"}))
            self.assertNotEqual(first["input_sha256"], second["input_sha256"])
            self.assertEqual(second["authors"][0]["name"], "B Sample")
            self.assertEqual(len(list((root / "history").glob("*.json"))), 1)

    def test_index_author_cap_is_not_complete(self):
        with tempfile.TemporaryDirectory() as folder:
            resolver = Resolver(Path(folder), None)
            self.assertEqual(resolver.validate({"title": "Large paper", "publication_info": {}},
                {"title": "Large paper", "provider": "openalex", "authors": [{"name": "Alice Example"}]*100}), "OpenAlex 100-author completeness limit")

    def test_cross_source_relationships_never_deduplicate(self):
        citing = {"title": "Citing paper", "result_id": "C", "link": "https://example.org/c", "publication_info": {"summary": "Charlie Fixture - Journal, 2000"}}
        papers = []
        for number, coauthor in ((1, "Alice Example"), (2, "Bob Example")):
            papers.append({"paper": {"title": "Source " + str(number), "citation_id": "OWNER:"+str(number), "link": "https://example.org/"+str(number)},
                           "detail": {"authors": "Fran Reader, " + coauthor}, "displayed": 1, "end_reason": "last_page", "citing": [citing]})
        data = {"author": {"name": "Fran Reader"}, "author_id": "OWNER", "snapshot_date": "2000-01-02", "papers": papers}
        profile = {"cited_by": {"table": [{"citations": {"all": 2}}]}}
        result = analyze(data, profile, {"C": {"complete": True, "title": "Citing paper", "source": "https://example.org/c", "authors": [{"name": "Charlie Fixture"}]}})
        self.assertEqual(result["retrieved"], 2)
        self.assertEqual(result["classification"]["independent"], 2)
        self.assertEqual(len(result["citation_edges"]), 2)

    def test_reused_metadata_is_validated_for_each_relationship(self):
        first = {"title": "Citing paper", "result_id": "C", "link": "https://example.org/c", "publication_info": {"summary": "C Fixture - Journal"}}
        second = dict(first, publication_info={"summary": "D Sample - Journal"})
        papers = [{"paper": {"title": "Source " + str(i), "citation_id": "OWNER:" + str(i), "link": "https://example.org/"+str(i)},
                   "detail": {"authors": "Fran Reader"}, "displayed": 1, "end_reason": "last_page", "citing": [item]} for i,item in enumerate([first, second])]
        result = analyze({"author": {"name": "Fran Reader"}, "author_id": "OWNER", "snapshot_date": "2000-01-02", "papers": papers},
            {"cited_by": {"table": [{"citations": {"all": 2}}]}},
            {"C": {"complete": True, "title": "Citing paper", "source": "https://example.org/c", "authors": [{"name": "Charlie Fixture"}]}})
        self.assertEqual(result["classification"], {"independent": 1, "non_independent": 0, "unresolved": 1})

    def test_empty_profile_cannot_hide_nonzero_total(self):
        result = analyze({"author": {"name": "Fran Reader"}, "author_id": "OWNER", "snapshot_date": "2000-01-02", "papers": []},
                         {"cited_by": {"table": [{"citations": {"all": 7}}]}}, {})
        self.assertFalse(result["complete_retrieval_counts"])


if __name__ == "__main__": unittest.main()
