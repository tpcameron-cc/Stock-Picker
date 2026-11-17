"""House of Representatives Periodic Transaction Reports (PTRs) from the Clerk's public disclosure site.

    index:  https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip   (XML list of filings)
    PTR:    https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{doc_id}.pdf

Electronically filed PTRs (DocID starting with 2) have a text layer and are parsed; scanned paper filings
have none and are reported as unparsed (never guessed). PDFs are immutable once filed, so they are cached
indefinitely. Legal: 5 U.S.C. 13107 prohibits using these reports for commercial purposes (other than news
media), credit ratings, or soliciting money. This project uses them for personal, non-commercial research.
"""

from __future__ import annotations

import io
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime

import pandas as pd
import requests

from core.config import Settings, settings as default_settings
from core.providers.base import DiskCache, RateLimiter

BASE = "https://disclosures-clerk.house.gov/public_disc"
INDEX_URL = BASE + "/financial-pdfs/{year}FD.zip"
PTR_URL = BASE + "/ptr-pdfs/{year}/{doc_id}.pdf"
HEADERS = {"User-Agent": "Mozilla/5.0 (stock-picker personal research script)"}

# One transaction anchor: [owner] asset... TYPE MM/DD/YYYY MM/DD/YYYY $amount[ - $amount]
TXN_RE = re.compile(
    r"^(?:(?P<owner>SP|JT|DC)\s+)?(?P<asset>.*?)\s*"
    r"(?P<type>P|S \(partial\)|S|E)\s+"
    r"(?P<date>\d{2}/\d{2}/\d{4})\s+(?P<notified>\d{2}/\d{2}/\d{4})\s+"
    r"(?P<amount>Spouse/DC Over(?:\s+\$[\d,]+)?|(?:Over\s+)?\$[\d,]+(?:\s*-\s*(?:\$[\d,]+)?)?)\s*(?P<rest>.*)$"
)
AMOUNT_TAIL_RE = re.compile(r"\$[\d,]+")
TICKER_RE = re.compile(r"\(([A-Z][A-Z0-9.\-]{0,6})\)")
ASSET_TYPE_RE = re.compile(r"\[([A-Z]{2})\]")
# Garbled field labels pdfplumber produces for "Filing Status:", "Subholding Of:", "Description:", "Comments:"
LABEL_RE = re.compile(r"^[A-Z](?:\s{2,}|\x00)")
END_MARKERS = ("* For the complete list of asset type", "I CERTIFY", "Initial Public Offerings")


@dataclass
class ParsedPTR:
    doc_id: str
    parsed: bool                       # False = no text layer (scanned) or nothing recognizable
    transactions: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)   # description / comment lines (e.g. managed account)
    member_name: str | None = None
    state_district: str | None = None


def parse_amount(text: str) -> tuple[float | None, float | None]:
    """'$1,001 - $15,000' -> (1001, 15000); 'Over $50,000,000' -> (50000000, None)."""
    nums = [float(x.replace("$", "").replace(",", "")) for x in AMOUNT_TAIL_RE.findall(text)]
    if not nums:
        return None, None
    if "over" in text.strip().lower()[:16]:
        return nums[0], None
    return nums[0], nums[1] if len(nums) > 1 else None


def _d(s: str) -> date | None:
    try:
        return datetime.strptime(s, "%m/%d/%Y").date()
    except ValueError:
        return None


