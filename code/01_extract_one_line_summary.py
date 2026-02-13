import argparse
import csv
import json
import re
import shutil
import shlex
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import yaml  # type: ignore
except Exception as e:  # pragma: no cover
    raise RuntimeError("PyYAML is required. Install with: pip install pyyaml") from e

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = PROJECT_ROOT / "config"
RULES_CONFIG_PATH = CONFIG_DIR / "rules_config.yaml"
RUNTIME_ACTIVE_PATH = CONFIG_DIR / "runtime_config_active.yaml"


def load_yaml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    raw = path.read_text(encoding="utf-8", errors="ignore")
    obj = yaml.safe_load(raw)
    if not isinstance(obj, dict):
        raise ValueError(f"Config root must be a mapping: {path}")
    return obj


def _runtime_vnum(runtime_version: str) -> str:
    s = (runtime_version or "").strip().lower()
    m = re.search(r"v(\d+)", s)
    if m:
        return f"v{int(m.group(1))}"
    m = re.search(r"(\d+)", s)
    if m:
        return f"v{int(m.group(1))}"
    return "v1"


def _resolve_path(p: str) -> Path:
    pp = Path(str(p or "").strip())
    if not pp:
        return Path("")
    return pp if pp.is_absolute() else (PROJECT_ROOT / pp).resolve()


def norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("\u00a0", " ")).strip()


def strip_pdf_noise(text: str) -> str:
    """Remove common page headers/footers extracted from PDFs."""
    t = text.replace("\r\n", "\n").replace("\r", "\n")
    cleaned_lines: List[str] = []
    for line in t.split("\n"):
        ln = norm_ws(line)
        if not ln:
            continue
        if re.fullmatch(r"(?i)page\s+\d+\s+of\s+\d+", ln):
            continue
        if re.fullmatch(r"(?i)complaint\s+\d+(?:-\d+){1,4}(?:\s+\d{4})?", ln):
            continue
        if re.search(r"(?i)page\s+\d+\s+of\s+\d+", ln) and re.search(r"(?i)complaint\s+\d", ln):
            continue
        cleaned_lines.append(ln)
    return "\n".join(cleaned_lines)

def read_pdf_text(pdf_path: Path) -> str:
    import pdfplumber

    parts: List[str] = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for page in pdf.pages:
            txt = page.extract_text() or ""
            if txt.strip():
                parts.append(txt)
    return strip_pdf_noise("\n".join(parts))


def extract_between_headings(text: str, start: str, end: str) -> str:
    m = re.search(rf"(?is){re.escape(start)}\s*(.*?)\s*{re.escape(end)}", text)
    if not m:
        return ""
    return norm_ws(m.group(1))


def _clean_summary_source(text: str) -> str:
    t = norm_ws(text)
    if not t:
        return t

    # Remove common document scaffolding / labels that pollute summarization.
    t = re.sub(
        r"(?i)\b(summary of dispute|key facts|requested action|my concerns|supporting documentation|attached|attachments?)\b",
        " ",
        t,
    )
    t = re.sub(r"(?i)\b(policy\s*(number|no\.?|id)|complaint\s*(id|ref(?:erence)?))\s*[:#-]?\s*[a-z0-9-]{4,}\b", " ", t)
    t = re.sub(r"(?i)\bquote\s*(number|no\.?)\s*[:#-]?\s*[a-z0-9-]{4,}\b", " ", t)
    t = re.sub(r"(?i)\b(regards|kind regards|yours sincerely)\b[^.]{0,120}", " ", t)
    t = re.sub(r"(?i)\b(report\s*ref)\s*[:#-]?\s*\d{6,}\b", " ", t)
    t = re.sub(r"(?i)\b(?:•|-)\s*", " ", t)
    return norm_ws(t)


