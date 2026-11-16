"""Turn classified posts into leads and write them to CSV and Excel."""

from __future__ import annotations

import csv
import re
from collections import Counter
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .classifier import CONFIDENCE_LEVELS, author_confidence, meets, named_confidence
from .models import Classification, Post

POST_TEXT_MAX_CHARS = 1000
MAX_OTHER_LINKS = 3

AUTHOR, NAMED = "Wrote the post", "Named in a post"
NAMED_SIGNAL = {"invested": "made an investment", "launched_fund": "launched a fund", "seeking_deals": "seeking deals"}

# (header, column width in Excel). One sheet holds both kinds of lead.
LEAD_COLUMNS: list[tuple[str, int]] = [
    ("Confidence", 11),
    ("Lead type", 16),
    ("Name", 26),
    ("Firm", 24),
    ("Role", 20),
    ("Investor type", 14),
    ("Signal", 22),
    ("Intent score", 12),
    ("Industry match", 14),
    ("Stage match", 12),
    ("Why", 60),
    ("Platform", 10),
    ("Post date", 12),
    ("Post link", 44),
    ("Profile link", 36),
    ("Mentions", 10),
    ("Other posts", 44),
    ("Sectors", 26),
    ("Stages", 18),
    ("Posted by", 24),
    ("Post text", 80),
    ("Bio / headline", 50),
    ("Followers", 11),
    ("Likes", 8),
    ("Matched query", 36),
]
AUDIT_COLUMNS: list[tuple[str, int]] = [
    ("Author is a lead", 16),
    ("Named investors", 40),
    ("Author", 26),
    ("Is investor", 11),
    ("Firm", 24),
    ("Role", 20),
    ("Investor type", 14),
    ("Signal", 22),
    ("Intent score", 12),
    ("Industry match", 14),
    ("Stage match", 12),
    ("Why", 60),
    ("Platform", 10),
    ("Post date", 12),
    ("Post link", 44),
    ("Profile link", 36),
    ("Post text", 80),
    ("Bio / headline", 50),
    ("Matched query", 36),
]
LINK_HEADERS = {"Post link", "Profile link"}
WRAP_HEADERS = {"Why", "Post text", "Bio / headline", "Other posts", "Named investors"}
CONFIDENCE_FILL = {"high": "C6EFCE", "medium": "FFEB9C", "low": "F2F2F2"}

# Excel cannot store these control characters.
_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


@dataclass
class Lead:
    """One row on the Leads sheet: an investor, with the post that best shows why."""

    confidence: str
    lead_type: str  # AUTHOR or NAMED
    name: str
    post: Post
    firm: str = ""
    role: str = ""
    investor_type: str = ""
    signal: str = ""
    intent_score: int | None = None
    industry_match: str = "unknown"
    stage_match: str = "unknown"
    why: str = ""
    sectors: list[str] = field(default_factory=list)
    stages: list[str] = field(default_factory=list)
    profile_url: str = ""
    posts: list[str] = field(default_factory=list)  # every post URL supporting this lead
    person: str = ""  # set when a named person has been folded into their firm's row

    def strength(self) -> tuple[int, int, int]:
        """Sort key: stronger leads sort first."""
        return (
            CONFIDENCE_LEVELS.index(self.confidence),
            -(self.intent_score if self.intent_score is not None else -1),
            -(self.post.engagement or 0),
        )


def _strongest_first(leads: list[Lead]) -> list[Lead]:
    """Order by confidence, then intent score, then likes; newest post breaks ties."""
    newest_first = sorted(leads, key=lambda lead: lead.post.posted_at, reverse=True)
    return sorted(newest_first, key=Lead.strength)


# Words that often trail an investor's name without changing who it is.
_NAME_SUFFIXES = {"llc", "inc", "ltd", "lp", "llp", "gmbh", "vc", "ventures", "capital", "partners", "fund",
                  "i", "ii", "iii", "iv"}


_GLUED_SUFFIXES = ("ventures", "capital", "partners")


def entity_key(name: str) -> str:
    """Key under which spellings of one investor's name are grouped.

    "Backed VC", "BACKED VC" and "Backed" share a key, as do "Index" and "Index Ventures".
    Trailing words are only dropped while at least four characters remain, so short names
    such as "BD Partners" keep theirs.
    """
    tokens = re.findall(r"[a-z0-9]+", re.sub(r"\([^)]*\)", " ", name.lower()))
    while len(tokens) > 1 and tokens[-1] in _NAME_SUFFIXES and len("".join(tokens[:-1])) >= 4:
        tokens.pop()
    key = "".join(tokens)
    # Names written as one word, such as "MerakCapital".
    for suffix in _GLUED_SUFFIXES:
        if key.endswith(suffix) and len(key) - len(suffix) >= 4:
            return key[: -len(suffix)]
    return key


def _similar(a: str, b: str) -> bool:
    """True for keys that differ only by a small typo ("andreessenhorowitz", "andreesonhorowitz")."""
    if a == b:
        return True
    if min(len(a), len(b)) < 10 or abs(len(a) - len(b)) > 2 or a[0] != b[0]:
        return False
    return SequenceMatcher(None, a, b).ratio() >= 0.9