def parse_ptr_text(text: str, doc_id: str = "") -> ParsedPTR:
    """Parse the text layer of one e-filed PTR into transactions. Pure function."""
    out = ParsedPTR(doc_id=doc_id, parsed=False)
    if not text or not text.strip():
        out.notes.append("no text layer (scanned filing)")
        return out
    m = re.search(r"Name:\s*(.+)", text)
    out.member_name = m.group(1).strip() if m else None
    m = re.search(r"State/District:\s*([A-Z]{2}\d{0,2})", text)
    out.state_district = m.group(1) if m else None

    lines = [ln.rstrip() for ln in text.splitlines()]
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip() == "$200?") + 1
    except StopIteration:
        start = 0
    body: list[str] = []
    for ln in lines[start:]:
        if any(ln.startswith(mk) for mk in END_MARKERS):
            break
        body.append(ln)

    # States: "txn" = lines continue the current transaction's asset text; "label" = lines continue a
    # field (filing status / subholding / description / comments) and belong to notes, never to an asset.
    current: dict | None = None
    mode: str | None = None
    for ln in body:
        s = ln.strip()
        # Column headers repeat at the top of every page; skip them wherever they appear.
        if not s or s.startswith(("ID Owner", "Type Date", "Filing ID")) or s == "$200?":
            continue
        s = s.replace(" $200? ", " ").replace("$200?", "").strip()
        a = TXN_RE.match(s)
        if a and _d(a.group("date")):
            if current:
                out.transactions.append(current)
            amt = a.group("amount")
            lo, hi = parse_amount(amt)
            current = {"owner": a.group("owner") or "self", "asset": a.group("asset").strip(),
                       "type": "S" if a.group("type").startswith("S") else a.group("type"),
                       "partial": a.group("type") == "S (partial)",
                       "trade_date": _d(a.group("date")), "notified_date": _d(a.group("notified")),
                       "amount_low": lo, "amount_high": hi, "_needs_high": amt.rstrip().endswith("-"),
                       "_needs_low": lo is None,          # "Spouse/DC Over" with the figure on the next line
                       "_tail": a.group("rest")}
            mode = "txn"
            continue
        if LABEL_RE.match(ln):
            if current:
                out.transactions.append(current)
                current = None
            body_txt = s.split(":", 1)[1].strip() if ":" in s else ""
            if s[0] in "CD":                     # Comments / Description: keep the text
                out.notes.append(s[1:].strip(" :\x00"))
                mode = "label"
            elif s[0] == "S" and out.transactions and not out.transactions[-1].get("account"):
                out.transactions[-1]["account"] = body_txt      # "Subholding Of": which account traded
                mode = "account"
            else:
                mode = "skip"
            continue
        if mode == "txn" and current is not None:
            if current["_needs_low"]:
                hit = AMOUNT_TAIL_RE.search(s)
                if hit:
                    current["amount_low"] = float(hit.group().replace("$", "").replace(",", ""))
                    current["_needs_low"] = False
                    s = (s[:hit.start()] + s[hit.end():]).strip()
            if current["_needs_high"]:
                hit = AMOUNT_TAIL_RE.search(s)
                if hit:
                    current["amount_high"] = float(hit.group().replace("$", "").replace(",", ""))
                    current["_needs_high"] = False
                    s = (s[:hit.start()] + s[hit.end():]).strip()
            current["asset"] = (current["asset"] + " " + s).strip()
        elif mode == "label" and out.notes and s:
            out.notes[-1] = (out.notes[-1] + " " + s).strip()
        elif mode == "account" and out.transactions and s:
            out.transactions[-1]["account"] = (out.transactions[-1]["account"] + " " + s).strip()
    if current:
        out.transactions.append(current)

    for t in out.transactions:
        tail = t.pop("_tail", "")
        t.pop("_needs_high", None)
        t.pop("_needs_low", None)
        tk = TICKER_RE.search(t["asset"])
        at = ASSET_TYPE_RE.search(t["asset"])
        t["ticker"] = tk.group(1).replace(".", "-") if tk else None
        t["asset_type"] = at.group(1) if at else None
        t["cap_gains_over_200"] = "Yes" in tail if tail else None
    out.parsed = bool(out.transactions)
    if not out.parsed:
        out.notes.append("text layer present but no transactions recognized")
    # Any managed-account / blind-trust language anywhere in the filing
    for kw in ("blind trust", "managed account", "discretionary", "managed by", "investment advisor"):
        if kw in text.lower():
            out.notes.append(f"keyword: {kw}")
    return out


