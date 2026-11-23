from collections import defaultdict
from datetime import date
import json
from pathlib import Path
import re
import unicodedata

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
CORE_ROLES = {"tenure-track", "research-track"}
SUBFIELDS = {
    "Computational and digital mental health": r"digital phenotyping|ecological momentary|wearable|machine learning|digital mental health|mobile health|artificial intelligence|passive sensing",
    "Trauma": r"trauma|posttraumatic|post traumatic|ptsd",
    "Child and adolescent": r"child|adolescen|youth|pediatric",
    "Anxiety and CBT": r"anxiety|obsessive compulsive|cognitive behavio|exposure therapy",
    "Assessment": r"assessment|psychometric|measurement",
    "Mood and suicide": r"depression|mood disorder|suicid|self harm",
    "Psychosis": r"psychosis|psychotic|schizophren",
    "Health psychology": r"health psychology|behavioral medicine|sleep|chronic pain",
}
DIRECTORIES = {
    "APA": "https://accreditation.apa.org/public-notice-programs-database",
    "PCSAS": "https://pcsas.org/pcsas-accredited-programs/",
    "CUDCP": "https://cudcp.org/CUDCP-member-programs",
    "CPA": "https://cpa.ca/accreditation/CPAaccreditedprograms/",
}


def read(path, default=None):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def normalize(value):
    text = "".join("-" if unicodedata.category(char) == "Pd" else "'" if char in "‘’" else char
                   for char in (value or ""))
    decomposed = unicodedata.normalize("NFKD", text)
    return " ".join(re.findall(r"[a-z0-9]+", decomposed.encode("ascii", "ignore").decode().lower()))


def short_id(value):
    return (value or "").rstrip("/").rsplit("/", 1)[-1]


def doi_key(value):
    doi = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", (value or "").strip(), flags=re.I)
    return doi.lower() if re.fullmatch(r"10\.\d{4,9}/\S+", doi) else ""


def earliest(values):
    candidates = [value for value in values if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value or "")]
    return min(candidates, default="")


def subfields(title, topics):
    text = normalize(" ".join([title, *topics]))
    return [name for name, pattern in SUBFIELDS.items() if re.search(pattern, text)]


def merge(sources):
    """Merge the same manifestation. Preserve linked preprints as distinct types."""
    groups, doi_groups, title_groups = [], {}, defaultdict(list)
    for work in sorted(sources, key=lambda row: not bool(doi_key(row.get("doi")))):
        kind = work["kind"]
        title = normalize(work["title"])
        year = work["date"][:4]
        doi = doi_key(work.get("doi"))
        key = (kind, title, year)
        exact = doi_groups.get((kind, doi)) if doi else None
        near = title_groups[key] if title and year else []
        # Two conflicting DOI records with the same title/year remain distinct.
        if exact is None and len(near) == 1 and not (doi and near[0]["doi"]):
            exact = near[0]
        if exact is None:
            exact = dict(work, doi=doi, provenance=[], people={}, issns=[], topics=[],
                         evidenceDates=[], linkedDois=[], date="")
            groups.append(exact)
        if title and year and not any(row is exact for row in near):
            title_groups[key].append(exact)
        if doi:
            doi_groups[(kind, doi)] = exact
            exact["doi"] = doi
        for attr in ("provenance", "issns", "topics", "evidenceDates", "linkedDois"):
            values = work.get(attr, [])
            exact[attr].extend(entry for entry in values if entry not in exact[attr])
        for person, evidence in work.get("people", {}).items():
            primary = exact["people"].setdefault(person, dict(evidence))
            for key in ("affiliations", "coFirst", "coLast"):
                primary[key] = list(dict.fromkeys([*primary.get(key, []), *evidence.get(key, [])]))
            if not primary.get("position"):
                primary["position"] = evidence.get("position")
        official = [entry["date"] for entry in exact["evidenceDates"]
                    if entry.get("type") in {"published-online", "published-print", "online-first", "official"}]
        exact["date"] = earliest(official) or earliest(entry["date"] for entry in exact["evidenceDates"])
        # OpenAlex supplies the default order. Publisher evidence can override byline count.
        if work.get("publisherByline"):
            for field in ("authorCount", "firstPositions", "lastPositions"):
                exact[field] = work[field]
            exact["publisherByline"] = True
        if work.get("peerReview") == "confirmed":
            exact["peerReview"] = "confirmed"
    for work in groups:
        work["databaseMissing"] = all(source["source"] in {"CV", "Lab"} for source in work["provenance"])
        work["subfields"] = subfields(work["title"], work["topics"])
    # An indexed journal DOI can also appear on a misclassified repository record.
    # Independently registered preprint DOIs remain distinct.
    verified_journal_dois = {work["doi"] for work in groups if work["kind"] == "journal" and work["doi"]
                             and any(source["source"] in {"Publisher", "Crossref", "PubMed"}
                                     for source in work["provenance"])}
    return [work for work in groups if work["kind"] != "preprint" or work["doi"] not in verified_journal_dois]


def weight(n, position, first_positions=(), last_positions=(), cap=50):
    if not n or not position or not 1 <= position <= n:
        return None
    if n <= 2:
        return 1 / n
    outer = {i for i in set(first_positions) | set(last_positions) if 1 <= i <= n} | {1, n}
    if n > cap:
        return 1 / len(outer) if position in outer else 0.0
    return (2 if position in outer else 1) / (n + len(outer))


def creditable_appointments(faculty, publication_date, affiliations):
    """Return eligible contemporaneous appointments. Ambiguous boundaries need corroboration."""
    if not publication_date:
        return []
    dates = date.fromisoformat(publication_date)
    listed = {normalize(value) for value in affiliations}
    active, boundary = [], []
    for appointment in faculty.get("appointments", []):
        if appointment.get("role") not in CORE_ROLES or not appointment.get("source"):
            continue
        start, end = str(appointment.get("start") or ""), str(appointment.get("end") or "")
        if start and start[:4] > str(dates.year) or end and end[:4] < str(dates.year):
            continue
        if len(start) == 10 and publication_date < start or len(end) == 10 and publication_date > end:
            continue
        overlap = bool(listed.intersection(normalize(term) for term in appointment.get("affiliations", [])))
        if not start or len(start) == 4 and start == str(dates.year) or len(end) == 4 and end == str(dates.year):
            if overlap:
                boundary.append(appointment)
        else:
            active.append(appointment)
    return active + boundary
