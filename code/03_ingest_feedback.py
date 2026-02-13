import argparse
import csv
import hashlib
from pathlib import Path
from typing import Dict, Iterable, List, Set

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LEARNING_ROOT = PROJECT_ROOT / "learning"
FEEDBACK_DIR = LEARNING_ROOT / "feedback"
DATASETS_DIR = LEARNING_ROOT / "datasets"

REQUIRED = ["original_summary", "human_summary_gold"]
OUTPUT_FIELDS = [
    "case_id",
    "file",
    "policy_number",
    "relative_path",
    "original_summary",
    "python_summary_old",
    "human_summary_gold",
    "error_tags",
    "source_file",
]


def _norm(x: str) -> str:
    return " ".join((x or "").strip().split())


def _case_id(row: Dict[str, str]) -> str:
    seed = "||".join([
        _norm(row.get("file", "")),
        _norm(row.get("policy_number", "")),
        _norm(row.get("relative_path", "")),
        _norm(row.get("original_summary", "")),
        _norm(row.get("human_summary_gold", "")),
    ])
    return hashlib.md5(seed.encode("utf-8", errors="ignore")).hexdigest()


def _iter_feedback_rows(csv_paths: Iterable[Path]) -> Iterable[Dict[str, str]]:
    for p in csv_paths:
        with open(p, "r", encoding="utf-8-sig", newline="") as f:
            rd = csv.DictReader(f)
            for row in rd:
                if not all(_norm(row.get(k, "")) for k in REQUIRED):
                    continue
                out = {
                    "case_id": "",
                    "file": row.get("file", "") or "",
                    "policy_number": row.get("policy_number", "") or "",
                    "relative_path": row.get("relative_path", "") or "",
                    "original_summary": row.get("original_summary", "") or "",
                    "python_summary_old": row.get("python_summary", row.get("python_summary_old", "")) or "",
                    "human_summary_gold": row.get("human_summary_gold", "") or "",
                    "error_tags": row.get("error_tags", "") or "",
                    "source_file": str(p),
                }
                out["case_id"] = _case_id(out)
                yield out


def ingest(csv_paths: List[Path], out_csv: Path) -> Dict[str, int]:
    out_csv.parent.mkdir(parents=True, exist_ok=True)

    existing_ids: Set[str] = set()
    existing_rows: List[Dict[str, str]] = []
    if out_csv.exists():
        with open(out_csv, "r", encoding="utf-8-sig", newline="") as f:
            rd = csv.DictReader(f)
            for r in rd:
                existing_rows.append({k: r.get(k, "") for k in OUTPUT_FIELDS})
                if r.get("case_id"):
                    existing_ids.add(r["case_id"])

    added = 0
    for row in _iter_feedback_rows(csv_paths):
        cid = row["case_id"]
        if cid in existing_ids:
            continue
        existing_rows.append(row)
        existing_ids.add(cid)
        added += 1

    with open(out_csv, "w", encoding="utf-8-sig", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=OUTPUT_FIELDS)
        wr.writeheader()
        wr.writerows(existing_rows)

    return {"total": len(existing_rows), "added": added}


def main() -> None:
    ap = argparse.ArgumentParser(description="Ingest human feedback CSVs into learning dataset.")
    ap.add_argument("--feedback_dir", default=str(FEEDBACK_DIR), help="Folder containing feedback CSV files")
    ap.add_argument("--out", default=str(DATASETS_DIR / "train.csv"), help="Output merged train CSV")
    args = ap.parse_args()

    fb_dir = Path(args.feedback_dir).expanduser().resolve()
    out_csv = Path(args.out).expanduser().resolve()
    csv_paths = sorted(fb_dir.glob("*.csv"))
    if not csv_paths:
        raise FileNotFoundError(f"No feedback CSV files found in: {fb_dir}")

    stats = ingest(csv_paths, out_csv)
    print(f"[OK] merged dataset -> {out_csv}")
    print(f"[STATS] total={stats['total']} added={stats['added']} source_files={len(csv_paths)}")


if __name__ == "__main__":
    main()
