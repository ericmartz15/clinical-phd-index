"""Propose potential official faculty pages and link-text candidates for manual verification."""
import argparse
from html.parser import HTMLParser
from pathlib import Path
import re
from urllib.parse import urljoin, urlsplit

from common import DATA, read, write
from sources import Client, SourceUnavailable


class Page(HTMLParser):
    def __init__(self, url):
        super().__init__(convert_charrefs=True)
        self.url = url
        self.heading = None
        self.link = None
        self.section = ""
        self.links, self.people = [], []

    def handle_starttag(self, tag, attrs):
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
        if self.heading and tag == self.heading[0]:
            name = " ".join("".join(self.heading[1]).split())
            if re.search(r"clinical|core|emerit|adjunct|staff|students|postdoc|training faculty", name, re.I):
                self.section = name
            self.heading = None
        if tag == "a" and self.link:
            url, tokens = self.link
            label = " ".join("".join(tokens).split())
            if urlsplit(url).scheme in ("https", "http") and label:
                self.links.append((url, label))
                if (re.search(r"clinical|core|training faculty", self.section, re.I) and
                    not re.search(r"emerit|adjunct|students|postdoc", self.section, re.I) and
                    1 < len(label.split()) <= 6 and re.search(r"/(?:people|faculty|profiles?|staff)/", url, re.I)):
                    self.people.append(dict(name=label, url=url, section=self.section, status="unreviewed"))
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
        pages = [start, *dict.fromkeys(url for url in discovered[:3] if url != start)]
        people = {p["url"]: p for p in page.people}
        for url in pages[1:]:
            try:
                people.update({p["url"]: p for p in parse(client, url).people})
            except SourceUnavailable:
                continue
        return dict(program=program["id"], status="needs role and completeness review", sources=pages,
                    candidates=list(people.values()))
    except SourceUnavailable as error:
        return dict(program=program["id"], status="review pending", source=start, reason=str(error))


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--cache", required=True, type=Path)
    cli.add_argument("--output", required=True, type=Path)
    cli.add_argument("--program", action="append")
    args = cli.parse_args()
    client = Client(args.cache, True)
    programs = read(DATA / "programs.json")
    write(args.output, [discover(client, row) for row in programs if not args.program or row["id"] in args.program])


if __name__ == "__main__":
    main()
