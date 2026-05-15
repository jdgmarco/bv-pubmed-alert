"""
Weekly alert on Biological Variation: PubMed + bioRxiv/medRxiv preprints.

- Queries PubMed E-utilities with a user-defined search string.
- Queries bioRxiv and medRxiv via the biorxiv.org API and filters preprints
  client-side by BV keywords (preprints have no MeSH indexing).
- Deduplicates against previously seen PMIDs / DOIs stored in seen_pmids.json
  (a single combined list — PMIDs are all digits, DOIs start with "10.", so
  they never collide).
- Sends an HTML email summary via Gmail SMTP to the configured recipients.

Designed to run weekly on GitHub Actions. Credentials are read from
environment variables (set as GitHub Actions secrets):
  - GMAIL_USER             : Gmail address used to send the email
  - GMAIL_APP_PASSWORD     : Gmail App Password (NOT the normal password)
"""

from __future__ import annotations

import json
import os
import smtplib
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

QUERY = (
    '(("Biological Variation, Population"[Mesh] OR "Biological Variation, Individual"[Mesh] '
    'OR "biological variation"[tiab] OR "biological variability"[tiab] '
    'OR "inter-individual variation"[tiab] OR "inter-individual variations"[tiab] '
    'OR "inter-individual variability"[tiab] OR "interindividual variability"[tiab] '
    'OR "inter-subject variation"[tiab] OR "inter-subject variations"[tiab] '
    'OR "inter-subject variability"[tiab] '
    'OR "between-subject variation"[tiab] OR "between-subject variations"[tiab] '
    'OR "between-subject variability"[tiab] OR "between-subjects variability"[tiab] '
    'OR "between-individual variation"[tiab] OR "between-individual variations"[tiab] '
    'OR "between-individual variability"[tiab] '
    'OR "intra-individual variation"[tiab] OR "intra-individual variations"[tiab] '
    'OR "intra-individual variability"[tiab] OR "intraindividual variability"[tiab] '
    'OR "intra-subject variation"[tiab] OR "intra-subject variations"[tiab] '
    'OR "intra-subject variability"[tiab] OR "intra-subjects variability"[tiab] '
    'OR "within-subject variation"[tiab] OR "within-subject variations"[tiab] '
    'OR "within-subject variability"[tiab] '
    'OR "within-subjects variation"[tiab] OR "within-subjects variations"[tiab] '
    'OR "within-subjects variability"[tiab] '
    'OR "within-individual variation"[tiab] OR "within-individual variations"[tiab] '
    'OR "within-individual variability"[tiab] '
    'OR "short-term variation"[tiab] OR "short-term variations"[tiab] '
    'OR "short-term variability"[tiab] OR "short-term biological"[tiab] '
    'OR "long-term variation"[tiab] OR "long-term variations"[tiab] '
    'OR "long-term variability"[tiab] OR "long-term biological"[tiab] '
    'OR "day-to-day variability"[tiab]))'
    ' AND humans[MeSH Terms]'
    ' NOT ("Plants"[MeSH] OR "Ecology"[MeSH] OR "Ecosystem"[MeSH] OR veterinary[sb])'
)

# Keywords searched in title + abstract of preprints (case-insensitive
# substring match). Kept slightly narrower than the PubMed list because
# preprints have no MeSH filter — we lean on specific multi-word phrases
# to keep precision high.
PREPRINT_KEYWORDS = [
    "biological variation",
    "biological variability",
    "intra-individual variation",
    "intra-individual variability",
    "intraindividual variation",
    "intraindividual variability",
    "inter-individual variation",
    "inter-individual variability",
    "interindividual variation",
    "interindividual variability",
    "intra-subject variation",
    "intra-subject variability",
    "inter-subject variation",
    "inter-subject variability",
    "within-subject variation",
    "within-subject variability",
    "within-individual variation",
    "within-individual variability",
    "between-subject variation",
    "between-subject variability",
    "between-individual variation",
    "between-individual variability",
    "short-term variability",
    "long-term variability",
    "day-to-day variability",
    "reference change value",
]

# bioRxiv categories to skip (clearly non-clinical). medRxiv is all clinical,
# so no exclusion applies there.
BIORXIV_EXCLUDE_CATEGORIES = {
    "ecology",
    "plant biology",
    "paleontology",
    "zoology",
    "animal behavior and cognition",
}

