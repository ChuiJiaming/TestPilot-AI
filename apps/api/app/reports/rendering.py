"""Deterministic, privacy-bounded HTML, PDF, JSON, and JUnit report rendering."""

from dataclasses import dataclass
from html import escape
from io import BytesIO
import json
import re
from typing import Literal
from xml.etree import ElementTree


ReportFormat = Literal["html", "pdf", "json", "junit"]

_CONTENT_TYPES = {
    "html": "text/html; charset=utf-8",
    "pdf": "application/pdf",
    "json": "application/json",
    "junit": "application/xml",
}
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


@dataclass(frozen=True)
class RenderedReport:
    data: bytes
    content_type: str


class ReportRenderer:
    def render(self, snapshot: dict, format: ReportFormat) -> RenderedReport:
        if format not in _CONTENT_TYPES:
            raise ValueError("unsupported report format")
        if not isinstance(snapshot, dict):
            raise ValueError("report snapshot must be an object")
        if format == "json":
            data = self._json(snapshot)
        elif format == "junit":
            data = self._junit(snapshot)
        elif format == "html":
            data = self._html(snapshot)
        else:
            data = self._pdf(snapshot)
        return RenderedReport(data=data, content_type=_CONTENT_TYPES[format])

    @staticmethod
    def _json(snapshot: dict) -> bytes:
        try:
            return json.dumps(
                snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError("report snapshot is not canonical JSON") from exc

    @classmethod
    def _html(cls, snapshot: dict) -> bytes:
        run = snapshot.get("run") if isinstance(snapshot.get("run"), dict) else {}
        summary = snapshot.get("summary") if isinstance(snapshot.get("summary"), dict) else {}
        counts = summary.get("counts") if isinstance(summary.get("counts"), dict) else {}
        cases = snapshot.get("cases") if isinstance(snapshot.get("cases"), list) else []
        labels = (
            ("Run", run.get("id")), ("Status", run.get("status")),
            ("Branch", run.get("branch")), ("Commit", run.get("commit_sha")),
            ("Environment", run.get("environment_id")),
        )
        metadata = "".join(
            f"<dt>{escape(label)}</dt><dd>{escape(_safe_text(value) or '—')}</dd>"
            for label, value in labels
        )
        count_rows = "".join(
            f"<tr><th>{escape(str(name).replace('_', ' ').title())}</th>"
            f"<td>{escape(_safe_text(value))}</td></tr>"
            for name, value in counts.items()
        )
        case_rows = "".join(
            "<tr>"
            f"<td>{escape(_safe_text(case.get('title')))}</td>"
            f"<td>{escape(_safe_text(case.get('browser')))}</td>"
            f"<td>{escape(_safe_text(case.get('status')))}</td>"
            f"<td>{escape(_safe_text(case.get('duration_seconds')))}</td>"
            "</tr>"
            for case in cases if isinstance(case, dict)
        )
        document = (
            "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<title>TestPilot 测试运行报告</title>"
            "<style>body{font:16px system-ui,sans-serif;margin:2rem;max-width:72rem;color:#152238}"
            "dl{display:grid;grid-template-columns:10rem 1fr;gap:.5rem}dt{font-weight:700}"
            "table{border-collapse:collapse;width:100%;margin:1rem 0}"
            "th,td{border:1px solid #ccd4df;padding:.6rem;text-align:left}</style></head><body>"
            "<h1>测试运行报告</h1><dl>" + metadata + "</dl>"
            "<h2>结果计数</h2><table><tbody>" + count_rows + "</tbody></table>"
            "<h2>用例结果</h2><table><thead><tr><th>用例</th><th>浏览器</th>"
            "<th>状态</th><th>耗时（秒）</th></tr></thead><tbody>" + case_rows
            + "</tbody></table></body></html>"
        )
        return document.encode("utf-8")

    @classmethod
    def _junit(cls, snapshot: dict) -> bytes:
        run = snapshot.get("run") if isinstance(snapshot.get("run"), dict) else {}
        case_rows = snapshot.get("cases") if isinstance(snapshot.get("cases"), list) else []
        cases = [case for case in case_rows if isinstance(case, dict)]
        failures = sum(case.get("status") == "FAILED" for case in cases)
        errors = sum(case.get("status") == "ERROR" for case in cases)
        skipped_statuses = {"SKIPPED", "CANCELLED", "QUEUED", "RUNNING"}
        skipped = sum(case.get("status") in skipped_statuses for case in cases)
        total_time = sum(_number(case.get("duration_seconds")) for case in cases)
        root = ElementTree.Element("testsuites")
        suite = ElementTree.SubElement(root, "testsuite", {
            "name": f"TestPilot Run {_safe_text(run.get('id'))}",
            "tests": str(len(cases)), "failures": str(failures),
            "errors": str(errors), "skipped": str(skipped),
            "time": _decimal(total_time),
        })
        properties = ElementTree.SubElement(suite, "properties")
        for name in ("branch", "commit_sha", "status"):
            value = _safe_text(run.get(name))
            if value:
                ElementTree.SubElement(properties, "property", {"name": name, "value": value})
        summary = snapshot.get("summary") if isinstance(snapshot.get("summary"), dict) else {}
        if summary.get("snapshot_hash"):
            ElementTree.SubElement(properties, "property", {
                "name": "snapshot_hash", "value": _safe_text(summary["snapshot_hash"]),
            })
        for case in cases:
            status = _safe_text(case.get("status")).upper()
            node = ElementTree.SubElement(suite, "testcase", {
                "name": _safe_text(case.get("title")) or "Untitled case",
                "classname": _safe_text(case.get("browser")) or "unknown-browser",
                "time": _decimal(_number(case.get("duration_seconds"))),
            })
            if status == "FAILED":
                ElementTree.SubElement(node, "failure", {
                    "message": _safe_text(_case_error_code(case)) or "FAILED",
                })
            elif status == "ERROR":
                ElementTree.SubElement(node, "error", {
                    "message": _safe_text(_case_error_code(case)) or "ERROR",
                })
            elif status in skipped_statuses or status not in {"PASSED", "FAILED", "ERROR"}:
                ElementTree.SubElement(node, "skipped", {"message": status or "UNKNOWN"})
        return ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)

    @classmethod
    def _pdf(cls, snapshot: dict) -> bytes:
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_CENTER
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import mm
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

        if "STSong-Light" not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
        run = snapshot.get("run") if isinstance(snapshot.get("run"), dict) else {}
        summary = snapshot.get("summary") if isinstance(snapshot.get("summary"), dict) else {}
        counts = summary.get("counts") if isinstance(summary.get("counts"), dict) else {}
        cases = snapshot.get("cases") if isinstance(snapshot.get("cases"), list) else []
        styles = getSampleStyleSheet()
        styles.add(ParagraphStyle(
            name="ReportTitleZh", parent=styles["Title"], fontName="STSong-Light",
            alignment=TA_CENTER, fontSize=18, leading=24,
        ))
        styles.add(ParagraphStyle(
            name="ReportBodyZh", parent=styles["BodyText"], fontName="STSong-Light",
            fontSize=9, leading=13, wordWrap="CJK",
        ))
        styles.add(ParagraphStyle(
            name="ReportHeadingZh", parent=styles["Heading2"], fontName="STSong-Light",
            fontSize=13, leading=18, spaceBefore=8,
        ))
        buffer = BytesIO()
        document = SimpleDocTemplate(
            buffer, pagesize=A4, title="TestPilot 测试运行报告",
            leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=16 * mm,
        )
        story = [Paragraph("测试运行报告", styles["ReportTitleZh"]), Spacer(1, 5 * mm)]
        metadata = [
            ["Run", run.get("id")], ["状态", run.get("status")],
            ["分支", run.get("branch")], ["Commit", run.get("commit_sha")],
            ["环境", run.get("environment_id")],
        ]
        story.append(_pdf_table(metadata, styles["ReportBodyZh"], colors))
        story.append(Paragraph("结果计数", styles["ReportHeadingZh"]))
        story.append(_pdf_table(
            [[str(key).replace("_", " "), _safe_text(value)] for key, value in counts.items()],
            styles["ReportBodyZh"], colors,
        ))
        story.append(Paragraph("用例结果", styles["ReportHeadingZh"]))
        case_table = [["用例", "浏览器", "状态", "耗时（秒）"]]
        case_table.extend([
            [case.get("title"), case.get("browser"), case.get("status"),
             case.get("duration_seconds")]
            for case in cases if isinstance(case, dict)
        ])
        story.append(_pdf_table(case_table, styles["ReportBodyZh"], colors, header=True))
        document.build(story)
        return buffer.getvalue()


