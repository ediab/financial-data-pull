"""Derived views — readable, rewritable renderings of what the store already holds.

Layout, under `data/derived/<issuer>/`:
    documents/8-k/<filing_date>-<accession>-<exhibit_file>   copies of preserved exhibits
    documents/transcript/<quarter>.md                        rendered call transcript
    8k_cells.csv                                             every cell of every release table

A view is never evidence. It is rebuilt from the store on demand, may be deleted at
any time, and is never consulted as proof of what a filing said — so nothing here
touches publication, cache identity or the scope key. Everything a view is built from
is hash-checked first: a table against its manifest hash, an original against the
content-addressed directory it sits in. A mismatch aborts the export instead of
emitting bytes the store cannot vouch for.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import sys
import tempfile
from pathlib import Path

from lxml import html as lxml_html
import pandas as pd

from . import contracts, store

EIGHT_K_PREFIX = "sec_8k_"
TRANSCRIPT_TABLE = "av_transcript"
CAPTION_LIMIT = 120

# One row per cell, in a fixed order; the file is meant to be read by a tool.
CELL_COLUMNS = ("accession", "filing_date", "exhibit_sha256", "table_index", "caption",
                "row_kind", "row_index", "col_index", "row_label", "column_label",
                "raw_text", "value", "unit")

# What a cell can say about its own unit: a marker on its own (`$`), or a sign inside
# text (`26.1 %`). A release may split them into separate cells (`$ | 29,591`), which
# is why a neighbouring marker cell is also consulted.
UNIT_MARKERS = {"$": "$", "us$": "$", "%": "%"}
DASHES = "\u2012\u2013\u2014\u2015—–"
# A trailing footnote marker, as in `Diluted EPS(1)` or `Net sales(2)`.
FOOTNOTE = re.compile(r"\(\d{1,2}\)$")
NUMBER = re.compile(r"\d*\.?\d+")
NULL_TEXT = {"", "-", "n/a", "na", "nm", "n/m", "nil"}
SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")
# Tags where a line break is meant: without them a `<br>` would glue `Three months
# ended` to `June 30, 2026`. Inline tags are deliberately absent — a number split
# across `<font>`/`<span>` (or a footnote marker appended to a label) must stay one word.
LINE_BREAKS = {"br", "div", "p", "li", "tr", "td", "th", "table", "caption",
               "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6"}
# An exhibit that is not HTML has no tables to dump. The SEC provider allows a PDF
# press release, and parsing one as HTML looks exactly like a release with no numbers.
HTML_MARKERS = (b"<html", b"<!doctype html", b"<table", b"<td", b"<th")


def _flat(element) -> str:
    """An element's text, whitespace-flattened, with line breaks kept apart.

    Cell text lives in nested `<span>`/`<font>` elements, so `element.text` alone reads
    as empty on a real press release, and the text after a break lives in the break's
    own tail rather than in any element's `.text`.
    """
    chunks: list[str] = []
    for node in element.iter():
        if not isinstance(node.tag, str):
            if node.tail:
                chunks.append(node.tail)  # a comment's tail is still cell text
            continue
        if node is not element and node.tag in LINE_BREAKS:
            chunks.append(" ")
        if node.text:
            chunks.append(node.text)
        if node is not element and node.tail:
            chunks.append(node.tail)
    return " ".join("".join(chunks).split())


def _verified_bytes(path: Path) -> tuple[bytes, str]:
    """The bytes of a preserved original and their sha256, checked against its address.

    Every original lives in `raw/<issuer>/<provider>/<sha256>/`. That directory name
    is the claim about its content, so a file that no longer hashes to it is
    corruption — the export aborts naming the file rather than deriving from bytes the
    store cannot vouch for.
    """
    try:
        payload = Path(path).read_bytes()
    except OSError as exc:
        raise ValueError(
            f"{path} is recorded as an original but cannot be read: {exc}") from exc
    digest = store.sha256_bytes(payload)
    if Path(path).parent.name != digest:
        raise ValueError(
            f"{path} hashes to {digest[:12]}… but sits in "
            f"{Path(path).parent.name[:12]}… — refusing to derive from an unverified original")
    return payload, digest


def _verified_table(snap: Path, table: str):
    """One snapshot table, read hash-checked, with a vanished parquet named as corruption.

    A snapshot records the file it published, so a missing one is damage rather than a
    snapshot that never carried the table — refused the way `export_csv` refuses it, so
    the CLI turns it into a message instead of a traceback.
    """
    try:
        return contracts.read_verified_table(snap, table)
    except KeyError as exc:
        raise ValueError(
            f"snapshot {snap.name} records the table {table!r} but its parquet is missing "
            f"— refusing to derive from a partial snapshot") from exc


def _write_if_changed(path: Path, data: bytes) -> bool:
    """Write only when the bytes differ, so a re-run leaves every file untouched."""
    if path.is_file() and path.read_bytes() == data:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    Path(tmp).replace(path)
    return True


def _newest_table(issuer: str, table: str) -> tuple[Path, dict] | None:
    """The newest snapshot carrying `table`, with its manifest, or None.

    Mirrors `export_csv`: each table is taken from the newest snapshot that actually
    has it, so a later run that acquired something else does not hide an earlier run's
    evidence — and a snapshot carrying nothing relevant is not consulted at all.
    """
    for snap in reversed(store.snapshot_dirs(issuer)):
        manifest = json.loads((snap / "snapshot.json").read_text())
        if table in (manifest.get("table_hashes") or {}):
            return snap, manifest
    return None


def _eight_k_metadata(issuer: str) -> dict[str, tuple[str, str]]:
    """accession -> (filing date, exhibit file name) for every filing the store names.

    The `sec_8k` table of the newest snapshot carrying it is the primary source;
    coverage rows name the filing date of filings that table never carried, and the
    payload's own file name stands in for the exhibit name it does not know.
    """
    names: dict[str, tuple[str, str]] = {}
    hit = _newest_table(issuer, "sec_8k")
    if hit:
        snap, _ = hit
        for record in _verified_table(snap, "sec_8k").to_dict("records"):
            accession = str(record.get("accession") or "")
            if accession:
                names[accession] = (str(record.get("filing_date") or ""),
                                    str(record.get("exhibit_file") or ""))
    for snap in reversed(store.snapshot_dirs(issuer)):
        manifest = json.loads((snap / "snapshot.json").read_text())
        coverage_path = manifest.get("coverage_path")
        if not coverage_path or not Path(coverage_path).is_file():
            continue
        for row in json.loads(Path(coverage_path).read_text()).get("rows", []):
            dataset = str(row.get("dataset") or "")
            if dataset.startswith(EIGHT_K_PREFIX):
                accession = dataset[len(EIGHT_K_PREFIX):]
                names.setdefault(accession, (str(row.get("period") or ""), ""))
    return names


def _exhibits(issuer: str) -> list[dict]:
    """Every Item 2.02 exhibit held, newest filing first, one entry per accession.

    A filing appears in more than one snapshot; the newest snapshot that preserved it
    wins, exactly as `export_csv` takes each table from the newest snapshot carrying
    it. The names come from the store's own metadata so the files are recognizable
    without opening them.
    """
    names = _eight_k_metadata(issuer)
    held: dict[str, dict] = {}
    for snap in reversed(store.snapshot_dirs(issuer)):
        manifest = json.loads((snap / "snapshot.json").read_text())
        for key, path in sorted((manifest.get("originals") or {}).items()):
            if key.startswith(EIGHT_K_PREFIX):
                accession = key[len(EIGHT_K_PREFIX):]
                held.setdefault(accession, {"accession": accession, "path": Path(path),
                                            "snapshot": snap.name})

    exhibits = []
    for accession, exhibit in held.items():
        filing_date, exhibit_file = names.get(accession, ("", ""))
        exhibit["filing_date"] = SAFE_NAME.sub("-", filing_date) or "unknown-date"
        # the exhibit's own name where the store knows it, else the payload's own file
        # name: never a path from the filing's metadata
        local = SAFE_NAME.sub("-", Path(exhibit_file).name) if exhibit_file \
            else exhibit["path"].name
        exhibit["name"] = store.safe_component(
            f"{exhibit['filing_date']}-{accession}-{local}", "exhibit name")
        exhibits.append(exhibit)
    # newest filing first, then accession: a byte-stable order for the CSV
    exhibits.sort(key=lambda e: (e["filing_date"], e["accession"]), reverse=True)
    return exhibits


def _verified_exhibits(issuer: str) -> list[tuple[dict, bytes, str]]:
    """Every held exhibit with its bytes and sha256, verified before anything is written.

    Verification runs for the whole set up front: a store that lost a byte must fail the
    export before it has written a partial view, not halfway through it.
    """
    verified = []
    for exhibit in _exhibits(issuer):
        payload, digest = _verified_bytes(exhibit["path"])
        verified.append((exhibit, payload, digest))
    return verified


def export_documents(issuer: str, verified: list[tuple[dict, bytes, str]] | None = None,
                     ) -> int:
    """Copy every preserved 8-K exhibit under a name a human can read. Returns the count.

    Copies only when the destination bytes differ, and never deletes: a view can carry
    files from an older store and rewriting it must not be a destructive act.
    """
    out_dir = store.DERIVED / issuer / "documents" / "8-k"
    verified = _verified_exhibits(issuer) if verified is None else verified
    for exhibit, payload, _ in verified:
        _write_if_changed(out_dir / exhibit["name"], payload)
    return len(verified)


def _transcript_quarters(issuer: str) -> dict[str, tuple[str, list[dict]]]:
    """quarter -> (the snapshot it came from, its segments in `segment` order).

    Per quarter, the newest snapshot that actually carries it: one run may hold two
    quarters and a later one three, and neither may hide the other's evidence.
    """
    quarters: dict[str, tuple[str, list[dict]]] = {}
    for snap in reversed(store.snapshot_dirs(issuer)):
        manifest = json.loads((snap / "snapshot.json").read_text())
        if TRANSCRIPT_TABLE not in (manifest.get("table_hashes") or {}):
            continue
        frame = _verified_table(snap, TRANSCRIPT_TABLE)
        by_quarter: dict[str, list[dict]] = {}
        for record in frame.to_dict("records"):
            row = {key: ("" if record.get(key) is None else str(record.get(key)))
                   for key in ("speaker", "title", "content")}
            by_quarter.setdefault(str(record.get("quarter")), []).append(row)
        for quarter, segments in by_quarter.items():
            quarters.setdefault(quarter, (snap.name, segments))
    return quarters


def transcript_markdown(issuer: str, quarter: str, snapshot_id: str,
                        segments: list[dict]) -> str:
    """One call as Markdown: a speaker turn per paragraph, blank turns included.

    Deterministic by construction — no wall-clock text and no reliance on dict order,
    so a re-render is byte-identical (`snapshot <run-id>` is part of the evidence,
    not a timestamp of the rendering).
    """
    lines = [f"# {issuer} — {quarter} earnings call transcript", "",
             f"Alpha Vantage EARNINGS_CALL_TRANSCRIPT · snapshot {snapshot_id} · "
             f"generated — do not edit", ""]
    for segment in segments:
        lines += [f"**{segment['speaker']}** — *{segment['title']}*", ""]
        if segment["content"]:
            lines += [segment["content"], ""]
    return "\n".join(lines).rstrip("\n") + "\n"


def export_transcripts(issuer: str) -> int:
    """Render one Markdown file per held call. Returns the number of files."""
    out_dir = store.DERIVED / issuer / "documents" / "transcript"
    quarters = _transcript_quarters(issuer)
    for quarter, (snapshot_id, segments) in quarters.items():
        text = transcript_markdown(issuer, quarter, snapshot_id, segments)
        _write_if_changed(out_dir / f"{SAFE_NAME.sub('-', quarter)}.md", text.encode())
    return len(quarters)


def _normalise(raw: str) -> str:
    """A cell's number as its digits with a sign, or "" when it is not a number.

    Real releases write `$ 2,810.6`, `(3.9)`, `26.1 %`, `+86`, `—`, `N/M` and
    `Diluted EPS(1)`. Currency, thousands separators, percent signs and footnote
    markers are noise around the number and are dropped; parentheses mean negative.
    Anything that still does not parse leaves `value` blank with `raw_text` intact, so
    a bad parse is visible rather than silent.
    """
    text = raw.replace("\u00a0", " ").strip()
    text = FOOTNOTE.sub("", text).strip()
    for dash in DASHES:
        text = text.replace(dash, "")
    text = text.replace("$", "").replace(",", "").replace("%", "").strip()
    if text.lower() in NULL_TEXT:
        return ""
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1].strip()
    text = text.lstrip("+")
    minus = text.startswith("-")
    if minus:
        text = text[1:].strip()
    if NUMBER.fullmatch(text) is None:
        return ""
    if float(text) == 0:
        return text  # `(0.0)` is zero, not a negative zero
    return f"-{text}" if negative != minus else text


def _rows_of(table) -> list:
    """The table's own rows, in document order — a nested table's rows are its own."""
    rows = []
    for child in table:
        if child.tag == "tr":
            rows.append(child)
        elif child.tag in ("thead", "tbody", "tfoot"):
            rows.extend(child.findall("tr"))
    return rows


def _span(cell, attribute: str) -> int:
    """A colspan/rowspan as a bounded positive integer; junk reads as 1."""
    try:
        return max(1, int(cell.get(attribute) or 1))
    except (TypeError, ValueError):
        return 1


def _in_thead(cell) -> bool:
    return any(ancestor.tag == "thead" for ancestor in cell.iterancestors())


def _caption(table) -> str:
    """The text that names the table.

    Its own `<caption>`, else the nearest preceding block outside a table (the
    heading that introduces it), else its first row that carries any text. Truncated:
    a caption is a label, not a paragraph.
    """
    own = table.find("caption")
    if own is not None and _flat(own):
        return _flat(own)[:CAPTION_LIMIT]
    node = table
    while node is not None and node.tag not in ("body", "html"):
        for sibling in node.itersiblings(preceding=True):
            if sibling.tag == "table" or sibling.findall(".//table"):
                continue  # a previous table's text is not this table's label
            text = _flat(sibling)
            if text:
                return text[:CAPTION_LIMIT]
        node = node.getparent()
    for row in _rows_of(table):
        text = " ".join(_flat(cell) for cell in row if cell.tag in ("td", "th")).strip()
        if text:
            return text[:CAPTION_LIMIT]
    return ""


def _row_label(records: list[dict]) -> str:
    """The row's leading label cells, joined: what the row's values are of.

    A bare unit marker (`$`) is not a label, and the run ends at the first cell holding a
    value. A row with no value at all (`Net sales | $0.53 - $0.57` where the range does
    not parse, a footnote) ends at the first cell after its leading one that carries a
    digit or a percent sign, so the metric keeps its name and the unreadable value keeps
    its own cell; the leading cell is always kept, even when its label contains a figure.
    """
    parts: list[str] = []
    leading = True
    for record in records:
        if record["value"]:
            break
        text = record["raw_text"].strip()
        if not text or text.lower() in UNIT_MARKERS or not text.strip(DASHES):
            continue
        if not leading and (any(character.isdigit() for character in text) or "%" in text):
            break
        parts.append(text)
        leading = False
    return " ".join(parts)


def _column_label(grid: dict[int, dict[int, dict]], row_index: int, col: int,
                  colspan: int) -> str:
    """The header cells above the cell's columns, resolved through colspan/rowspan.

    Blank when no header covers the column — every row still carries `col_index`, and
    the header rows themselves are dumped, so an unresolvable label is a gap a reader
    can fill rather than a guess this module makes.
    """
    labels: list[str] = []
    for above in range(row_index):
        for column in range(col, col + colspan):
            slot = grid.get(above, {}).get(column)
            if slot and slot["kind"] == "header" and slot["text"] \
                    and slot["text"] not in labels:
                labels.append(slot["text"])
    return " ".join(labels)


def _units(records: list[dict]) -> list[str]:
    """The unit each cell's value is stated in, blank when it is not stated.

    A cell states its own unit (`$`, `26.1 %`), its column header can state it, and a
    neighboring bare marker cell states it too — some releases write `$ | 29,591`
    where others write `$29,591`. Nearest marker wins, the earlier one on a tie.
    """
    markers = {index: UNIT_MARKERS[record["raw_text"].strip().lower()]
               for index, record in enumerate(records)
               if record["raw_text"].strip().lower() in UNIT_MARKERS}
    units = []
    for index, record in enumerate(records):
        raw, column = record["raw_text"], record["column_label"]
        if "%" in raw:
            units.append("%")
        elif "$" in raw:
            units.append("$")
        elif "%" in column:
            units.append("%")
        elif "$" in column:
            units.append("$")
        elif record["value"] and markers:
            nearest = min(markers, key=lambda other: (abs(other - index), other > index))
            units.append(markers[nearest])
        elif re.search(r"\bshares\b", f"{raw} {record['row_label']}", re.IGNORECASE):
            units.append("shares")
        else:
            units.append("")
    return units


def _first_value_row(table) -> int | None:
    """The first row of the table that carries a parsable value, or None when it has none.

    Header rows are the rows above it: a release that writes its column labels as
    ordinary `<td>`s (the common case — real exhibits carry no `<th>` at all) still
    gets them marked, and a table with no parsable value at all is left alone rather
    than guessed at.
    """
    for row_index, row in enumerate(_rows_of(table)):
        if any(_normalise(_flat(cell)) for cell in row if cell.tag in ("td", "th")):
            return row_index
    return None


def _table_rows(table, table_index: int, caption: str, accession: str, filing_date: str,
                exhibit_sha256: str) -> list[dict]:
    """One record per `<td>/<th>` of one table, in document order.

    A grid is laid out first so colspan/rowspan can be resolved: a cell's `col_index`
    is the first column it occupies and the header cells that span it are found there.
    """
    grid: dict[int, dict[int, dict]] = {}
    records: list[dict] = []
    pending: dict[int, int] = {}  # column -> rows a span above still occupies
    first_value_row = _first_value_row(table)
    for row_index, row in enumerate(_rows_of(table)):
        cells = [cell for cell in row if cell.tag in ("td", "th")]
        col = 0
        row_records: list[dict] = []
        colspans: list[int] = []
        started: dict[int, int] = {}  # column -> rowspan of a span starting in this row
        for cell in cells:
            while pending.get(col, 0) > 0:
                col += 1
            colspan, rowspan = _span(cell, "colspan"), _span(cell, "rowspan")
            header_row = first_value_row is not None and row_index < first_value_row
            slot = {"kind": "header" if cell.tag == "th" or _in_thead(cell) or header_row
                    else "data", "text": _flat(cell)}
            for spanned in range(col, col + colspan):
                grid.setdefault(row_index, {})[spanned] = slot
                if rowspan > 1:
                    for below in range(row_index + 1, row_index + rowspan):
                        grid.setdefault(below, {})[spanned] = slot
                    started[spanned] = rowspan
            row_records.append({
                "accession": accession, "filing_date": filing_date,
                "exhibit_sha256": exhibit_sha256, "table_index": table_index,
                "caption": caption, "row_kind": slot["kind"], "row_index": row_index,
                "col_index": col, "raw_text": slot["text"],
                "value": _normalise(slot["text"]),
            })
            col += colspan
            colspans.append(colspan)
        label = _row_label(row_records)
        for record, colspan in zip(row_records, colspans):
            record["row_label"] = label
            record["column_label"] = _column_label(grid, row_index, record["col_index"],
                                                   colspan)
        for unit, record in zip(_units(row_records), row_records):
            record["unit"] = unit
        records.extend(row_records)
        # the spans from above are consumed by this row; the ones starting here begin
        # covering the rows below — decrementing before adding them is what keeps a
        # rowspan cell's own row from skipping its own columns
        for spanned in list(pending):
            pending[spanned] -= 1
            if pending[spanned] <= 0:
                del pending[spanned]
        for spanned, rowspan in started.items():
            pending[spanned] = rowspan - 1
    return records


# A document that declares its own encoding is parsed as bytes, so libxml2 honours the
# declaration; one that declares nothing is decoded as UTF-8 first, because libxml2's
# fallback is Latin-1, which turns an em dash into `â€”`.
DECLARED_ENCODING = re.compile(
    rb"<meta[^>]+charset\s*=\s*[\"']?\s*[\w-]+"
    rb"|<\?xml[^>]+encoding\s*=\s*[\"'][\w-]+"
    rb"|charset\s*=\s*[\"']?[\w-]+", re.IGNORECASE)


def _document(payload: bytes):
    """Parse one preserved exhibit into an HTML tree, honouring its declared encoding."""
    if DECLARED_ENCODING.search(payload[:4096]):
        return lxml_html.fromstring(payload)
    return lxml_html.fromstring(payload.decode("utf-8", errors="replace"))


def _exhibit_rows(payload: bytes, exhibit: dict, digest: str) -> list[dict]:
    """Every cell of every table in one preserved exhibit, in document order."""
    document = _document(payload)
    tables = [document] if document.tag == "table" \
        else document.xpath(".//table[not(ancestor::table)]")
    rows = []
    for table_index, table in enumerate(tables):
        rows.extend(_table_rows(table, table_index, _caption(table), exhibit["accession"],
                                exhibit["filing_date"], digest))
    return rows


def export_8k_cells(issuer: str, verified: list[tuple[dict, bytes, str]] | None = None) -> int:
    """Every cell of every held release table as one CSV. Returns the row count."""
    verified = _verified_exhibits(issuer) if verified is None else verified
    rows: list[dict] = []
    for exhibit, payload, digest in verified:
        if not any(marker in payload[:4096].lower() for marker in HTML_MARKERS):
            # named rather than parsed: a PDF exhibit has no cells, and dumping it as
            # HTML would be indistinguishable from a release with no numbers
            print(f"warning: {exhibit['name']} is not an HTML release, so it contributes "
                  f"no cells — read its copy under documents/8-k/ instead", file=sys.stderr)
            continue
        rows.extend(_exhibit_rows(payload, exhibit, digest))
    if not rows:
        return 0  # nothing dumpable: no artifact, and every skip was named above
    # newest filing first, then document order: a byte-stable file
    rows.sort(key=lambda r: (r["table_index"], r["row_index"], r["col_index"]))
    rows.sort(key=lambda r: (r["filing_date"], r["accession"]), reverse=True)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CELL_COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    _write_if_changed(store.DERIVED / issuer / "8k_cells.csv", buffer.getvalue().encode())
    return len(rows)


def export_views(issuer: str) -> dict[str, int]:
    """Write every derived view for one issuer. Makes no network call.

    Returns one count per group: files for the document views, rows for the cell dump.
    """
    store.safe_component(issuer, "issuer")
    verified = _verified_exhibits(issuer)
    return {
        "documents/8-k": export_documents(issuer, verified),
        "documents/transcript": export_transcripts(issuer),
        "8k_cells.csv": export_8k_cells(issuer, verified),
    }


_HISTORY_PERIOD = re.compile(r"^(\d{4}-\d{2}-\d{2}) \((Q[1-4]|FY|YTD)\)$")
_HISTORY_POINT = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_HISTORY_RESERVED = ("concept", "label", "dimension")


def build_history(frames: dict[str, pd.DataFrame], family: str) -> pd.DataFrame:
    """Build one quarter-wide history from statement filing frames.

    The newest frame that states a row/quarter wins. Columns mix filed labels and
    derived labels according to how each value was obtained: standalone (Qn) values
    keep their filed labels, while Q4 and cash-flow differences are marked derived.
    This is a computed view, not a published store table.
    """
    if family not in {"income", "balance", "cashflow"}:
        raise ValueError(f"unknown statement family {family!r}")
    reserved = tuple(column for column in _HISTORY_RESERVED
                     if any(column in frame.columns for frame in frames.values()))
    if not reserved:
        return pd.DataFrame(columns=[])

    # Keep table index order: _0 is the newest filing, which governs restatements.
    ordered = sorted(frames.items(), key=lambda pair: (
        0 if "_quarterly_" in pair[0] else 1,
        int(pair[0].rsplit("_", 1)[-1]) if pair[0].rsplit("_", 1)[-1].isdigit() else 0,
        pair[0]))
    rows: dict[tuple, dict] = {}
    direct: dict[tuple[str, str], dict[tuple, object]] = {}
    ytd: dict[tuple[int, int], dict[tuple, object]] = {}
    owner_ytd: dict[tuple[int, int], dict[tuple, object]] = {}
    owner_annual: dict[int, dict[tuple, object]] = {}
    candidates: dict[str, tuple[str, int, int]] = {}

    def row_key(record):
        return tuple(record.get(column) for column in reserved)

    for name, frame in ordered:
        is_balance = family == "balance"
        own_end = next((match.group(1) for column in frame.columns
                        if (match := _HISTORY_PERIOD.fullmatch(str(column)))), None)
        for _, record in frame.iterrows():
            key = row_key(record)
            rows.setdefault(key, {column: record.get(column) for column in reserved})
        for column in frame.columns:
            col = str(column)
            if col in reserved:
                continue
            match = _HISTORY_POINT.fullmatch(col) if is_balance else _HISTORY_PERIOD.fullmatch(col)
            if not match:
                continue
            end = match.group(1) if not is_balance else col
            if is_balance:
                year, month = int(end[:4]), int(end[5:7])
                quarter = (month - 1) // 3 + 1
                candidates.setdefault(end, (end, year, quarter))
                dest = direct.setdefault((end, "point"), {})
            else:
                kind = match.group(2)
                year, month = int(end[:4]), int(end[5:7])
                quarter = (month - 1) // 3 + 1
                candidates.setdefault(end, (end, year, quarter))
                if kind.startswith("Q"):
                    # A standalone quarterly value is the best evidence for either flow.
                    dest = direct.setdefault((end, kind), {})
                    for _, record in frame.iterrows():
                        dest.setdefault(row_key(record), record.get(col))
                    continue
                if kind == "YTD":
                    dest = ytd.setdefault((year, quarter), {})
                    for _, record in frame.iterrows():
                        dest.setdefault(row_key(record), record.get(col))
                    if end == own_end:
                        own_dest = owner_ytd.setdefault((year, quarter), {})
                        for _, record in frame.iterrows():
                            own_dest.setdefault(row_key(record), record.get(col))
                elif kind == "FY":
                    if end == own_end:
                        own_dest = owner_annual.setdefault(year, {})
                        for _, record in frame.iterrows():
                            own_dest.setdefault(row_key(record), record.get(col))

    # The output labels preserve the period column as filed; only derived values gain
    # a marker. Choose candidate periods from values actually available for the family.
    if family == "balance":
        available = {key[0] for key in direct}
    elif family == "income":
        available = {end for end, kind in direct if kind.startswith("Q")}
    else:
        available = {end for end, kind in direct if kind.startswith("Q")}
        available.update(end for (year, q) in ytd
                         for end, (_, ey, eq) in candidates.items()
                         if (ey, eq) == (year, q))
    for year in owner_annual:
        # Q4 is derivable only when both the year's FY and Q3 YTD are held.
        q4_ends = [end for end, (_, ey, eq) in candidates.items() if ey == year and eq == 4]
        q3_ends = [end for end, (_, ey, eq) in candidates.items() if ey == year and eq == 3]
        if q4_ends and q3_ends and ((year, 3) in owner_ytd or (year, 3) in ytd):
            available.add(sorted(q4_ends)[-1])
    selected = sorted(available, reverse=True)[:12]
    selected.reverse()

    # For flow values, a Q4 is FY less Q3 YTD. Cash-flow Q1..Q3 are changes in
    # successive as-filed YTD totals, computed separately for each row.
    values_by_end: dict[str, dict[tuple, object]] = {}
    labels: dict[str, str] = {}
    if family == "balance":
        for end in selected:
            values_by_end[end] = direct.get((end, "point"), {})
            labels[end] = end
    elif family == "income":
        for end in selected:
            _, year, quarter = candidates[end]
            if quarter == 4 and year in owner_annual and ((year, 3) in owner_ytd or (year, 3) in ytd):
                fy = owner_annual[year]
                nine = owner_ytd.get((year, 3), ytd[(year, 3)])
                marker = "Q4 derived" if (year, 3) in owner_ytd else "Q4 derived from comparative"
                values_by_end[end] = {key: (fy[key] - nine[key])
                                      for key in fy.keys() & nine.keys()
                                      if pd.notna(fy[key]) and pd.notna(nine[key])}
                labels[end] = f"{end} ({marker})"
            else:
                kind = f"Q{quarter}"
                values_by_end[end] = direct.get((end, kind), {})
                labels[end] = f"{end} ({kind})"
    else:
        for end in selected:
            _, year, quarter = candidates[end]
            if quarter == 4 and year in owner_annual and ((year, 3) in owner_ytd or (year, 3) in ytd):
                fy = owner_annual[year]
                nine = owner_ytd.get((year, 3), ytd[(year, 3)])
                marker = "Q4 derived" if (year, 3) in owner_ytd else "Q4 derived from comparative"
                values_by_end[end] = {key: fy[key] - nine[key]
                                      for key in fy.keys() & nine.keys()
                                      if pd.notna(fy[key]) and pd.notna(nine[key])}
                labels[end] = f"{end} ({marker})"
            elif direct.get((end, f"Q{quarter}")):
                values_by_end[end] = direct[(end, f"Q{quarter}")]
                labels[end] = f"{end} (Q{quarter})"
            elif (year, quarter) in ytd:
                current = ytd[(year, quarter)]
                prior = ytd.get((year, quarter - 1), {}) if quarter > 1 else {}
                values_by_end[end] = ({key: current[key] for key in current}
                                      if quarter == 1 else
                                      {key: current[key] - prior[key]
                                       for key in current.keys() & prior.keys()
                                       if pd.notna(current[key]) and pd.notna(prior[key])})
                labels[end] = f"{end} (Q{quarter} derived)"

    result = []
    for key, identity in rows.items():
        row = dict(identity)
        for end in selected:
            row[labels.get(end, end)] = values_by_end.get(end, {}).get(key, float("nan"))
        result.append(row)
    columns = [*reserved, *(labels.get(end, end) for end in selected)]
    return pd.DataFrame(result, columns=columns)
