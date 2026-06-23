"""
dump_codebase.py
Dumps every file in the project directory into a single txt file.
Each file is separated by a clear header banner.
"""

import os

# ── CONFIG ──────────────────────────────────────────────────────────────────
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))   # same folder as this script
OUTPUT_FILE = os.path.join(PROJECT_DIR, "codebase_dump.txt")

# File extensions to include (add more as needed)
INCLUDE_EXTENSIONS = {
    ".py", ".txt", ".json", ".yaml", ".yml",
    ".md", ".env.example", ".cfg", ".toml", ".ini", ".sh",
    "Dockerfile",   # no extension — handled separately below
}

# Names / paths to always skip
SKIP_NAMES = {
    "codebase_dump.txt",   # don't include the output file itself
    "dump_codebase.py",    # skip this script too
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    "node_modules",
}
# ─────────────────────────────────────────────────────────────────────────────


def should_include(filepath: str) -> bool:
    """Return True if this file should be included in the dump."""
    basename = os.path.basename(filepath)
    _, ext = os.path.splitext(basename)

    # Skip blacklisted names anywhere in the path
    parts = filepath.replace("\\", "/").split("/")
    for part in parts:
        if part in SKIP_NAMES:
            return False

    # Include by extension, or by exact filename (e.g. Dockerfile)
    return ext in INCLUDE_EXTENSIONS or basename in INCLUDE_EXTENSIONS


def dump_codebase():
    collected = []

    for root, dirs, files in os.walk(PROJECT_DIR):
        # Prune unwanted directories in-place so os.walk skips them
        dirs[:] = [d for d in dirs if d not in SKIP_NAMES]

        for filename in sorted(files):
            full_path = os.path.join(root, filename)
            rel_path  = os.path.relpath(full_path, PROJECT_DIR)

            if not should_include(full_path):
                continue

            try:
                with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
            except Exception as e:
                content = f"[ERROR reading file: {e}]"

            banner = (
                f"\n{'=' * 72}\n"
                f"FILE: {rel_path}\n"
                f"{'=' * 72}\n"
            )
            collected.append(banner + content)

    if not collected:
        print("[WARN] No files found to dump.")
        return

    with open(OUTPUT_FILE, "w", encoding="utf-8") as out:
        out.write(f"CODEBASE DUMP — {PROJECT_DIR}\n")
        out.write(f"Total files: {len(collected)}\n")
        out.write("=" * 72 + "\n")
        out.writelines(collected)

    print(f"[OK] Dumped {len(collected)} files -> {OUTPUT_FILE}")


if __name__ == "__main__":
    dump_codebase()
