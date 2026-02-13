import argparse
from pathlib import Path


def ensure_folder_structure(root: Path) -> None:
    """Create the project folder structure required by Step1 scripts."""
    dirs = [
        root / "code",
        root / "config",
        root / "complaints",
        root / "_triage_output",
        root / "_triage_output" / "current",
        root / "_triage_output" / "archive",
        root / "_triage_output" / "AFCA_Complaints",
        root / "_triage_output" / "AFCA_Complaints" / "archive",
        root / "learning",
        root / "learning" / "feedback",
        root / "learning" / "datasets",
        root / "learning" / "models",
        root / "learning" / "history",
    ]

    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create AFCA complaint triage folder structure."
    )
    parser.add_argument(
        "--root",
        default=str(Path(__file__).resolve().parents[1]),
        help="Project root path (default: repo root).",
    )
    args = parser.parse_args()

    root = Path(args.root).expanduser().resolve()
    ensure_folder_structure(root)
    print(f"[OK] Folder structure ensured under: {root}")


if __name__ == "__main__":
    main()
