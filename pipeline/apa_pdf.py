"""Extract clinical PhD candidates from a dated APA doctoral list PDF."""
import argparse
from collections import Counter
from pathlib import Path
import re

from common import write


def extract(pages, source, as_of):
    records = []
    for page in pages:
        lines = page.splitlines()
        blocks = [[(hit.start(), hit[0].strip()) for hit in
                   re.finditer(r"\S.*?(?= {3,}|$)", line)] for line in lines]
        counter = Counter(start for line in blocks for start, _ in line if start > 35)
        starts = sorted([0, *[start for start, frequency in counter.most_common(2)]])
        if len(starts) != 3:
            continue
        for j, start in enumerate(starts):
            column = [line[start:starts[j+1] if j+1<len(starts) else None].strip() for line in lines]
            previous = []
            for i, line in enumerate(column):
                if not line or line.isupper() and len(line) < 28 or line.startswith(("Next site", "Accredited")):
                    previous = []
                elif re.match(r"^\([^)]*(?:Ph\.\s?D\.|Psy\.\s?D\.)", line, re.I):
                    if "clinical" in line.lower() and re.search(r"Ph\.\s?D\.", line, re.I) and previous:
                        name = " ".join(previous)
                        locality = next((re.search(r"(.+?),\s+([A-Z]{2})\s+\d{5}", s)
                                         for s in column[i+1:i+10] if re.search(r",\s+[A-Z]{2}\s+\d{5}", s)),None)
                        status = next((s for s in column[i+1:i+12] if s.lower().startswith("accredited")), "")
                        records.append(dict(name=name, degree="PhD", clinical=True, track=line.strip("()"),
                                            city=locality[1] if locality else "", state=locality[2] if locality else "",
                                            status=status, source=source, asOf=as_of))
                    previous = []
                else:
                    previous.append(line)
    return [row for row in records if not row["name"].startswith(("Next site", "Accredited", "November"))]


def main():
    from pypdf import PdfReader
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("pdf", type=Path)
    p.add_argument("output", type=Path)
    p.add_argument("--source", required=True)
    p.add_argument("--as-of", required=True)
    args=p.parse_args()
    reader=PdfReader(args.pdf)
    write(args.output, extract((page.extract_text(extraction_mode="layout") for page in reader.pages), args.source, args.as_of))


if __name__ == "__main__":
    main()
