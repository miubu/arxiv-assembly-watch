"""Collect recent arXiv candidates. Scores select abstracts for screening, not reading priority.

Modified 2026-11-18 from TideDra/zotero-arxiv-daily, commit
35c23ec44617b7595b30ac8e5bf4326c6cfd2336, retriever/arxiv_retriever.py.
Reuses the Atom abstract cleanup and new/cross-list handling. AGPL-3.0, see ../LICENSE.
"""
import argparse
import json
import os
import re
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


A = "{http://www.w3.org/2005/Atom}"
X = "{http://arxiv.org/schemas/atom}"
DC = "{http://purl.org/dc/elements/1.1/}"
OS = "{http://a9.com/-/spec/opensearch/1.1/}"
CATEGORIES = "cs.RO+cs.AI+cs.LG+cs.NE+cs.CV+cs.CL+cs.SY+eess.SY+stat.ML"
RSS = "https://rss.arxiv.org/atom/" + CATEGORIES
API = "https://export.arxiv.org/api/query"
ID = re.compile(r"(?P<base>\d{4}\.\d{4,5}|[A-Za-z][\w.-]*/\d{7})(?:v\d+)?")

HTML_TAG_PATTERN = re.compile(
    r"</?(?:p|a|span|div|b|i|strong|em|br|hr|h[1-6]|ul|ol|li|sub|sup|table|tr|td|th)\b(?:\s+[^>]*)?/?>", re.I)
ARXIV_HEADER_PATTERN = re.compile(
    r"^(?:arxiv:\s*\S+(?:\s+\[[^\]]*\])?)?\s*(?:announce\s+type:\s*[\w-]+)?\s*(?:abstract:\s*)?", re.I)

# Phrases are a shortlist filter; the agent judges novelty and transfer from the paper.
ASSEMBLY = re.compile(r"\b(?:assembl(?:y|ies|ing)|disassembl(?:y|ies|ing)|"
                      r"peg[- ]in[- ]hole|press[- ]fit|snap[- ]fit|screwdriving|"
                      r"parts? mating|contact[- ]rich (?:manipulation|insertion))\b", re.I)
MECHANICAL = re.compile(r"\b(?:robots?|robotic|industrial|manufacturing|mechanical|"
                        r"process planning|sequence planning|parts?|CAD|workstation|"
                        r"production line|fastener|tolerance|fixture|insertion|force control|"
                        r"bimanual|manipulator|subassembl\w*)\b", re.I)
ASSEMBLY_FOCUS = re.compile(r"\b(?:assembly (?:process|sequence|planning|plan|task|line|operation)|"
                            r"robot(?:ic)? (?:part )?assembly|mechanical assembly|"
                            r"peg[- ]in[- ]hole|press[- ]fit|snap[- ]fit|screwdriving|"
                            r"disassembly (?:planning|sequence|process)|assembly[- ]aware)\b", re.I)
UNRELATED = re.compile(r"\b(?:genom\w*|protein|ribosom\w*|self[- ]assembly|metagenom\w*|"
                       r"molecular assembly|assembly language|webassembly|cell assembly)\b", re.I)
NETWORK = re.compile(r"\b(?:neural networks?|deep (?:networks?|learning)|transformers?|"
                     r"language models?|diffusion models?|foundation models?|mixture[- ]of[- ]experts|"
                     r"state[- ]space models?|LLMs?|vision[- ]language models?|"
                     r"neural (?:architectures?|solvers?|optimizers?|operators?|methods?))\b", re.I)
METHOD = re.compile(r"\b(?:architectur\w*|optimizers?|optimization|training|pre[- ]?training|"
                    r"post[- ]?training|generalization|scaling|distillation|sparsity|quantization|"
                    r"attention|convergence|fine[- ]?tuning|inference efficiency|sample efficiency|"
                    r"representation learning|continual learning|learning dynamics|normalization)\b", re.I)
PLANNING_METHOD = re.compile(r"\b(?:combinatorial optimization|constraint satisfaction|"
                             r"sequence (?:planning|optimization)|scheduling|task planning)\b", re.I)
API_QUERIES = {
    "assembly": '(ti:assembly OR ti:disassembly OR abs:"assembly sequence" OR abs:"assembly planning" '
                'OR abs:"robotic assembly" OR abs:"peg-in-hole" OR abs:"contact-rich manipulation" '
                'OR abs:"assembly process" OR abs:"mechanical assembly" OR abs:screwdriving)',
    "neural_networks": '(cat:cs.LG OR cat:cs.NE OR cat:stat.ML OR cat:cs.CL OR cat:cs.CV) AND '
                       '(ti:architecture OR ti:optimizer OR ti:optimization OR ti:attention OR ti:transformer '
                       'OR ti:scaling OR ti:distillation OR ti:pretraining OR ti:quantization '
                       'OR ti:"representation learning" OR ti:"neural network" OR ti:"post-training")',
}


