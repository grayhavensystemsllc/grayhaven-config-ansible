"""Bash DEBUG-trap coverage support for managed shell programs."""

from __future__ import annotations

import pathlib
import time
import xml.etree.ElementTree as ET


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
TRACE_PATH = REPO_ROOT / "shell-coverage.lines"
XML_PATH = REPO_ROOT / "shell-coverage.xml"
# Runner and poller are orchestration scripts tested with targeted stubs; the
# line report covers only the standalone helpers listed here.
PROGRAMS = (
    pathlib.Path("roles/admin_access/files/gtmux"),
    pathlib.Path("scripts/install-galaxy-collections"),
)

TRACE_SCRIPT = r"""
set -o functrace
__grayhaven_cov_file="${GRAYHAVEN_SHELL_COVERAGE_FILE:-}"
trap '__src="${BASH_SOURCE[0]:-}"; __line="${LINENO}"
  case "${__src}" in
    */grayhaven-ansible-runner|*/grayhaven-ansible-poller|*/gtmux|*/install-galaxy-collections)
      if [[ -n "${__grayhaven_cov_file}" ]]; then
        printf "%s|%s\n" "${__src}" "${__line}" >> "${__grayhaven_cov_file}"
      fi
      ;;
  esac
' DEBUG
"""


def coverage_environment(trace_script: pathlib.Path) -> dict[str, str]:
    """Return environment additions that activate Bash line tracing."""
    return {
        "BASH_ENV": str(trace_script),
        "GRAYHAVEN_SHELL_COVERAGE_FILE": str(TRACE_PATH),
    }


def executable_lines(path: pathlib.Path) -> list[int]:
    """Return executable Bash lines, excluding syntax-only delimiters."""
    lines: list[int] = []
    in_heredoc = False
    heredoc_end = ""
    continued = False
    for number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = raw_line.strip()
        if in_heredoc:
            if stripped == heredoc_end:
                in_heredoc = False
            continue
        if continued:
            continued = raw_line.rstrip().endswith("\\")
            continue
        if not stripped or stripped.startswith("#"):
            continue
        continued = raw_line.rstrip().endswith("\\")
        if stripped in {"(", ")", "}", ";;", "esac", "fi", "done", "else", "then"}:
            continue
        if stripped.endswith("() {"):
            continue
        lines.append(number)
        if "<<'PY'" in raw_line:
            in_heredoc, heredoc_end = True, "PY"
        elif "<<'EOF'" in raw_line or "<<EOF" in raw_line:
            in_heredoc, heredoc_end = True, "EOF"
    return lines


def write_report() -> None:
    """Write a Cobertura report for the selected shell helpers."""
    traced: dict[str, set[int]] = {}
    if TRACE_PATH.exists():
        for record in TRACE_PATH.read_text(encoding="utf-8").splitlines():
            source, separator, raw_line = record.rpartition("|")
            if separator and raw_line.isdigit():
                traced.setdefault(str(pathlib.Path(source).resolve()), set()).add(int(raw_line))

    data: list[tuple[pathlib.Path, list[int], set[int]]] = []
    total = covered_total = 0
    for relative in PROGRAMS:
        source = REPO_ROOT / relative
        valid = executable_lines(source)
        observed = traced.get(str(source.resolve()), set())
        covered = set(valid) & observed
        data.append((relative, valid, covered))
        total += len(valid)
        covered_total += len(covered)

    rate = covered_total / total if total else 1.0
    root = ET.Element(
        "coverage",
        {
            "line-rate": f"{rate:.4f}",
            "branch-rate": "0",
            "version": "grayhaven-shell-unittest",
            "timestamp": str(int(time.time())),
            "lines-covered": str(covered_total),
            "lines-valid": str(total),
            "branches-covered": "0",
            "branches-valid": "0",
        },
    )
    sources = ET.SubElement(root, "sources")
    ET.SubElement(sources, "source").text = str(REPO_ROOT)
    packages = ET.SubElement(root, "packages")
    package = ET.SubElement(
        packages,
        "package",
        {"name": "grayhaven-config-shell", "line-rate": f"{rate:.4f}", "branch-rate": "0"},
    )
    classes = ET.SubElement(package, "classes")
    for relative, valid, covered in data:
        class_rate = len(covered) / len(valid) if valid else 1.0
        class_node = ET.SubElement(
            classes,
            "class",
            {
                "name": relative.name,
                "filename": relative.as_posix(),
                "line-rate": f"{class_rate:.4f}",
                "branch-rate": "0",
            },
        )
        lines = ET.SubElement(class_node, "lines")
        for number in valid:
            ET.SubElement(lines, "line", {"number": str(number), "hits": "1" if number in covered else "0"})
    ET.ElementTree(root).write(XML_PATH, encoding="utf-8", xml_declaration=True)


def reset() -> None:
    """Remove coverage artifacts before a test run."""
    TRACE_PATH.unlink(missing_ok=True)
    XML_PATH.unlink(missing_ok=True)