def _extract_amount_timeline(text: str) -> List[Tuple[str, str]]:
    """Extract year/period -> amount pairs for multi-year renewal complaints."""
    t = text
    out: List[Tuple[str, str]] = []
    seen = set()

    # 1) Dense series such as: 2018- $94.06 2019- $113.00 ... 2025-$964.79
    series_pat = re.compile(r"(?is)\b(?P<period>(?:20\d{2}|\d{2}[/\-]\d{2}))\s*[-:]\s*\$\s*(?P<amt>[\d,]+(?:\.\d{1,2})?)")
    series_hits = list(series_pat.finditer(t))
    if len(series_hits) >= 3:
        for m in series_hits:
            period = norm_ws(m.group('period'))
            amt = norm_ws(m.group('amt'))
            key = (period.lower(), amt)
            if key in seen:
                continue
            seen.add(key)
            out.append((period, amt))
            if len(out) >= 10:
                break
        return out

    # 2) Period with nearby amount in premium context
    pat = re.compile(
        r"(?is)\b(?P<period>(?:20\d{2}(?:[/-]20?\d{2})?|\d{2}[/\-]\d{2}))\b[^$\n]{0,70}?\$\s*(?P<amt>[\d,]+(?:\.\d{1,2})?)"
    )
    for m in pat.finditer(t):
        period = norm_ws(m.group('period'))
        amt = norm_ws(m.group('amt'))
        ctx = t[max(0, m.start()-80):min(len(t), m.end()+80)].lower()
        if not any(k in ctx for k in ["premium", "renewal", "annual", "monthly", "policy", "payments"]):
            continue
        if any(k in ctx for k in ["reduction", "discount", "offered", "refund only", "compensation", "best part of", "give or take"]):
            continue
        key = (period.lower(), amt)
        if key in seen:
            continue
        seen.add(key)
        out.append((period, amt))
        if len(out) >= 10:
            break

    # 3) Fallback: plain year followed by amount in nearby context
    if len(out) < 2:
        pat2 = re.compile(r"(?is)\b(?P<period>20\d{2})\b[^$\n]{0,40}?\$\s*(?P<amt>[\d,]+(?:\.\d{1,2})?)")
        for m in pat2.finditer(t):
            period = m.group('period')
            amt = norm_ws(m.group('amt'))
            ctx = t[max(0, m.start()-80):min(len(t), m.end()+80)].lower()
            if any(k in ctx for k in ["best part of", "give or take", "reduction", "discount", "offered"]):
                continue
            key = (period.lower(), amt)
            if key in seen:
                continue
            seen.add(key)
            out.append((period, amt))
            if len(out) >= 10:
                break

    return out


def _money_to_float(x: str) -> Optional[float]:
    try:
        return float(str(x).replace(",", ""))
    except Exception:
        return None


def _extract_percent_series(text: str) -> List[str]:
    vals = []
    for m in re.finditer(r"\b(\d{1,3}(?:\.\d+)?)\s*%", text):
        v = m.group(1)
        if v not in vals:
            vals.append(v)
        if len(vals) >= 6:
            break
    return vals


def _extract_trend_clause(text: str) -> Optional[str]:
    timeline = _extract_amount_timeline(text)
    if len(timeline) < 2:
        return None

    # Prefer recency for long timelines so current disputed premium is retained.
    selected = timeline[:]
    if len(timeline) > 4:
        selected = [timeline[0]] + timeline[-3:]

        # If there is a clear acceleration point, pivot around it.
        vals = [_money_to_float(a) for _, a in timeline]
        diffs: List[Tuple[float, int]] = []
        for i in range(1, len(vals)):
            if vals[i] is None or vals[i - 1] is None:
                continue
            diffs.append((vals[i] - vals[i - 1], i))
        if diffs:
            diffs.sort(reverse=True)
            jump_i = diffs[0][1]
            if jump_i >= 2:
                # keep one pre-jump anchor + all post-jump points
                selected = [timeline[jump_i - 1]] + timeline[jump_i:]
                if len(selected) > 4:
                    selected = [selected[0]] + selected[-3:]

    parts = [f"{p} ${a}" for p, a in selected[:4]]
    pct = _extract_percent_series(text)
    pct_txt = ""
    if len(pct) >= 2:
        pct_txt = f"; reported increases {', '.join(x + '%' for x in pct[:4])}"
    elif len(pct) == 1:
        pct_txt = f"; reported increase {pct[0]}%"

    return f"multi-period premium trend: {' -> '.join(parts)}{pct_txt}"


