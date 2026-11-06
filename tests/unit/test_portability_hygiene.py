"""Portability hygiene test: scan for developer-specific paths.

These tests must themselves be clean of the literals they forbid, or they
would be their own violation. The patterns are therefore assembled at runtime
from fragments: there is no contiguous ``/home/<user>`` string in this file,
so it needs no exemption from its own scan.
"""

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent

#: Assembled, never written out: keeps this scanner out of its own results.
PRIVATE_PATH = "/" + "home" + "/" + "zacch"
PRIVATE_MODEL_NAME = "Qwen3.8-27B-" + "TTURBO-Fable-C-Fusion-709-L-Uncen-NM-DAU-NEO-" + "MTP-IQ4_XS"

BINARY_SUFFIXES = (".gguf", ".bin", ".png", ".jpg", ".jpeg")


def _names_for(target, root=REPO_ROOT):
    """File names to scan under ``target``.

    Tracked files plus untracked ones that are not gitignored. The untracked
    half matters: a brand-new directory is exactly when a private path is most
    likely to be introduced, and it must not become visible only after it has
    been committed. Ignored files stay out — a local ``.env`` is not
    project-authored text and is not shipped.
    """
    try:
        result = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", target],
            cwd=root, capture_output=True, text=True, timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        result = None
    if result is not None and result.returncode == 0:
        return result.stdout.split() if result.stdout.strip() else []

    # No git available: fall back to the working tree.
    base = root / target
    if base.is_dir():
        return [str(p.relative_to(root)) for p in sorted(base.rglob("*")) if p.is_file()]
    if base.exists():
        return [target]
    return []


def _tracked_files(targets, root=REPO_ROOT):
    """Yield (repo-relative name, text) for scanned, non-binary files."""
    for target in targets:
        if not (root / target).exists():
            continue
        for file in _names_for(target, root):
            path = root / file
            if not path.exists() or path.suffix in BINARY_SUFFIXES:
                continue
            try:
                yield file, path.read_text(encoding="utf-8", errors="ignore")
            except (OSError, UnicodeDecodeError):
                continue


def _scan(targets, needle, label):
    return _violations(_tracked_files(targets), needle, label)


def _violations(files, needle, label):
    """Pure detection step, so the check itself can be tested."""
    return [f"{file}: contains {label}"
            for file, content in files if needle in content]


#: Everywhere project-authored text can live. benchmarks/ and scripts/ were
#: added after a private model name reached a setup script unnoticed: the scan
#: is only as good as its target list. observability/ joined for the same
#: reason — compose files and dashboard JSON are hand-written project text too.
SCAN_TARGETS = ["src/", "tests/", "profiles/", "docs/", "benchmarks/",
                "scripts/", "observability/", "README.md", "ARCHITECTURE.md",
                "SPEC.md"]


def test_no_private_paths_in_source():
    """No tracked source or test file hardcodes a developer home path."""
    targets = SCAN_TARGETS
    assert PRIVATE_PATH not in Path(__file__).read_text(), \
        "this scanner must not contain its own forbidden literal"
    violations = _scan(targets, PRIVATE_PATH, "a private home path")
    assert not violations, f"Private paths found: {violations}"


def test_no_private_model_name_in_source():
    """No tracked source file names the private model artifact."""
    targets = SCAN_TARGETS
    violations = _scan(targets, PRIVATE_MODEL_NAME, "the private model name")
    assert not violations, f"Private model name found: {violations}"


def test_detection_step_reports_a_real_violation():
    """Guard: a genuine hit must be reported, not silently swallowed."""
    forged = [("forged.py", f'PATH = "/{PRIVATE_PATH}/projects"\n'),
              ("clean.py", "PATH = '/usr/bin'\n")]
    hits = _violations(forged, PRIVATE_PATH, "a private home path")
    assert hits == ["forged.py: contains a private home path"]


def test_untracked_files_are_scanned_and_ignored_ones_are_not(tmp_path):
    """Guard: a never-committed file cannot hide a violation.

    The ignored half is the other half of the same rule: a local env file is
    not project-authored text and must not make the suite fail.
    """
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, timeout=10)
    (tmp_path / "new").mkdir()
    (tmp_path / "new" / "compose.yml").write_text(f'PATH = "/{PRIVATE_PATH}/x"\n')
    (tmp_path / "new" / ".env").write_text(f'PATH = "/{PRIVATE_PATH}/x"\n')
    (tmp_path / ".gitignore").write_text(".env\n")

    hits = _violations(_tracked_files(["new/"], root=tmp_path),
                       PRIVATE_PATH, "a private home path")
    assert hits == ["new/compose.yml: contains a private home path"]


def test_scan_covers_every_project_text_directory():
    """A new top-level text directory must not slip past the scan."""
    for target in ("benchmarks/", "scripts/", "observability/"):
        assert target in SCAN_TARGETS, target
