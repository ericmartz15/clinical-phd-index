"""Resolve authors, index works, attribute credits, and publish a static snapshot."""
import argparse
from collections import defaultdict
import csv
from datetime import date
from hashlib import sha256
import json
from pathlib import Path
import random

from common import (DATA, ROOT, CORE_ROLES, creditable_appointments, doi_key, merge,
                    normalize, read, weight, write)
from sources import (Client, SourceUnavailable, crossref, crossref_discover, oa_record, oa_works,
                     psyarxiv, publisher, pubmed, resolve, semantic, semantic_discover)


def pull_person(client, person, reviewed, start, end, crosschecks, partial, crosscheck_limit):
    reused = []
    errors, review, records = {}, [], []
    try:
        identity = resolve(client, person, reviewed.get(person["id"]))
    except SourceUnavailable as error:
        identity = dict(ids=[], status="unavailable", confidence=0, evidence=[])
        errors["identity"] = str(error)
    if not identity["ids"]:
        return dict(person, identity=identity, retrieval=dict(openAlex="unresolved", errors=errors)), records, review
    complete = not identity.get("splitReview")
    for author in identity["ids"]:
        try:
            items, finished = oa_works(client, author, start, end, allow_partial=partial)
            complete &= finished
            records.extend(work for row in items if (work := oa_record(row, author, person["id"])))
        except SourceUnavailable as error:
            complete = False
            errors["OpenAlex"] = str(error)
    provider_state = {key: "not run" for key in ["PubMed", "Crossref", "Semantic Scholar", "PsyArXiv"]}
    if crosschecks:
        discovery = {}
        if crosscheck_limit is None:
            for label, adapter in (("Crossref", crossref_discover), ("Semantic Scholar", semantic_discover)):
                try:
                    candidates, done = adapter(client, person, start, end)
                    reused.extend(candidates)
                    discovery[label] = done
                except SourceUnavailable as error:
                    errors[label + " author query"] = str(error)
                    discovery[label] = False
        known = {w["doi"]: w["date"] for w in records if w["doi"]}
        for label, adapter in [("PubMed", pubmed), ("PsyArXiv", psyarxiv)]:
            try:
                result = adapter(client, person, start, end, known) if label == "PubMed" else adapter(client, person, start, end)
                if label == "PubMed":
                    result, review = result
                reused.extend(result)
                provider_state[label] = "queried"
            except SourceUnavailable as error:
                errors[label] = str(error)
                provider_state[label] = "incomplete"
        seen_dois = set()
        for item in [*records, *reused]:
            if not item["doi"] or item["doi"] in seen_dois:
                continue
            if crosscheck_limit is not None and len(seen_dois) >= crosscheck_limit:
                for label in ("Crossref", "Semantic Scholar"):
                    if label not in errors:
                        provider_state[label] = "sampled"
                break
            seen_dois.add(item["doi"])
            if item.get("pmcid"):
                try:
                    byline = publisher(client, item)
                    if byline:
                        reused.append(byline)
                except SourceUnavailable:
                    pass
            for label, adapter in [("Crossref", crossref), ("Semantic Scholar", semantic)]:
                if label in errors:
                    continue
                try:
                    match = adapter(client, item)
                    if match:
                        reused.append(match)
                    provider_state[label] = "queried"
                except SourceUnavailable as error:
                    # Missing provider record is expected. Connection and budget errors affect completion.
                    if "HTTP 404" not in str(error):
                        errors[label] = str(error)
                        provider_state[label] = "incomplete"
        for label, done in discovery.items():
            if not done and provider_state[label] != "incomplete":
                provider_state[label] = "partial author discovery"
    return dict(person, identity=identity, retrieval=dict(
        openAlex="complete" if complete else "partial", crosschecks=provider_state, errors=errors)), records + reused, review


def classify(work, venues):
    for group, rows in venues.items():
        if any(set(issns).intersection(work["issns"]) for _, *issns in rows):
            return group
    return "other"


