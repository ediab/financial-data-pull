#!/usr/bin/env python3
"""Is the latest snapshot a complete, correct view? Run this against your own store.

Not part of `tests/run_all.sh` — that suite is offline and self-contained, while this
needs a populated `data/` store. It reads nothing but what is already held (zero
network, nothing written) and answers the question a consumer actually has: if I model
from the newest snapshot today, what am I missing and is it right?

Three independent kinds of check:

* **Plumbing** — every table in the latest manifest is readable with no `run_id`, one
  CSV per table, the named index survives as a column.
* **Internal arithmetic** — `Assets = Liabilities + Equity` at every quarter-end,
  `YTD = Σ quarters` across filings, cash-flow ending cash vs balance-sheet cash,
  12 contiguous quarters of revenue with Q4 derived as `FY − 9M YTD`.
* **External** — every quarter's revenue must appear in that quarter's own earnings
  release (`data/derived/<issuer>/8k_cells.csv`), which is an independent parse of the
  company's own document. A derived Q4 that ties to the press release to the dollar is
  the strongest offline evidence the tables are right.

Usage:
    .venv/bin/python scripts/check_store_view.py            # every issuer held
    .venv/bin/python scripts/check_store_view.py ANET VRT   # named issuers

Exit code 0 when every check passes, 1 otherwise.
"""
from __future__ import annotations

import glob
import re
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from financial_data_pull import read_table, store  # noqa: E402
from financial_data_pull.pull import (TRANSCRIPT_QUARTERS, held_transcript_quarters,  # noqa: E402
                                     manifest)

PERIOD = re.compile(r"^\d{4}-\d{2}-\d{2}")
QUARTER_MONTH = {3: "Q1", 6: "Q2", 9: "Q3", 12: "Q4"}
REVENUE = "RevenueFromContractWithCustomerExcludingAssessedTax"
REVENUE_LABEL = r"revenue|net sales"          # ANET: "Total revenue"; VRT: "Net sales"
TALLY = {"ok": 0, "fail": 0, "warn": 0}


def check(cond, label, detail=""):
    TALLY["ok" if cond else "fail"] += 1
    print(f"    {'PASS' if cond else 'FAIL'}  {label}" + (f"  [{detail}]" if detail else ""))
    return cond


def warn(detail):
    """A real gap that is not a defect: report it without failing the run."""
    TALLY["warn"] += 1
    print(f"    WARN  {detail}")


def cols(df):
    """Period columns, in document order."""
    return [c for c in df.columns if PERIOD.match(str(c))]


def pick(df, concept_end, col, label_pat=None):
    """First non-null consolidated value for a concept, in one period column.

    `dimension` is a boolean: the consolidated line is `False`, segment rows are `True`
    and share the same concept, so it must be filtered or the first segment wins.
    """
    r = df[df.concept.astype(str).str.endswith(concept_end, na=False)]
    if "dimension" in df.columns:
        r = r[~r.dimension.astype(bool)]
    if label_pat is not None:
        r = r[r.label.astype(str).str.contains(label_pat, case=False, na=False, regex=True)]
    for _, x in r.iterrows():
        v = pd.to_numeric(x[col], errors="coerce")
        if not pd.isna(v):
            return float(v)
    return None


def qlabel(col):
    """The document's own quarter label: '2026-08-02 (Q3)' -> ('2026Q3', '2026-08-02').

    A fiscal filer's quarters do not land on calendar months (AVGO ends 2026-08-02), so the
    label decides the quarter and its date is the quarter end. Without a marker, fall back
    to the calendar month.
    """
    date = str(col)[:10]
    m = re.search(r"\((Q[1-4])\)", str(col))
    if m:
        return f"{date[:4]}{m.group(1)}", date
    return f"{date[:4]}{QUARTER_MONTH[int(date[5:7])]}", date


def consecutive(quarters):
    """Are these quarter labels one after another with no gap?"""
    ords = [int(q[:4]) * 4 + int(q[5]) - 1 for q in quarters]
    return all(b - a == 1 for a, b in zip(ords, ords[1:]))


