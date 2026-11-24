"use strict";

const $ = id => document.getElementById(id);
const data = publicationData;
const escapeHtml = value => String(value ?? "").replace(/[&<>"']/g, character => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
})[character]);
const normalized = value => String(value).toLowerCase().normalize("NFKD")
  .replace(/\p{M}/gu, "").replace(/[^a-z0-9]+/g, " ").trim();
const displayScore = value => value.toLocaleString("en-US", {minimumFractionDigits: 2, maximumFractionDigits: 2});

function statusBadges(program) {
  return Object.entries(program.statuses).map(([name, detail]) =>
    `<span class="status-badge" title="${escapeHtml(detail.label)}">${escapeHtml(name)}</span>`).join("");
}

function programCoverage(program) {
  if (program.rankable) return "Coverage verified";
  if (program.facultyCount) return "Partial publication coverage";
  return program.eligibility === "historical" ? "Historical entry" : "Coverage pending";
}

function publicationUrl(work) {
  if (work.doi) return "https://doi.org/" + encodeURI(work.doi);
  return work.provenance[0]?.url || "";
}

function initializeRankings() {
  const filter = $("filters");
  const start = $("start-year");
  const end = $("end-year");
  const minimum = Number(data.start.slice(0, 4));
  const maximum = Number(data.asOf.slice(0, 4));
  const pageSize = 25;
  const hasRankings = data.programs.some(program => program.rankable);
  const programsWithPapers = new Set(data.works.flatMap(work => work.credit.map(credit => credit.program)));
  const defaultSort = hasRankings ? "score" : "coverage";
  let page = 1;
  let rows = [];
  $("sort").value = defaultSort;
  $("ranking-title").textContent = hasRankings ? "Program rankings" : "Publication preview";
  $("heading-rank").hidden = !hasRankings;
  $("ranked-only").disabled = !hasRankings;
  $("ranked-only").closest("label").hidden = !hasRankings;
  $("paper-stat").textContent = data.counts.works.toLocaleString("en-US");
  $("program-stat").textContent = data.programs.length;
  $("journal-stat").textContent = data.counts.faculty;
  $("year-stat").textContent = `${minimum}–${maximum}`;
  for (const field of [start, end]) {
    field.min = minimum;
    field.max = maximum;
    field.defaultValue = field === start ? minimum : maximum;
    field.value = field.defaultValue;
  }
  for (const name of [...new Set(data.programs.map(p => p.region))].sort()) {
    $("region").add(new Option(name, name));
  }
  const fields = ["Computational and digital mental health", "Trauma", "Child and adolescent",
    "Anxiety and CBT", "Assessment", "Mood and suicide", "Psychosis", "Health psychology"];
  for (const name of fields) $("subfield").add(new Option(name, name));
  $("coverage-message").textContent = hasRankings
    ? "Programs awaiting validation show partial counts."
    : "Counts reflect indexed papers and reviewed faculty so far. Rankings are pending the coverage audit.";

  function calculate() {
    const q = normalized($("search").value).split(" ").filter(Boolean);
    const mode = $("credit").value;
    const venue = $("venue").value;
    const selectedField = $("subfield").value;
    const perProgram = new Map();
    for (const work of data.works) {
      const year = Number(work.date.slice(0, 4));
      if (!year || year < Number(start.value) || year > Number(end.value)) continue;
      if (venue === "preprint" ? work.kind !== "preprint" : work.kind !== "journal") continue;
      if (venue === "top" || venue === "digital") if (venue !== work.venueTier) continue;
      if (selectedField && !work.subfields.includes(selectedField)) continue;
      for (const credit of work.credit) {
        if ($("current-only").checked && !credit.current) continue;
        if (!(credit[mode] > 0)) continue;
        if (!perProgram.has(credit.program)) perProgram.set(credit.program, {score: 0, papers: new Set(), contributors: new Set()});
        const stats = perProgram.get(credit.program);
        stats.score += credit[mode];
        stats.papers.add(work);
        stats.contributors.add(credit.person);
      }
    }
    const results = data.programs.filter(program => {
      const fields = normalized([program.name, program.location, ...(program.programmeAliases || [])].join(" "));
      if (!q.every(term => fields.includes(term))) return false;
      if ($("region").value && $("region").value !== program.region) return false;
      if ($("accreditation").value && !program.statuses[$("accreditation").value]) return false;
      const international = !["US", "CA"].includes(program.country);
      if (international && !$("international").checked && !q.length) return false;
      if ($("ranked-only").checked && (!program.rankable || (international && !$("international").checked))) return false;
      if (selectedField && program.rankable && !perProgram.get(program.id)?.papers.size) return false;
      return true;
    }).map(program => {
      const sum = perProgram.get(program.id);
      const rankable = program.rankable && (["US", "CA"].includes(program.country) || $("international").checked);
      const hasData = rankable || programsWithPapers.has(program.id);
      return {program, rankable, hasData, score: hasData && venue !== "preprint" ? (sum?.score || 0) : null,
        papers: hasData ? (sum?.papers.size || 0) : null, faculty: program.facultyCount, rank: null};
    });
    const metric = $("sort").value;
    const names = (a, b) => a.program.name.localeCompare(b.program.name);
    let previous, rank;
    const ranked = results.filter(row => row.rankable && row.score > 0)
      .sort((a, b) => b.score - a.score || names(a, b));
    for (const [index, row] of ranked.entries()) {
      const rounded = Math.round(row.score * 100) / 100;
      if (rounded !== previous) rank = index + 1;
      row.rank = rank;
      previous = rounded;
    }
    results.sort((a, b) => {
      if (metric === "coverage") return Number(b.hasData) - Number(a.hasData) || names(a, b);
      if (metric === "name") return names(a, b);
      const rankedFirst = metric === "score" && hasRankings ? Number(b.rankable) - Number(a.rankable) : 0;
      return rankedFirst || (b[metric] ?? -1) - (a[metric] ?? -1) || names(a, b);
    });
    return results;
  }

  function render() {
    rows = calculate();
    $("start-label").textContent = start.value;
    $("end-label").textContent = end.value;
    const maxPage = Math.max(1, Math.ceil(rows.length / pageSize));
    page = Math.min(page, maxPage);
    const offset = (page - 1) * pageSize;
    const visible = rows.slice(offset, offset + pageSize);
    $("pagination").hidden = maxPage === 1;
    $("prev-page").disabled = page === 1;
    $("next-page").disabled = page === maxPage;
    $("page-summary").textContent = `Page ${page} of ${maxPage}`;
    $("result-summary").textContent = visible.length
      ? `${offset + 1}–${offset + visible.length} of ${rows.length} programs · ${rows.filter(row => row.hasData).length} with publication data${hasRankings ? ` · ${rows.filter(row => row.rank).length} ranked` : ""}`
      : "0 programs";
    $("empty").hidden = rows.length > 0;
    $("export").disabled = !rows.length;
    $("results-body").innerHTML = visible.map(({program, rank, score, faculty, papers}) => {
      const id = encodeURIComponent(program.id);
      return `<tr>
        ${hasRankings ? `<td class="rank">${rank ?? "—"}</td>` : ""}
        <th scope="row" class="program-cell">
          <a class="program-button" href="program.html?id=${id}">${escapeHtml(program.name)}</a>
          <span class="location">${escapeHtml(program.location)} · ${escapeHtml(programCoverage(program))}</span>
          <span class="status-badges">${statusBadges(program)}</span>
        </th>
        <td class="number score">${score == null ? "—" : displayScore(score)}</td>
        <td class="number">${papers ?? "—"}</td><td class="number">${faculty || "—"}</td>
        <td class="open-cell"><a class="chevron-button" aria-label="View ${escapeHtml(program.name)}" href="program.html?id=${id}">›</a></td>
      </tr>`;
    }).join("");
  }

  function resetPage() {
    page = 1;
    render();
  }
  filter.addEventListener("input", event => {
    if (Number(start.value) > Number(end.value)) {
      if (event.target === start) end.value = start.value;
      else start.value = end.value;
    }
    resetPage();
  });
  filter.addEventListener("reset", () => {
    $("search").value = "";
    $("sort").value = defaultSort;
    setTimeout(resetPage, 0);
  });
  $("clear").addEventListener("click", () => filter.reset());
  $("search").addEventListener("input", resetPage);
  $("sort").addEventListener("change", resetPage);
  $("prev-page").addEventListener("click", () => { page--; render(); $("result-summary").scrollIntoView(); });
  $("next-page").addEventListener("click", () => { page++; render(); $("result-summary").scrollIntoView(); });
  $("export").addEventListener("click", () => {
    saveCsv(hasRankings ? "clinical-phd-rankings.csv" : "clinical-phd-publication-preview.csv", [
      ["Rank", "Program", "Location", "Adjusted count", "Papers counted", "Faculty records", "Accreditation and membership", "Coverage"],
      ...rows.map(row => [row.rank, row.program.name, row.program.location, row.score, row.papers, row.faculty,
        Object.keys(row.program.statuses).join(" + "), programCoverage(row.program)])
    ]);
  });
  render();
}

