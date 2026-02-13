import argparse
import csv
import importlib.util
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATASETS_DIR = PROJECT_ROOT / "learning" / "datasets"
HISTORY_DIR = PROJECT_ROOT / "learning" / "history"
STEP1_PATH = PROJECT_ROOT / "code" / "01_extract_one_line_summary.py"


def _load_step1_module():
    import sys
    import types

    # Step1 hard-requires yaml at import time; provide a minimal fallback for offline eval environments.
    if "yaml" not in sys.modules:
        sys.modules["yaml"] = types.SimpleNamespace(safe_load=lambda s: {})

    spec = importlib.util.spec_from_file_location("step1", STEP1_PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _tok(s: str) -> List[str]:
    return [w for w in re.sub(r"[^a-z0-9$%]+", " ", (s or "").lower()).split() if w]


def _money(s: str) -> List[str]:
    return re.findall(r"\$\s*[\d,]+(?:\.\d{1,2})?", s or "")


def _f1(pred: str, gold: str) -> float:
    p, g = set(_tok(pred)), set(_tok(gold))
    if not p and not g:
        return 1.0
    if not p or not g:
        return 0.0
    tp = len(p & g)
    prec = tp / len(p)
    rec = tp / len(g)
    return 0.0 if (prec + rec) == 0 else 2 * prec * rec / (prec + rec)


def evaluate(dataset_csv: Path, max_len: int) -> Dict[str, float]:
    mod = _load_step1_module()
    n = 0
    f1_sum = 0.0
    money_hit = 0

    with open(dataset_csv, "r", encoding="utf-8-sig", newline="") as f:
        rd = csv.DictReader(f)
        for row in rd:
            orig = row.get("original_summary", "") or ""
            gold = row.get("human_summary_gold", "") or ""
            if not orig or not gold:
                continue
            pred = mod.summarize_one_line(orig, max_len=max_len, runtime_cfg={})
            n += 1
            f1_sum += _f1(pred, gold)
            g_money = set(_money(gold))
            if not g_money or (set(_money(pred)) & g_money):
                money_hit += 1

    avg_f1 = (f1_sum / n) if n else 0.0
    money_recall = (money_hit / n) if n else 0.0
    return {"n": n, "avg_token_f1": avg_f1, "money_recall_at_case": money_recall}


def main() -> None:
    ap = argparse.ArgumentParser(description="Evaluate Step1 summary quality against human gold dataset.")
    ap.add_argument("--dataset", default=str(DATASETS_DIR / "train.csv"), help="Dataset CSV containing original_summary and human_summary_gold")
    ap.add_argument("--max_len", type=int, default=320, help="Summary max length for evaluation")
    ap.add_argument("--version", default=datetime.now().strftime("%Y%m%d_%H%M%S"), help="Version tag for report")
    args = ap.parse_args()

    dataset = Path(args.dataset).expanduser().resolve()
    stats = evaluate(dataset, args.max_len)

    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    out_json = HISTORY_DIR / f"eval_report_{args.version}.json"
    out_txt = HISTORY_DIR / f"eval_report_{args.version}.txt"

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "dataset": str(dataset),
        "max_len": args.max_len,
        "metrics": stats,
    }
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_txt.write_text(
        "\n".join([
            f"generated_at: {payload['generated_at']}",
            f"dataset: {dataset}",
            f"n: {stats['n']}",
            f"avg_token_f1: {stats['avg_token_f1']:.4f}",
            f"money_recall_at_case: {stats['money_recall_at_case']:.4f}",
        ]) + "\n",
        encoding="utf-8",
    )
    print(f"[OK] {out_json}")
    print(f"[OK] {out_txt}")


if __name__ == "__main__":
    main()