def _extract_primary_change_clause(text: str) -> Optional[str]:
    t = text

    # Prefer explicit from->to money movement.
    m = re.search(r"(?is)\$\s*([\d,]+(?:\.\d{1,2})?)\s*(?:to|->|\u2192|vs)\s*\$\s*([\d,]+(?:\.\d{1,2})?)", t)
    if m:
        a, b = m.group(1), m.group(2)
        um = re.search(r"(?i)(per\s*month|monthly|per\s*year|annual|annually)", t[max(0, m.start()-60):m.end()+60])
        unit = f" {um.group(1).lower()}" if um else ""
        return f"premium changed from ${a} to ${b}{unit}".replace("  ", " ")

    # If only one amount + % increase is present, use that.
    p = re.search(r"(?i)(\d{1,3}(?:\.\d+)?)\s*%", t)
    a = re.search(r"\$\s*([\d,]+(?:\.\d{1,2})?)", t)
    if p and a:
        return f"premium increase of about {p.group(1)}% (amount referenced around ${a.group(1)})"
    if p:
        return f"premium increase of about {p.group(1)}%"

    return None


def _sentences(text: str) -> List[str]:
    text = text.replace("...", ".")
    parts = [norm_ws(x) for x in re.split(r"(?<=[.!?])\s+", text) if norm_ws(x)]
    out: List[str] = []
    for s in parts:
        # Drop noisy list/meta fragments that are rarely good summaries.
        bad = ["report ref", "policy number", "quote number", "attached", "kind regards", "yours sincerely", "i am lodging a formal complaint", "dear afca"]
        if any(b in s.lower() for b in bad):
            continue
        if len(s) < 24:
            continue
        out.append(s)
    return out


def _pick_key_clauses(text: str, limit: int = 3) -> List[str]:
    sents = _sentences(text)
    if not sents:
        return []

    themes = {
        "cause": [
            "without consent", "without my knowledge", "did not receive", "no sms", "no email", "misled", "misleading",
            "privacy", "old address", "error", "wrong", "misclassification", "flood zone", "payment frequency", "pro-rata",
        ],
        "response": [
            "offered", "reduction", "discount", "no response", "no follow", "30 days", "not escalated",
            "commercial sensitivity", "unable to provide", "generic", "declined", "refused", "not helpful",
        ],
        "impact": [
            "hardship", "unaffordable", "cancer", "homeless", "single", "stress", "distress", "financial",
            "disability", "domestic violence", "medical", "loyal", "locked", "cannot leave",
        ],
        "request": [
            "request", "requested", "refund", "reassess", "review", "recalculation", "assistance", "afca",
            "backdate", "explanation", "breakdown", "compensation",
        ],
        "comparison": [
            "other insurer", "competitor", "comparison", "cheaper", "floodwise", "market value", "no claims",
            "no at-fault", "insured value", "lifetime", "new for old",
        ],
        "service": [
            "contact", "callback", "transferred", "supervisor", "delay", "complaint handling", "bot", "email",
        ],
    }

    def score(sent: str) -> Tuple[int, str]:
        l = sent.lower()
        sc = 0
        sc += 5 * len(re.findall(r"\$\s*[\d,]+(?:\.\d{1,2})?", sent))
        sc += 3 * len(re.findall(r"\b\d{1,3}(?:\.\d+)?%", sent))
        if re.search(r"(?i)\b(increase|increased|overcharged|renewal|premium)\b", l):
            sc += 4
        for kws in themes.values():
            if any(k in l for k in kws):
                sc += 3
        if 45 <= len(sent) <= 230:
            sc += 2
        return sc, sent

    ranked = sorted((score(x) for x in sents), key=lambda x: x[0], reverse=True)

    chosen: List[str] = []
    used_theme: set = set()
    for _, sent in ranked:
        l = sent.lower()
        hit = None
        for name, kws in themes.items():
            if any(k in l for k in kws):
                hit = name
                break
        if hit and hit in used_theme:
            continue
        used_theme.add(hit)
        chosen.append(sent)
        if len(chosen) >= limit:
            break

    # Second pass: fill remaining slots with next-best clauses even if theme repeats,
    # so dense complaints can carry more reasons.
    if len(chosen) < limit:
        for _, sent in ranked:
            if sent in chosen:
                continue
            chosen.append(sent)
            if len(chosen) >= limit:
                break

    if not chosen and ranked:
        chosen = [ranked[0][1]]
    return chosen


