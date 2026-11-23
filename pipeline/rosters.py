"""Propose potential official faculty pages and link-text candidates for manual verification."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from pathlib import Path
import re
from urllib.parse import urljoin, urlsplit

from common import DATA, normalize, read, write
from sources import Client, SourceUnavailable

SECONDARY_SECTION = re.compile(
    r"\b(emerit\w*|retired|adjunct|affiliat\w*|students?|postdocs?|staff|associates?)\b", re.I
)
CLINICAL_FACULTY = re.compile(
    r"\bclinical(?: science| psychology)? (?:faculty|mentors?)\b|"
    r"\bfaculty (?:who supervise|mentors?)\b|\b(?:core|training) faculty\b", re.I
)


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
    return bool(re.search(r"/(?:person|people|faculty|faculty-page|profiles?|staff|directory|our-faculty)/[^/]+", path))


class Page(HTMLParser):
    def __init__(self, url):
        super().__init__(convert_charrefs=True)
        self.url = url
        self.heading = None
        self.link = None
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
        if tag in ("script", "style"):
            self.ignored = tag
        if re.fullmatch(r"h[1-6]", tag):
            self.heading = [tag, []]
        if tag == "a":
            attributes = dict(attrs)
            href = attributes.get("href") or ""
            self.control = attributes.get("aria-controls") if attributes.get("role") == "button" else None
            self.link = [urljoin(self.url, href), []]

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
            self.heading = None
        if tag == "a" and self.link:
            url, tokens = self.link
            label = " ".join("".join(tokens).split())
            # Some official program pages publish their faculty list in an accordion.
            if self.control:
                self.set_scope(3, label)
            name = re.sub(r"^(?:Dr\.|Prof\.)\s+", "", label, flags=re.I)
            if urlsplit(url).scheme in ("https", "http") and label:
                self.links.append((url, label))
                if self.in_main:
                    self.main_links.append((url, label))
                if all(self.scopes.values()) and person_link(name, url):
                    if self.scopes:
                        person = dict(name=name, url=url, section=self.scopes[max(self.scopes)],
                                      status="unreviewed", rosterUrl=self.url)
                        self.people.append(person)
                        if self.in_main:
                            self.main_people.append(person)
                    elif self.clinical_page:
                        person = dict(name=name, url=url, section="Clinical program page",
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
            self.scopes[level] = None
        elif CLINICAL_FACULTY.search(label) or (
                self.clinical_page and re.fullmatch(r"(?:area |program |research )?faculty(?: mentors)?", label, re.I)):
            self.scopes[level] = label


def parse(client, url):
    body = client.get(url.split("#",1)[0], as_json=False)
    if body.startswith(b"%PDF"):
        raise SourceUnavailable("PDF roster requires manual review")
    page = Page(url)
    page.feed(body.decode("utf-8", errors="replace"))
    return page


def source_links(page, institution_host):
    found = {}
    for url, label in page.links:
        location = urlsplit(url)
        if (location.hostname or "").removeprefix("www.") != institution_host:
            continue
        if re.search(r"clinical.*faculty|core faculty|area faculty|faculty mentors", label, re.I):
            priority = 0
        elif re.fullmatch(r"clinical (?:psychology|science)(?: (?:program|area|phd))?", normalize(label)):
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
    pending = {start.split("#", 1)[0]: -1}
    alternate = program.get("programUrl")
    if alternate and urlsplit(alternate).hostname.removeprefix("www.") == institution:
        pending.setdefault(alternate.split("#", 1)[0], 3)
    visited, sources, failures, people = set(), [], [], {}
    while pending and len(visited) < 6:
        url = min(pending, key=pending.get)
        pending.pop(url)
        visited.add(url)
        try:
            page = parse(client, url)
        except SourceUnavailable as error:
            failures.append(dict(url=url, reason=str(error)))
            continue
        sources.append(url)
        roster_names = page.main_people or page.main_fallback_people if page.has_main else page.people or page.fallback_people
        for candidate in roster_names:
            people.setdefault(candidate["url"], candidate)
        for next_url, priority in source_links(page, institution).items():
            if next_url not in visited:
                pending[next_url] = min(pending.get(next_url, priority), priority)
    status = "needs role and completeness review" if sources else "review pending"
    return dict(program=program["id"], status=status, sources=sources,
                candidates=list(people.values()), failedSources=failures)


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
