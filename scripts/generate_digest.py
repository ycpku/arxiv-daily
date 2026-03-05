import argparse
import datetime as dt
import html
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml

ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}
ARXIV_NS = {"arxiv": "http://arxiv.org/schemas/atom"}


def load_config(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    data.setdefault("categories", [])
    data.setdefault("max_results", 200)
    data.setdefault("lookback_hours", 30)
    data.setdefault("keywords_any", [])
    data.setdefault("keywords_all", [])
    data.setdefault("authors_any", [])
    data.setdefault("match_mode", "any")
    data.setdefault("category_rules", [])
    data.setdefault("authors_watchlist", {})
    return data


def build_category_query(categories: list[str]) -> str:
    if not categories:
        return "all:*"
    joined = " OR ".join(f"cat:{c}" for c in categories)
    return f"({joined})"


def build_author_query(author_name: str) -> str:
    escaped = author_name.replace('"', "")
    return f'au:"{escaped}"'


def fetch_arxiv_entries(query: str, max_results: int) -> list[dict]:
    params = {
        "search_query": query,
        "start": 0,
        "max_results": max_results,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }
    url = "https://export.arxiv.org/api/query?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "arxiv-daily-bot/1.1"})

    with urllib.request.urlopen(req, timeout=30) as resp:
        xml_text = resp.read()

    root = ET.fromstring(xml_text)
    entries: list[dict] = []

    for entry in root.findall("atom:entry", ATOM_NS):
        entry_id = text_or_empty(entry.find("atom:id", ATOM_NS))
        title = normalize_whitespace(text_or_empty(entry.find("atom:title", ATOM_NS)))
        summary = normalize_whitespace(text_or_empty(entry.find("atom:summary", ATOM_NS)))

        published_raw = text_or_empty(entry.find("atom:published", ATOM_NS))
        updated_raw = text_or_empty(entry.find("atom:updated", ATOM_NS))
        published = parse_arxiv_datetime(published_raw)
        updated = parse_arxiv_datetime(updated_raw)

        authors = [
            normalize_whitespace(text_or_empty(a.find("atom:name", ATOM_NS)))
            for a in entry.findall("atom:author", ATOM_NS)
        ]

        categories = [
            c.attrib.get("term", "")
            for c in entry.findall("atom:category", ATOM_NS)
            if c.attrib.get("term")
        ]

        comment = text_or_empty(entry.find("arxiv:comment", ARXIV_NS))
        primary_category_node = entry.find("arxiv:primary_category", ARXIV_NS)
        primary_category = ""
        if primary_category_node is not None:
            primary_category = primary_category_node.attrib.get("term", "")

        entries.append(
            {
                "id": entry_id,
                "title": title,
                "summary": summary,
                "published": published,
                "updated": updated,
                "authors": authors,
                "categories": categories,
                "primary_category": primary_category,
                "comment": normalize_whitespace(comment),
            }
        )

    return entries


def text_or_empty(node) -> str:
    if node is None or node.text is None:
        return ""
    return html.unescape(node.text)


def normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def parse_arxiv_datetime(value: str) -> dt.datetime | None:
    if not value:
        return None
    return dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)


def contains_any(haystack: str, needles: list[str]) -> bool:
    if not needles:
        return True
    lower_haystack = haystack.lower()
    return any(n.lower() in lower_haystack for n in needles if n.strip())


def contains_all(haystack: str, needles: list[str]) -> bool:
    if not needles:
        return True
    lower_haystack = haystack.lower()
    return all(n.lower() in lower_haystack for n in needles if n.strip())


def author_match(authors: list[str], filters: list[str]) -> bool:
    if not filters:
        return True
    lower_authors = " | ".join(authors).lower()
    return any(f.lower() in lower_authors for f in filters if f.strip())


def keyword_relevance(entry: dict, keywords: list[str]) -> int:
    if not keywords:
        return 0

    title = entry["title"].lower()
    summary = entry["summary"].lower()
    score = 0

    for kw in keywords:
        token = kw.strip().lower()
        if not token:
            continue
        score += title.count(token) * 3
        score += summary.count(token)

    return score


