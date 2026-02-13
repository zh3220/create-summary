import argparse
import csv
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Dict, List

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATASETS_DIR = PROJECT_ROOT / "learning" / "datasets"
MODELS_DIR = PROJECT_ROOT / "learning" / "models"
HISTORY_DIR = PROJECT_ROOT / "learning" / "history"


def _tok(text: str) -> List[str]:
    t = re.sub(r"[^a-z0-9$%]+", " ", (text or "").lower())
    return [w for w in t.split() if len(w) >= 3]


def _money_tokens(text: str) -> List[str]:
    return re.findall(r"\$\s*[\d,]+(?:\.\d{1,2})?", text or "")


def learn(train_csv: Path, out_rules: Path, out_weights: Path, out_report: Path) -> Dict[str, int]:
    if not train_csv.exists():
        raise FileNotFoundError(f"train csv not found: {train_csv}")

    token_gain = Counter()
    tag_counts = Counter()
    n = 0

    with open(train_csv, "r", encoding="utf-8-sig", newline="") as f:
        rd = csv.DictReader(f)
        for row in rd:
            orig = row.get("original_summary", "") or ""
            gold = row.get("human_summary_gold", "") or ""
            pred = row.get("python_summary_old", "") or ""
            tags = [x.strip().lower() for x in (row.get("error_tags", "") or "").split(",") if x.strip()]

            if not orig or not gold:
                continue
            n += 1
            for t in tags:
                tag_counts[t] += 1

            gold_set = set(_tok(gold))
            pred_set = set(_tok(pred))
            for t in gold_set:
                token_gain[t] += 2
            for t in pred_set - gold_set:
                token_gain[t] -= 1

            # encourage keeping money mentions present in gold but missing in pred
            g_money = set(_money_tokens(gold))
            p_money = set(_money_tokens(pred))
            if g_money - p_money:
                token_gain["__boost_money_presence__"] += len(g_money - p_money)

    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    out_rules.parent.mkdir(parents=True, exist_ok=True)
    out_weights.parent.mkdir(parents=True, exist_ok=True)
    out_report.parent.mkdir(parents=True, exist_ok=True)

    top_tokens = [{"token": k, "weight": v} for k, v in token_gain.most_common(200) if v > 0]
    down_tokens = [{"token": k, "weight": v} for k, v in token_gain.items() if v < 0]

    rules = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source_train_csv": str(train_csv),
        "n_samples": n,
        "auto_promote_terms": [x["token"] for x in top_tokens[:80]],
        "auto_demote_terms": [x["token"] for x in sorted(down_tokens, key=lambda x: x["weight"])[:40]],
        "error_tags_top": [{"tag": k, "count": c} for k, c in tag_counts.most_common(30)],
    }

    weights = {
        "generated_at": rules["generated_at"],
        "n_samples": n,
        "token_weights": {x["token"]: int(x["weight"]) for x in top_tokens},
        "special": {
            "boost_money_presence": int(token_gain.get("__boost_money_presence__", 0)),
        },
    }

    out_rules.write_text(json.dumps(rules, ensure_ascii=False, indent=2), encoding="utf-8")
    out_weights.write_text(json.dumps(weights, ensure_ascii=False, indent=2), encoding="utf-8")

    report_lines = [
        f"generated_at: {rules['generated_at']}",
        f"source_train_csv: {train_csv}",
        f"n_samples: {n}",
        "",
        "top error tags:",
    ]
    for x in rules["error_tags_top"][:15]:
        report_lines.append(f"  - {x['tag']}: {x['count']}")
    report_lines.append("\ntop promoted tokens:")
    for x in top_tokens[:30]:
        report_lines.append(f"  - {x['token']}: {x['weight']}")

    out_report.write_text("\n".join(report_lines) + "\n", encoding="utf-8")
    return {"n_samples": n, "promoted": len(top_tokens)}


def main() -> None:
    ap = argparse.ArgumentParser(description="Learn token weights/rules from human feedback dataset.")
    ap.add_argument("--train", default=str(DATASETS_DIR / "train.csv"), help="Input merged training CSV")
    ap.add_argument("--version", default=datetime.now().strftime("%Y%m%d_%H%M%S"), help="Version tag for output model files")
    args = ap.parse_args()

    train = Path(args.train).expanduser().resolve()
    version = args.version
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)

    out_rules = MODELS_DIR / f"rules_auto_{version}.json"
    out_weights = MODELS_DIR / f"weights_{version}.json"
    out_report = HISTORY_DIR / f"learn_report_{version}.txt"
    stats = learn(train, out_rules, out_weights, out_report)
    print(f"[OK] rules -> {out_rules}")
    print(f"[OK] weights -> {out_weights}")
    print(f"[OK] report -> {out_report}")
    print(f"[STATS] n_samples={stats['n_samples']} promoted={stats['promoted']}")


if __name__ == "__main__":
    main()
