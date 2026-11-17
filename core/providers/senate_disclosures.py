"""Senate Periodic Transaction Reports from the Senate eFD site (efdsearch.senate.gov).

Access requires accepting the site's agreement ("I understand the prohibitions on obtaining and use of financial
disclosure reports", Ethics in Government Act). The user read and accepted it on 2026-11-16 for personal,
non-commercial research and authorized this provider to accept it on their behalf (see CLAUDE.md).

Electronic PTRs are HTML tables and are parsed; paper filings are scanned images and are counted as unparsed.
Report pages never change (amendments are new reports), so their HTML is cached indefinitely.
"""

from __future__ import annotations

import html as htmllib
import re
from datetime import date, datetime

import pandas as pd
import requests

from core.config import Settings, settings as default_settings
from core.providers.base import DiskCache, RateLimiter
from core.providers.house_disclosures import parse_amount

BASE = "https://efdsearch.senate.gov"
HEADERS = {"User-Agent": "Mozilla/5.0 (stock-picker personal research script)"}
PTR_REPORT_TYPE = "[11]"
OWNER = {"self": "self", "spouse": "SP", "joint": "JT", "child": "DC", "dependent child": "DC"}
ASSET = {"stock": "ST", "stock option": "OP", "corporate bond": "CS", "municipal security": "GS",
         "government security": "GS", "mutual fund": "MF", "exchange traded fund/note": "EF",
         "other securities": "OT", "cryptocurrency": "CT", "non-public stock": "OT"}


def _clean(cell: str) -> str:
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", cell))).strip()


def parse_ptr_html(page: str) -> list[dict]:
    """Transactions from an electronic Senate PTR page. Pure function."""
    m = re.search(r"<tbody>(.*?)</tbody>", page, re.S)
    if not m:
        return []
    out = []
    for tr in re.findall(r"<tr>(.*?)</tr>", m.group(1), re.S):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
        if len(cells) < 8:
            continue
        num, tdate, owner, ticker, asset, atype, ttype, amount = (_clean(c) for c in cells[:8])
        comment = _clean(cells[8]) if len(cells) > 8 else ""
        try:
            d = datetime.strptime(tdate, "%m/%d/%Y").date()
        except ValueError:
            continue
        tl = ttype.lower()
        typ = "P" if tl.startswith("purchase") else "S" if tl.startswith("sale") else "E" if tl.startswith("exchange") else tl
        lo, hi = parse_amount(amount)
        out.append({"owner": OWNER.get(owner.lower(), owner or "self"), "asset": asset,
                    "ticker": None if ticker in ("", "--") else ticker.replace(".", "-").upper(),
                    "asset_type": ASSET.get(atype.lower(), "OT"), "type": typ, "partial": "partial" in tl,
                    "trade_date": d, "notified_date": None, "amount_low": lo, "amount_high": hi,
                    "account": None, "comment": "" if comment == "--" else comment})
    return out


def parse_search_row(row: list) -> dict | None:
    """One row of the search API: [first, last, office, link_html, date_received]."""
    link = re.search(r'href="([^"]+)"', row[3] or "")
    if not link:
        return None
    href = link.group(1)
    try:
        received = datetime.strptime(row[4], "%m/%d/%Y").date()
    except (ValueError, TypeError):
        received = None
    return {"first": row[0], "last": row[1], "office": row[2], "title": _clean(row[3]), "href": href,
            "doc_id": href.rstrip("/").split("/")[-1], "paper": "/paper/" in href, "filing_date": received,
            "is_senator": "(Senator)" in (row[2] or ""), "amendment": "amendment" in _clean(row[3]).lower()}


