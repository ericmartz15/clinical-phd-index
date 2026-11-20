from collections import defaultdict
from hashlib import sha256
from tempfile import NamedTemporaryFile
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET
import json
import os
import re
import time

from common import doi_key, normalize, short_id

OA = "https://api.openalex.org"
PM = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
CR = "https://api.crossref.org"
SS = "https://api.semanticscholar.org/graph/v1"


class SourceUnavailable(Exception):
    pass


class Client:
    def __init__(self, cache, network):
        self.cache = cache
        self.network = network

    def get(self, url, params=None, headers=None, as_json=True):
        uri = url + ("?" + urlencode(params) if params else "")
        path = self.cache / (sha256(uri.encode()).hexdigest() + ".cache")
        if path.exists():
            body = path.read_bytes()
        elif self.network:
            request = Request(uri, headers={"User-Agent": "ClinicalPhDIndex/2.0", **(headers or {})})
            for attempt in range(3):
                try:
                    with urlopen(request, timeout=15) as response:
                        body = response.read()
                    break
                except HTTPError as error:
                    if error.code not in (429, 500, 502, 503, 504) or attempt == 2:
                        raise SourceUnavailable(f"HTTP {error.code} from {url.split('/')[2]}") from error
                    time.sleep(2 ** attempt)
                except (URLError, TimeoutError) as error:
                    raise SourceUnavailable(f"Connection failed for {url.split('/')[2]}") from error
            self.cache.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(dir=self.cache, delete=False) as temporary:
                temporary.write(body)
            os.replace(temporary.name, path)
        else:
            raise SourceUnavailable(f"No cached response for {url.split('/')[2]}")
        if not as_json:
            return body
        try:
            return json.loads(body)
        except ValueError as error:
            raise SourceUnavailable(f"Invalid JSON from {url.split('/')[2]}") from error


def oa(client, route, params=None):
    key = os.environ.get("OPENALEX_API_KEY")
    return client.get(OA + route, params, {"Authorization": f"Bearer {key}"} if key else {})


def resolve(client, person, reviewed):
    if reviewed:
        return reviewed
    orcid = short_id(person.get("orcid"))
    if orcid:
        try:
            hit = oa(client, f"/authors/orcid:{quote(orcid)}")
        except SourceUnavailable as error:
            if "HTTP 404" not in str(error):
                raise
            hit = {}
        if short_id(hit.get("orcid")) == orcid:
            return dict(ids=[short_id(hit["id"])], confidence=1, status="resolved", evidence=["ORCID match"])
    names = {normalize(name) for name in [person["name"], *person.get("aliases", [])]}
    terms = {normalize(term) for a in person.get("appointments", []) for term in a.get("affiliations", [])}
    interests = set(normalize(" ".join(person.get("areas", []))).split()) - {"the", "and", "of"}
    candidates = {}
    for name in [person["name"], *person.get("aliases", [])]:
        for row in oa(client, "/authors", {"search": name, "per_page": 10}).get("results", []):
            ident = short_id(row["id"])
            if ident in candidates:
                continue
            evidence = []
            candidate_names = {normalize(n) for n in [row.get("display_name"), *row.get("display_name_alternatives", [])]}
            score = 0
            if names.intersection(candidate_names):
                evidence.append("Full name or documented name variant")
                score += .45
            institutions = {normalize(a["institution"]["display_name"]) for a in row.get("affiliations", [])}
            if institutions & terms:
                evidence.append("Institution in OpenAlex affiliation history")
                score += .35
            topics = normalize(" ".join(t.get("display_name", "") for t in row.get("topics", [])))
            if len(interests.intersection(topics.split())) >= 2:
                evidence.append("Multiple research topic terms agree")
                score += .18
            candidates[ident] = dict(id=ident, name=row.get("display_name"), confidence=round(score, 2), evidence=evidence)
    ordered = sorted(candidates.values(), key=lambda r: (-r["confidence"], r["id"]))
    chosen = ordered[0] if ordered else {}
    margin = chosen.get("confidence", 0) - (ordered[1]["confidence"] if len(ordered) > 1 else 0)
    accepted = chosen.get("confidence", 0) >= .98 and margin >= .15
    return dict(ids=[chosen["id"]] if accepted else [], confidence=chosen.get("confidence", 0),
                status="resolved" if accepted else "review", evidence=chosen.get("evidence", []), candidates=ordered[:5])