def scores(title, abstract):
    text = title + " " + abstract
    ranks = {}
    if ASSEMBLY.search(text) and MECHANICAL.search(text) and not UNRELATED.search(title):
        ranks["assembly"] = (6 * bool(ASSEMBLY_FOCUS.search(title)) + 3 * bool(ASSEMBLY.search(title))
                             + 2 * bool(ASSEMBLY_FOCUS.search(abstract)) + 1)
    if NETWORK.search(text) and METHOD.search(text):
        ranks["neural_networks"] = (3 * bool(NETWORK.search(title)) + 4 * bool(METHOD.search(title))
                                    + 5 * bool(PLANNING_METHOD.search(title))
                                    + min(4, len({m.lower() for m in METHOD.findall(abstract)})))
    return ranks


def fetch(url):
    request = Request(url, headers={"User-Agent": "arxiv-assembly-watch/1.0 (personal literature monitor)"})
    for attempt in range(3):
        try:
            with urlopen(request, timeout=25) as response:
                root = ET.parse(response).getroot()
            break
        except (OSError, HTTPException, ET.ParseError) as exc:
            if attempt == 2 or (isinstance(exc, HTTPError) and exc.code not in {408, 429} and exc.code < 500):
                raise
            time.sleep(4 * (attempt + 1))
    if root.tag != A + "feed" or "error" in root.findtext(A + "title", "").lower():
        raise ValueError("invalid or error Atom feed")
    return root


def parse_entry(entry, rss):
    raw = entry.findtext(A + "id", "").removeprefix("oai:arXiv.org:").split("/abs/")[-1]
    match = ID.fullmatch(raw)
    if not match:
        raise ValueError(f"unexpected arXiv ID {raw!r}")
    authors = [e.text.strip() for e in entry.findall(A + "author/" + A + "name") if e.text]
    if not authors:
        authors = [n.strip() for n in entry.findtext(DC + "creator", "").split(",") if n.strip()]
    summary = HTML_TAG_PATTERN.sub(" ", entry.findtext(A + "summary", "")).strip()
    summary = " ".join(ARXIV_HEADER_PATTERN.sub("", summary).split())
    published = entry.findtext(A + "published", "")[:10]
    date.fromisoformat(published)
    paper = dict(arxiv_id=match["base"], versioned_id=raw,
                 title=" ".join(entry.findtext(A + "title", "").split()), authors=authors,
                 abstract=summary, categories=[c.get("term") for c in entry.findall(A + "category")],
                 doi=entry.findtext(X + "doi") or entry.findtext(X + "DOI"),
                 url="https://arxiv.org/abs/" + raw, pdf_url="https://arxiv.org/pdf/" + raw,
                 announced=published if rss else None, submitted=None if rss else published)
    return paper


def scan_feed(root, rss, start, end, pool):
    count = 0
    for entry in root.findall(A + "entry"):
        if rss and entry.findtext(X + "announce_type", "new").lower() not in {"new", "cross"}:
            continue
        paper = parse_entry(entry, rss)
        event = paper["announced"] or paper["submitted"]
        if not (start.isoformat() <= event <= end.isoformat()):
            continue
        count += 1
        ranks = scores(paper["title"], paper["abstract"])
        if not ranks:
            continue
        existing = pool.get(paper["arxiv_id"])
        if existing:
            existing["announced"] = existing["announced"] or paper["announced"]
            existing["submitted"] = existing["submitted"] or paper["submitted"]
        else:
            paper["screening_scores"] = ranks
            paper["tracks"] = list(ranks)
            pool[paper["arxiv_id"]] = paper
    return count