def _to_third_person(sentence: str) -> str:
    s = norm_ws(sentence)
    if not s:
        return s

    # Convert frequent first-person complaint framing to neutral third-person wording.
    replacements = [
        (r"(?i)^i am lodging a formal complaint", "The customer lodged a formal complaint"),
        (r"(?i)^i am writing to lodge", "The customer lodged"),
        (r"(?i)^i lodged a complaint", "The customer lodged a complaint"),
        (r"(?i)^i currently hold", "The customer holds"),
        (r"(?i)^i have", "The customer has"),
        (r"(?i)^i had", "The customer had"),
        (r"(?i)^i was", "The customer was"),
        (r"(?i)^i requested", "The customer requested"),
        (r"(?i)^i request", "The customer requests"),
        (r"(?i)^i contacted", "The customer contacted"),
        (r"(?i)^i called", "The customer called"),
        (r"(?i)^we have", "The customer states they have"),
        (r"(?i)^we were", "The customer states they were"),
        (r"(?i)\bmy premium\b", "the premium"),
        (r"(?i)\bmy policy\b", "the policy"),
        (r"(?i)\bmy renewal\b", "the renewal"),
        (r"(?i)\bmy complaint\b", "the complaint"),
    ]
    for pat, rep in replacements:
        s = re.sub(pat, rep, s)

    # Normalize first-person pronouns in remaining text.
    s = re.sub(r"(?i)\bI\b", "the customer", s)
    s = re.sub(r"(?i)\bme\b", "the customer", s)
    s = re.sub(r"(?i)\bmy\b", "the customer's", s)

    return norm_ws(s)


def _collect_numeric_tokens(text: str) -> List[str]:
    out: List[str] = []
    seen = set()

    # Money tokens with context filtering.
    for m in re.finditer(r"\$\s*[\d,]+(?:\s+\d{3})*(?:\.\d{1,2})?", text, flags=re.I):
        tok = norm_ws(m.group(0)).replace(" ", "")
        ctx = text[max(0, m.start() - 100): min(len(text), m.end() + 100)].lower()
        if any(k in ctx for k in ["net profit", "npat", "billion", "million", "profit increase"]):
            continue
        if any(k in ctx for k in ["other insurer", "competitor", "comparison", "quoted figures", "another insurer"]):
            continue
        key = tok.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(tok)

    patterns = [
        r"\b\d{1,3}(?:\.\d+)?%",  # percentages
        r"\b\d+(?:\.\d+)?\s*(?:months|days|years)\b",  # time units
        r"\b20\d{2}\b",  # years
    ]
    for pat in patterns:
        for m in re.finditer(pat, text, flags=re.I):
            tok = norm_ws(m.group(0))
            key = tok.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(tok)

    # keep reasonably broad but bounded to avoid noisy tails
    return out[:16]



def _is_noisy_summary_text(s: str) -> bool:
    t = (s or "").lower()
    return any(k in t for k in ["net profit", "npat", "billions and billions", "profit increase"])


def _run_local_ai_summary(text: str, runtime_cfg: Optional[Dict[str, Any]]) -> Optional[str]:
    """Run an optional offline local-AI command to generate summary text.

    Expected runtime config (optional):
      summarizer:
        mode: rule | local_ai | hybrid
        local_ai_command: "python local_model.py --summarize"
        local_ai_timeout_s: 20
    The command receives full text on stdin and should emit one-line summary on stdout.
    """
    cfg = (runtime_cfg or {}).get("summarizer") or {}
    cmd = str(cfg.get("local_ai_command") or "").strip()
    if not cmd:
        return None

    timeout_s = int(cfg.get("local_ai_timeout_s") or 20)
    try:
        proc = subprocess.run(
            shlex.split(cmd),
            input=(text or ""),
            text=True,
            capture_output=True,
            timeout=max(3, timeout_s),
            check=False,
        )
    except Exception:
        return None

    if proc.returncode != 0:
        return None

    out = norm_ws(proc.stdout or "")
    if not out:
        return None

    # Keep first non-empty line if model returns paragraphs
    line = next((norm_ws(x) for x in out.splitlines() if norm_ws(x)), out)
    if _is_noisy_summary_text(line):
        return None
    return line