def _pdf_table(rows, body_style, colors, *, header=False):
    from reportlab.platypus import Paragraph, Table, TableStyle

    safe_rows = [[Paragraph(escape(_safe_text(value) or "—"), body_style) for value in row]
                 for row in rows]
    table = Table(safe_rows, repeatRows=1 if header else 0, hAlign="LEFT")
    commands = [
        ("FONTNAME", (0, 0), (-1, -1), "STSong-Light"),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#CBD5E1")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    if header and rows:
        commands.extend([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EAF0F7")),
            ("FONTNAME", (0, 0), (-1, 0), "STSong-Light"),
        ])
    table.setStyle(TableStyle(commands))
    return table


def _safe_text(value) -> str:
    if value is None:
        return ""
    text = str(value)
    text = _CONTROL_CHARACTERS.sub("", text)
    return "".join(character for character in text if not 0xD800 <= ord(character) <= 0xDFFF)


def _number(value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, number) if number == number else 0.0


def _decimal(value: float) -> str:
    return f"{value:.3f}"


def _case_error_code(case: dict) -> str | None:
    steps = case.get("steps") if isinstance(case.get("steps"), list) else []
    for step in steps:
        if isinstance(step, dict) and isinstance(step.get("error_code"), str):
            return step["error_code"]
    return None