function saveCsv(filename, rows) {
  const content = rows.map(row => row.map(item => '"' + String(item ?? "").replaceAll('"', '""') + '"').join(",")).join("\r\n");
  saveBlob(filename, content, "text/csv;charset=utf-8");
}

function saveBlob(filename, content, type) {
  const url = URL.createObjectURL(new Blob([content], {type}));
  const link = document.createElement("a");
  link.download = filename;
  link.href = url;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function initializeProgram() {
  const key = new URLSearchParams(location.search).get("id");
  const program = data.programs.find(p => p.id === key || p.legacyIds?.includes(key));
  if (!program) {
    $("program-title").textContent = "Program unavailable";
    return;
  }
  document.title = program.name + " · Clinical PhD Index";
  $("program-title").textContent = program.name;
  $("program-location").textContent = program.location + " · " +
    (program.rankable ? "Coverage verified" :
      program.currentRosterReview?.status === "complete" ? "Current roster verified" :
      program.currentRosterReview?.status === "partial" ? "Current roster partly verified" : "Coverage pending");
  $("program-badges").innerHTML = statusBadges(program);
  $("report-link").href = "report.html?id=" + encodeURIComponent(program.id);
  const links = [["Program source", program.programUrl], ["Faculty roster source", program.rosterUrl],
    ...(program.currentRosterReview?.sources || []).map(source =>
      typeof source === "string" ? ["Additional roster evidence", source] : [source.label, source.url]),
    ["PhD track evidence", program.degreeSource],
    ...(program.directoryHistory || []).map(row => [row.directory + " historical listing (" + row.data.asOf.slice(0, 4) + ")", row.data.source]),
    ...Object.entries(program.statuses).map(([name, detail]) => [name + " · " + detail.label, detail.source])];
  $("program-sources").innerHTML = links.filter(([, url]) => url).map(([label, url]) =>
    `<a href="${escapeHtml(url)}">${escapeHtml(label)} ↗</a>`).join("");
  const faculty = data.faculty.filter(person => person.appointments.some(a => a.program === program.id));
  const people = $("faculty-list");
  if (!faculty.length) people.innerHTML = "<p>Faculty verification is pending for this program.</p>";
  for (const person of faculty) {
    const works = data.works.filter(work => work.people[person.id]);
    const section = document.createElement("details");
    section.innerHTML = `<summary>${escapeHtml(person.name)} <span class="location">${works.length} works retrieved · ${escapeHtml(person.retrieval.openAlex)}</span></summary>
      <p>${escapeHtml(person.areas.join(", "))}</p>
      <p>${[person.lab && ["Lab",person.lab],person.cv && ["CV",person.cv],person.publicationList && ["Lab bibliography",person.publicationList],person.orcid && ["ORCID","https://orcid.org/"+person.orcid]]
        .filter(Boolean).map(([label,url])=>`<a href="${escapeHtml(url)}">${label} ↗</a>`).join(" · ")}</p>
      <ol class="publication-list">${works.map(work => `<li><a href="${escapeHtml(publicationUrl(work))}">${escapeHtml(work.title)}</a>
        <span>${escapeHtml(work.date)} · ${escapeHtml(work.venue)}${work.kind === "preprint" ? " · Preprint" : ""}</span>
        <span>${work.credit.some(c => c.person === person.id && c.program === program.id) ? "Eligible appointment matched" : "Program attribution pending or ineligible"} · ${work.provenance.map(p => `<a href="${escapeHtml(p.url)}">${escapeHtml(p.source)}</a>`).join(", ")}</span></li>`).join("")}</ol>`;
    people.append(section);
  }
  const otherFaculty = (program.currentRosterReview?.roleDecisions || []).filter(person => person.label);
  if (otherFaculty.length) {
    const section = document.createElement("section");
    section.innerHTML = `<h3>Also listed by the program</h3>` + otherFaculty.map(person => `
      <details>
        <summary>${escapeHtml(person.name)} <span class="location">${escapeHtml(person.label)}</span></summary>
        <p>${escapeHtml(person.reason)}</p>
        <p><a href="${escapeHtml(person.source)}">Official faculty profile ↗</a></p>
      </details>`).join("");
    people.append(section);
  }
}

function initializeQuality() {
  const report = data.report;
  $("recall-value").textContent = report.recall == null ? "Pending" : `${(100 * report.recall).toFixed(1)}%`;
  const auditLabel = report.stage === "holdout" ? "holdout" : "calibration";
  $("recall-detail").textContent = `${report.sample.length} faculty in the ${auditLabel} sample. ${report.audited} complete reference lists reviewed. ${report.matches} of ${report.denominator} eligible references matched.`;
  if (report.missing?.length) {
    const names = new Map(data.faculty.map(person => [person.id, person.name]));
    $("audit-missing-list").innerHTML = report.missing.map(paper => {
      const title = escapeHtml(paper.title);
      const article = paper.doi ? `<a href="https://doi.org/${escapeHtml(paper.doi)}">${title}</a>` : title;
      return `<li>${article} · ${escapeHtml(names.get(paper.person) || paper.person)}. ${escapeHtml(paper.reason)}</li>`;
    }).join("");
    $("audit-missing").hidden = false;
  }
  const diagnostics = [report.diagnostic, ...(report.diagnosticChecks || [])].filter(Boolean);
  if (diagnostics.length) {
    $("diagnostic-detail").hidden = false;
    $("diagnostic-detail").textContent = "Pilot checks matched " +
      diagnostics.map(d => `${d.matches} of ${d.denominator} papers in ${d.scope}`).join(", and ") +
      ". These checks are outside the 50-faculty audit.";
  }
  $("coverage-counts").innerHTML = [
    ["Program entries", data.programs.length], ["Current rosters verified", data.counts.currentRostersVerified || 0],
    ["Eligible programs awaiting current roster verification", data.programs.filter(program =>
      ["directory eligible", "active"].includes(program.eligibility) && program.currentRosterReview?.status !== "complete").length],
    ["Current and historical rosters verified", data.counts.rosterVerified],
    ["Verified faculty records", data.counts.faculty], ["Resolved identities", data.counts.resolved],
    ["Identities awaiting resolution", data.counts.faculty - data.counts.resolved],
    ["Completed primary retrievals", data.counts.complete], ["Programs released to rankings", data.counts.ranked]
  ].map(([label, count]) => `<li><strong>${count}</strong> ${label}</li>`).join("");
  $("directory-status").innerHTML = Object.entries(data.directories.directories).map(([name, info]) =>
    `<li><strong>${name}</strong> · ${escapeHtml(info.status)} <a href="${escapeHtml(info.source)}">Official source ↗</a></li>`).join("");
  if (data.collection && $("collection-status")) {
    const collection = data.collection;
    $("collection-status").hidden = false;
    $("collection-detail").textContent = `On ${collection.date}, the collector checked ${collection.programs} program entries and reached a source for ${collection.sourcesReached}. It matched ${collection.namesConfirmed} names to official profiles and staged ${collection.publicationsRetrieved.toLocaleString("en-US")} publication records for faculty and attribution review.`;
    $("collection-run").href = collection.run;
  }
}

function initializeReport() {
  const select = $("report-program");
  const key = new URLSearchParams(location.search).get("id");
  for (const p of [...data.programs].sort((a, b) => a.name.localeCompare(b.name))) {
    select.add(new Option(p.name, p.id, key === p.id, key === p.id));
  }
  if (data.reportTarget) {
    $("report-label").textContent = "Open public GitHub draft";
    $("report-explain").textContent = "Review your draft on GitHub before posting. Keep private information out of the report.";
  }
  $("report-form").addEventListener("submit", event => {
    event.preventDefault();
    const program = data.programs.find(p => p.id === select.value);
    const values = new FormData(event.currentTarget);
    const title = `Publication correction · ${program?.name ?? "Program to confirm"}`;
    const draft = ["Program: " + (program?.name ?? ""), "Issue: " + values.get("type"),
      "Paper or DOI: " + values.get("paper"), "Source: " + values.get("evidence"),
      "", String(values.get("details"))].join("\n");
    if (data.reportTarget) {
      window.location.href = data.reportTarget + "?" + new URLSearchParams({title, body: draft});
      return;
    }
    saveBlob("clinical-phd-correction.txt", title + "\n\n" + draft, "text/plain;charset=utf-8");
  });
}

if ($("rankings")) initializeRankings();
if ($("faculty-list")) initializeProgram();
if ($("recall-value")) initializeQuality();
if ($("report-form")) initializeReport();