def family(tables, prefix):
    """Table names of one family, oldest index last: income_quarterly_0 is the newest filing."""
    return sorted((t for t in tables if t.startswith(prefix)),
                  key=lambda s: int(s.rsplit("_", 1)[1]))


def revenue_quarters(T, inc_q, inc_a):
    """Quarterly revenue by the filing's own fiscal quarter, plus Q4 derived as FY − 9M YTD.

    Both sides of the derivation come from the filing that *owns* the period — the original
    10-K for FY, the original Q3 10-Q for the 9M — never from a later filing's comparative
    column, which can be rounded.
    """
    qdata = {}
    for name in inc_q:
        df = read_table(T, name)
        dated = sorted(c for c in cols(df) if "(YTD)" not in c)
        if not dated:
            continue
        cur = dated[-1]                                  # own period; the other is the comparative
        key, end = qlabel(cur)
        own_ytd = next((c for c in cols(df) if "(YTD)" in c and c[:10] == end), None)
        qdata[key] = {"q": pick(df, REVENUE, cur, REVENUE_LABEL), "end": end,
                      "ytd": pick(df, REVENUE, own_ytd, REVENUE_LABEL) if own_ytd else None}
    for name in inc_a:
        df = read_table(T, name)
        fycols = sorted(c for c in cols(df) if "(FY)" in c)
        if not fycols:
            continue
        own = fycols[-1]                                 # this 10-K's own year
        fy, year = pick(df, REVENUE, own, REVENUE_LABEL), own[:4]
        nine = (qdata.get(f"{year}Q3") or {}).get("ytd")
        if fy and nine:
            qdata[f"{year}Q4"] = {"q": fy - nine, "end": own[:10], "ytd": None}
    return qdata