def _audit_and_enrich_summary(candidate: str, source_text: str, target_len: int) -> str:
    """Guardrail pass: sanitize noise, enforce 3rd person, and backfill key figures/anchors."""
    out = _to_third_person(norm_ws(candidate or ""))
    if _is_noisy_summary_text(out):
        out = ""

    trend_clause = _extract_trend_clause(source_text)
    primary = _extract_primary_change_clause(source_text)

    if trend_clause and (not out or "multi-period premium trend" not in out.lower()):
        cand = norm_ws(f"{_to_third_person(trend_clause)}; {out}" if out else _to_third_person(trend_clause))
        if len(cand) <= target_len:
            out = cand
    if primary and primary.lower() not in out.lower():
        cand = norm_ws(f"{out}; {_to_third_person(primary)}" if out else _to_third_person(primary))
        if len(cand) <= target_len:
            out = cand

    nums = _collect_numeric_tokens(source_text)
    present = set(re.findall(r"\$\s*[\d,]+(?:\.\d{1,2})?|\b\d{1,3}(?:\.\d+)?%\b|\b\d+(?:\.\d+)?\s*(?:months|days|years)\b|\b20\d{2}\b", out, flags=re.I))
    missing = [n for n in nums if n not in present]
    if missing:
        suffix = "key figures: " + ", ".join(missing[:10])
        cand = norm_ws(f"{out}; {suffix}" if out else suffix)
        if len(cand) <= target_len:
            out = cand

    out = norm_ws(out)
    if len(out) > target_len:
        cut = out[:target_len].rstrip()
        if "; " in cut:
            cut = cut.rsplit("; ", 1)[0].rstrip()
        out = cut
    out = out.rstrip(";,:- ")
    if out and not out.endswith((".", "!", "?")):
        out += "."
    return out