# Preprints must mention at least one of these context terms in title or
# abstract. This is the single biggest noise-reducer: methodological uses of
# "biological variation" in bioinformatics/neuroimaging, ecological studies,
# and pure animal/plant biology almost never include these clinical-lab terms.
PREPRINT_REQUIRED_CONTEXT = [
    "biomarker",            # covers "biomarker" and "biomarkers"
    "measurand",            # covers "measurand" and "measurands"
    "analyte",              # covers "analyte" and "analytes"
    "serum",
    "plasma",
    "whole blood",
    "urine",
    "saliva",
    "cerebrospinal fluid",
    "reference change value",
    "reference interval",
    "analytical performance",
    "analytical variation",
    "laboratory medicine",
    "clinical chemistry",
    "coefficient of variation",
]

RECIPIENTS = ["jdgmarco@gmail.com", "isabelmorenoparro@gmail.com"]

# Slightly larger than 7 to absorb scheduling jitter / late indexing;
# duplicates are filtered via seen_pmids.json
RELDATE_DAYS = 10
MAX_RESULTS = 500

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
BIORXIV_API = "https://api.biorxiv.org/details"
TOOL_NAME = "bv-pubmed-alert"
CONTACT_EMAIL = "jdgmarco@gmail.com"

SEEN_FILE = Path("seen_pmids.json")
SEEN_MAX_KEEP = 8000  # cap to keep file small; PMIDs + DOIs share this budget

# -----------------------------------------------------------------------------
# PubMed access
# -----------------------------------------------------------------------------

def esearch(query: str, days: int) -> list[str]:
    params = {
        "db": "pubmed",
        "term": query,
        "reldate": days,
        "datetype": "edat",  # entry date
        "retmax": MAX_RESULTS,
        "retmode": "json",
        "tool": TOOL_NAME,
        "email": CONTACT_EMAIL,
    }
    r = requests.get(f"{EUTILS}/esearch.fcgi", params=params, timeout=30)
    r.raise_for_status()
    return r.json().get("esearchresult", {}).get("idlist", [])


def efetch(pmids: list[str]) -> list[dict]:
    articles: list[dict] = []
    for i in range(0, len(pmids), 200):
        batch = pmids[i : i + 200]
        params = {
            "db": "pubmed",
            "id": ",".join(batch),
            "rettype": "xml",
            "retmode": "xml",
            "tool": TOOL_NAME,
            "email": CONTACT_EMAIL,
        }
        r = requests.get(f"{EUTILS}/efetch.fcgi", params=params, timeout=60)
        r.raise_for_status()
        articles.extend(parse_pubmed_xml(r.content))
        time.sleep(0.4)  # NCBI rate limit: max 3 req/s without API key
    return articles


def _text(node) -> str:
    if node is None:
        return ""
    return "".join(node.itertext()).strip()


def parse_pubmed_xml(xml_bytes: bytes) -> list[dict]:
    root = ET.fromstring(xml_bytes)
    out = []
    for art in root.findall(".//PubmedArticle"):
        pmid = art.findtext(".//PMID") or ""
        title = _text(art.find(".//ArticleTitle"))
        journal = art.findtext(".//Journal/Title") or ""
        year = (
            art.findtext(".//PubDate/Year")
            or art.findtext(".//PubDate/MedlineDate")
            or ""
        )

        authors: list[str] = []
        for au in art.findall(".//Author")[:6]:
            last = au.findtext("LastName") or ""
            init = au.findtext("Initials") or ""
            if last:
                authors.append(f"{last} {init}".strip())

        abst_parts = []
        for ab in art.findall(".//Abstract/AbstractText"):
            label = ab.get("Label")
            text = _text(ab)
            abst_parts.append(f"{label}: {text}" if label else text)
        abstract = " ".join(abst_parts).strip()

        doi = ""
        for aid in art.findall(".//ArticleId"):
            if aid.get("IdType") == "doi":
                doi = (aid.text or "").strip()
                break

        out.append(
            {
                "pmid": pmid,
                "title": title,
                "journal": journal,
                "year": year,
                "authors": authors,
                "abstract": abstract,
                "doi": doi,
            }
        )
    return out


# -----------------------------------------------------------------------------
# bioRxiv / medRxiv access
# -----------------------------------------------------------------------------

def fetch_preprints_server(server: str, days: int) -> list[dict]:
    """Page through bioRxiv/medRxiv API for the given date range."""
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=days)
    date_range = f"{start.isoformat()}/{today.isoformat()}"

    results: list[dict] = []
    cursor = 0
    for _ in range(100):  # hard safety cap: ~10,000 records per server max
        url = f"{BIORXIV_API}/{server}/{date_range}/{cursor}"
        r = requests.get(url, timeout=60)
        r.raise_for_status()
        data = r.json()
        collection = data.get("collection", []) or []
        if not collection:
            break
        results.extend(collection)

        messages = data.get("messages") or []
        try:
            count = int(messages[0].get("count", 0))
            total = int(messages[0].get("total", 0))
        except (IndexError, ValueError, TypeError, AttributeError):
            break

        cursor += count if count else 100
        if count == 0 or cursor >= total:
            break
        time.sleep(0.5)  # polite pacing
    return results


