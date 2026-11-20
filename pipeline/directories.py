"""Extract directory records and collect roster-page diffs for review."""
import argparse
from datetime import date
from hashlib import sha256
from html import unescape
import re
from urllib.parse import urlparse

from common import DATA, DIRECTORIES, normalize, read, write
from sources import Client, SourceUnavailable


def plain(html):
    return " ".join(unescape(re.sub(r"<[^>]*>", " ", html)).split())


def first_url(html):
    hit = re.search(r'href=["\'](https?://[^"\']+)["\']', html, re.I)
    return unescape(hit[1]) if hit else ""


def pcsas_rows(html):
    rows = []
    for row in re.findall(r"<tr\b[^>]*>(.*?)</tr>", html, re.I | re.S):
        cells = re.findall(r"<td\b[^>]*>(.*?)</td>", row, re.I | re.S)
        if len(cells) < 3 or "accredited" not in plain(cells[2]).lower() or not first_url(cells[0]):
            continue
        rows.append(dict(name=plain(cells[0]), status=plain(cells[2]).replace(" ,", ","),
                         programUrl=first_url(cells[0]), source=DIRECTORIES["PCSAS"], directory="PCSAS"))
    return rows


def cpa_rows(html):
    rows = []
    for section in ("clinical", "clinicalneuro"):
        start = re.search(r'id="' + section + r'"', html, re.I)
        if not start:
            continue
        fragment = html[start.end():].split("</table>", 1)[0]
        items = re.split(r"<h4\b[^>]*>(.*?)</h4>", fragment, flags=re.I | re.S)
        for title, context in zip(items[1::2], items[2::2]):
            name, url = plain(title), first_url(title) or first_url(context)
            if not url or "psyd" in normalize(name + " " + url).replace(" ", ""):
                continue
            status_match = re.search(r"Accredited(?:\s*(?:on contingency|– inactive|\(inactive\)))?", plain(context), re.I)
            if status_match:
                rows.append(dict(name=name, status=status_match[0], programUrl=url,
                                 source=DIRECTORIES["CPA"], directory="CPA",
                                 track="Clinical neuropsychology" if section=="clinicalneuro" else "Clinical psychology"))
    return rows


def roster_check(client, programs, snapshot):
    prior = read(snapshot, {})
    checks = dict(prior)
    events = []
    for program in programs:
        source = program.get("rosterUrl")
        if not source:
            continue
        try:
            body = client.get(source.split("#", 1)[0], as_json=False)
            html = body.decode("utf-8", errors="replace")
            visible = plain(re.sub(r"<(script|style)\b[^>]*>.*?</\1>", "", html, flags=re.I | re.S))
            digest = sha256((visible.encode() if "<html" in html[:2048].lower() else body)).hexdigest()
            old = prior.get(program["id"], {})
            status = ("changed" if old.get("hash") != digest else old.get("status", "unchanged")) if old else "baseline recorded"
            checks[program["id"]] = dict(hash=digest, status=status, checked=date.today().isoformat(), url=source)
            if status == "changed":
                events.append(dict(program=program["id"], status=status, source=source))
        except SourceUnavailable as error:
            events.append(dict(program=program["id"], status="unavailable", reason=str(error), source=source))
    write(snapshot, checks)
    return events


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--cache", required=True, type=__import__("pathlib").Path)
    cli.add_argument("--output", required=True, type=__import__("pathlib").Path)
    cli.add_argument("--roster-state", type=__import__("pathlib").Path)
    cli.add_argument("--apa-export", type=__import__("pathlib").Path, help="Normalized official APA doctoral directory export")
    cli.add_argument("--cudcp-export", type=__import__("pathlib").Path, help="Normalized official CUDCP member directory export")
    args = cli.parse_args()
    client = Client(args.cache, True)
    rows, status = [], {}
    for name, parser in [("PCSAS", pcsas_rows), ("CPA", cpa_rows)]:
        try:
            extracted = parser(client.get(DIRECTORIES[name], as_json=False).decode("utf-8"))
            if not extracted:
                raise SourceUnavailable("No matching directory entries parsed")
            rows.extend(extracted)
            status[name] = dict(rows=len(extracted), source=DIRECTORIES[name])
        except SourceUnavailable as error:
            status[name] = dict(reason=str(error), source=DIRECTORIES[name])
    for name, path in [("APA", args.apa_export), ("CUDCP", args.cudcp_export)]:
        entries = read(path, []) if path else []
        for row in entries:
            if row.get("degree") == "PhD" and row.get("clinical") is True:
                rows.append(dict(row, directory=name, source=row.get("source") or DIRECTORIES[name]))
        status[name] = dict(rows=sum(row["directory"] == name for row in rows), source=DIRECTORIES[name])
        if not entries:
            status[name]["reason"] = "Official directory export pending"
    output = dict(checked=date.today().isoformat(), directories=status, candidates=rows)
    if args.roster_state:
        output["rosterReview"] = roster_check(client, read(DATA / "programs.json", []), args.roster_state)
    write(args.output, output)


if __name__ == "__main__":
    main()