def summarize_one_line(text: str, max_len: int = 280, runtime_cfg: Optional[Dict[str, Any]] = None) -> str:
    cleaned = _clean_summary_source(text)
    if not cleaned:
        return "Unable to extract summary from document."

    mode = str(((runtime_cfg or {}).get("summarizer") or {}).get("mode") or "hybrid").strip().lower()

    signal_n = len(re.findall(r"\$\s*[\d,]+(?:\.\d{1,2})?|\b\d{1,3}(?:\.\d+)?%\b|\b(refund|consent|misled|privacy|hardship|delay|overcharg|unnotified)\b", cleaned, flags=re.I))
    target_len = min(max_len, 560 if signal_n >= 12 else 500 if signal_n >= 8 else 400)

    # Optional offline local-AI summary with rule-based guardrail audit.
    if mode in {"local_ai", "hybrid"}:
        ai_summary = _run_local_ai_summary(cleaned, runtime_cfg=runtime_cfg)
        if ai_summary:
            audited = _audit_and_enrich_summary(ai_summary, cleaned, target_len)
            if audited:
                return audited

    trend_clause = _extract_trend_clause(cleaned)
    primary = _extract_primary_change_clause(cleaned)

    clauses: List[str] = []
    if trend_clause:
        clauses.append(_to_third_person(trend_clause))
    if primary:
        primary_l = primary.lower()
        if not (trend_clause and "amount referenced around" in primary_l):
            clauses.append(_to_third_person(primary))

    for c in _pick_key_clauses(cleaned, limit=6):
        c2 = _to_third_person(norm_ws(c) if c else c)
        lc2 = (c2 or "").lower()
        if not c2 or c2 in clauses:
            continue
        if any(k in lc2 for k in ["net profit", "npat", "profit increase", "billions and billions"]):
            continue
        if any(k in lc2 for k in ["best part of", "give or take"]):
            continue
        if trend_clause and c2.lower() in trend_clause.lower():
            continue
        if primary and len(set(re.findall(r"\$\s*[\d,]+(?:\.\d{1,2})?", c2)) & set(re.findall(r"\$\s*[\d,]+(?:\.\d{1,2})?", primary))) >= 2:
            continue
        clauses.append(c2)

    if not clauses:
        clauses = [_to_third_person(x) for x in (_sentences(cleaned)[:2] or [cleaned])]

    out = ""
    for c in clauses:
        candidate = norm_ws(f"{out}; {c}" if out else c)
        if len(candidate) <= target_len:
            out = candidate

    if not out:
        out = clauses[0]

    nums = _collect_numeric_tokens(cleaned)
    present = set(re.findall(r"\$\s*[\d,]+(?:\.\d{1,2})?|\b\d{1,3}(?:\.\d+)?%\b|\b\d+(?:\.\d+)?\s*(?:months|days|years)\b|\b20\d{2}\b", out, flags=re.I))
    missing = [n for n in nums if n not in present]
    if missing:
        suffix = "key figures: " + ", ".join(missing[:12])
        candidate = norm_ws(f"{out}; {suffix}")
        if len(candidate) <= target_len:
            out = candidate

    out = norm_ws(out)
    if len(out) > target_len:
        cut = out[:target_len].rstrip()
        if "; " in cut:
            cut = cut.rsplit("; ", 1)[0].rstrip()
        if len(cut) < max(80, target_len // 2) and " " in cut:
            cut = cut.rsplit(" ", 1)[0].rstrip()
        out = cut

    out = out.rstrip(";,:- ")
    if not out.endswith((".", "!", "?")):
        out += "."
    return out


def archive_dirname(tag: str) -> str:
    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    return f"{ts}_{tag}"


def clear_current_dir(current_dir: Path) -> None:
    current_dir.mkdir(parents=True, exist_ok=True)
    for p in current_dir.iterdir():
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        else:
            p.unlink(missing_ok=True)


def archive_current_run(current_dir: Path, archive_root: Path, tag: str) -> Path:
    archive_root.mkdir(parents=True, exist_ok=True)
    run_dir = archive_root / archive_dirname(tag)
    shutil.copytree(current_dir, run_dir)
    return run_dir


def resolve_pdf_scope(complaints_root: Path, scope: str, date_dir: Optional[str]) -> Path:
    scope = (scope or "date").strip().lower()
    if scope == "all":
        return complaints_root
    if scope == "date":
        day = (date_dir or datetime.now().strftime("%Y-%m-%d")).strip()
        if not day:
            raise ValueError("date_dir cannot be empty when scope=date")
        return complaints_root / day
    raise ValueError("--scope must be 'all' or 'date'")




def _date_bucket_from_relative_path(relative_path: str) -> str:
    rel = str(relative_path or "").replace("\\", "/")
    parts = [x for x in rel.split("/") if x]
    if not parts:
        return "unknown"
    first = parts[0]
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", first):
        return first
    if re.fullmatch(r"\d{8}", first):
        return f"{first[:4]}-{first[4:6]}-{first[6:8]}"
    return first


def _escape_html(s: str) -> str:
    return (
        str(s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _build_power_automate_html_body(
    generated_at: str,
    scope: str,
    date_dir: Optional[str],
    date_counts: Dict[str, int],
    case_rows: List[Dict[str, str]],
) -> str:
    today_display = (generated_at.split("T", 1)[0] if generated_at else datetime.now().strftime("%Y-%m-%d"))
    today_label = date_dir if date_dir else datetime.now().strftime("%Y-%m-%d")
    today_total = date_counts.get(today_label, 0)
    if not date_dir:
        today_total = len(case_rows)

    counts_items = "".join(
        f"<li><span style='color:#334155'>{_escape_html(d)}</span>: <strong>{date_counts[d]}</strong></li>"
        for d in sorted(date_counts.keys())
    )

    case_blocks: List[str] = []
    for i, row in enumerate(case_rows, start=1):
        policy = _escape_html(row.get("policy_number", ""))
        one_line = _escape_html(row.get("python_summary", ""))
        original = _escape_html(row.get("original_summary", "")).replace("\n", "<br>")
        block = (
            "<div style='border-top:1px solid #e2e8f0; margin-top:18px; padding-top:14px;'>"
            f"<h3 style='margin:0 0 8px 0; color:#0f172a; font-size:16px;'>Case {i}</h3>"
            f"<h4 style='margin:0 0 8px 0; color:#1d4ed8; font-size:14px;'>Policy Number: {policy}</h4>"
            f"<p style='margin:0 0 8px 0;'><strong>One-line Summary:</strong> {one_line}</p>"
            f"<p style='margin:0;'><strong>Original Summary:</strong><br>{original}</p>"
            "</div>"
        )
        case_blocks.append(block)

    cases_html = "".join(case_blocks) if case_blocks else "<p>No complaints found.</p>"

    return (
        "<html><body style='font-family:Arial,Helvetica,sans-serif; color:#111827; line-height:1.45;'>"
        "<p style='margin:0 0 8px 0;'>Hello team,</p>"
        "<p style='margin:0 0 14px 0;'>Today's AFCA pricing complaints summary is ready. "
        "Please review the details below.</p>"
        f"<p style='margin:0 0 12px 0;'><strong>Date:</strong> {_escape_html(today_display)}</p>"
        "<h2 style='margin:14px 0 8px 0; color:#0f172a; font-size:18px;'>Complaints count by date</h2>"
        f"<ul style='margin-top:6px;'>{counts_items}</ul>"
        f"<p style='margin:14px 0 18px 0; font-size:15px;'><strong>Today total pricing related complaints: {today_total}</strong></p>"
        "<h2 style='margin:12px 0 8px 0; color:#b45309; font-size:18px;'>Complaint Details</h2>"
        f"{cases_html}"
        "</body></html>"
    )


def _write_power_automate_artifacts(
    pa_root: Path,
    run_date: str,
    body_html: str,
) -> Path:
    pa_root.mkdir(parents=True, exist_ok=True)
    html_path = pa_root / f"complaints-{run_date}.html"
    html_path.write_text(body_html, encoding="utf-8")
    return html_path

def run_step1(scope: str, date_dir: Optional[str], do_print: bool) -> Dict[str, Path]:
    rules_cfg = load_yaml(RULES_CONFIG_PATH)
    runtime_cfg = load_yaml(RUNTIME_ACTIVE_PATH)

    paths = runtime_cfg.get("paths") or {}
    complaints_root = _resolve_path(str(paths.get("complaints_root") or "complaints"))
    output_root = _resolve_path(str(paths.get("output_root") or "_triage_output"))
    current_dir = output_root / "current"
    archive_dir = output_root / "archive"
    pa_root = output_root / "Power_Automate"

    # Early exit: if there are no PDFs for this run scope/date, do not generate any artifacts.
    pdf_root = resolve_pdf_scope(complaints_root, scope, date_dir)
    if not pdf_root.exists():
        print(f"[SKIP] PDF root not found: {pdf_root}. No artifacts generated.")
        return {}
    pdfs = sorted(pdf_root.rglob("*.pdf"))
    if not pdfs:
        print(f"[SKIP] No PDFs found under: {pdf_root}. No artifacts generated.")
        return {}

    ver_any = runtime_cfg.get("version") or (runtime_cfg.get("runtime") or {}).get("version") or "runtime_v1"
    rv = _runtime_vnum(str(ver_any))

    current_dir.mkdir(parents=True, exist_ok=True)
    archive_dir.mkdir(parents=True, exist_ok=True)

    if any(current_dir.iterdir()):
        pre = archive_current_run(current_dir, archive_dir, f"preclear_runtime_{rv}")
        print(f"[ARCHIVE] preclear archived current artifacts -> {pre}")
    clear_current_dir(current_dir)

    csv_path = current_dir / f"one_line_summary_runtime_{rv}.csv"
    diag_path = current_dir / f"triage_diagnostics_runtime_{rv}.csv"
    report_path = current_dir / f"run_quality_report_runtime_{rv}.txt"
    meta_path = current_dir / f"run_meta_one_line_summary_runtime_{rv}.json"
    learned_terms_path = current_dir / "learned_terms.json"
    learned_terms_report = current_dir / "learned_terms_report.txt"

    start_heading = str(rules_cfg.get("start_heading") or "Complaint summary")
    end_heading = str(rules_cfg.get("end_heading") or "Financial firm reference")
    max_chars = int(((runtime_cfg.get("io") or {}).get("py_summary_max_chars")) or 280)

    main_fields = [
        "file", "policy_number", "relative_path", "flags", "python_summary", "best_score",
        "original_summary", "needs_review", "needs_review_reason", "human_summary", "notes",
    ]
    diag_fields = [
        "file", "policy_number", "relative_path", "flags", "best_score", "best_summary",
        "needs_review", "needs_review_reason", "evidence_max_items", "evidence_selected_n",
    ]

    total = 0
    needs_review = 0
    low_score = 0
    date_counts: Dict[str, int] = {}
    case_rows: List[Dict[str, str]] = []

    with open(csv_path, "w", encoding="utf-8-sig", newline="") as fm, open(diag_path, "w", encoding="utf-8-sig", newline="") as fd:
        wm = csv.DictWriter(fm, fieldnames=main_fields)
        wd = csv.DictWriter(fd, fieldnames=diag_fields)
        wm.writeheader()
        wd.writeheader()

        for pdf in pdfs:
            total += 1
            rel = str(pdf.relative_to(complaints_root))
            policy = pdf.stem.split("_", 1)[0]
            date_bucket = _date_bucket_from_relative_path(rel)
            date_counts[date_bucket] = date_counts.get(date_bucket, 0) + 1
            try:
                full_text = read_pdf_text(pdf)
                section = extract_between_headings(full_text, start_heading, end_heading)
                original = section or full_text
                summary = summarize_one_line(original, max_len=max_chars, runtime_cfg=runtime_cfg)
                best_score = 10 if original.strip() else -99
                nr = "FALSE"
                nr_reason = ""
                notes = ""
            except Exception as e:
                original = ""
                summary = "Unable to extract summary from document."
                best_score = -99
                nr = "TRUE"
                nr_reason = "pdf_parse_error"
                notes = f"PDF_READ_ERROR: {type(e).__name__}: {e}"

            if nr == "TRUE":
                needs_review += 1
            if best_score < 6:
                low_score += 1

            row = {
                "file": pdf.name,
                "policy_number": policy,
                "relative_path": rel,
                "flags": "",
                "python_summary": summary,
                "best_score": str(best_score),
                "original_summary": original,
                "needs_review": nr,
                "needs_review_reason": nr_reason,
                "human_summary": "",
                "notes": notes,
            }
            wm.writerow(row)
            case_rows.append({
                "policy_number": policy,
                "python_summary": summary,
                "original_summary": original,
                "relative_path": rel,
            })
            wd.writerow({
                "file": pdf.name,
                "policy_number": policy,
                "relative_path": rel,
                "flags": "",
                "best_score": str(best_score),
                "best_summary": summary,
                "needs_review": nr,
                "needs_review_reason": nr_reason,
                "evidence_max_items": "0",
                "evidence_selected_n": "0",
            })

            if do_print:
                print(pdf.name)
                print("BEST_SCORE:", best_score)
                print("SUMMARY:", summary)
                print("-" * 110)

    learned_terms_path.write_text("{}\n", encoding="utf-8")
    learned_terms_report.write_text("Learning disabled in this deterministic Step1 script.\n", encoding="utf-8")

    generated_at = datetime.now().isoformat(timespec="seconds")
    run_date = generated_at.split("T", 1)[0]
    pa_html = _build_power_automate_html_body(
        generated_at=generated_at,
        scope=scope,
        date_dir=date_dir,
        date_counts=date_counts,
        case_rows=case_rows,
    )
    pa_html_path = _write_power_automate_artifacts(
        pa_root=pa_root,
        run_date=run_date,
        body_html=pa_html,
    )

    report_lines = [
        f"Generated: {generated_at}",
        f"PDF root: {pdf_root}",
        f"Rules config: {RULES_CONFIG_PATH}",
        f"Runtime config: {RUNTIME_ACTIVE_PATH}",
        f"Runtime version: {rv}",
        f"Total PDFs: {total}",
        f"Needs_review: {needs_review}",
        f"Low score (<6): {low_score}",
    ]
    report_path.write_text("\n".join(report_lines) + "\n", encoding="utf-8")

    meta_path.write_text(json.dumps({
        "generated_at": generated_at,
        "scope": scope,
        "date_dir": date_dir,
        "runtime_version": rv,
        "counts": {"total_pdfs": total, "needs_review": needs_review, "low_score": low_score},
        "outputs": {
            "csv": str(csv_path),
            "diag_csv": str(diag_path),
            "quality_report": str(report_path),
            "learned_terms": str(learned_terms_path),
            "learned_terms_report": str(learned_terms_report),
            "pa_html": str(pa_html_path),
        },
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    run_arch = archive_current_run(current_dir, archive_dir, f"runtime_{rv}")
    print(f"[ARCHIVE] archived run -> {run_arch}")

    return {
        "csv": csv_path,
        "diag_csv": diag_path,
        "report": report_path,
        "meta": meta_path,
        "pa_html": pa_html_path,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--scope",
        default="date",
        choices=["all", "date"],
        help="all or date (default: date, using today's folder unless --date_dir is provided)",
    )
    ap.add_argument(
        "--date_dir",
        default=None,
        help="when scope=date, folder name under complaints/ (default: today's YYYY-MM-DD)",
    )
    ap.add_argument("--print", dest="do_print", action="store_true", help="print per-file summaries")
    args = ap.parse_args()

    run_step1(
        scope=args.scope,
        date_dir=args.date_dir if args.scope == "date" else None,
        do_print=bool(args.do_print),
    )


if __name__ == "__main__":
    main()