class HouseDisclosureProvider:
    name = "house_clerk_ptr"
    source_url = "https://disclosures-clerk.house.gov/FinancialDisclosure"

    def __init__(self, cfg: Settings | None = None):
        self.cfg = cfg or default_settings()
        self.cache = DiskCache(self.cfg.path_for("paths.cache_dir"), "house_disclosures")
        self.limiter = RateLimiter(float(self.cfg.get("political.house.min_seconds_between_calls", 0.5)))

    def _get(self, url: str) -> bytes:
        self.limiter.wait()
        r = requests.get(url, headers=HEADERS, timeout=60)
        r.raise_for_status()
        return r.content

    def index(self, year: int) -> pd.DataFrame:
        """All filings for a year: last, first, filing_type, state_district, filing_date, doc_id, year."""
        ttl = 24 * 365 if year < date.today().year - 1 else 12

        def fetch() -> pd.DataFrame:
            z = zipfile.ZipFile(io.BytesIO(self._get(INDEX_URL.format(year=year))))
            xml = z.read(f"{year}FD.xml").decode("utf-8-sig", "replace")
            rows = []
            for e in ET.fromstring(xml).findall("Member"):
                rows.append({"last": (e.findtext("Last") or "").strip(), "first": (e.findtext("First") or "").strip(),
                             "filing_type": e.findtext("FilingType"), "state_district": e.findtext("StateDst"),
                             "filing_date": pd.to_datetime(e.findtext("FilingDate"), errors="coerce"),
                             "doc_id": (e.findtext("DocID") or "").strip(), "year": year})
            df = pd.DataFrame(rows)
            df["filing_date"] = df["filing_date"].dt.date
            return df

        return self.cache.get_or_fetch(f"index|{year}", ttl, fetch).value

    def ptr_text(self, year: int, doc_id: str) -> str:
        """Extracted text of one PTR. The TEXT is cached indefinitely (filings never change; amendments are new
        filings), so parser improvements re-parse from cache without downloading again."""
        import pdfplumber

        def fetch() -> str:
            raw = self._get(PTR_URL.format(year=year, doc_id=doc_id))
            with pdfplumber.open(io.BytesIO(raw)) as pdf:
                return "\n".join((p.extract_text() or "") for p in pdf.pages)

        return self.cache.get_or_fetch(f"ptrtext|{year}|{doc_id}", 24 * 3650, fetch).value

    def ptr(self, year: int, doc_id: str) -> ParsedPTR:
        return parse_ptr_text(self.ptr_text(year, doc_id), doc_id)

    def transactions(self, years: list[int], progress: bool = False) -> tuple[pd.DataFrame, pd.DataFrame]:
        """All parsed PTR transactions for the given years, plus a per-filing coverage table."""
        rows, cover = [], []
        for y in years:
            idx = self.index(y)
            ptrs = idx[idx["filing_type"] == "P"]
            for i, f in enumerate(ptrs.itertuples()):
                if progress and i % 50 == 0:
                    print(f"  {y}: {i}/{len(ptrs)} PTRs", flush=True)
                try:
                    p = self.ptr(y, f.doc_id)
                except Exception as e:
                    cover.append({"doc_id": f.doc_id, "year": y, "last": f.last, "state_district": f.state_district,
                                  "parsed": False, "n": 0, "note": f"download/parse error {type(e).__name__}"})
                    continue
                cover.append({"doc_id": f.doc_id, "year": y, "last": f.last, "state_district": f.state_district,
                              "parsed": p.parsed, "n": len(p.transactions), "note": "; ".join(p.notes)})
                for t in p.transactions:
                    rows.append({**t, "doc_id": f.doc_id, "year": y, "last": f.last, "first": f.first,
                                 "state_district": f.state_district, "filing_date": f.filing_date,
                                 "filing_notes": "; ".join(p.notes),
                                 "source_url": PTR_URL.format(year=y, doc_id=f.doc_id)})
        return pd.DataFrame(rows), pd.DataFrame(cover)