def oa_works(client, author, start, end, allow_partial=False):
    def query(lower, upper, cursor=None):
        params = {"filter": f"author.id:{author},from_publication_date:{lower},to_publication_date:{upper},type:article|review|preprint",
                  "per_page": 200}
        if cursor is not None:
            params["cursor"] = cursor
        return oa(client, "/works", params)

    keyed = bool(os.environ.get("OPENALEX_API_KEY"))
    cursor = "*" if keyed else None
    records = {}
    while True:
        page = query(start, end, cursor)
        rows = page["results"]
        records.update((row["id"], row) for row in rows)
        count = page["meta"]["count"]
        if len(records) >= count:
            return list(records.values()), True
        next_cursor = page["meta"].get("next_cursor")
        if not keyed and allow_partial:
            # A bounded public query per year can independently report whether its first page was exhaustive.
            complete = True
            for year in range(int(end[:4]), int(start[:4])-1, -1):
                try:
                    year_page = query(max(start, f"{year}-01-01"), min(end, f"{year}-12-31"))
                except SourceUnavailable:
                    complete = False
                    break
                year_rows = year_page["results"]
                records.update((row["id"], row) for row in year_rows)
                complete &= len(year_rows) >= year_page["meta"]["count"]
            return list(records.values()), complete and len(records) >= count
        if not keyed or not rows or not next_cursor or next_cursor == cursor:
            if allow_partial:
                return list(records.values()), False
            raise SourceUnavailable("OpenAlex could not finish pagination for this author")
        cursor = next_cursor


def oa_record(row, author, person):
    authors = row.get("authorships") or []
    positions = [index for index, a in enumerate(authors, 1) if short_id(a["author"].get("id")) == author]
    if not positions or not row.get("title") or row.get("is_retracted"):
        return None
    position = positions[0]
    byline = authors[position - 1]
    locations = [item.get("source") or {} for item in row.get("locations", [])]
    primary = (row.get("primary_location") or {}).get("source") or {}
    journals = [source for source in [primary, *locations] if source.get("type") == "journal"]
    kind = "preprint" if row.get("type") == "preprint" else "journal" if journals else None
    doi = doi_key(row.get("doi"))
    if kind == "preprint" and not (doi.startswith("10.31234/") or any("psyarxiv" in source.get("display_name", "").lower() for source in locations)):
        return None
    if not kind:
        return None
    date = row.get("publication_date") or ""
    venue = journals[0] if journals else {}
    count = row.get("authors_count") or len(authors)
    if byline.get("author_position") == "last":
        position = count
    if byline.get("author_position") == "middle" and count > len(authors) and position >= len(authors):
        position = None
    return dict(title=row["title"], doi=doi, date=date, kind=kind, venue=venue.get("display_name") or "PsyArXiv",
                authorCount=count, pmcid=short_id((row.get("ids") or {}).get("pmcid")), people={person: dict(position=position,
                affiliations=[s["display_name"] for s in byline.get("institutions", [])])},
                issns=list(dict.fromkeys(s for journal in journals for s in (journal.get("issn") or []))),
                topics=[t["display_name"] for t in row.get("topics", [])],
                evidenceDates=[dict(date=date, source="OpenAlex", type="publication")],
                provenance=[dict(source="OpenAlex", id=short_id(row["id"]), url=row["id"])],
                peerReview="inferred" if kind == "journal" else "not applicable")


def crossref_day(message, field):
    parts = ((message.get(field) or {}).get("date-parts") or [[]])[0]
    return f"{parts[0]:04}-{parts[1]:02}-{parts[2]:02}" if len(parts) >= 3 else ""


def crossref_record(message, paper):
    kind = paper["kind"]
    if kind == "journal" and message.get("type") != "journal-article":
        return None
    doi = doi_key(message.get("DOI"))
    dates = [dict(date=d, type=field, source="Crossref")
             for field in ("published-online", "published-print")
             if (d := crossref_day(message, field))]
    linked = [doi_key(link.get("id")) for relation in message.get("relation", {}).values()
              for link in relation if link.get("id-type") == "doi"]
    return dict(title=(message.get("title") or [paper["title"]])[0], doi=doi,
                date=min((d["date"] for d in dates), default=paper["date"]), kind=kind,
                evidenceDates=dates, issns=message.get("ISSN", []), linkedDois=[v for v in linked if v],
                provenance=[dict(source="Crossref", id=doi, url=f"https://doi.org/{doi}")])


def crossref(client, paper):
    return crossref_record(client.get(CR + "/works/" + quote(paper["doi"], safe="/"))["message"], paper)


def psyarxiv(client, person, start, end):
    orcid = short_id(person.get("orcid"))
    response = client.get(CR + "/works", {"query.author": person["name"], "rows": 50,
                          "filter": f"prefix:10.31234,from-pub-date:{start},until-pub-date:{end}"})
    found = []
    for row in response["message"]["items"]:
        doi = doi_key(row.get("DOI"))
        author_list = row.get("author", [])
        title = (row.get("title") or [""])[0]
        if not doi or not title or not orcid:
            continue
        for i, author in enumerate(author_list, 1):
            if short_id(author.get("ORCID")) != orcid:
                continue
            posted = crossref_day(row, "posted") or crossref_day(row, "published")
            base = dict(title=title, doi=doi, date=posted, venue="PsyArXiv", kind="preprint", authorCount=len(author_list),
                        people={person["id"]: dict(position=i, affiliations=[a["name"] for a in author.get("affiliation", []) if a.get("name")])},
                        evidenceDates=[dict(date=posted, source="PsyArXiv via Crossref", type="posted")])
            meta = crossref_record(row, base)
            base.update(linkedDois=meta["linkedDois"], provenance=[dict(source="PsyArXiv via Crossref", id=doi, url=f"https://doi.org/{doi}")])
            found.append(base)
    return found


