import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

try:
    import yaml  # type: ignore
except Exception as e:  # pragma: no cover
    raise RuntimeError("PyYAML is required. Install with: pip install pyyaml") from e

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = PROJECT_ROOT / "config"
ACTIVE_PATH = CONFIG_DIR / "runtime_config_active.yaml"
NEXT_PATH = CONFIG_DIR / "runtime_config_next.yaml"


def _resolve_arg_path(p: str) -> Path:
    pp = Path(str(p or "").strip()).expanduser()
    return pp if pp.is_absolute() else (PROJECT_ROOT / pp).resolve()


def load_yaml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(path)
    obj = yaml.safe_load(path.read_text(encoding="utf-8", errors="ignore"))
    if not isinstance(obj, dict):
        raise ValueError(f"Config root must be mapping: {path}")
    return obj


def main() -> None:
    ap = argparse.ArgumentParser(description="Promote learned model paths into runtime_config_next.yaml.")
    ap.add_argument("--eval_report", required=True, help="Path to eval_report_*.json")
    ap.add_argument("--rules", required=True, help="Path to rules_auto_*.json")
    ap.add_argument("--weights", required=True, help="Path to weights_*.json")
    ap.add_argument("--min_f1", type=float, default=0.30)
    ap.add_argument("--min_money_recall", type=float, default=0.70)
    args = ap.parse_args()

    eval_path = _resolve_arg_path(args.eval_report)
    rules_path = _resolve_arg_path(args.rules)
    weights_path = _resolve_arg_path(args.weights)

    missing = [str(p) for p in (eval_path, rules_path, weights_path) if not p.exists()]
    if missing:
        raise FileNotFoundError(
            "Missing input file(s):\n- " + "\n- ".join(missing) +
            f"\nCurrent working dir: {Path.cwd()}\nProject root: {PROJECT_ROOT}"
        )

    report = json.loads(eval_path.read_text(encoding="utf-8"))
    metrics = report.get("metrics", {})
    f1 = float(metrics.get("avg_token_f1", 0.0))
    mr = float(metrics.get("money_recall_at_case", 0.0))

    if f1 < args.min_f1 or mr < args.min_money_recall:
        raise SystemExit(
            f"promotion blocked: avg_token_f1={f1:.4f} (min {args.min_f1}), "
            f"money_recall_at_case={mr:.4f} (min {args.min_money_recall})"
        )

    cfg = load_yaml(ACTIVE_PATH)
    cfg.setdefault("learning_runtime", {})
    cfg["learning_runtime"].update({
        "enabled": True,
        "rules_auto_path": str(rules_path),
        "weights_path": str(weights_path),
        "promoted_at": datetime.now().isoformat(timespec="seconds"),
        "eval_report": str(eval_path),
    })

    NEXT_PATH.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True), encoding="utf-8")
    print(f"[OK] promoted config written: {NEXT_PATH}")


if __name__ == "__main__":
    main()
