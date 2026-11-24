"""Propose potential official faculty pages and link-text candidates for manual verification."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from io import BytesIO
from itertools import islice
from pathlib import Path
import re
from urllib.parse import quote, urljoin, urlsplit

from common import DATA, normalize, read, write
from sources import Client, SourceUnavailable

SECONDARY_SECTION = re.compile(
    r"\b(emerit\w*|retired|recall\w*|adjunct|affiliat\w*|associated|secondary|joint|"
    r"students?|postdocs?|staff|associates?|lecturers?)\b", re.I
)
CLINICAL_FACULTY = re.compile(
    r"\bclinical(?: science| psychology)? (?:faculty|mentors?)\b|"
    r"\bfaculty (?:who supervise|mentors?)\b|\b(?:core|training) (?:doctoral )?faculty\b|"
    r"\bfaculty whose primary area is clinical\b|\btenured (?:and |or )tenure[- ]track faculty\b", re.I
)
EXPLICIT_ROSTER = re.compile(
    r"\bcore\b|\bfaculty whose primary area is clinical\b|\btenured (?:and |or )tenure[- ]track faculty\b", re.I
)
NON_FACULTY_SECTION = re.compile(r"\b(students?|postdocs?|staff)\b", re.I)
HANDBOOK_SECTION = re.compile(
    r"^(?:core(?: clinical)?(?: psychology| doctoral| program)? faculty|"
    r"(?:clinical(?: psychology)?(?: adjunct)?|complementary clinical psychology|"
    r"associated program|affiliated|adjunct|emeritus) faculty)"
    r"(?:\s*[-–:]\s*[\w ,]+)?$", re.I
)
HANDBOOK_BYLINE = re.compile(
    r"^([A-Z][\w’'‐–-]+(?: +[A-Z][\w.’'‐–-]*){1,5}),? +"
    r"(?:Ph\.?\s*D\.?|Psy\.?\s*D\.?|M\.?\s*D\.?)(?=[\s,.(]|$)"
)


class HandbookDocument(SourceUnavailable):
    def __init__(self, content):
        super().__init__("PDF roster requires manual review")
        self.content = content


def handbook_faculty_pages(pages, source):
    """Find faculty rosters for comparison with dated handbooks and online directories."""
    evidence, section, section_page = [], "", 0
    for number, page in enumerate(pages, 1):
        page = re.sub(
            r"(?im)^(core clinical psychology|complementary clinical|clinical psychology adjunct)"
            r"[ \t]*\n[ \t]*(faculty(?: *- *registered)?|psychology faculty)"
            r"(?:[ \t]*\n[ \t]*(psychologists))?",
            lambda match: " ".join(part for part in match.groups() if part), page)
        for line in page.splitlines():
            line = " ".join(line.split())
            if HANDBOOK_SECTION.fullmatch(line):
                section, section_page = line, number
            elif re.fullmatch(r"(?:clinical )?program (?:overview|requirements|governance)|other contributors",
                              line, re.I):
                section = ""
            elif section and number <= section_page + 1 and (match := HANDBOOK_BYLINE.match(line)):
                evidence.append(dict(name=match[1], source=source, page=number, section=section))
    return evidence


def person_link(label, url):
    parts = normalize(label).split()
    path = urlsplit(url).path.lower()
    if not 2 <= len(parts) <= 7 or re.search(
            r"\b(faculty|directory|students?|admissions?|clinical|department|programs?|staff|people|psychology|undergraduate)\b",
            normalize(label)):
        return False
    if normalize(label) in {"apply now", "contact us", "how to apply", "news and events"}:
        return False
    if label != label.title() and not all(word[0].isupper() for word in label.split() if word.isalpha()):
        return False
    return bool(re.search(
        r"/(?:person|people|faculty|faculty-page|profiles?|staff|directory|our-faculty|team-member|bio|bios)/[^/]+",
        path))


class Page(HTMLParser):
    def __init__(self, url):
        super().__init__(convert_charrefs=True)
        self.url = url
        self.heading = None
        self.link = None
        self.person_heading = ""
        self.clinical_page = bool(re.search(r"/clinical(?:-science|-psychology)?/(?:people|faculty)(?:/|$)|"
                                           r"/(?:faculty|area-faculty)/clinical(?:-science|-psychology)?(?:[./]|$)|"
                                           r"/clinical(?:-science|-psychology)?-faculty(?:[./]|$)|"
                                           r"/clinical-(?:science|psychology)(?:[./]|$)",
                                           urlsplit(url).path, re.I))
        self.scopes = {}
        self.control = None
        self.in_main = self.has_main = False
        self.body_text, self.main_text = [], []
        self.ignored = None
        self.headings, self.main_links, self.main_people = [], [], []
        self.links, self.people = [], []
        self.fallback_people, self.main_fallback_people = [], []

    def handle_starttag(self, tag, attrs):
        if tag == "main":
            self.has_main = self.in_main = True
            self.scopes.clear()
            self.person_heading = ""
        if tag in ("script", "style"):
            self.ignored = tag
        if re.fullmatch(r"h[1-6]", tag):
            self.heading = [tag, []]
        if tag == "a":
            attributes = dict(attrs)
            href = attributes.get("href") or ""
            self.control = attributes.get("aria-controls") if attributes.get("role") == "button" else None
            self.link = [urljoin(self.url, href), [], attributes.get("aria-label", "")]

    def handle_data(self, value):
        if self.ignored:
            return
        self.body_text.append(value)
        if self.in_main:
            self.main_text.append(value)
        if self.heading is not None:
            self.heading[1].append(value)
        if self.link is not None:
            self.link[1].append(value)

    def handle_endtag(self, tag):
        if tag == self.ignored:
            self.ignored = None
        if tag == "main":
            self.in_main = False
        if self.heading and tag == self.heading[0]:
            name = " ".join("".join(self.heading[1]).split())
            if tag == "h1":
                self.headings.append(name)
                self.clinical_page |= bool(re.fullmatch(r"Clinical (?:Psychology|Science)(?: Faculty)?", name, re.I))
            self.set_scope(int(tag[1]), name)
            if tag in {"h2", "h3"}:
                self.person_heading = name
            self.heading = None
        if tag == "a" and self.link:
            url, tokens, accessible_label = self.link
            label = " ".join(("".join(tokens).replace("\u200b", "").strip() or accessible_label).split())
            # Some official program pages publish their faculty list in an accordion.
            if self.control:
                self.set_scope(3, label)
            name = re.sub(r"^(?:Dr\.|Prof\.)\s+", "", label, flags=re.I)
            if self.scopes and normalize(label) in {"faculty profile", "view profile", "view faculty profile", "read bio"}:
                name = self.person_heading
            if urlsplit(url).scheme in ("https", "http") and label:
                self.links.append((url, label))
                if self.in_main:
                    self.main_links.append((url, label))
                if person_link(name, url):
                    if self.scopes and not any(NON_FACULTY_SECTION.search(s) and not re.match(
                            r"faculty who supervise\b", s, re.I) for s in self.scopes.values()):
                        section = self.scopes[max(self.scopes)]
                        context = ("secondary" if any(SECONDARY_SECTION.search(s) and not re.match(
                            r"faculty who supervise\b", s, re.I) for s in self.scopes.values())
                                   else "explicit core" if EXPLICIT_ROSTER.search(section)
                                   else "clinical section")
                        person = dict(name=name, url=url, section=section, rosterContext=context,
                                      status="unreviewed", rosterUrl=self.url)
                        self.people.append(person)
                        if self.in_main:
                            self.main_people.append(person)
                    elif not self.scopes and self.clinical_page:
                        person = dict(name=name, url=url, section="Clinical program page", rosterContext="page only",
                                      status="unreviewed", rosterUrl=self.url)
                        self.fallback_people.append(person)
                        if self.in_main:
                            self.main_fallback_people.append(person)
            self.link = None
            self.control = None

    def set_scope(self, level, label):
        for deeper in [depth for depth in self.scopes if depth >= level]:
            del self.scopes[deeper]
        if re.match(r"faculty who supervise\b", label, re.I):
            self.scopes[level] = label
        elif SECONDARY_SECTION.search(label):
            self.scopes[level] = label
        elif CLINICAL_FACULTY.search(label) or (
                self.clinical_page and re.fullmatch(r"(?:area |program |research )?faculty(?: mentors)?", label, re.I)):
            self.scopes[level] = label


def parse(client, url):
    request_url = quote(url.split("#", 1)[0], safe=":/?&=+@%!$'()*;,[]~")
    body = client.get(request_url, as_json=False)
    if body.startswith(b"%PDF"):
        raise HandbookDocument(body)
    page = Page(url)
    page.feed(body.decode("utf-8", errors="replace"))
    return page


def source_links(page, institution_host):
    found = {}
    for url, label in page.links:
        location = urlsplit(url)
        if (location.hostname or "").removeprefix("www.") != institution_host:
            continue
        if re.search(r"clinical.*faculty|core faculty|area faculty|faculty mentors|program faculty", label, re.I):
            priority = 0
        elif page.clinical_page and re.search(r"\b(?:clinical .*handbook|program handbook|program manual)\b",
                                              normalize(label)):
            priority = 1
        elif re.fullmatch(r"clinical (?:psychology|science)(?: (?:program|area|phd|research|concentration))?", normalize(label)):
            priority = 1
        elif page.clinical_page and normalize(label) in {"faculty", "people", "program faculty"}:
            priority = 2
        else:
            continue
        key = url.split("#", 1)[0]
        found[key] = min(found.get(key, priority), priority)
    return found


def discover(client, program):
    start = program.get("rosterUrl") or program.get("programUrl")
    if not start:
        return dict(program=program["id"], status="official source needed")
    institution = urlsplit(start).hostname.removeprefix("www.")
    pending = {start.split("#", 1)[0]: 0}
    for lead in program.get("rosterSourceLeads", []):
        pending.setdefault(lead.split("#", 1)[0], -1)
    alternate = program.get("programUrl")
    if alternate and urlsplit(alternate).hostname.removeprefix("www.") == institution:
        pending.setdefault(alternate.split("#", 1)[0], 3)
    visited, sources, failures, people, handbook_leads = set(), [], [], {}, []
    while pending and len(visited) < 6:
        url = min(pending, key=pending.get)
        pending.pop(url)
        visited.add(url)
        try:
            page = parse(client, url)
        except HandbookDocument as document:
            try:
                from pypdf import PdfReader
                from pypdf.errors import PyPdfError
            except ImportError:
                failures.append(dict(url=url, reason="Install pypdf for handbook text"))
                continue
            try:
                reader = PdfReader(BytesIO(document.content))
                document_leads = handbook_faculty_pages(
                    (page.extract_text() for page in islice(reader.pages, 30)), url)
            except (PyPdfError, ValueError, OSError) as error:
                failures.append(dict(url=url, reason=f"Handbook text unavailable ({type(error).__name__})"))
                continue
            sources.append(url)
            handbook_leads.extend(document_leads)
            continue
        except SourceUnavailable as error:
            failures.append(dict(url=url, reason=str(error)))
            continue
        sources.append(url)
        roster_names = page.main_people or page.main_fallback_people if page.has_main else page.people or page.fallback_people
        for candidate in roster_names:
            key = (candidate["url"].split("#", 1)[0], candidate["rosterUrl"], candidate["section"])
            people.setdefault(key, candidate)
        for next_url, priority in source_links(page, institution).items():
            if next_url not in visited:
                pending[next_url] = min(pending.get(next_url, priority), priority)
    status = "needs role and completeness review" if sources else "review pending"
    return dict(program=program["id"], status=status, sources=sources,
                candidates=list(people.values()), handbookFacultyLeads=handbook_leads, failedSources=failures)


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--cache", required=True, type=Path)
    cli.add_argument("--output", required=True, type=Path)
    cli.add_argument("--program", action="append")
    cli.add_argument("--workers", type=int, default=8, choices=range(1, 13), metavar="1–12")
    args = cli.parse_args()
    client = Client(args.cache, True)
    programs = [row for row in read(DATA / "programs.json") if not args.program or row["id"] in args.program]
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(lambda program: discover(client, program), programs))
    write(args.output, results)


if __name__ == "__main__":
    main()