def semantic(client, paper):
    key = os.environ.get("SEMANTIC_SCHOLAR_API_KEY")
    row = client.get(SS + "/paper/DOI:" + quote(paper["doi"], safe="/"),
                     {"fields": "paperId,url,title,publicationDate,externalIds"},
                     {"x-api-key": key} if key else {})
    if doi_key(row.get("externalIds", {}).get("DOI")) != paper["doi"]:
        return None
    return dict(title=row.get("title") or paper["title"], doi=paper["doi"], date=paper["date"], kind=paper["kind"],
                provenance=[dict(source="Semantic Scholar", id=row["paperId"], url=row["url"])])


def pubmed(client, person, start, end, known):
    labels = list(dict.fromkeys(term for a in person.get("appointments", []) for term in a.get("affiliations", [])))
    if not labels:
        return [], []
    given, _, family = person["name"].rpartition(" ")
    first = given.split()[0].strip(".") if given else ""
    family = re.sub(r'[^\w -]', "", family)
    term = f'"{family} {first[:1]}"[Author] AND (' + " OR ".join(
        f'"{label.replace(chr(34), "")}"[Affiliation]' for label in labels[:5]
    ) + f') AND "{start}"[Date - Publication] : "{end}"[Date - Publication]'
    ids, offset = [], 0
    while True:
        page = client.get(PM + "/esearch.fcgi", {"db": "pubmed", "term": term, "retmode": "json",
                                                "retstart": offset, "retmax": 100})["esearchresult"]
        ids.extend(page["idlist"])
        if len(ids) >= int(page["count"]) or not page["idlist"]:
            break
        offset += len(page["idlist"])
    matched, queue = [], []
    orcid = short_id(person.get("orcid"))
    for i in range(0, len(ids), 100):
        xml = client.get(PM + "/efetch.fcgi", {"db": "pubmed", "retmode": "xml", "id": ",".join(ids[i:i + 100])}, as_json=False)
        for node in ET.fromstring(xml).findall("PubmedArticle"):
            pmid = node.findtext("MedlineCitation/PMID")
            doi = doi_key(node.findtext(".//ArticleId[@IdType='doi']"))
            title = "".join(node.find(".//ArticleTitle").itertext()) if node.find(".//ArticleTitle") is not None else ""
            author_list = node.findall(".//Article/AuthorList/Author")
            confirmed = [i for i, a in enumerate(author_list, 1) if orcid and any(
                short_id(value.text) == orcid for value in a.findall("Identifier[@Source='ORCID']")
            )]
            if (doi not in known and not confirmed) or not title:
                queue.append(dict(source="PubMed", person=person["id"], id=pmid, doi=doi, title=title))
                continue
            types = {item.text for item in node.findall(".//PublicationTypeList/PublicationType")}
            if types & {"Editorial", "Letter", "Comment", "Published Erratum", "Retracted Publication", "News", "Preprint"}:
                continue
            dated = node.find(".//ArticleDate[@DateType='Electronic']")
            date = "-".join(dated.findtext(part, "") for part in ("Year","Month","Day")) if dated is not None else ""
            work = dict(title=title, doi=doi, date=date or known.get(doi, ""),
                        venue=node.findtext(".//Journal/Title") or "", kind="journal", authorCount=len(author_list),
                        issns=[node.findtext(".//Journal/ISSN") or ""],
                        evidenceDates=[dict(date=date, source="PubMed", type="online-first")],
                        provenance=[dict(source="PubMed", id=pmid, url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/")])
            if confirmed:
                a = author_list[confirmed[0] - 1]
                work["people"] = {person["id"]: dict(position=confirmed[0], affiliations=[
                    match for a in a.findall("AffiliationInfo") for source in a.findall("Affiliation")
                    for match in labels if normalize(match) in normalize(source.text)])}
            matched.append(work)
    return matched, queue


def published_byline(xml, expected_doi, expected_count):
    root = ET.fromstring(xml)
    for item in root.iter():
        item.tag = item.tag.rsplit("}", 1)[-1]
    article = root.find("./front/article-meta")
    if article is None or doi_key(article.findtext("./article-id[@pub-id-type='doi']")) != expected_doi:
        return None
    authors = article.findall("./contrib-group/contrib[@contrib-type='author']")
    if not authors or len(authors) != expected_count:
        return None
    equal = {}
    for index, author in enumerate(authors, 1):
        for link in author.findall(".//xref"):
            for identifier in (link.get("rid") or "").split():
                equal.setdefault(identifier, []).append(index)
    first, last = {1}, {len(authors)}
    for note in article.findall(".//fn"):
        group = equal.get(note.get("id"), [])
        if len(group) < 2:
            continue
        wording = normalize(" ".join(note.itertext()))
        if re.search(r"co first|joint first|shared first", wording):
            first.update(group)
        elif re.search(r"co senior|joint senior|shared senior|co last|joint last", wording):
            last.update(group)
        elif "contributed equally" in wording:
            if 1 in group and len(authors) not in group:
                first.update(group)
            elif len(authors) in group and 1 not in group:
                last.update(group)
    return dict(authorCount=len(authors), firstPositions=sorted(first),
                lastPositions=sorted(last), publisherByline=True)


def publisher(client, paper):
    pmcid = paper.get("pmcid")
    if not pmcid or not re.fullmatch(r"PMC[0-9]+", pmcid):
        return None
    endpoint = "https://www.ebi.ac.uk/europepmc/webservices/rest/" + pmcid + "/fullTextXML"
    xml = client.get(endpoint, as_json=False)
    byline = published_byline(xml, paper["doi"], paper["authorCount"])
    if not byline:
        return None
    return dict(byline, title=paper["title"], doi=paper["doi"], kind=paper["kind"], date=paper["date"],
                provenance=[dict(source="Publisher JATS", id=pmcid, url="https://europepmc.org/articles/" + pmcid)])


def crossref_discover(client, person, start, end):
    orcid = short_id(person.get("orcid"))
    if not orcid:
        return [], False
    cursor = "*"
    results = []
    seen = set()
    while cursor:
        response = client.get(CR + "/works", {"filter": f"orcid:{orcid},type:journal-article,from-pub-date:{start},until-pub-date:{end}",
                              "rows": 100, "cursor": cursor})["message"]
        items = response["items"]
        if not items:
            return results, True
        for row in items:
            doi = doi_key(row.get("DOI"))
            title = (row.get("title") or [""])[0]
            if not doi or not title or doi in seen:
                continue
            seen.add(doi)
            authors = row.get("author", [])
            matched = [(idx, author) for idx, author in enumerate(authors, 1)
                       if short_id(author.get("ORCID")) == orcid]
            if len(matched) != 1:
                continue
            idx, author = matched[0]
            date = crossref_day(row, "published-online") or crossref_day(row, "published-print")
            base = crossref_record(row, dict(kind="journal", title=title, date=date))
            base.update(authorCount=len(authors), venue=(row.get("container-title") or [""])[0],
                        people={person["id"]: dict(position=idx, affiliations=[
                            a["name"] for a in author.get("affiliation", []) if a.get("name")])})
            results.append(base)
        if len(items) < 100:
            return results, True
        new_cursor = response.get("next-cursor")
        if new_cursor == cursor or not new_cursor:
            return results, False
        cursor = new_cursor
    return results, False


def semantic_discover(client, person, start, end):
    key = os.environ.get("SEMANTIC_SCHOLAR_API_KEY")
    ids = person.get("semanticIds", [])
    records = []
    if not ids:
        return records, False
    for author_id in ids:
        offset = 0
        while offset is not None:
            page = client.get(SS + f"/author/{quote(author_id)}/papers",
                              {"limit": 100, "offset": offset, "fields": "paperId,url,title,publicationDate,externalIds,authors,journal,publicationTypes"},
                              {"x-api-key": key} if key else {})
            for work in page["data"]:
                published = work.get("publicationDate")
                journal = work.get("journal") or {}
                types = set(work.get("publicationTypes") or [])
                if not published or not start <= published <= end or not journal.get("name"):
                    continue
                if not types.intersection({"JournalArticle", "Review", "MetaAnalysis"}):
                    continue
                authors = work.get("authors") or []
                positions = [i for i,a in enumerate(authors, 1) if a.get("authorId") == author_id]
                if len(positions) != 1 or not work.get("title"):
                    continue
                records.append(dict(title=work["title"], doi=doi_key((work.get("externalIds") or {}).get("DOI")),
                            kind="journal", venue=journal["name"], date=published, authorCount=len(authors),
                            people={person["id"]: dict(position=positions[0], affiliations=[])},
                            evidenceDates=[dict(date=published, source="Semantic Scholar", type="publication")],
                            provenance=[dict(source="Semantic Scholar", id=work["paperId"], url=work["url"])]))
            next_offset = page.get("next")
            if next_offset is not None and next_offset <= offset:
                return records, False
            offset = next_offset
    return records, True