def _fold_people_into_firms(named: dict[str, list[Lead]]) -> dict[str, list[Lead]]:
    """Group people under their firm when the firm is itself a lead, or has two or more
    people named. One row for the firm is more useful than a row per partner."""
    people_by_firm: dict[str, list[str]] = {}  # lists, to keep the order people were first seen in
    for key, group in named.items():
        for lead in group:
            firm_key = entity_key(lead.firm)
            if firm_key and firm_key != key and key not in people_by_firm.setdefault(firm_key, []):
                people_by_firm[firm_key].append(key)

    folded = {key: list(group) for key, group in named.items()}
    for firm_key, person_keys in people_by_firm.items():
        if firm_key not in named and len(person_keys) < 2:
            continue
        for person_key in person_keys:
            staying = []
            for lead in folded.pop(person_key, []):
                if entity_key(lead.firm) == firm_key:
                    lead.person, lead.name, lead.firm = lead.name, lead.firm, ""
                    folded.setdefault(firm_key, []).append(lead)
                else:
                    staying.append(lead)
            if staying:
                folded[person_key] = staying
    return folded


def _merge_similar(groups: dict[str, list[Lead]]) -> dict[str, list[Lead]]:
    """Merge groups whose keys are near-identical. The most-mentioned spelling keeps the group."""
    merged: dict[str, list[Lead]] = {}
    for key in sorted(groups, key=lambda k: -len(groups[k])):
        match = next((kept for kept in merged if _similar(key, kept)), None)
        merged.setdefault(match or key, []).extend(groups[key])
    return merged


def _one_row(group: list[Lead]) -> Lead:
    """Pick the strongest piece of evidence in a group and fold the rest into it."""
    ordered = sorted(_strongest_first(group), key=lambda lead: (CONFIDENCE_LEVELS.index(lead.confidence), bool(lead.person)))
    best = ordered[0]
    if best.lead_type == NAMED:
        # Use the spelling most posts use.
        best.name = Counter(lead.name for lead in group if lead.lead_type == NAMED).most_common(1)[0][0]
    best.posts = list(dict.fromkeys(lead.post.url for lead in ordered))
    for attribute in ("firm", "role", "profile_url"):
        if not getattr(best, attribute):
            setattr(best, attribute, next((getattr(lead, attribute) for lead in ordered if getattr(lead, attribute)), ""))
    people = list(dict.fromkeys(lead.person for lead in ordered if lead.person))
    if people:
        best.why = f"{best.why.rstrip()} People named: {', '.join(people)}."
    return best


def build_leads(classified: list[tuple[Post, Classification]], min_confidence: str) -> list[Lead]:
    """Collect author leads and named-investor leads, one row per investor, strongest first.

    The same investor often turns up in several posts (six outlets reporting one funding
    round, say), under slightly different spellings, or as a firm in one post and its
    partners in another. Those are merged into one row that keeps the strongest evidence
    and counts the mentions.
    """
    authors: dict[str, list[Lead]] = {}
    named: dict[str, list[Lead]] = {}

    for post, c in classified:
        confidence = author_confidence(c)
        if meets(confidence, min_confidence):
            author_key = f"{post.platform}:{entity_key(post.author_handle or post.author_name) or post.url}"
            authors.setdefault(author_key, []).append(
                Lead(
                    confidence=confidence,
                    lead_type=AUTHOR,
                    name=post.author_name or post.author_handle,
                    post=post,
                    firm=c.firm,
                    role=c.role,
                    investor_type=c.author_type,
                    signal=c.intent.replace("_", " "),
                    intent_score=c.intent_score,
                    industry_match=c.industry_match,
                    stage_match=c.stage_match,
                    why=c.reason,
                    sectors=c.sectors,
                    stages=c.stages,
                    profile_url=post.author_url,
                )
            )

        author_names = {entity_key(post.author_name), entity_key(post.author_handle)}
        for investor in c.named_investors:
            confidence = named_confidence(investor)
            key = entity_key(investor.name)
            if not key or key in author_names or not meets(confidence, min_confidence):
                continue
            named.setdefault(key, []).append(
                Lead(
                    confidence=confidence,
                    lead_type=NAMED,
                    name=investor.name,
                    post=post,
                    firm=investor.firm,
                    investor_type=investor.kind,
                    signal=NAMED_SIGNAL.get(investor.action, "named in post"),
                    industry_match=investor.industry_match,
                    stage_match=investor.stage_match,
                    why=investor.evidence,
                )
            )

    named = _merge_similar(_fold_people_into_firms(named))

    # An investor who posted themselves and is also named elsewhere becomes one row.
    groups: list[list[Lead]] = []
    for group in authors.values():
        match = next((key for key in named if _similar(entity_key(group[0].name), key)), None)
        groups.append(group + named.pop(match) if match else group)
    groups.extend(named.values())

    return _strongest_first([_one_row(group) for group in groups])