class SenateDisclosureProvider:
    name = "senate_efd_ptr"
    source_url = BASE + "/search/"

    def __init__(self, cfg: Settings | None = None):
        self.cfg = cfg or default_settings()
        self.cache = DiskCache(self.cfg.path_for("paths.cache_dir"), "senate_disclosures")
        self.limiter = RateLimiter(float(self.cfg.get("political.senate.min_seconds_between_calls", 1.0)))
        self._session: requests.Session | None = None

    def _agreed_session(self) -> requests.Session:
        """Session that has accepted the eFD agreement (authorized by the user; see module docstring)."""
        if self._session is not None:
            return self._session
        s = requests.Session()
        s.headers.update(HEADERS)
        self.limiter.wait()
        r = s.get(BASE + "/search/home/", timeout=30)
        r.raise_for_status()
        tok = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', r.text)
        if not tok:
            raise RuntimeError("eFD agreement form not found (site layout changed?)")
        self.limiter.wait()
        r = s.post(BASE + "/search/home/", data={"prohibition_agreement": "1", "csrfmiddlewaretoken": tok.group(1)},
                   headers={"Referer": BASE + "/search/home/"}, timeout=30)
        r.raise_for_status()
        self._session = s
        return s

    def reports(self, start: date, end: date | None = None) -> pd.DataFrame:
        """PTR filings received in [start, end]. Cached 12h."""
        def fetch() -> pd.DataFrame:
            s = self._agreed_session()
            rows, offset = [], 0
            while True:
                self.limiter.wait()
                data = {"start": str(offset), "length": "100", "report_types": PTR_REPORT_TYPE, "filer_types": "[]",
                        "submitted_start_date": start.strftime("%m/%d/%Y 00:00:00"),
                        "submitted_end_date": end.strftime("%m/%d/%Y 23:59:59") if end else "",
                        "candidate_state": "", "senator_state": "", "office_id": "", "first_name": "", "last_name": "",
                        "csrfmiddlewaretoken": s.cookies.get("csrftoken", "")}
                r = s.post(BASE + "/search/report/data/", data=data, timeout=60,
                           headers={"Referer": BASE + "/search/", "X-CSRFToken": s.cookies.get("csrftoken", "")})
                r.raise_for_status()
                j = r.json()
                batch = [x for x in (parse_search_row(row) for row in j.get("data", [])) if x]
                rows += batch
                offset += 100
                if offset >= int(j.get("recordsTotal", 0)) or not batch:
                    break
            return pd.DataFrame(rows)

        return self.cache.get_or_fetch(f"reports|{start}|{end}", 12, fetch).value

    def report_html(self, href: str) -> str:
        def fetch() -> str:
            s = self._agreed_session()
            self.limiter.wait()
            r = s.get(BASE + href, timeout=60)
            r.raise_for_status()
            if "prohibition_agreement" in r.text and "<tbody>" not in r.text:
                raise RuntimeError("eFD session lost the agreement; retry")
            return r.text

        return self.cache.get_or_fetch(f"report|{href}", 24 * 3650, fetch).value

    def transactions(self, start: date, progress: bool = False) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Senators' PTR transactions filed since `start`, plus a per-filing coverage table."""
        reps = self.reports(start)
        reps = reps[reps["is_senator"]] if len(reps) else reps
        rows, cover = [], []
        for i, f in enumerate(reps.itertuples()):
            if progress and i % 50 == 0:
                print(f"  senate: {i}/{len(reps)} PTRs", flush=True)
            base = {"doc_id": f.doc_id, "year": f.filing_date.year if f.filing_date else None, "last": f.last,
                    "state_district": None}
            if f.paper:
                cover.append({**base, "parsed": False, "n": 0, "note": "paper filing (scanned image)"})
                continue
            try:
                txs = parse_ptr_html(self.report_html(f.href))
            except Exception as e:
                cover.append({**base, "parsed": False, "n": 0, "note": f"download/parse error {type(e).__name__}"})
                continue
            cover.append({**base, "parsed": bool(txs), "n": len(txs),
                          "note": "amendment" if f.amendment else ("" if txs else "no transactions recognized")})
            for t in txs:
                comment = t.pop("comment", "")
                rows.append({**t, "doc_id": f.doc_id, "year": base["year"], "last": f.last, "first": f.first,
                             "state_district": None, "filing_date": f.filing_date, "chamber": "senate",
                             "filing_notes": comment, "amendment": f.amendment, "source_url": BASE + f.href})
        return pd.DataFrame(rows), pd.DataFrame(cover)