def check_issuer(T):
    print(f"\n{'='*80}\n{T}\n{'='*80}")
    try:
        m = manifest(T)
        latest = store.latest_snapshot_dir(T).name
    except (FileNotFoundError, KeyError):
        check(False, "issuer held in the store", f"no snapshot for {T}")
        return
    tables = sorted(m["table_hashes"])
    print(f"  latest snapshot {latest}  ({len(tables)} tables, sources {m['sources']})")

    # ---- plumbing -------------------------------------------------------------
    unreadable = []
    for name in tables:
        try:
            read_table(T, name)
        except Exception as e:  # noqa: BLE001
            unreadable.append(f"{name}:{type(e).__name__}")
    check(not unreadable, "every table readable from the latest snapshot alone",
          f"{len(tables)} tables" + (f"; broken {unreadable}" if unreadable else ""))

    csvdir = ROOT / "data" / "csv" / T
    n_csv = len(list(csvdir.glob("*.csv")))
    check(n_csv in (len(tables), len(tables) + 3),
          "one CSV per table, plus histories after export",
          f"{n_csv} vs {len(tables)} or {len(tables) + 3}")
    if (csvdir / "yahoo_prices.csv").exists():
        hdr = (csvdir / "yahoo_prices.csv").read_text().splitlines()[0]
        check(hdr.startswith("Date,"), "yahoo_prices.csv keeps its Date column", hdr[:58])

    inc_q, inc_a = family(tables, "income_quarterly_"), family(tables, "income_annual_")
    bal_a, bal_q = family(tables, "balance_annual_"), family(tables, "balance_quarterly_")
    cf_q = family(tables, "cashflow_quarterly_")

    # ---- quarters of revenue --------------------------------------------------
    qdata = revenue_quarters(T, inc_q, inc_a)
    got = sorted(qdata)
    check(consecutive(got) and len(got) >= 11 and all(qdata[q]["q"] for q in got),
          "contiguous quarters of revenue, none empty",
          f"{len(got)} quarters {got[0]}..{got[-1]}" if got else "none")

    # ---- internal arithmetic --------------------------------------------------
    gaps = []
    for q in got:
        if q.endswith(("Q2", "Q3")) and qdata[q]["ytd"]:
            parts = [qdata.get(f"{q[:4]}Q{i}", {}).get("q") for i in range(1, int(q[5]) + 1)]
            if all(parts):
                gaps.append(abs(qdata[q]["ytd"] - sum(parts)))
    check(bool(gaps) and max(gaps) < 1000, "YTD ties to the sum of quarters across filings",
          f"{len(gaps)} checks, worst gap ${max(gaps):,.0f}" if gaps else "no comparable years")

    worst, dates = 0.0, set()
    for name in bal_a + bal_q:
        df = read_table(T, name)
        for c in cols(df):
            a = pick(df, "Assets", c, r"^total")
            l = pick(df, "Liabilities", c, r"^total")
            # The equity total is not one concept: AVGO uses the noncontrolling-interest
            # variant, ANET and VRT the plain one.
            e = (pick(df, "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest", c, r"total.*equity")
                 or pick(df, "StockholdersEquity", c, r"total.*equity"))
            if None not in (a, l, e):
                worst = max(worst, abs(a - l - e))
                dates.add(c[:10])
    dsorted = sorted(dates)
    window = dsorted[-12:]
    day_gaps = [(pd.Timestamp(b) - pd.Timestamp(a)).days for a, b in zip(window, window[1:])]
    check(worst < 1000 and len(dsorted) >= 12, "Assets = Liabilities + Equity at every date",
          f"{len(dsorted)} dates, worst gap ${worst:,.0f}")
    check(len(window) == 12 and all(80 <= g <= 105 for g in day_gaps),
          "12 contiguous quarter-ends in the model window",
          f"{window[0]}..{window[-1]}" + (f", plus older {dsorted[0]}" if len(dsorted) > 12 else ""))

    worst_c, n_c, detail = 0.0, 0, ""
    for cf_name, bal_name in zip(cf_q, bal_q):
        cf, bh = read_table(T, cf_name), read_table(T, bal_name)
        for c in cols(cf):
            end = pick(cf, "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents", c, r"end")
            bcol = [x for x in cols(bh) if x.startswith(c[:10])]
            cash = pick(bh, "CashAndCashEquivalentsAtCarryingValue", bcol[0], r"^cash and cash equiv") if bcol else None
            if end is None or cash is None:
                continue
            n_c += 1
            if abs(end - cash) > worst_c:
                worst_c, detail = abs(end - cash), f"{c[:10]}: ${end/1e6:,.1f}M vs ${cash/1e6:,.1f}M"
    # The statement's figure is cash *and restricted cash*, so a gap is expected, not a bug.
    check(n_c >= len(bal_q) - 1 and worst_c < 0.05 * 5e9,
          "cash-flow ending cash reconciles to the balance sheet",
          f"{n_c} filings, worst gap ${worst_c/1e6:,.1f}M ({detail})")

    # ---- external: against the issuers' own earnings releases -----------------
    cells_path = ROOT / "data" / "derived" / T / "8k_cells.csv"
    eight = pd.read_csv(csvdir / "sec_8k.csv")
    matched, missing, scales, worst_delta = 0, [], {}, 0.0
    if cells_path.exists() and len(eight):
        k = pd.read_csv(cells_path)
        # Row labels carry the metric in all three issuers; the period column is labelled
        # "Three Months Ended ..." for ANET/VRT and "GAAP Q3 26" for AVGO, so the quarter
        # is identified by the release itself plus an exact value match, not by that label.
        num = k[k.value.notna()
                & k.row_label.astype(str).str.contains(REVENUE_LABEL, case=False, na=False)]
        for q in got:
            v = qdata[q]["q"]
            qe = pd.Timestamp(qdata[q]["end"])
            rel = eight[(pd.to_datetime(eight.filing_date) >= qe)
                        & (pd.to_datetime(eight.filing_date) <= qe + pd.Timedelta(days=75))]
            cand = num[num.filing_date.isin(rel.filing_date)] if len(rel) else num.iloc[0:0]
            for x in cand.value.tolist():
                for scale in (1e6, 1e3):   # releases use millions in some tables, thousands in others
                    delta = abs(float(x) * scale - v)
                    if delta <= 0.5 * scale:
                        matched += 1
                        scales[scale] = scales.get(scale, 0) + 1
                        worst_delta = max(worst_delta, delta)
                        break
                else:
                    continue
                break
            else:
                missing.append(f"{q}=${v/1e6:,.1f}M")
        check(matched == len(got),
              "every quarter's revenue appears in that quarter's own release",
              f"{matched}/{len(got)} matched, worst delta ${worst_delta:,.0f}, "
              f"scales { {int(k_): v for k_, v in scales.items()} }"
              + (f"; missing {missing}" if missing else ""))
    else:
        check(False, "releases held for the external check", "run --export-views first")

    files = glob.glob(str(ROOT / "data" / "derived" / T / "documents" / "8-k" / "*"))
    n_cells = k.groupby("accession").size() if cells_path.exists() else pd.Series(dtype=int)
    near = sum(1 for q in got if any(0 <= (pd.Timestamp(f) - pd.Timestamp(qdata[q]["end"])).days <= 75
                                     for f in eight.filing_date))
    check(len(eight) == 12 and len(files) == 12 and len(n_cells) == 12,
          "12 earnings 8-Ks: rows, exhibit files, parsed cell tables",
          f"{eight.filing_date.min()}..{eight.filing_date.max()}, cells {int(n_cells.min()) if len(n_cells) else 0}..{int(n_cells.max()) if len(n_cells) else 0}")
    check(near == len(got), "each release follows its own quarter end by <= 75 days", f"{near}/{len(got)}")

    # ---- transcripts: the canonical newest quarters must be carried, extras are a bonus
    tr = read_table(T, "av_transcript")
    carried = sorted(str(q) for q in tr.quarter.unique())
    held = sorted(held_transcript_quarters(T))
    check(len(carried) >= TRANSCRIPT_QUARTERS and consecutive(carried),
          "latest snapshot carries contiguous transcript quarters",
          f"{carried[0]}..{carried[-1]}" if carried else "none")
    if set(got[-TRANSCRIPT_QUARTERS:]) - set(carried):
        warn(f"newest filing quarters without a carried transcript: "
             f"{sorted(set(got[-TRANSCRIPT_QUARTERS:]) - set(carried))}"
             f" (transcript scope is calendar quarters, filings are fiscal — pass the labels explicitly to widen)")
    if set(held) - set(carried):
        warn(f"{len(set(held) - set(carried))} held transcript quarter(s) not carried by the latest snapshot: "
             f"{sorted(set(held) - set(carried))}")
    lens = tr.groupby("quarter").content.apply(lambda s: sum(len(str(x)) for x in s))
    check(int(lens.min()) > 5000 and int(tr.groupby("quarter").speaker.nunique().min()) >= 3,
          "transcripts carry real content",
          f"chars {int(lens.min()):,}..{int(lens.max()):,}, min speakers {int(tr.groupby('quarter').speaker.nunique().min())}")

    # ---- market data ----------------------------------------------------------
    px = read_table(T, "yahoo_prices")
    check(len(px) > 200 and str(px.index.name) == "Date", "price history with a Date index",
          f"{len(px)} rows, {px.index.min():%Y-%m-%d}..{px.index.max():%Y-%m-%d}")
    for name in ("yahoo_eps_trend", "yahoo_analyst_price_targets", "yahoo_recommendations",
                 "yahoo_upgrades_downgrades", "yahoo_earnings_estimate", "yahoo_revenue_estimate",
                 "yahoo_eps_revisions", "av_earnings_estimates"):
        if name not in tables:
            check(False, f"{name} held", "absent from the latest snapshot")
            continue
        rows = len(read_table(T, name))
        check(rows > 0, f"{name} held", f"{rows} rows")


def main(argv):
    tickers = argv[1:]
    if not tickers:
        tickers = sorted(p.name for p in (ROOT / "data" / "tables").glob("*")
                         if p.is_dir() and any(p.glob("*/snapshot.json")))
    if not tickers:
        print("no issuers held under data/tables/")
        return 1
    for T in tickers:
        check_issuer(T)
    print(f"\n{'='*80}\nTOTAL: {TALLY['ok']} passed, {TALLY['fail']} failed, "
          f"{TALLY['warn']} warning(s)  ({', '.join(tickers)})\n{'='*80}")
    return 1 if TALLY["fail"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