def _lead_row(lead: Lead) -> list[Any]:
    post = lead.post
    others = [url for url in lead.posts if url != post.url][:MAX_OTHER_LINKS]
    return [
        lead.confidence,
        lead.lead_type,
        lead.name,
        lead.firm,
        lead.role,
        lead.investor_type.replace("_", " "),
        lead.signal,
        lead.intent_score,
        lead.industry_match,
        lead.stage_match,
        lead.why,
        "X" if post.platform == "x" else "LinkedIn",
        post.posted_at[:10],
        post.url,
        lead.profile_url,
        len(lead.posts),
        "\n".join(others),
        ", ".join(lead.sectors),
        ", ".join(lead.stages),
        post.author_name or post.author_handle,
        post.text[:POST_TEXT_MAX_CHARS],
        post.author_headline,
        post.author_followers,
        post.engagement,
        post.matched_query,
    ]


def _listed(confidence: str | None, min_confidence: str) -> str:
    """How the audit sheet describes a result: its confidence if exported, and why not otherwise."""
    if confidence is None:
        return "not a lead"
    return confidence if meets(confidence, min_confidence) else f"{confidence}, not listed"


def _audit_row(post: Post, c: Classification, min_confidence: str) -> list[Any]:
    named = "; ".join(f"{n.name} ({_listed(named_confidence(n), min_confidence)})" for n in c.named_investors)
    return [
        _listed(author_confidence(c), min_confidence),
        named,
        post.author_name or post.author_handle,
        "yes" if c.is_investor else "no",
        c.firm,
        c.role,
        c.author_type.replace("_", " "),
        c.intent.replace("_", " "),
        c.intent_score,
        c.industry_match,
        c.stage_match,
        c.reason,
        "X" if post.platform == "x" else "LinkedIn",
        post.posted_at[:10],
        post.url,
        post.author_url,
        post.text[:POST_TEXT_MAX_CHARS],
        post.author_headline,
        post.matched_query,
    ]


def _csv_safe(value: Any) -> Any:
    """Stop spreadsheet apps from running post text as a formula when the CSV is opened."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def _xlsx_safe(value: Any) -> Any:
    return _ILLEGAL.sub("", value) if isinstance(value, str) else value


def write_csv(path: Path, rows: list[list[Any]]) -> None:
    # utf-8-sig so Excel detects the encoding and shows names and emoji correctly.
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow([header for header, _ in LEAD_COLUMNS])
        writer.writerows([[_csv_safe(v) for v in row] for row in rows])


def _sheet(workbook: Workbook, title: str, columns: list[tuple[str, int]], rows: list[list[Any]], freeze: str) -> None:
    sheet = workbook.create_sheet(title)
    headers = [header for header, _ in columns]
    sheet.append(headers)
    for cell in sheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", start_color="1F2937")
        cell.alignment = Alignment(vertical="center")

    for row in rows:
        sheet.append([_xlsx_safe(v) for v in row])
        for index, header in enumerate(headers, start=1):
            cell = sheet.cell(row=sheet.max_row, column=index)
            if isinstance(cell.value, str):
                # Store text as text, so a post that starts with "=" is never run as a formula.
                cell.data_type = "s"
                if header in LINK_HEADERS and cell.value.startswith("https://"):
                    cell.hyperlink = cell.value
                    cell.style = "Hyperlink"
            cell.alignment = Alignment(wrap_text=header in WRAP_HEADERS, vertical="top")
            if header == "Confidence" and cell.value in CONFIDENCE_FILL:
                cell.fill = PatternFill("solid", start_color=CONFIDENCE_FILL[cell.value])

    for index, (_, width) in enumerate(columns, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = width
    sheet.freeze_panes = freeze
    sheet.auto_filter.ref = sheet.dimensions


def write_xlsx(path: Path, lead_rows: list[list[Any]], audit_rows: list[list[Any]]) -> None:
    workbook = Workbook()
    workbook.remove(workbook.active)
    _sheet(workbook, "Leads", LEAD_COLUMNS, lead_rows, freeze="D2")
    _sheet(workbook, "All classified", AUDIT_COLUMNS, audit_rows, freeze="D2")
    workbook.save(path)


def export(
    classified: list[tuple[Post, Classification]],
    output_dir: Path,
    name: str,
    min_confidence: str,
) -> tuple[Path, Path, list[Lead]]:
    """Write `<name>.csv` (leads) and `<name>.xlsx` (leads, plus every classified post).

    Returns the two paths and the leads.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    leads = build_leads(classified, min_confidence)
    lead_rows = [_lead_row(lead) for lead in leads]
    audit_rows = [_audit_row(post, c, min_confidence) for post, c in classified]

    csv_path, xlsx_path = output_dir / f"{name}.csv", output_dir / f"{name}.xlsx"
    write_csv(csv_path, lead_rows)
    write_xlsx(xlsx_path, lead_rows, audit_rows)
    return csv_path, xlsx_path, leads
