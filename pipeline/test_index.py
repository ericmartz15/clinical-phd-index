import unittest
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.error import HTTPError

from build import audit, diagnostic_check
from collect import stage_publications
from common import creditable_appointments, doi_key, merge, normalize, weight
from directories import cpa_rows
from rosters import Page
from sources import Client, OA, SourceUnavailable, published_byline


class ScoringTest(unittest.TestCase):
    def test_weight_rules(self):
        self.assertEqual(weight(1, 1), 1)
        self.assertEqual([weight(2, i) for i in range(1, 3)], [.5, .5])
        self.assertEqual([weight(4, i) for i in range(1, 5)], [2/6, 1/6, 1/6, 2/6])
        self.assertEqual([weight(4, i, (2,)) for i in range(1, 5)], [2/7, 2/7, 1/7, 2/7])
        self.assertEqual(weight(51, 5), 0)
        self.assertAlmostEqual(sum(weight(51, i, (2,), (50,)) for i in range(1, 52)), 1)

    def test_publication_time_and_year_boundaries(self):
        person = {"appointments": [
            {"program": "A", "role": "tenure-track", "start":"2019-03-01", "end":"2023",
             "source":"CV", "affiliations":["Old University"]},
            {"program": "B", "role": "tenure-track", "start":"2023", "end":None,
             "source":"CV", "affiliations":["New University"]},
            {"program": "C", "role": "postdoc", "start":"2015", "end":"2019", "source":"CV"}
        ]}
        self.assertEqual(creditable_appointments(person, "2019-01-01", []), [])
        self.assertEqual([a["program"] for a in creditable_appointments(person, "2023-03-01", ["Old University"])], ["A"])
        self.assertEqual([a["program"] for a in creditable_appointments(person, "2024-03-01", ["Old University"])], ["B"])