def normalize_category_rules(cfg: dict) -> list[dict]:
    explicit_rules = cfg.get("category_rules", []) or []
    rules: list[dict] = []

    for idx, raw in enumerate(explicit_rules, start=1):
        if not isinstance(raw, dict):
            continue
        rules.append(
            {
                "name": str(raw.get("name") or f"Category Rule {idx}"),
                "categories": raw.get("categories", []) or [],
                "keywords_any": raw.get("keywords_any", []) or [],
                "keywords_all": raw.get("keywords_all", []) or [],
                "authors_any": raw.get("authors_any", []) or [],
                "match_mode": str(raw.get("match_mode", "any")).strip().lower(),
                "max_papers": int(raw.get("max_papers", 20)),
                "query_max_results": int(raw.get("query_max_results", cfg.get("max_results", 200))),
            }
        )

    if rules:
        return rules

    # Legacy fallback: convert old top-level config to a single category rule.
    rules.append(
        {
            "name": "Default Category",
            "categories": cfg.get("categories", []) or [],
            "keywords_any": cfg.get("keywords_any", []) or [],
            "keywords_all": cfg.get("keywords_all", []) or [],
            "authors_any": cfg.get("authors_any", []) or [],
            "match_mode": str(cfg.get("match_mode", "any")).strip().lower(),
            "max_papers": 50,
            "query_max_results": int(cfg.get("max_results", 200)),
        }
    )
    return rules


def filter_rule_entries(entries: list[dict], rule: dict, now_utc: dt.datetime, lookback_hours: int) -> list[dict]:
    min_time = now_utc - dt.timedelta(hours=lookback_hours)
    keywords_any = rule.get("keywords_any", []) or []
    keywords_all = rule.get("keywords_all", []) or []
    authors_any = rule.get("authors_any", []) or []
    match_mode = str(rule.get("match_mode", "any")).strip().lower()

    matched = []
    seen = set()

    for e in entries:
        if e["id"] in seen:
            continue
        seen.add(e["id"])

        published = e.get("published")
        if not published or published < min_time:
            continue

        text = f"{e['title']}\n{e['summary']}"
        keyword_pass = contains_any(text, keywords_any) and contains_all(text, keywords_all)
        author_pass = author_match(e.get("authors", []), authors_any)
        has_keyword_filter = bool(keywords_any or keywords_all)
        has_author_filter = bool(authors_any)

        if has_keyword_filter and has_author_filter:
            if match_mode == "all":
                if not (keyword_pass and author_pass):
                    continue
            else:
                if not (keyword_pass or author_pass):
                    continue
        elif has_keyword_filter:
            if not keyword_pass:
                continue
        elif has_author_filter:
            if not author_pass:
                continue

        relevance = keyword_relevance(e, keywords_any + keywords_all)
        if e.get("primary_category") in (rule.get("categories", []) or []):
            relevance += 1
        e2 = dict(e)
        e2["relevance"] = relevance
        matched.append(e2)

    matched.sort(
        key=lambda x: (
            x.get("relevance", 0),
            x.get("published") or dt.datetime.min.replace(tzinfo=dt.timezone.utc),
        ),
        reverse=True,
    )
    return matched[: int(rule.get("max_papers", 20))]


def get_watch_authors(cfg: dict) -> tuple[list[str], int, int, int]:
    watch_cfg = cfg.get("authors_watchlist", {}) or {}
    authors = watch_cfg.get("authors", []) or []
    if not authors:
        authors = cfg.get("authors_any", []) or []

    max_papers_per_author = int(watch_cfg.get("max_papers_per_author", 1))
    query_max_results = int(watch_cfg.get("query_max_results", 10))
    lookback_hours = int(watch_cfg.get("lookback_hours", 24))
    return authors, max_papers_per_author, query_max_results, lookback_hours


def fetch_latest_for_authors(
    authors: list[str],
    per_author: int,
    query_max_results: int,
    now_utc: dt.datetime,
    lookback_hours: int,
) -> dict[str, list[dict]]:
    result: dict[str, list[dict]] = {}
    min_time = now_utc - dt.timedelta(hours=lookback_hours)

    for author in authors:
        query = build_author_query(author)
        try:
            entries = fetch_arxiv_entries(query, query_max_results)
        except Exception:
            continue

        matched = []
        seen = set()
        author_lower = author.lower()

        for e in entries:
            if e["id"] in seen:
                continue
            seen.add(e["id"])

            author_blob = " | ".join(e.get("authors", [])).lower()
            if author_lower not in author_blob:
                continue
            published = e.get("published")
            if not published or published < min_time:
                continue
            matched.append(e)

            if len(matched) >= per_author:
                break

        if matched:
            result[author] = matched

    return result


def format_datetime(d: dt.datetime | None) -> str:
    if not d:
        return "N/A"
    return d.strftime("%Y-%m-%d %H:%M UTC")


