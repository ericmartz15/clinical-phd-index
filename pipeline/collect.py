"""Collect public roster and publication evidence for review without scoring candidates."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import date
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

from common import DATA, normalize, read, short_id, write
from rosters import parse
from sources import Client, SourceUnavailable, oa, oa_record, oa_works, resolve

PROFILE_TOPICS = (
    "addiction", "adolescent", "aging", "alcohol", "anxiety", "autism", "bipolar", "cardiovascular",
    "child development", "cognition", "depression", "eating disorder", "emotion regulation", "grief",
    "health disparities", "insomnia", "memory", "neuroimaging", "neuropsychology", "personality",
    "psychosis", "psychophysiology", "psychotherapy", "schizophrenia", "sleep", "stress", "suicide", "trauma",
)


def profile_evidence(client, candidate):
    result = dict(candidate, profileStatus="name needs confirmation", orcids=[], cvLinks=[], labLinks=[])
    try:
        profile = parse(client, candidate["url"])
    except SourceUnavailable as error:
        return dict(result, profileStatus="unavailable", error=str(error))
    if normalize(candidate["name"]) not in {normalize(heading) for heading in profile.headings}:
        return result

    links = profile.main_links if profile.has_main else profile.links
    orcids = set()
    for url, label in links:
        location = urlsplit(url)
        if location.hostname == "orcid.org" and re.fullmatch(r"/\d{4}-\d{4}-\d{4}-\d{3}[\dX]", location.path):
            orcids.add(location.path[1:])
        if re.search(r"\b(cv|vitae|curriculum vita)\b", normalize(label)):
            result["cvLinks"].append(url)
        if re.search(r"\b(lab|laboratory)\b", normalize(label)):
            result["labLinks"].append(url)
    profile_text = normalize(" ".join(profile.main_text if profile.has_main else profile.body_text))
    topics = [topic for topic in PROFILE_TOPICS if re.search(rf"\b{re.escape(topic)}", profile_text)]
    return dict(result, profileStatus="name confirmed on profile", orcids=sorted(orcids), publicProfileTopics=topics,
                cvLinks=list(dict.fromkeys(result["cvLinks"])), labLinks=list(dict.fromkeys(result["labLinks"])))


def stage_publications(client, candidate, affiliations, start, end):
    """Use institutional profile evidence to resolve an author before retrieving works by ID."""
    result = dict(profile=candidate["url"], name=candidate["name"], status="identity requires review", works=[])
    if candidate["profileStatus"] != "name confirmed on profile" or len(candidate["orcids"]) > 1:
        return result
    try:
        if candidate["orcids"]:
            orcid = candidate["orcids"][0]
            author = oa(client, f"/authors/orcid:{orcid}")
            names = [author.get("display_name"), *(author.get("display_name_alternatives") or [])]
            if short_id(author.get("orcid")) != orcid or normalize(candidate["name"]) not in {normalize(name) for name in names}:
                return result
            aid = short_id(author["id"])
            identity = dict(ids=[aid], confidence=1, evidence=["Official profile and OpenAlex agree on name and ORCID"])
        else:
            topics = candidate["publicProfileTopics"]
            if len(topics) < 2:
                return result
            person = dict(name=candidate["name"], areas=topics, appointments=[
                dict(affiliations=affiliations[pid]) for pid in candidate["programs"]
            ])
            identity = resolve(client, person, None)
            if identity["status"] != "resolved":
                return dict(result, identity=identity)
            aid = identity["ids"][0]
        rows, complete = oa_works(client, aid, start, end)
        works = [work for row in rows if (work := oa_record(row, aid, aid))]
        return dict(result, openAlex=aid, identity=identity,
                    status="retrieved" if complete else "incomplete", works=works)
    except SourceUnavailable as error:
        return dict(result, status="retrieval incomplete", error=str(error))


def audit_leads(sample, faculty, profiles, curated=None):
    people = {person["id"]: person for person in faculty}
    references = []
    for pid in sample:
        person = people[pid]
        names = {normalize(name) for name in [person["name"], *person.get("aliases", [])]}
        programs = {a["program"] for a in person["appointments"]}
        relevant = [profile for profile in profiles if programs.intersection(profile["programs"]) and
                    normalize(profile["name"]) in names and profile["profileStatus"] == "name confirmed on profile"]
        urls = [url for url in (person.get("cv"), person.get("publicationList"), person.get("lab")) if url]
        urls.extend(url for profile in relevant for url in [*profile["cvLinks"], *profile["labLinks"]])
        urls.extend((curated or {}).get(pid, []))
        references.append(dict(person=pid, name=person["name"], sourceLeads=list(dict.fromkeys(urls)),
                               status="verify public bibliography" if urls else "independent reference source needed"))
    return references


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rosters", required=True, type=Path)
    parser.add_argument("--cache", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    rosters = read(args.rosters)
    seen = {}
    for roster in rosters:
        for row in roster.get("candidates", []):
            key = row["url"].split("#")[0]
            entry = seen.setdefault(key, dict(row, url=key, programs=[]))
            if roster["program"] not in entry["programs"]:
                entry["programs"].append(roster["program"])

    client = Client(args.cache, True)
    with ThreadPoolExecutor(max_workers=8) as executor:
        profiles = list(executor.map(lambda row: profile_evidence(client, row), seen.values()))
    collected = []
    if os.environ.get("OPENALEX_API_KEY"):
        affiliations = {row["id"]: [row["name"], *row.get("affiliations", [])]
                        for row in read(DATA / "programs.json")}
        as_of = date.today()
        start = as_of.replace(year=as_of.year - 10, day=min(as_of.day, 28) if as_of.month == 2 else as_of.day)
        with ThreadPoolExecutor(max_workers=2) as executor:
            collected = list(executor.map(
                lambda row: stage_publications(client, row, affiliations, start.isoformat(), as_of.isoformat()), profiles))

    faculty = read(DATA / "faculty-input.json")
    report = read(DATA / "recall-report.json")
    references = audit_leads(report["sample"], faculty, profiles, read(DATA / "audit-source-leads.json", {}))
    summary = dict(
        programs=len(rosters),
        sourcesReached=sum(bool(roster.get("sources")) for roster in rosters),
        programsWithCandidateLinks=sum(bool(roster.get("candidates")) for roster in rosters),
        uniqueProfiles=len(profiles),
        namesConfirmed=sum(row["profileStatus"] == "name confirmed on profile" for row in profiles),
        publicationsRetrieved=sum(len(row["works"]) for row in collected),
        publicationIdentitiesRetrieved=sum(row["status"] == "retrieved" for row in collected),
        auditSample=len(references),
        auditMembersWithSourceLeads=sum(bool(row["sourceLeads"]) for row in references),
        openAlexEnabled=bool(os.environ.get("OPENALEX_API_KEY")),
    )
    for name, result in [("faculty-evidence", profiles), ("publication-candidates", collected),
                         ("audit-reference-leads", references), ("collection-summary", summary)]:
        write(args.output / f"{name}.json", result)
    print(summary)


if __name__ == "__main__":
    main()