def search_preprints(days: int) -> list[dict]:
    """Return preprints matching BV keywords AND a clinical-lab context term,
    deduplicated by DOI (keeping the highest version)."""
    keywords_lower = [k.lower() for k in PREPRINT_KEYWORDS]
    context_lower = [c.lower() for c in PREPRINT_REQUIRED_CONTEXT]
    matches: list[dict] = []

    for server in ("biorxiv", "medrxiv"):
        try:
            raw = fetch_preprints_server(server, days)
        except requests.RequestException as e:
            print(f"WARNING: {server} fetch failed: {e}", file=sys.stderr)
            continue

        for pp in raw:
            category = (pp.get("category") or "").lower().strip()
            if server == "biorxiv" and category in BIORXIV_EXCLUDE_CATEGORIES:
                continue

            title = (pp.get("title") or "").strip()
            abstract = (pp.get("abstract") or "").strip()
            haystack = f"{title} {abstract}".lower()
            if not any(kw in haystack for kw in keywords_lower):
                continue
            if not any(ctx in haystack for ctx in context_lower):
                continue

            matches.append(
                {
                    "doi": (pp.get("doi") or "").strip(),
                    "title": title,
                    "abstract": abstract,
                    "authors": (pp.get("authors") or "").strip(),
                    "date": (pp.get("date") or "").strip(),
                    "category": (pp.get("category") or "").strip(),
                    "server": server,
                    "version": str(pp.get("version") or "1"),
                }
            )

    # Deduplicate by DOI, keep highest version
    by_doi: dict[str, dict] = {}
    for m in matches:
        doi = m["doi"]
        if not doi:
            continue
        existing = by_doi.get(doi)
        if existing is None:
            by_doi[doi] = m
        else:
            try:
                if int(m["version"]) > int(existing["version"]):
                    by_doi[doi] = m
            except ValueError:
                pass

    out = list(by_doi.values())
    out.sort(key=lambda m: m["date"], reverse=True)  # newest first
    return out


# -----------------------------------------------------------------------------
# Seen-ID persistence (PMIDs and DOIs combined, insertion-ordered)
# -----------------------------------------------------------------------------

def load_seen() -> list[str]:
    if SEEN_FILE.exists():
        try:
            data = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [str(x) for x in data]
        except json.JSONDecodeError:
            pass
    return []


def save_seen(ordered: list[str]) -> None:
    # Trim from the front (oldest) to keep the most recent SEEN_MAX_KEEP
    trimmed = ordered[-SEEN_MAX_KEEP:]
    SEEN_FILE.write_text(json.dumps(trimmed, indent=2), encoding="utf-8")


# -----------------------------------------------------------------------------
# Email rendering & sending
# -----------------------------------------------------------------------------

def _truncate(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n] + "…"


def render_articles_block(articles: list[dict]) -> str:
    if not articles:
        return "<p style='color:#666;'>No new PubMed articles this week.</p>"
    blocks = []
    for a in articles:
        authors_str = ", ".join(a["authors"])
        if len(a["authors"]) >= 6:
            authors_str += ", et al."
        pmid_link = (
            f'<a href="https://pubmed.ncbi.nlm.nih.gov/{a["pmid"]}/">'
            f'PMID {a["pmid"]}</a>'
        )
        doi_link = (
            f' · <a href="https://doi.org/{a["doi"]}">DOI</a>' if a["doi"] else ""
        )
        abstract = a["abstract"] or "<i>No abstract available.</i>"
        abstract = _truncate(abstract, 1500)
        blocks.append(
            f'<div style="margin:18px 0;padding-bottom:14px;'
            f'border-bottom:1px solid #e2e2e2;">'
            f'<div style="font-size:15px;font-weight:600;line-height:1.35;">'
            f'{a["title"]}</div>'
            f'<div style="color:#555;font-size:13px;margin:4px 0;">'
            f'<i>{authors_str}</i></div>'
            f'<div style="color:#555;font-size:13px;margin:4px 0;">'
            f'{a["journal"]} ({a["year"]}) · {pmid_link}{doi_link}</div>'
            f'<div style="font-size:13px;margin-top:8px;line-height:1.45;">'
            f'{abstract}</div>'
            f'</div>'
        )
    return "".join(blocks)