def append_paper(lines: list[str], paper: dict, with_relevance: bool) -> None:
    lines.append(f"- **{paper['title']}**")
    lines.append(f"  - arXiv: {paper['id']}")
    lines.append(f"  - Published: {format_datetime(paper.get('published'))}")
    lines.append(f"  - Authors: {', '.join(paper.get('authors', []))}")
    lines.append(f"  - Primary category: {paper.get('primary_category') or 'N/A'}")
    if with_relevance:
        lines.append(f"  - Relevance score: {paper.get('relevance', 0)}")
    lines.append(f"  - Abstract: {paper.get('summary', '')}")
    lines.append("")


def write_markdown(
    category_results: list[dict],
    author_latest: dict[str, list[dict]],
    output_dir: Path,
    now_utc: dt.datetime,
    author_lookback_hours: int,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    date_tag = now_utc.strftime("%Y-%m-%d")
    out_file = output_dir / f"{date_tag}.md"

    total_category_papers = sum(len(item["papers"]) for item in category_results)
    total_author_papers = sum(len(papers) for papers in author_latest.values())

    lines = []
    lines.append(f"# arXiv Daily Digest ({date_tag})")
    lines.append("")
    lines.append(f"- Generated at: {format_datetime(now_utc)}")
    lines.append(f"- Category-matched papers: {total_category_papers}")
    lines.append(f"- Followed-author latest papers: {total_author_papers}")
    lines.append("")

    lines.append("## Category Digest")
    lines.append("")
    if not category_results:
        lines.append("No category rules configured.")
        lines.append("")
    else:
        for section in category_results:
            rule = section["rule"]
            papers = section["papers"]
            lines.append(f"### {rule['name']} ({len(papers)})")
            lines.append("")
            if not papers:
                lines.append("No matched papers in this category today.")
                lines.append("")
                continue
            for paper in papers:
                append_paper(lines, paper, with_relevance=True)

    lines.append("## Followed Authors - Latest")
    lines.append("")
    if not author_latest:
        lines.append(f"No followed-author new papers in the last {author_lookback_hours} hours.")
        lines.append("")
    else:
        for author, papers in author_latest.items():
            lines.append(f"### {author}")
            lines.append("")
            for paper in papers:
                append_paper(lines, paper, with_relevance=False)

    out_file.write_text("\n".join(lines), encoding="utf-8")
    return out_file


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate daily arXiv markdown digest")
    parser.add_argument("--config", default="config.yaml", help="Path to YAML config")
    parser.add_argument("--output-dir", default="reports", help="Directory for markdown output")
    args = parser.parse_args()

    cfg_path = Path(args.config)
    out_dir = Path(args.output_dir)
    if not cfg_path.exists():
        print(f"Config file not found: {cfg_path}", file=sys.stderr)
        return 2

    cfg = load_config(cfg_path)
    now_utc = dt.datetime.now(dt.timezone.utc)
    lookback_hours = int(cfg.get("lookback_hours", 30))
    rules = normalize_category_rules(cfg)

    # Cache query results to avoid duplicated API calls when rules share categories.
    query_cache: dict[str, list[dict]] = {}
    category_results: list[dict] = []

    try:
        for rule in rules:
            query = build_category_query(rule.get("categories", []) or [])
            if query not in query_cache:
                query_cache[query] = fetch_arxiv_entries(query, int(rule.get("query_max_results", cfg.get("max_results", 200))))
            papers = filter_rule_entries(query_cache[query], rule, now_utc, lookback_hours)
            category_results.append({"rule": rule, "papers": papers})
    except Exception as exc:
        print(f"Failed to fetch category feed: {exc}", file=sys.stderr)
        return 1

    authors, per_author, author_query_max, author_lookback_hours = get_watch_authors(cfg)
    author_latest = (
        fetch_latest_for_authors(authors, per_author, author_query_max, now_utc, author_lookback_hours)
        if authors
        else {}
    )

    out_file = write_markdown(category_results, author_latest, out_dir, now_utc, author_lookback_hours)

    total_fetched = sum(len(v) for v in query_cache.values())
    total_category_matched = sum(len(section["papers"]) for section in category_results)
    total_author_matched = sum(len(v) for v in author_latest.values())
    print(f"Fetched from category queries: {total_fetched} papers")
    print(f"Category matched: {total_category_matched} papers")
    print(f"Author latest matched: {total_author_matched} papers")
    print(f"Output: {out_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
