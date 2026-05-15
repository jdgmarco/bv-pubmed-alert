"""
Weekly PubMed alert on Biological Variation.

- Queries PubMed E-utilities with a user-defined search string.
- Filters to articles indexed in the last N days, deduplicating against
  previously seen PMIDs stored in seen_pmids.json.
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
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Iterable

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

RECIPIENTS = ["jdgmarco@gmail.com", "isabelmorenoparro@gmail.com"]

# Slightly larger than 7 to absorb scheduling jitter / late indexing;
# duplicates are filtered via seen_pmids.json
RELDATE_DAYS = 10
MAX_RESULTS = 500

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
TOOL_NAME = "bv-pubmed-alert"
CONTACT_EMAIL = "jdgmarco@gmail.com"

SEEN_FILE = Path("seen_pmids.json")
SEEN_MAX_KEEP = 5000  # cap to keep file small

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
# Seen-PMID persistence
# -----------------------------------------------------------------------------

def load_seen() -> set[str]:
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            return set()
    return set()


def save_seen(seen: Iterable[str]) -> None:
    ordered = sorted({s for s in seen if s.isdigit()}, key=int, reverse=True)
    SEEN_FILE.write_text(
        json.dumps(ordered[:SEEN_MAX_KEEP], indent=2), encoding="utf-8"
    )


# -----------------------------------------------------------------------------
# Email rendering & sending
# -----------------------------------------------------------------------------

def render_html(articles: list[dict], total_hits: int) -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    head = (
        f'<div style="font-family:Arial,Helvetica,sans-serif;max-width:780px;">'
        f'<h2 style="margin-bottom:4px;">PubMed Biological Variation alert</h2>'
        f'<p style="color:#666;margin-top:0;">{today} · '
        f'{len(articles)} new of {total_hits} hit(s) in the last {RELDATE_DAYS} days</p>'
    )

    if not articles:
        return head + "<p>No new articles this week.</p></div>"

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
        if len(abstract) > 1500:
            abstract = abstract[:1500] + "…"
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
    return head + "".join(blocks) + "</div>"


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
    seen = load_seen()
    pmids = esearch(QUERY, RELDATE_DAYS)
    new_pmids = [p for p in pmids if p not in seen]

    articles = efetch(new_pmids) if new_pmids else []
    # Preserve PubMed's relevance ordering (esearch default)
    order = {p: i for i, p in enumerate(new_pmids)}
    articles.sort(key=lambda a: order.get(a["pmid"], 1e9))

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    subject = f"PubMed BV alert · {len(articles)} new · {today}"
    html = render_html(articles, total_hits=len(pmids))
    send_email(html, subject, RECIPIENTS)

    seen.update(new_pmids)
    save_seen(seen)

    print(
        f"[{today}] hits={len(pmids)} new={len(new_pmids)} "
        f"emailed_to={','.join(RECIPIENTS)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