def assign(works, faculty, venues, issues):
    people = {person["id"]: person for person in faculty}
    for work in works:
        work["venueTier"] = classify(work, venues)
        work["credit"] = []
        for pid, metadata in work["people"].items():
            person = people.get(pid)
            if not person or person.get("identity", {}).get("status") != "resolved":
                continue
            if not work["date"]:
                issues.append(dict(person=pid, doi=work["doi"], issue="Official publication date unavailable"))
                continue
            appointments = creditable_appointments(person, work["date"], metadata.get("affiliations", []))
            if not appointments:
                issues.append(dict(person=pid, doi=work["doi"], issue="Eligible appointment at publication unverified"))
                continue
            unique_programs = {a["program"]: a for a in appointments}
            byline = work.get("authorCount")
            if not byline:
                issues.append(dict(person=pid, doi=work["doi"], issue="Author count unavailable"))
                continue
            weighted = weight(byline, metadata.get("position"), work.get("firstPositions", []), work.get("lastPositions", []))
            if weighted is None:
                issues.append(dict(person=pid, doi=work["doi"], issue="Ordered byline unavailable for weighted credit"))
            for program, appointment in unique_programs.items():
                current = any(a["program"] == program and a.get("current") and a["role"] in CORE_ROLES
                              for a in person["appointments"])
                work["credit"].append(dict(person=pid, program=program, current=current,
                            fractional=1 / (byline * len(unique_programs)),
                            weighted=weighted / len(unique_programs) if weighted is not None else None,
                            appointmentSource=appointment["source"]))
    return works


def audit(faculty, works, references, seed, stage, start, end, excluded=()):
    """Sample before collecting references, then compare eligible reference rows to the frozen output."""
    cohort = sorted({row["id"] for row in faculty if any(a.get("current") and a["role"] in CORE_ROLES
                                                         for a in row.get("appointments", []))})
    sampling_frame = [person for person in cohort if person not in excluded]
    sample = random.Random(seed).sample(sampling_frame, min(50, len(sampling_frame)))
    included = defaultdict(set)
    for work in works:
        if work["kind"] != "journal" or not start <= work["date"] <= end:
            continue
        for person in work["people"]:
            if not work["databaseMissing"]:
                included[person].update([doi_key(work["doi"]), normalize(work["title"]) + ":" + work["date"][:4]])
    rows, unavailable, missing = [], [], []
    matches = total = 0
    for pid in sample:
        if pid not in references or references[pid].get("complete") is not True:
            unavailable.append(pid)
            continue
        person_ref = references[pid]
        papers = person_ref.get("papers", [])
        for paper in papers:
            if paper.get("kind") != "journal" or not start <= paper["date"] <= end:
                continue
            keys = {doi_key(paper.get("doi")), normalize(paper["title"]) + ":" + paper["date"][:4]} - {""}
            found = bool(keys & included[pid])
            total += 1
            matches += found
            outcome = dict(person=pid, title=paper["title"], doi=paper.get("doi"), date=paper["date"],
                           reference=person_ref.get("source"), matched=found)
            rows.append(outcome)
            if not found:
                missing.append(dict(outcome, reason=paper.get("diagnosis") or "Diagnosis pending"))
    # Cluster bootstrap samples faculty rather than treating a prolific author's papers as independent.
    interval = None
    audited = len(sample) - len(unavailable)
    if total and audited >= 2:
        scores = [(sum(r["matched"] for r in rows if r["person"] == pid), sum(r["person"] == pid for r in rows))
                  for pid in sample if pid not in unavailable]
        rng = random.Random(seed + 1)
        estimates = []
        for _ in range(1000):
            replay = rng.choices(scores, k=len(scores))
            denom = sum(n for _, n in replay)
            if denom:
                estimates.append(sum(n for n, _ in replay) / denom)
        if estimates:
            estimates.sort()
            interval = [estimates[int((len(estimates) - 1) * p)] for p in (.025, .975)]
    recall = matches / total if total else None
    validation = stage == "holdout" and bool(excluded) and len(sample) == 50 and audited == 50 and recall is not None
    return dict(status="measured" if validation else "pending", stage=stage, target=.95, seed=seed,
                window=[start, end], eligibleFaculty=len(cohort), samplingFrame=len(sampling_frame),
                excludedCalibration=list(excluded), sample=sample, audited=audited,
                unavailable=unavailable, matches=matches, denominator=total, recall=recall if validation else None,
                diagnosticRecall=recall, interval=interval if validation else None, targetMet=validation and recall >= .95,
                missing=missing, rows=rows)


