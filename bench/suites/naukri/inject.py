"""Starting state for NaukriApply tasks (cwd = workspace). `inject.py <task>` plants the bug;
`--fix` applies the reference fix (soundness test only). Replacements assert their anchor exists."""
import sys
from pathlib import Path

MATCHING = Path("naukri_assistant/matching.py")
TEXT = Path("naukri_assistant/text_utils.py")

BUGS = {
    # skills are looked up in the title only, so description skills never count
    "bugfix": (MATCHING, "matched_skills = sorted(skill for skill in resume_skills if skill and skill in job_blob)",
               "matched_skills = sorted(skill for skill in resume_skills if skill and skill in title_blob)"),
    # "1 to 3 years" no longer parses as a range (only "1-3 years" does)
    "debug": (TEXT, r'r"(?P<low>\d+(?:\.\d+)?)\s*(?:-|to)\s*(?P<high>\d+(?:\.\d+)?)\s*(?:years?|yrs?)",',
              r'r"(?P<low>\d+(?:\.\d+)?)\s*(?:-)\s*(?P<high>\d+(?:\.\d+)?)\s*(?:years?|yrs?)",'),
}


def replace(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    if old not in text:
        sys.exit(f"inject: expected text not found in {path}: {old[:60]!r}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


if __name__ == "__main__":
    task, fix = sys.argv[1], "--fix" in sys.argv
    if task in BUGS:
        path, good, bad = BUGS[task]
        replace(path, bad, good) if fix else replace(path, good, bad)