def render_preprints_block(preprints: list[dict]) -> str:
    if not preprints:
        return "<p style='color:#666;'>No new preprints this week.</p>"
    blocks = []
    for pp in preprints:
        server_label = "bioRxiv" if pp["server"] == "biorxiv" else "medRxiv"
        doi_link = (
            f'<a href="https://doi.org/{pp["doi"]}">DOI</a>' if pp["doi"] else ""
        )
        category = f' · {pp["category"]}' if pp["category"] else ""
        version = f' · v{pp["version"]}' if pp["version"] not in ("", "1") else ""
        abstract = pp["abstract"] or "<i>No abstract available.</i>"
        abstract = _truncate(abstract, 1500)
        authors_str = _truncate(pp["authors"], 200)
        blocks.append(
            f'<div style="margin:18px 0;padding-bottom:14px;'
            f'border-bottom:1px solid #e2e2e2;">'
            f'<div style="font-size:15px;font-weight:600;line-height:1.35;">'
            f'{pp["title"]}</div>'
            f'<div style="color:#555;font-size:13px;margin:4px 0;">'
            f'<i>{authors_str}</i></div>'
            f'<div style="color:#555;font-size:13px;margin:4px 0;">'
            f'{server_label} preprint · {pp["date"]}{category}{version} · {doi_link}'
            f'</div>'
            f'<div style="font-size:13px;margin-top:8px;line-height:1.45;">'
            f'{abstract}</div>'
            f'</div>'
        )
    return "".join(blocks)


def render_html(
    articles: list[dict],
    preprints: list[dict],
    total_pubmed_hits: int,
) -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    head = (
        f'<div style="font-family:Arial,Helvetica,sans-serif;max-width:780px;">'
        f'<h2 style="margin-bottom:4px;">Biological Variation alert</h2>'
        f'<p style="color:#666;margin-top:0;">{today} · '
        f'PubMed: {len(articles)} new of {total_pubmed_hits} hit(s) · '
        f'Preprints: {len(preprints)} new · '
        f'window: last {RELDATE_DAYS} days</p>'
    )
    pubmed_section = (
        '<h3 style="margin-top:24px;border-bottom:2px solid #1a73e8;'
        'padding-bottom:4px;">📄 PubMed</h3>' + render_articles_block(articles)
    )
    preprint_section = (
        '<h3 style="margin-top:24px;border-bottom:2px solid #2ca02c;'
        'padding-bottom:4px;">📝 Preprints (bioRxiv / medRxiv)</h3>'
        + render_preprints_block(preprints)
    )
    return head + pubmed_section + preprint_section + "</div>"


def send_email(html: str, subject: str, recipients: list[str]) -> None:
    user = os.environ["GMAIL_USER"]
    pwd = os.environ["GMAIL_APP_PASSWORD"]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText("HTML email. Please view in an HTML-capable client.", "plain"))
    msg.attach(MIMEText(html, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(user, pwd)
        s.sendmail(user, recipients, msg.as_string())


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

def main() -> int:
    seen_list = load_seen()
    seen_set = set(seen_list)

    # --- PubMed ---
    pmids = esearch(QUERY, RELDATE_DAYS)
    new_pmids = [p for p in pmids if p not in seen_set]
    articles = efetch(new_pmids) if new_pmids else []
    order = {p: i for i, p in enumerate(new_pmids)}
    articles.sort(key=lambda a: order.get(a["pmid"], 1e9))

    # --- Preprints ---
    try:
        all_preprints = search_preprints(RELDATE_DAYS)
    except Exception as e:  # noqa: BLE001 -- never let preprints break PubMed flow
        print(f"WARNING: preprint search failed: {e}", file=sys.stderr)
        all_preprints = []
    new_preprints = [pp for pp in all_preprints if pp["doi"] not in seen_set]

    # --- Email ---
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    subject = (
        f"BV alert · {len(articles)} PubMed + {len(new_preprints)} preprints · {today}"
    )
    html = render_html(articles, new_preprints, total_pubmed_hits=len(pmids))
    send_email(html, subject, RECIPIENTS)

    # --- Persist ---
    seen_list.extend(new_pmids)
    seen_list.extend(pp["doi"] for pp in new_preprints if pp["doi"])
    save_seen(seen_list)

    print(
        f"[{today}] pubmed_hits={len(pmids)} pubmed_new={len(new_pmids)} "
        f"preprint_new={len(new_preprints)} "
        f"emailed_to={','.join(RECIPIENTS)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