def discover(since, until, limit, api_limit=300):
    pool, sources = {}, []
    rss_day = None
    try:
        root = fetch(RSS)
        dates = [e.text[:10] for e in root.findall(A + "entry/" + A + "published") if e.text]
        day = date.fromisoformat(max(dates) if dates else root.findtext(A + "updated", "")[:10])
        n = scan_feed(root, True, since, until, pool)
        rss_day = day
        sources.append(dict(source="rss", date=str(day), entries_in_window=n, url=RSS))
    except (OSError, HTTPException, ValueError, ET.ParseError) as exc:
        sources.append(dict(source="rss", error=str(exc), url=RSS))

    complete = True
    if since != until or rss_day != until:
        # RSS announces after submission. Include an overlap for weekends and moderation.
        api_start = since - timedelta(days=4)
        api_end = until - timedelta(days=1) if rss_day == until else until
        last_request = 0.0
        for track, query in API_QUERIES.items():
            try:
                total_read = 0
                total = 0
                while total_read < api_limit:
                    time.sleep(max(0.0, 3.1 - (time.monotonic() - last_request)))
                    last_request = time.monotonic()
                    terms = f"({query}) AND submittedDate:[{api_start:%Y%m%d}0000 TO {api_end:%Y%m%d}2359]"
                    batch = min(100, api_limit - total_read)
                    url = API + "?" + urlencode(dict(search_query=terms, start=total_read, max_results=batch,
                                                     sortBy="submittedDate", sortOrder="descending"))
                    root = fetch(url)
                    total = int(root.findtext(OS + "totalResults", ""))
                    rows = root.findall(A + "entry")
                    scan_feed(root, False, api_start, api_end, pool)
                    total_read += len(rows)
                    if total_read >= total or not rows:
                        break
                capped = total_read < total
                complete &= not capped
                sources.append(dict(source="api", track=track, submission_start=str(api_start),
                                    submission_end=str(api_end), fetched=total_read, total=total, capped=capped))
            except (OSError, HTTPException, ValueError, ET.ParseError) as exc:
                complete = False
                sources.append(dict(source="api", track=track, submission_start=str(api_start),
                                    submission_end=str(api_end), fetched=total_read, error=str(exc)))

    chosen, counts = {}, {}
    def priority(paper, track):
        in_window = (paper["announced"] or paper["submitted"]) >= str(since)
        return in_window, paper["screening_scores"][track], paper["arxiv_id"]

    for track in API_QUERIES:
        ranked = sorted((p for p in pool.values() if track in p["tracks"]),
                        key=lambda p: priority(p, track), reverse=True)
        counts[track] = dict(matched=len(ranked), shortlisted=min(limit, len(ranked)))
        for paper in ranked[:limit]:
            chosen[paper["arxiv_id"]] = paper
    sources_ok = any("error" not in s and (s["source"] == "api" or
                     str(since) <= s["date"] <= str(until)) for s in sources)
    status = "complete" if complete else ("partial" if sources_ok else "unavailable")
    return dict(coverage=dict(status=status, since=str(since), until=str(until), sources=sources,
                              counts=counts, limit_per_track=limit, api_limit_per_query=api_limit),
                papers=list(chosen.values()))


def self_test():
    sample = ET.fromstring(f"""<feed xmlns="{A[1:-1]}" xmlns:arxiv="{X[1:-1]}"><entry>
        <id>oai:arXiv.org:2611.12345v1</id><title>Robotic Assembly Sequence Planning</title>
        <summary>arXiv:2611.12345v1 Announce Type: new Abstract: Fasteners are inserted.</summary>
        <published>2026-11-18T00:00:00-05:00</published><arxiv:announce_type>cross</arxiv:announce_type>
        </entry></feed>""")
    day = date(2026, 11, 18)
    pool = {}
    assert scan_feed(sample, True, day, day, pool) == 1
    assert pool["2611.12345"]["abstract"] == "Fasteners are inserted."
    assert pool["2611.12345"]["tracks"] == ["assembly"]
    sample.find(A + "entry/" + X + "announce_type").text = "replace"
    assert scan_feed(sample, True, day, day, pool) == 0
    assert "neural_networks" in scores("Optimizers for Transformers", "We study pretraining.")
    assert "neural_networks" in scores("Neural Solvers for Combinatorial Optimization", "We compare solvers.")
    assert not scores("Genome assembly", "We identify mechanical parts.")
    print("discovery self-test passed")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--since", type=date.fromisoformat, help="first announcement day, inclusive (default --until)")
    parser.add_argument("--until", type=date.fromisoformat, default=date.today())
    parser.add_argument("--limit", type=int, default=12, help="shortlist size per track")
    parser.add_argument("--api-limit", type=int, default=300, help="maximum records per backfill query")
    parser.add_argument("--output", type=Path, help="write JSON atomically to this file")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return
    since = args.since or args.until
    if since > args.until or args.until > date.today() or min(args.limit, args.api_limit) < 1:
        parser.error("require since <= until <= today and positive limits")
    result = discover(since, args.until, args.limit, args.api_limit)
    output = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=args.output.parent, delete=False) as file:
            file.write(output)
        os.replace(file.name, args.output)
    else:
        sys.stdout.buffer.write(output.encode("utf-8"))
    if result["coverage"]["status"] == "unavailable":
        sys.exit(1)


if __name__ == "__main__":
    main()