class DataTest(unittest.TestCase):
    def test_clinical_roster_accordion_and_affiliated_section(self):
        page = Page("https://example.edu/graduate/clinical")
        page.feed("""
        <main>
          <h1>Clinical Psychology</h1>
          <a role="button" aria-controls="area" href="#area">Area Faculty</a>
          <div id="area"><a href="/profile/jane-doe">Dr. Jane Doe</a></div>
          <a role="button" aria-controls="admissions" href="#admissions">Admissions</a>
          <div id="admissions"><a href="/profile/john-doe">John Doe</a></div>
          <h2>Affiliated Faculty</h2>
          <h3><a href="/profile/alex-doe">Alex Doe</a></h3>
          <h2>Clinical Faculty</h2>
          <h3><a href="/profile/lee-doe">Lee Doe</a></h3>
        </main>
        """)
        self.assertEqual([(row["name"], row["section"]) for row in page.main_people],
                         [("Jane Doe", "Area Faculty"), ("Lee Doe", "Clinical Faculty")])

    def test_openalex_budget_limit_leaves_later_requests_queued(self):
        url = OA + "/authors"
        error = HTTPError(url, 429, "Too Many Requests", {}, BytesIO(b'{"reason":"daily_budget"}'))
        with TemporaryDirectory() as directory, patch("sources.urlopen", side_effect=error) as request:
            client = Client(Path(directory), True)
            for name in ("First author", "Second author"):
                with self.assertRaisesRegex(SourceUnavailable, "daily API budget"):
                    client.get(url, {"search": name})
            request.assert_called_once()

    def test_candidate_requires_resolved_identity_before_works_retrieval(self):
        candidate = {"name": "Jane Doe", "url": "https://example.edu/people/jane-doe", "orcids": [],
                     "profileStatus": "name confirmed on profile", "publicProfileTopics": ["depression", "sleep"],
                     "programs": ["a"]}
        with patch("collect.resolve", return_value={"status": "review", "ids": []}), patch("collect.oa_works") as works:
            result = stage_publications(object(), candidate, {"a": ["Example University"]}, "2016-01-01", "2026-01-01")
            self.assertEqual(result["status"], "identity requires review")
            works.assert_not_called()
        identity = {"status": "resolved", "ids": ["A123"], "confidence": .98}
        with patch("collect.resolve", return_value=identity), patch("collect.oa_works", return_value=([], True)) as works:
            result = stage_publications(object(), candidate, {"a": ["Example University"]}, "2016-01-01", "2026-01-01")
            self.assertEqual(result["openAlex"], "A123")
            self.assertEqual(works.call_args.args[1], "A123")

    def test_title_dash_is_normalized_consistently(self):
        self.assertEqual(normalize("Decision‐making"), normalize("decision-making"))

    def test_merge_retains_source_and_preprint(self):
        original = {"title":"A paper", "doi":"10.1234/x", "date":"2022-06-01", "kind":"journal",
                    "provenance":[{"source":"OpenAlex"}], "evidenceDates":[{"date":"2022-06-01","type":"publication"}]}
        publisher = dict(original, doi="https://doi.org/10.1234/X", provenance=[{"source":"Crossref"}],
                         evidenceDates=[{"date":"2022-04-01","type":"published-online"}])
        preprint = dict(original, kind="preprint", doi="10.31234/other")
        records = merge([original, publisher, preprint])
        self.assertEqual(len(records), 2)
        article = next(work for work in records if work["kind"] == "journal")
        self.assertEqual(article["date"], "2022-04-01")
        self.assertEqual(doi_key(publisher["doi"]), article["doi"])
        self.assertEqual(len(article["provenance"]), 2)

    def test_no_small_sample_promotion(self):
        faculty = [{"id":"a","appointments":[{"role":"tenure-track","current":True}]}]
        refs = {"a":{"complete":True,"papers":[{"kind":"journal","date":"2025-01-01","title":"Paper"}]}}
        works = [{"kind":"journal","date":"2025-01-01","title":"Paper","doi":"",
                  "people":{"a":{}}, "databaseMissing":False}]
        result = audit(faculty, works, refs, 13, "holdout", "2021-01-01", "2026-01-01")
        self.assertEqual(result["diagnosticRecall"], 1)
        self.assertIsNone(result["recall"])
        self.assertFalse(result["targetMet"])

    def test_fixed_sample_rejects_a_conflicting_doi(self):
        faculty = [{"id": pid, "appointments": [{"role": "tenure-track", "current": True}]} for pid in ["a", "b"]]
        reference = {"b": {"complete": True, "papers": [
            {"kind": "journal", "date": "2025-01-01", "title": "Same title", "doi": "10.1234/reference"}
        ]}}
        work = {"kind": "journal", "date": "2025-01-01", "title": "Same title", "doi": "10.1234/other",
                "people": {"b": {}}, "databaseMissing": False}
        result = audit(faculty, [work], reference, 13, "calibration", "2021-01-01", "2026-01-01",
                       fixed_sample=["b"])
        self.assertEqual(result["sample"], ["b"])
        self.assertEqual(result["matches"], 0)
        work["doi"] = ""
        result = audit(faculty, [work], reference, 13, "calibration", "2021-01-01", "2026-01-01",
                       fixed_sample=["b"])
        self.assertEqual(result["matches"], 1)

    def test_diagnostic_uses_documented_doi_for_title_variants(self):
        work = {"kind": "journal", "title": "Published title", "date": "2025-01-01",
                "people": {"a": {}}, "doi": "10.1234/example", "databaseMissing": False}
        reference = {"person": "a", "scope": "CV check", "source": "CV", "papers": [
            {"title": "Title as shown on CV", "year": 2025, "doi": "https://doi.org/10.1234/Example"}
        ]}
        self.assertEqual(diagnostic_check([work], reference)["matches"], 1)
        self.assertEqual(diagnostic_check([dict(work, databaseMissing=True)], reference)["matches"], 0)

    def test_explicit_jats_first_author_group(self):
        xml = b"""<article><front><article-meta>
        <article-id pub-id-type="doi">10.1234/a</article-id><contrib-group>
        <contrib contrib-type="author"><xref rid="eq"/></contrib>
        <contrib contrib-type="author"><xref rid="eq"/></contrib>
        <contrib contrib-type="author"/>
        </contrib-group><author-notes><fn id="eq"><p>These authors contributed equally.</p></fn></author-notes>
        </article-meta></front></article>"""
        self.assertEqual(published_byline(xml,"10.1234/a",3)["firstPositions"], [1,2])
        self.assertIsNone(published_byline(xml,"10.1234/z",3))

    def test_cpa_program_scope(self):
        html = '<a id="clinical"></a><table><h4><a href="https://example.org/phd">PhD University</a></h4>Accredited<h4><a href="https://example.org/psyd">PsyD School</a></h4>Accredited</table>'
        self.assertEqual([row["name"] for row in cpa_rows(html)], ["PhD University"])