def diagnostic_check(works, reference):
    if not reference:
        return None
    eligible = [work for work in works if reference["person"] in work["people"] and
                work["kind"] == "journal" and not work["databaseMissing"]]
    titles = {normalize(work["title"]): work for work in eligible}
    dois = {work["doi"]: work for work in eligible if work["doi"]}
    rows = []
    for paper in reference["papers"]:
        exact = dois.get(doi_key(paper.get("doi")))
        work = exact or titles.get(normalize(paper["title"]))
        found = bool(exact or (work and work["date"] and abs(int(work["date"][:4]) - paper["year"]) <= 1))
        rows.append(dict(paper, matched=found, matchedDoi=work["doi"] if found else None,
                         diagnosis=None if found else "Review database indexing, title variants, and author profile splits"))
    return dict(person=reference["person"], scope=reference["scope"], source=reference["source"],
                iterations=reference.get("iterations", []),
                matches=sum(row["matched"] for row in rows), denominator=len(rows), rows=rows)


def csv_export(path, rows, fieldnames):
    with path.open("w", newline="", encoding="utf-8") as out:
        writer = csv.DictWriter(out, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: json.dumps(row.get(key), ensure_ascii=False) if isinstance(row.get(key), (list, dict))
                             else row.get(key, "") for key in fieldnames})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", default=date.today().isoformat())
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--network", action="store_true")
    parser.add_argument("--allow-partial", action="store_true")
    parser.add_argument("--skip-crosschecks", action="store_true")
    parser.add_argument("--previous-snapshot", type=Path,
                        help="Retain prior evidence for unchanged faculty inputs during a primary-only refresh")
    parser.add_argument("--crosscheck-limit", type=int, help="Limit DOI lookups per author for diagnostics")
    parser.add_argument("--audit-seed", type=int, default=291756)
    parser.add_argument("--audit-stage", choices=("calibration", "holdout"), default="calibration")
    parser.add_argument("--calibration-report", type=Path, help="Exclude the calibration sample from the holdout frame")
    parser.add_argument("--report-target", default="")
    args = parser.parse_args()
    now = date.fromisoformat(args.as_of)
    def ago(n):
        return now.replace(year=now.year - n, day=min(now.day, 28) if now.month == 2 else now.day).isoformat()
    start = ago(10)
    client = Client(args.cache, args.network)
    venues = read(DATA / "venues.json")
    programs = read(DATA / "programs.json")
    faculty_input = read(DATA / "faculty-input.json")
    reviewed = read(DATA / "reviewed-identities.json", {})
    independent = read(DATA / "reviewed-works.json", [])
    reference = read(DATA / "audit-references.json", {})
    previous = args.previous_snapshot
    if previous and not args.skip_crosschecks:
        parser.error("--previous-snapshot requires --skip-crosschecks")
    records, faculty, queue = list(independent), [], []
    prior_people = {}
    if previous:
        prior_people = {person["id"]: person for person in read(previous / "faculty.json")}
        unchanged = {person["id"] for person in faculty_input if person["id"] in prior_people and
                     all(prior_people[person["id"]].get(key) == value for key, value in person.items()) and
                     (person["id"] not in reviewed or prior_people[person["id"]]["identity"] == reviewed[person["id"]]) and
                     (not args.network or person["id"] in reviewed)}
        prior_people = {pid: prior_people[pid] for pid in unchanged}
        for work in read(previous / "publications.json"):
            retained = {pid: evidence for pid, evidence in work["people"].items() if pid in unchanged}
            if retained and start <= work["date"] <= args.as_of:
                records.append(dict(work, people=retained))
        queue.extend(row for row in read(previous / "review-queue.json", {}).get("publicationCandidates", [])
                     if row.get("person") in unchanged)
    for person in faculty_input:
        if person["id"] in prior_people:
            faculty.append(prior_people[person["id"]])
            continue
        result, works, review = pull_person(client, person, reviewed, start, args.as_of,
                                            not args.skip_crosschecks, args.allow_partial, args.crosscheck_limit)
        print(person["name"], result["identity"]["status"], result["retrieval"]["openAlex"], len(works), flush=True)
        faculty.append(result)
        records.extend(works)
        queue.extend(review)
    attribution = []
    exclusions = {(row["source"], row["id"]) for row in read(DATA / "reviewed-exclusions.json", [])}
    works = assign([work for work in merge(records) if not any(
        (source["source"], source.get("id")) in exclusions for source in work["provenance"]
    )], faculty, venues, attribution)
    calibration = read(args.calibration_report, {}) if args.calibration_report else {}
    excluded = calibration.get("sample", []) if calibration.get("stage") == "calibration" else []
    report = audit(faculty, works, reference, args.audit_seed, args.audit_stage, ago(5), args.as_of, excluded)
    report["corpusSha256"] = sha256(json.dumps(sorted([
        (w.get("doi", ""), w["title"], w["date"], tuple(sorted(w["people"]))) for w in works
    ]), ensure_ascii=False).encode()).hexdigest()
    report["diagnostic"] = diagnostic_check(works, read(DATA / "audit-diagnostic.json"))
    report["diagnosticChecks"] = [diagnostic_check(works, item)
                                  for item in read(DATA / "audit-diagnostic-checks.json", [])]
    indexed = {p["id"] for p in faculty if p["retrieval"]["openAlex"] == "complete"}
    crosschecked = {p["id"] for p in faculty if all(state == "queried" for state in p["retrieval"].get("crosschecks", {}).values())
                    and p["retrieval"].get("crosschecks")}
    backlog = [dict(pid=p["id"], **p["identity"]) for p in faculty if p["identity"]["status"] != "resolved" or p["identity"].get("splitReview")]
    roster_coverage = {p["id"]:p.get("rosterReview")=="complete" for p in programs}
    rankable = []
    for p in programs:
        linked = {person["id"] for person in faculty if any(a["program"] == p["id"] for a in person["appointments"])}
        p["facultyCount"] = len(linked)
        p["retrievalComplete"] = len(linked & indexed)
        p["rankable"] = (report["targetMet"] and roster_coverage[p["id"]] and bool(linked) and
                         linked <= indexed and linked <= crosschecked and p["eligibility"] in {"directory eligible", "active"})
        if p["rankable"]:
            rankable.append(p["id"])
    snapshot = dict(asOf=args.as_of, start=start, programs=programs, faculty=faculty, works=works, venues=venues,
                    directories=read(DATA / "directory-status.json"), report=report,
                    counts=dict(rosterVerified=sum(roster_coverage.values()),
                                currentRostersVerified=sum(p.get("currentRosterReview", {}).get("status") == "complete" for p in programs),
                                faculty=len(faculty), resolved=sum(p["identity"]["status"]=="resolved" for p in faculty),
                                complete=len(indexed), works=len(works), ranked=len(rankable)),
                    reportTarget=args.report_target)
    write(DATA / "faculty.json", faculty)
    write(DATA / "publications.json", works)
    write(DATA / "recall-report.json", report)
    write(DATA / "review-queue.json", dict(authorIdentities=backlog, publicationCandidates=queue, attribution=attribution))
    csv_export(DATA / "faculty.csv", faculty, ["id", "name", "aliases", "areas", "lab", "cv", "orcid", "appointments", "identity", "retrieval"])
    csv_export(DATA / "publications.csv", works, ["title", "doi", "date", "kind", "venue", "venueTier", "subfields", "authorCount", "provenance", "credit", "databaseMissing"])
    (ROOT / "publication-data.js").write_text("const publicationData = " +
        json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")) + ";\n", encoding="utf-8")
    print(f"{len(programs)} programs, {len(faculty)} reviewed faculty records, {len(works)} works, {len(rankable)} publishable rankings.")


if __name__ == "__main__":
    main()
