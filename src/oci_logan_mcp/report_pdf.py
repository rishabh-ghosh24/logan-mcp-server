"""Markdown-to-PDF rendering for incident reports."""
from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Iterable, List, Tuple

import matplotlib

matplotlib.use("Agg")

from matplotlib import pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages


class ReportPdfError(ValueError):
    """Raised when a report cannot be rendered to PDF."""


def render_markdown_pdf(markdown: str, title: str | None, output_path: Path) -> Path:
    """Render the report-generator Markdown subset to a valid local PDF."""
    if not isinstance(markdown, str) or not markdown.strip():
        raise ReportPdfError("markdown is required and must be a non-empty string")

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    lines = _layout_lines(markdown, title=title)
    pages = _paginate(lines, lines_per_page=52)
    metadata = {
        "Creator": "logan-mcp-server",
        "Producer": "matplotlib",
        "CreationDate": None,
        "ModDate": None,
    }

    with PdfPages(output_path, metadata=metadata) as pdf:
        for page in pages:
            fig = plt.figure(figsize=(8.27, 11.69))
            fig.patch.set_facecolor("white")
            ax = fig.add_axes([0, 0, 1, 1])
            ax.axis("off")

            y = 0.96
            for text, style in page:
                font_size = 10
                weight = "normal"
                family = "DejaVu Sans"
                if style == "title":
                    font_size = 18
                    weight = "bold"
                elif style == "h1":
                    font_size = 15
                    weight = "bold"
                elif style == "h2":
                    font_size = 12
                    weight = "bold"
                elif style == "code":
                    font_size = 8
                    family = "DejaVu Sans Mono"

                ax.text(
                    0.08,
                    y,
                    text,
                    fontsize=font_size,
                    fontweight=weight,
                    fontfamily=family,
                    va="top",
                    wrap=False,
                )
                y -= 0.027 if style == "code" else 0.032
            pdf.savefig(fig, bbox_inches="tight")
            plt.close(fig)

    return output_path


def _layout_lines(markdown: str, title: str | None) -> List[Tuple[str, str]]:
    laid_out: List[Tuple[str, str]] = []
    if title:
        laid_out.append((title.strip(), "title"))
        laid_out.append(("", "body"))

    in_code = False
    for raw in markdown.splitlines():
        line = raw.rstrip()
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            laid_out.extend((chunk, "code") for chunk in _wrap(line, width=92))
            continue
        if not line.strip():
            laid_out.append(("", "body"))
            continue
        if line.startswith("# "):
            laid_out.append((line[2:].strip(), "h1"))
            continue
        if line.startswith("## "):
            laid_out.append((line[3:].strip(), "h2"))
            continue

        indent = "  " if line.lstrip().startswith(("- ", "* ")) else ""
        for idx, chunk in enumerate(_wrap(line, width=92)):
            laid_out.append(((indent if idx else "") + chunk, "body"))
    return laid_out


def _wrap(text: str, width: int) -> List[str]:
    return textwrap.wrap(
        text,
        width=width,
        replace_whitespace=False,
        drop_whitespace=False,
        break_long_words=True,
        break_on_hyphens=False,
    ) or [""]


def _paginate(
    lines: Iterable[Tuple[str, str]],
    lines_per_page: int,
) -> List[List[Tuple[str, str]]]:
    pages: List[List[Tuple[str, str]]] = []
    current: List[Tuple[str, str]] = []
    for line in lines:
        current.append(line)
        if len(current) >= lines_per_page:
            pages.append(current)
            current = []
    if current:
        pages.append(current)
    return pages or [[("No report content.", "body")]]
