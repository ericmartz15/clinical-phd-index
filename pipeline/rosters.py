"""Propose potential official faculty pages and link-text candidates for manual verification."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from pathlib import Path
import re
from urllib.parse import urljoin, urlsplit

from common import DATA, normalize, read, write
from sources import Client, SourceUnavailable


def person_link(label, url):
    parts = normalize(label).split()
    path = urlsplit(url).path.lower()
    if not 2 <= len(parts) <= 7 or re.search(
            r"\b(faculty|directory|student|admission|clinical|department|program|staff|people|psychology)\b",
            normalize(label)):
        return False
    if label != label.title() and not all(word[0].isupper() for word in label.split() if word.isalpha()):
        return False
    return bool(re.search(r"/(?:people|faculty|faculty-page|profiles?|staff)/[^/]+", path))


class Page(HTMLParser):
    def __init__(self, url):
        super().__init__(convert_charrefs=True)
        self.url = url
        self.heading = None
        self.link = None
        self.section = ""
        self.in_main = self.has_main = False
        self.headings, self.main_links, self.main_people = [], [], []
        self.links, self.people = [], []

    def handle_starttag(self, tag, attrs):
        if tag == "main":
            self.has_main = self.in_main = True
            self.section = ""
        if re.fullmatch(r"h[1-6]", tag):
            self.heading = [tag, []]
        if tag == "a":
            href = dict(attrs).get("href") or ""
            self.link = [urljoin(self.url, href), []]

    def handle_data(self, value):
        if self.heading is not None:
            self.heading[1].append(value)
        if self.link is not None:
            self.link[1].append(value)

    def handle_endtag(self, tag):
        if tag == "main":
            self.in_main = False
        if self.heading and tag == self.heading[0]:
            name = " ".join("".join(self.heading[1]).split())
            if tag == "h1":
                self.headings.append(name)
            if re.search(r"clinical|core|emerit|adjunct|staff|students|postdoc|training faculty", name, re.I):
                self.section = name
            self.heading = None
        if tag == "a" and self.link:
            url, tokens = self.link
            label = " ".join("".join(tokens).split())
            if urlsplit(url).scheme in ("https", "http") and label:
                self.links.append((url, label))
                if self.in_main:
                    self.main_links.append((url, label))
                if (re.search(r"clinical|core|training faculty", self.section, re.I) and
                    not re.search(r"emerit|adjunct|students|postdoc", self.section, re.I) and
                    person_link(label, url)):
                    person = dict(name=label, url=url, section=self.section, status="unreviewed", rosterUrl=self.url)
                    self.people.append(person)
                    if self.in_main:
                        self.main_people.append(person)
            self.link = None


def parse(client, url):
    body = client.get(url.split("#",1)[0], as_json=False)
    if body.startswith(b"%PDF"):
        raise SourceUnavailable("PDF roster requires manual review")
    page = Page(url)
    page.feed(body.decode("utf-8", errors="replace"))
    return page


def discover(client, program):
    start = program.get("rosterUrl") or program.get("programUrl")
    if not start:
        return dict(program=program["id"], status="official source needed")
    try:
        page = parse(client, start)
        discovered = [doc for doc, label in page.links if
            urlsplit(doc).hostname == urlsplit(start).hostname and
            re.search(r"clinical.*faculty|core faculty|area faculty|faculty mentors", label, re.I)]
        pages = [start, *list(dict.fromkeys(url.split("#", 1)[0] for url in discovered
                                           if url.split("#", 1)[0] != start.split("#", 1)[0]))[:4]]
        people = {p["url"]: p for p in (page.main_people if page.has_main else page.people)}
        failures = []
        for url in pages[1:]:
            try:
                child = parse(client, url)
                people.update({p["url"]: p for p in (child.main_people if child.has_main else child.people)})
            except SourceUnavailable as error:
                failures.append(dict(url=url, reason=str(error)))
        return dict(program=program["id"], status="needs role and completeness review", sources=pages,
                    candidates=list(people.values()), failedSources=failures)
    except SourceUnavailable as error:
        return dict(program=program["id"], status="review pending", source=start, reason=str(error))


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
