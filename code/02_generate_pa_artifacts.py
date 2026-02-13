import argparse
import csv
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import yaml  # type: ignore
except Exception as e:  # pragma: no cover
    raise RuntimeError("PyYAML is required. Install with: pip install pyyaml") from e

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ACTIVE_PATH = PROJECT_ROOT / "config" / "runtime_config_active.yaml"


def load_yaml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    obj = yaml.safe_load(path.read_text(encoding="utf-8", errors="ignore"))
    if not isinstance(obj, dict):
        raise ValueError(f"Config root must be mapping: {path}")
    return obj


def _resolve_path(p: str) -> Path:
    pp = Path(str(p or "").strip())
    return pp if pp.is_absolute() else (PROJECT_ROOT / pp).resolve()


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
    date_dir: Optional[str],
    date_counts: Dict[str, int],
    case_rows: List[Dict[str, str]],
) -> str:
    today_display = generated_at.split("T", 1)[0]
    today_label = date_dir if date_dir else today_display
    today_total = date_counts.get(today_label, 0) if date_dir else len(case_rows)

    counts_items = "".join(
        f"<li><span style='color:#334155'>{_escape_html(d)}</span>: <strong>{date_counts[d]}</strong></li>"
        for d in sorted(date_counts.keys())
    )

    case_blocks: List[str] = []
    for i, row in enumerate(case_rows, start=1):
        policy = _escape_html(row.get("policy_number", ""))
        one_line = _escape_html(row.get("python_summary", ""))
        original = _escape_html(row.get("original_summary", "")).replace("\n", "<br>")
        case_blocks.append(
            "<div style='border-top:1px solid #e2e8f0; margin-top:18px; padding-top:14px;'>"
            f"<h3 style='margin:0 0 8px 0; color:#0f172a; font-size:16px;'>Case {i}</h3>"
            f"<h4 style='margin:0 0 8px 0; color:#1d4ed8; font-size:14px;'>Policy Number: {policy}</h4>"
            f"<p style='margin:0 0 8px 0;'><strong>One-line Summary:</strong> {one_line}</p>"
            f"<p style='margin:0;'><strong>Original Summary:</strong><br>{original}</p>"
            "</div>"
        )

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


def _find_latest_summary_csv(current_dir: Path) -> Path:
    files = sorted(current_dir.glob("one_line_summary_runtime_*.csv"), key=lambda p: p.stat().st_mtime)
    if not files:
        raise FileNotFoundError(f"No one_line_summary_runtime_*.csv found in {current_dir}")
    return files[-1]


def run_pa_generation(csv_path: Optional[Path], date_dir: Optional[str]) -> Dict[str, Path]:
    runtime_cfg = load_yaml(RUNTIME_ACTIVE_PATH)
    paths = runtime_cfg.get("paths") or {}
    output_root = _resolve_path(str(paths.get("output_root") or "_triage_output"))
    current_dir = output_root / "current"
    pa_root = output_root / "AFCA_Complaints"

    src_csv = csv_path or _find_latest_summary_csv(current_dir)
    if not src_csv.exists():
        raise FileNotFoundError(f"CSV not found: {src_csv}")

    case_rows: List[Dict[str, str]] = []
    date_counts: Dict[str, int] = {}

    with open(src_csv, "r", encoding="utf-8-sig", newline="") as f:
        rd = csv.DictReader(f)
        for row in rd:
            rel = str(row.get("relative_path") or "")
            d = _date_bucket_from_relative_path(rel)
            date_counts[d] = date_counts.get(d, 0) + 1
            case_rows.append({
                "file": str(row.get("file") or Path(rel).name),
                "policy_number": str(row.get("policy_number") or ""),
                "python_summary": str(row.get("python_summary") or ""),
                "original_summary": str(row.get("original_summary") or ""),
                "relative_path": rel,
            })

    generated_at = datetime.now().isoformat(timespec="seconds")
    run_date = generated_at.split("T", 1)[0]
    body_html = _build_power_automate_html_body(
        generated_at=generated_at,
        date_dir=date_dir,
        date_counts=date_counts,
        case_rows=case_rows,
    )

    pa_root.mkdir(parents=True, exist_ok=True)
    html_path = pa_root / "complaints.html"
    html_path.write_text(body_html, encoding="utf-8")

    txt_path = pa_root / "pdf_list.txt"
    txt_lines = [f"/{run_date}/{r.get('file','')}" for r in case_rows if r.get("file")]
    txt_path.write_text("\n".join(txt_lines), encoding="utf-8")

    meta_path = pa_root / "pa_generation_meta.json"
    meta_path.write_text(json.dumps({
        "generated_at": generated_at,
        "source_csv": str(src_csv),
        "counts": {"total": len(case_rows)},
        "outputs": {"pa_html": str(html_path), "pa_pdf_list": str(txt_path)},
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[OK] PA artifacts generated from: {src_csv}")
    return {"pa_html": html_path, "pa_pdf_names_txt": txt_path, "pa_meta": meta_path}


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate Power Automate artifacts from Step1 CSV output.")
    ap.add_argument("--csv", default=None, help="Path to one_line_summary_runtime_*.csv (default: latest in _triage_output/current)")
    ap.add_argument("--date_dir", default=None, help="Optional date folder label for 'today total' calculation")
    args = ap.parse_args()

    csv_path = Path(args.csv).expanduser().resolve() if args.csv else None
    run_pa_generation(csv_path=csv_path, date_dir=args.date_dir)


if __name__ == "__main__":
    main()
