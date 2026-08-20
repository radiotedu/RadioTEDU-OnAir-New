from __future__ import annotations

import io
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence

from PIL import Image
from pypdf import PdfReader
from reportlab.lib.colors import Color, HexColor, white
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas


ROOT = Path(__file__).resolve().parents[2]
ASSET_DIR = ROOT / "docs" / "manuals" / "assets" / "internal"
OUTPUT_DIR = ROOT / "output" / "pdf"
W, H = A4


INK = HexColor("#152033")
MUTED = HexColor("#5D6B82")
PALE = HexColor("#F4F7FB")
LINE = HexColor("#DCE3ED")
BLUE = HexColor("#2764E7")
BLUE_DARK = HexColor("#173B8F")
CYAN = HexColor("#2FB7C9")
GREEN = HexColor("#18A66A")
AMBER = HexColor("#E89B2C")
RED = HexColor("#E64151")
PURPLE = HexColor("#7257D8")
NIGHT = HexColor("#0E1420")
NIGHT_2 = HexColor("#151E2D")
NIGHT_3 = HexColor("#202B3E")
LIGHT_TEXT = HexColor("#DDE6F3")


@dataclass(frozen=True)
class ManualSpec:
    title: str
    subtitle: str
    edition: str
    document_id: str
    internal: bool
    accent: Color


WORKER = ManualSpec(
    title="RadioTEDU OnAir",
    subtitle="Worker Operations Manual",
    edition="Internal operator edition | Version 1.0",
    document_id="RT-ONAIR-OPS-001",
    internal=True,
    accent=RED,
)

PUBLIC = ManualSpec(
    title="Deterministic Broadcast Console",
    subtitle="Operator User Manual",
    edition="Brand-neutral edition | Version 1.0",
    document_id="DBC-USER-001",
    internal=False,
    accent=BLUE,
)


def rgb_tuple(color: Color) -> tuple[float, float, float]:
    return color.red, color.green, color.blue


def tint(color: Color, amount: float) -> Color:
    r, g, b = rgb_tuple(color)
    return Color(
        r + (1 - r) * amount,
        g + (1 - g) * amount,
        b + (1 - b) * amount,
    )


def shade(color: Color, amount: float) -> Color:
    r, g, b = rgb_tuple(color)
    return Color(r * (1 - amount), g * (1 - amount), b * (1 - amount))


def wrap_lines(text: str, font: str, size: float, width: float) -> list[str]:
    output: list[str] = []
    for paragraph in text.split("\n"):
        words = paragraph.split()
        if not words:
            output.append("")
            continue
        line = words[0]
        for word in words[1:]:
            candidate = f"{line} {word}"
            if stringWidth(candidate, font, size) <= width:
                line = candidate
            else:
                output.append(line)
                line = word
        output.append(line)
    return output


def draw_text(
    c: canvas.Canvas,
    text: str,
    x: float,
    y: float,
    width: float,
    *,
    font: str = "Helvetica",
    size: float = 9.2,
    leading: float | None = None,
    color: Color = INK,
    max_lines: int | None = None,
) -> float:
    leading = leading or size * 1.42
    lines = wrap_lines(text, font, size, width)
    if max_lines is not None:
        lines = lines[:max_lines]
    c.setFillColor(color)
    c.setFont(font, size)
    cursor = y
    for line in lines:
        c.drawString(x, cursor, line)
        cursor -= leading
    return cursor


def draw_bullets(
    c: canvas.Canvas,
    items: Sequence[str],
    x: float,
    y: float,
    width: float,
    *,
    color: Color = INK,
    bullet_color: Color = BLUE,
    size: float = 9.0,
    gap: float = 8,
) -> float:
    cursor = y
    for item in items:
        lines = wrap_lines(item, "Helvetica", size, width - 20)
        c.setFillColor(bullet_color)
        c.circle(x + 3, cursor + 3, 2.2, stroke=0, fill=1)
        c.setFillColor(color)
        c.setFont("Helvetica", size)
        for line in lines:
            c.drawString(x + 15, cursor, line)
            cursor -= size * 1.4
        cursor -= gap
    return cursor


def top_label(c: canvas.Canvas, text: str, x: float, y: float, color: Color) -> None:
    c.setFillColor(color)
    c.setFont("Helvetica-Bold", 7.4)
    c.drawString(x, y, text.upper())


def page_title(
    c: canvas.Canvas,
    kicker: str,
    title: str,
    intro: str,
    spec: ManualSpec,
) -> float:
    top_label(c, kicker, 42, H - 78, spec.accent)
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 25)
    c.drawString(42, H - 108, title)
    return draw_text(c, intro, 42, H - 132, W - 84, size=9.5, color=MUTED, leading=13.5)


def draw_header_footer(
    c: canvas.Canvas,
    spec: ManualSpec,
    page_no: int,
    section: str,
) -> None:
    c.setStrokeColor(LINE)
    c.setLineWidth(0.6)
    c.line(42, H - 42, W - 42, H - 42)
    c.setFillColor(MUTED)
    c.setFont("Helvetica-Bold", 7)
    c.drawString(42, H - 31, spec.title.upper())
    c.setFont("Helvetica", 7)
    c.drawRightString(W - 42, H - 31, section.upper())
    c.line(42, 34, W - 42, 34)
    c.setFillColor(MUTED)
    c.setFont("Helvetica", 7)
    c.drawString(42, 21, f"{spec.document_id} | {spec.edition}")
    c.drawRightString(W - 42, 21, f"{page_no:02d}")


def new_page(c: canvas.Canvas, spec: ManualSpec, page_no: int, section: str) -> None:
    if page_no > 1:
        c.showPage()
    c.setTitle(f"{spec.title} - {spec.subtitle}")
    c.setAuthor("Broadcast Operations Documentation" if not spec.internal else "RadioTEDU")
    c.setSubject(spec.subtitle)
    c.bookmarkPage(f"page-{page_no}")
    draw_header_footer(c, spec, page_no, section)


def rounded_card(
    c: canvas.Canvas,
    x: float,
    y: float,
    w: float,
    h: float,
    *,
    fill: Color = white,
    stroke: Color = LINE,
    radius: float = 10,
) -> None:
    c.setFillColor(fill)
    c.setStrokeColor(stroke)
    c.setLineWidth(0.7)
    c.roundRect(x, y, w, h, radius, stroke=1, fill=1)


def metric_card(
    c: canvas.Canvas,
    x: float,
    y: float,
    w: float,
    h: float,
    number: str,
    label: str,
    accent: Color,
) -> None:
    rounded_card(c, x, y, w, h, fill=PALE, stroke=tint(accent, 0.68))
    c.setFillColor(accent)
    c.setFont("Helvetica-Bold", 20)
    c.drawString(x + 14, y + h - 26, number)
    draw_text(c, label, x + 14, y + 15, w - 28, size=7.8, color=MUTED, leading=10)


def callout(
    c: canvas.Canvas,
    x: float,
    y: float,
    w: float,
    h: float,
    title: str,
    body: str,
    *,
    accent: Color = BLUE,
    kind: str = "NOTE",
) -> None:
    rounded_card(c, x, y, w, h, fill=tint(accent, 0.93), stroke=tint(accent, 0.65))
    pill_w = max(43, stringWidth(kind, "Helvetica-Bold", 6.8) + 18)
    c.setFillColor(accent)
    c.roundRect(x + 12, y + h - 29, pill_w, 17, 8.5, stroke=0, fill=1)
    c.setFillColor(white)
    c.setFont("Helvetica-Bold", 6.8)
    c.drawCentredString(x + 12 + pill_w / 2, y + h - 23.2, kind)
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 9.8)
    c.drawString(x + 22 + pill_w, y + h - 24, title)
    draw_text(c, body, x + 14, y + h - 47, w - 28, size=8.3, color=INK, leading=11.5)


def step_row(
    c: canvas.Canvas,
    y: float,
    number: int,
    title: str,
    body: str,
    spec: ManualSpec,
    *,
    x: float = 48,
    width: float = W - 96,
) -> float:
    c.setFillColor(spec.accent)
    c.circle(x + 13, y - 4, 13, stroke=0, fill=1)
    c.setFillColor(white)
    c.setFont("Helvetica-Bold", 9)
    c.drawCentredString(x + 13, y - 7, str(number))
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 10.5)
    c.drawString(x + 38, y, title)
    end = draw_text(c, body, x + 38, y - 16, width - 40, size=8.5, color=MUTED, leading=11.3)
    c.setStrokeColor(LINE)
    c.line(x + 38, end - 3, x + width, end - 3)
    return end - 20


def draw_arrow(
    c: canvas.Canvas,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    color: Color,
    width: float = 1.8,
) -> None:
    import math

    c.setStrokeColor(color)
    c.setFillColor(color)
    c.setLineWidth(width)
    c.line(x1, y1, x2, y2)
    angle = math.atan2(y2 - y1, x2 - x1)
    head = 7
    a1 = angle + 2.55
    a2 = angle - 2.55
    p1 = (x2 + head * math.cos(a1), y2 + head * math.sin(a1))
    p2 = (x2 + head * math.cos(a2), y2 + head * math.sin(a2))
    path = c.beginPath()
    path.moveTo(x2, y2)
    path.lineTo(*p1)
    path.lineTo(*p2)
    path.close()
    c.drawPath(path, stroke=0, fill=1)


def draw_marker(
    c: canvas.Canvas,
    number: int,
    target_x: float,
    target_y: float,
    accent: Color,
    *,
    dx: float = 35,
    dy: float = 28,
) -> None:
    cx, cy = target_x + dx, target_y + dy
    draw_arrow(c, cx, cy, target_x, target_y, accent)
    c.setFillColor(white)
    c.setStrokeColor(accent)
    c.setLineWidth(2)
    c.circle(cx, cy, 9.5, stroke=1, fill=1)
    c.setFillColor(accent)
    c.setFont("Helvetica-Bold", 8)
    c.drawCentredString(cx, cy - 2.8, str(number))


def screenshot_image(path: Path, crop_browser: bool = True) -> ImageReader:
    image = Image.open(path).convert("RGB")
    if crop_browser and image.height >= 1000:
        image = image.crop((0, 88, image.width, image.height - 40))
    data = io.BytesIO()
    image.save(data, format="PNG", optimize=True)
    data.seek(0)
    return ImageReader(data)


def annotated_screenshot(
    c: canvas.Canvas,
    path: Path,
    y: float,
    height: float,
    markers: Sequence[tuple[int, float, float, float, float]],
    legend: Sequence[tuple[int, str]],
    spec: ManualSpec,
    *,
    crop_browser: bool = True,
) -> float:
    x = 42
    w = W - 84
    image = screenshot_image(path, crop_browser=crop_browser)
    c.setFillColor(NIGHT)
    c.roundRect(x - 3, y - 3, w + 6, height + 6, 7, stroke=0, fill=1)
    c.drawImage(image, x, y, width=w, height=height, preserveAspectRatio=False, mask="auto")
    for number, nx, ny, dx, dy in markers:
        tx = x + nx * w
        ty = y + ny * height
        draw_marker(c, number, tx, ty, spec.accent, dx=dx, dy=dy)

    legend_y = y - 22
    col_w = (w - 18) / 2
    for index, (number, text) in enumerate(legend):
        col = index % 2
        row = index // 2
        lx = x + col * (col_w + 18)
        ly = legend_y - row * 34
        c.setFillColor(spec.accent)
        c.circle(lx + 8, ly + 2, 7, stroke=0, fill=1)
        c.setFillColor(white)
        c.setFont("Helvetica-Bold", 7)
        c.drawCentredString(lx + 8, ly - 0.6, str(number))
        draw_text(c, text, lx + 20, ly + 7, col_w - 20, size=7.6, color=INK, leading=10)
    rows = (len(legend) + 1) // 2
    return legend_y - rows * 34


def draw_flow(
    c: canvas.Canvas,
    items: Sequence[tuple[str, str, Color]],
    x: float,
    y: float,
    width: float,
) -> None:
    gap = 12
    box_w = (width - gap * (len(items) - 1)) / len(items)
    for i, (title, body, color) in enumerate(items):
        bx = x + i * (box_w + gap)
        rounded_card(c, bx, y, box_w, 92, fill=tint(color, 0.94), stroke=tint(color, 0.62))
        c.setFillColor(color)
        c.circle(bx + 18, y + 70, 7, stroke=0, fill=1)
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 9.2)
        c.drawString(bx + 31, y + 67, title)
        draw_text(c, body, bx + 14, y + 49, box_w - 28, size=7.6, color=MUTED, leading=10)
        if i < len(items) - 1:
            draw_arrow(c, bx + box_w + 2, y + 46, bx + box_w + gap - 2, y + 46, MUTED, width=1.2)


def draw_status_dot(c: canvas.Canvas, x: float, y: float, label: str, color: Color) -> None:
    c.setFillColor(color)
    c.circle(x, y, 4, stroke=0, fill=1)
    c.setFillColor(LIGHT_TEXT)
    c.setFont("Helvetica-Bold", 6.7)
    c.drawString(x + 8, y - 2.4, label)


def neutral_console(
    c: canvas.Canvas,
    variant: str,
    x: float,
    y: float,
    w: float,
    h: float,
    markers: Sequence[tuple[int, float, float, float, float]] = (),
) -> None:
    c.setFillColor(NIGHT)
    c.roundRect(x, y, w, h, 8, stroke=0, fill=1)
    sidebar = w * 0.18
    top = h * 0.13
    c.setFillColor(HexColor("#0A0F18"))
    c.rect(x, y, sidebar, h, stroke=0, fill=1)
    c.setFillColor(HexColor("#0C121D"))
    c.rect(x + sidebar, y + h - top, w - sidebar, top, stroke=0, fill=1)
    c.setFillColor(BLUE)
    c.circle(x + 20, y + h - 22, 8, stroke=0, fill=1)
    c.setFillColor(white)
    c.setFont("Helvetica-Bold", 7.5)
    c.drawString(x + 34, y + h - 25, "BROADCAST CONSOLE")
    draw_status_dot(c, x + w - 96, y + h - 22, "SYSTEM READY", GREEN)

    nav = ["On Air", "Media", "Automation", "Priority Audio", "Settings", "Health"]
    active_map = {
        "onair": 0,
        "media": 1,
        "automation": 2,
        "emergency": 3,
        "settings": 4,
        "health": 5,
    }
    active = active_map.get(variant, 0)
    for i, item in enumerate(nav):
        ny = y + h - top - 27 - i * 29
        if i == active:
            c.setFillColor(HexColor("#182946"))
            c.roundRect(x + 8, ny - 9, sidebar - 16, 22, 5, stroke=0, fill=1)
        c.setFillColor(white if i == active else HexColor("#9BA9BE"))
        c.setFont("Helvetica-Bold" if i == active else "Helvetica", 6.6)
        c.drawString(x + 18, ny, item)

    content_x = x + sidebar + 16
    content_w = w - sidebar - 32
    content_top = y + h - top - 18
    title_map = {
        "onair": "On Air",
        "media": "Media Library",
        "automation": "Automation Rules",
        "emergency": "Priority External Audio",
        "settings": "Output Settings",
        "health": "System Health",
    }
    c.setFillColor(white)
    c.setFont("Helvetica-Bold", 13)
    c.drawString(content_x, content_top, title_map.get(variant, "Console"))
    c.setFillColor(HexColor("#9BA9BE"))
    c.setFont("Helvetica", 6.4)
    c.drawString(content_x, content_top - 13, "Operator-owned controls with explicit verification")

    card_y = y + 26
    card_h = h - top - 76
    c.setFillColor(NIGHT_2)
    c.setStrokeColor(NIGHT_3)
    c.roundRect(content_x, card_y, content_w, card_h, 7, stroke=1, fill=1)

    if variant == "onair":
        draw_status_dot(c, content_x + 18, card_y + card_h - 24, "ON AIR", GREEN)
        c.setFillColor(white)
        c.setFont("Helvetica-Bold", 11)
        c.drawString(content_x + 18, card_y + card_h - 49, "Current track: Example Song")
        c.setFillColor(HexColor("#A8B5C8"))
        c.setFont("Helvetica", 7)
        c.drawString(content_x + 18, card_y + card_h - 65, "Queue and timing remain visible during playout")
        by = card_y + 22
        bw = (content_w - 48) / 2
        c.setFillColor(GREEN)
        c.roundRect(content_x + 16, by, bw, 25, 5, stroke=0, fill=1)
        c.setFillColor(white)
        c.setFont("Helvetica-Bold", 7)
        c.drawCentredString(content_x + 16 + bw / 2, by + 9, "START / RESUME")
        c.setFillColor(RED)
        c.roundRect(content_x + 32 + bw, by, bw, 25, 5, stroke=0, fill=1)
        c.setFillColor(white)
        c.drawCentredString(content_x + 32 + bw + bw / 2, by + 9, "STOP - KEEP QUEUE")
    elif variant == "media":
        c.setFillColor(white)
        c.setFont("Helvetica-Bold", 8.5)
        c.drawString(content_x + 16, card_y + card_h - 24, "Managed folder")
        c.setFillColor(HexColor("#0B1421"))
        c.roundRect(content_x + 16, card_y + card_h - 52, content_w - 108, 20, 4, stroke=0, fill=1)
        c.setFillColor(HexColor("#A8B5C8"))
        c.setFont("Helvetica", 6.5)
        c.drawString(content_x + 24, card_y + card_h - 45, "D:\\Audio\\Program")
        c.setFillColor(BLUE)
        c.roundRect(content_x + content_w - 82, card_y + card_h - 52, 66, 20, 4, stroke=0, fill=1)
        c.setFillColor(white)
        c.setFont("Helvetica-Bold", 6.4)
        c.drawCentredString(content_x + content_w - 49, card_y + card_h - 45, "BROWSE")
        for i, name in enumerate(["Morning Theme", "Example Song", "Evening Bed"]):
            yy = card_y + card_h - 82 - i * 28
            c.setFillColor(HexColor("#0B1421"))
            c.roundRect(content_x + 16, yy, content_w - 32, 22, 4, stroke=0, fill=1)
            c.setFillColor(LIGHT_TEXT)
            c.setFont("Helvetica", 6.6)
            c.drawString(content_x + 24, yy + 8, name)
            c.setFillColor(GREEN)
            c.drawRightString(content_x + content_w - 24, yy + 8, "+ QUEUE")
    elif variant == "automation":
        c.setFillColor(white)
        c.setFont("Helvetica-Bold", 8.5)
        c.drawString(content_x + 16, card_y + card_h - 25, "Automatic inserts")
        c.setFillColor(GREEN)
        c.roundRect(content_x + content_w - 56, card_y + card_h - 33, 39, 17, 8, stroke=0, fill=1)
        c.setFillColor(white)
        c.setFont("Helvetica-Bold", 6)
        c.drawCentredString(content_x + content_w - 36.5, card_y + card_h - 27, "ON")
        labels = [("Songs between inserts", "2"), ("Selection order", "Rotation")]
        for i, (label, value) in enumerate(labels):
            yy = card_y + card_h - 70 - i * 43
            c.setFillColor(HexColor("#A8B5C8"))
            c.setFont("Helvetica", 6.4)
            c.drawString(content_x + 16, yy + 18, label)
            c.setFillColor(HexColor("#0B1421"))
            c.roundRect(content_x + 16, yy - 2, content_w - 32, 18, 4, stroke=0, fill=1)
            c.setFillColor(white)
            c.drawString(content_x + 24, yy + 4, value)
        c.setFillColor(BLUE)
        c.roundRect(content_x + 16, card_y + 15, content_w - 32, 22, 4, stroke=0, fill=1)
        c.setFillColor(white)
        c.setFont("Helvetica-Bold", 6.7)
        c.drawCentredString(content_x + content_w / 2, card_y + 23, "SAVE AND VERIFY RULE")
    elif variant == "emergency":
        c.setFillColor(RED)
        c.roundRect(content_x + content_w - 56, card_y + card_h - 34, 39, 17, 8, stroke=0, fill=1)
        c.setFillColor(white)
        c.setFont("Helvetica-Bold", 6)
        c.drawCentredString(content_x + content_w - 36.5, card_y + card_h - 28, "OFF")
        c.setFillColor(white)
        c.setFont("Helvetica-Bold", 8.5)
        c.drawString(content_x + 16, card_y + card_h - 25, "Approved source")
        c.setFillColor(HexColor("#0B1421"))
        c.roundRect(content_x + 16, card_y + card_h - 60, content_w - 32, 21, 4, stroke=0, fill=1)
        c.setFillColor(HexColor("#A8B5C8"))
        c.setFont("Helvetica", 6.4)
        c.drawString(content_x + 24, card_y + card_h - 52, "https://public-service.example/live")
        c.setFillColor(AMBER)
        c.roundRect(content_x + 16, card_y + 51, content_w - 32, 27, 4, stroke=0, fill=1)
        c.setFillColor(NIGHT)
        c.setFont("Helvetica-Bold", 6.7)
        c.drawCentredString(content_x + content_w / 2, card_y + 61, "PREVIEW AND CONFIRM SHARED AUDIO")
        c.setFillColor(RED)
        c.roundRect(content_x + 16, card_y + 17, content_w - 32, 25, 4, stroke=0, fill=1)
        c.setFillColor(white)
        c.drawCentredString(content_x + content_w / 2, card_y + 26, "ARM PRIORITY TAKEOVER")
    elif variant == "settings":
        fields = [("Station label", "Primary Program"), ("Output gain", "0 dB"), ("Stream output", "Enabled")]
        for i, (label, value) in enumerate(fields):
            yy = card_y + card_h - 34 - i * 40
            c.setFillColor(HexColor("#A8B5C8"))
            c.setFont("Helvetica", 6.2)
            c.drawString(content_x + 16, yy + 14, label)
            c.setFillColor(HexColor("#0B1421"))
            c.roundRect(content_x + 16, yy - 4, content_w - 32, 18, 4, stroke=0, fill=1)
            c.setFillColor(white)
            c.drawString(content_x + 24, yy + 2, value)
        c.setFillColor(BLUE)
        c.roundRect(content_x + 16, card_y + 14, content_w - 32, 22, 4, stroke=0, fill=1)
        c.setFillColor(white)
        c.setFont("Helvetica-Bold", 6.5)
        c.drawCentredString(content_x + content_w / 2, card_y + 22, "SAVE, APPLY, AND VERIFY")
    elif variant == "health":
        checks = [
            ("Application service", GREEN),
            ("Media tools", GREEN),
            ("Station profile", GREEN),
            ("Local monitor", AMBER),
            ("Stream destination", GREEN),
        ]
        for i, (label, color) in enumerate(checks):
            yy = card_y + card_h - 28 - i * 28
            c.setFillColor(HexColor("#0B1421"))
            c.roundRect(content_x + 14, yy - 7, content_w - 28, 21, 4, stroke=0, fill=1)
            c.setFillColor(color)
            c.circle(content_x + 25, yy + 3, 3, stroke=0, fill=1)
            c.setFillColor(LIGHT_TEXT)
            c.setFont("Helvetica-Bold", 6.5)
            c.drawString(content_x + 35, yy + 1, label)

    for number, nx, ny, dx, dy in markers:
        draw_marker(c, number, x + nx * w, y + ny * h, BLUE, dx=dx, dy=dy)


def neutral_figure_with_legend(
    c: canvas.Canvas,
    variant: str,
    y: float,
    markers: Sequence[tuple[int, float, float, float, float]],
    legend: Sequence[tuple[int, str]],
) -> float:
    x, w, h = 42, W - 84, 286
    neutral_console(c, variant, x, y, w, h, markers)
    legend_y = y - 22
    col_w = (w - 18) / 2
    for index, (number, text) in enumerate(legend):
        col = index % 2
        row = index // 2
        lx = x + col * (col_w + 18)
        ly = legend_y - row * 34
        c.setFillColor(BLUE)
        c.circle(lx + 8, ly + 2, 7, stroke=0, fill=1)
        c.setFillColor(white)
        c.setFont("Helvetica-Bold", 7)
        c.drawCentredString(lx + 8, ly - 0.6, str(number))
        draw_text(c, text, lx + 20, ly + 7, col_w - 20, size=7.6, color=INK, leading=10)
    return legend_y - ((len(legend) + 1) // 2) * 34


def cover(c: canvas.Canvas, spec: ManualSpec) -> None:
    c.setFillColor(NIGHT)
    c.rect(0, 0, W, H, stroke=0, fill=1)
    c.setFillColor(shade(spec.accent, 0.18))
    c.circle(W - 30, H - 100, 190, stroke=0, fill=1)
    c.setFillColor(tint(spec.accent, 0.25))
    c.circle(W - 88, H - 148, 112, stroke=0, fill=1)
    c.setStrokeColor(tint(spec.accent, 0.5))
    c.setLineWidth(2)
    for offset in range(0, 110, 18):
        c.line(42, H - 205 - offset, 42 + 80 + offset * 1.3, H - 205 - offset)

    if spec.internal:
        logo = ASSET_DIR / "radiotedu-onair-logo.png"
        if logo.exists():
            c.drawImage(str(logo), 42, H - 125, width=132, height=58, preserveAspectRatio=True, mask="auto")
        c.setFillColor(RED)
        c.roundRect(42, H - 162, 96, 20, 10, stroke=0, fill=1)
        c.setFillColor(white)
        c.setFont("Helvetica-Bold", 7.5)
        c.drawCentredString(90, H - 155, "INTERNAL USE")
    else:
        c.setFillColor(spec.accent)
        c.roundRect(42, H - 112, 58, 58, 12, stroke=0, fill=1)
        c.setStrokeColor(white)
        c.setLineWidth(2)
        c.circle(71, H - 83, 13, stroke=1, fill=0)
        c.line(71, H - 96, 71, H - 70)
        c.line(58, H - 83, 84, H - 83)
        c.setFillColor(white)
        c.setFont("Helvetica-Bold", 7.5)
        c.drawString(114, H - 80, "OPERATOR DOCUMENTATION")
        c.setFont("Helvetica", 7.2)
        c.setFillColor(LIGHT_TEXT)
        c.drawString(114, H - 96, "Brand-neutral reference")

    c.setFillColor(white)
    c.setFont("Helvetica-Bold", 29)
    c.drawString(42, H - 330, spec.title)
    c.setFillColor(tint(spec.accent, 0.2))
    c.setFont("Helvetica-Bold", 22)
    c.drawString(42, H - 362, spec.subtitle)
    c.setFillColor(LIGHT_TEXT)
    c.setFont("Helvetica", 10)
    c.drawString(42, H - 391, spec.edition)
    draw_text(
        c,
        "A visual, task-focused guide for safe mouse-driven broadcast operation.",
        42,
        H - 445,
        350,
        size=13,
        color=white,
        leading=18,
    )
    c.setFillColor(spec.accent)
    c.roundRect(42, 79, 176, 33, 16.5, stroke=0, fill=1)
    c.setFillColor(white)
    c.setFont("Helvetica-Bold", 8)
    c.drawCentredString(130, 91, "CONTROL. VERIFY. BROADCAST.")
    c.setFillColor(HexColor("#8997AC"))
    c.setFont("Helvetica", 7)
    c.drawString(42, 50, f"{spec.document_id} | JULY 2026")


def principles_page(c: canvas.Canvas, spec: ManualSpec, page_no: int) -> None:
    new_page(c, spec, page_no, "Read this first")
    page_title(
        c,
        "Purpose and safety",
        "Designed for operator authority",
        "The console is deterministic: the person at the workstation decides when a stream starts, stops, changes source, or changes automation.",
        spec,
    )
    cards = [
        ("01", "Explicit actions", "Broadcast state changes require a visible mouse action and a verified result."),
        ("02", "Queue continuity", "A normal stop preserves order. Resume restarts the interrupted item from the beginning."),
        ("03", "Scoped settings", "Media, automation, output, and optional services are configured per station profile."),
    ]
    y = H - 310
    for i, (num, title, body) in enumerate(cards):
        x = 42 + i * 172
        rounded_card(c, x, y, 158, 132, fill=white, stroke=tint(spec.accent, 0.65))
        c.setFillColor(spec.accent)
        c.setFont("Helvetica-Bold", 19)
        c.drawString(x + 14, y + 96, num)
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 10)
        c.drawString(x + 14, y + 73, title)
        draw_text(c, body, x + 14, y + 55, 130, size=7.8, color=MUTED, leading=10.5)

    callout(
        c,
        42,
        H - 490,
        W - 84,
        110,
        "The three checks before any state change",
        "Confirm the selected station profile, confirm the visible current status, and confirm the action result. Never infer success from a button press alone.",
        accent=AMBER,
        kind="SAFETY",
    )

    draw_flow(
        c,
        [
            ("SELECT", "Choose the intended station profile.", BLUE),
            ("ACT", "Click the required control.", spec.accent),
            ("VERIFY", "Read the returned status and timeline.", GREEN),
        ],
        42,
        135,
        W - 84,
    )


def toc_page(c: canvas.Canvas, spec: ManualSpec, page_no: int, entries: Sequence[tuple[str, int]]) -> None:
    new_page(c, spec, page_no, "Contents")
    page_title(
        c,
        "Navigation",
        "Contents",
        "Use this manual during onboarding, live operation, and incident recovery.",
        spec,
    )
    y = H - 190
    for title, page in entries:
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 9.2)
        c.drawString(48, y, title)
        dot_start = 48 + stringWidth(title, "Helvetica-Bold", 9.2) + 8
        c.setStrokeColor(LINE)
        c.setDash(1, 3)
        c.line(dot_start, y + 2, W - 72, y + 2)
        c.setDash()
        c.setFillColor(spec.accent)
        c.setFont("Helvetica-Bold", 9.2)
        c.drawRightString(W - 48, y, f"{page:02d}")
        y -= 35
    callout(
        c,
        42,
        96,
        W - 84,
        86,
        "Reading pattern",
        "Every task page follows the same sequence: goal, annotated interface, exact mouse actions, and a visible verification point.",
        accent=spec.accent,
        kind="GUIDE",
    )


def interface_tour_worker(c: canvas.Canvas, page_no: int) -> None:
    spec = WORKER
    new_page(c, spec, page_no, "Interface tour")
    page_title(
        c,
        "Orientation",
        "The operator wall at a glance",
        "Navigation stays on the left, station scope stays at the top, and the active workspace occupies the center.",
        spec,
    )
    annotated_screenshot(
        c,
        ASSET_DIR / "onair-stop-confirmation.png",
        350,
        294,
        [
            (1, 0.06, 0.72, 35, 20),
            (2, 0.70, 0.93, -10, -35),
            (3, 0.85, 0.58, -45, 25),
            (4, 0.75, 0.30, 20, 35),
        ],
        [
            (1, "Workspace menu: On Air, Media, Automation, Emergency, Services, Settings, and Diagnostics."),
            (2, "Controlling Station: every action applies to the selected profile."),
            (3, "Live state: read ON AIR or OFF AIR before acting."),
            (4, "Primary action controls use clear colors and verified labels."),
        ],
        spec,
    )
    callout(
        c,
        42,
        82,
        W - 84,
        82,
        "Selection rule",
        "If the station name at the top is not the intended target, stop and select the correct profile before changing anything.",
        accent=AMBER,
        kind="RULE",
    )


def first_shift_page(c: canvas.Canvas, spec: ManualSpec, page_no: int, internal: bool) -> None:
    new_page(c, spec, page_no, "Quick start")
    page_title(
        c,
        "Start of shift",
        "Five-minute readiness routine",
        "Complete this sequence at the beginning of every operating period and after any restart.",
        spec,
    )
    steps = [
        ("Sign in locally", "Use your assigned operator account. Do not share passwords or leave a signed-in console unattended."),
        ("Select the station", "Read the active station label at the top of the screen before touching broadcast controls."),
        ("Open Health or Diagnostics", "Confirm the application, media tools, station profile, monitor, and stream destination."),
        ("Review Media and Automation", "Confirm the managed folder, pending queue, insert interval, and selection order."),
        ("Return to On Air", "Read the current state. Start or resume only when the scheduled program should be live."),
    ]
    y = H - 190
    for idx, (title, body) in enumerate(steps, 1):
        y = step_row(c, y, idx, title, body, spec)
    callout(
        c,
        42,
        86,
        W - 84,
        92,
        "A green connection badge is necessary, not sufficient",
        "The backend may be reachable while a station output or local monitor still needs attention. The readiness page is the authoritative checklist.",
        accent=AMBER,
        kind="CHECK",
    )


def onair_worker(c: canvas.Canvas, page_no: int) -> None:
    spec = WORKER
    new_page(c, spec, page_no, "On Air")
    page_title(
        c,
        "Core operation",
        "Start, stop, and preserve the playlist",
        "The stop control is deliberately two-step. It stops scheduler and outputs without clearing, advancing, or reordering the queue.",
        spec,
    )
    annotated_screenshot(
        c,
        ASSET_DIR / "onair-stop-confirmation.png",
        352,
        292,
        [
            (1, 0.84, 0.78, -35, 20),
            (2, 0.55, 0.57, -35, 22),
            (3, 0.78, 0.36, 15, 35),
            (4, 0.28, 0.36, 20, 35),
        ],
        [
            (1, "Confirm the visible live state before clicking."),
            (2, "Engine and scheduler must agree with the live badge."),
            (3, "First Stop press arms the action; second press confirms within 20 seconds."),
            (4, "Start / resume restarts the interrupted item from its beginning."),
        ],
        spec,
    )
    callout(
        c,
        42,
        76,
        W - 84,
        92,
        "Verification after Stop",
        "Look for OFF AIR, Engine: Stopped, Scheduler: Stopped, and an unchanged queue. If any field disagrees, open Diagnostics before proceeding.",
        accent=GREEN,
        kind="VERIFY",
    )


def media_worker(c: canvas.Canvas, page_no: int) -> None:
    spec = WORKER
    new_page(c, spec, page_no, "Media")
    page_title(
        c,
        "Library and queue",
        "Assign a managed music folder",
        "Each station should use its own exact media folder so autoplay and the pending queue cannot drift into another format.",
        spec,
    )
    annotated_screenshot(
        c,
        ASSET_DIR / "media-library.png",
        350,
        294,
        [
            (1, 0.52, 0.83, 20, -35),
            (2, 0.92, 0.52, -35, 24),
            (3, 0.47, 0.32, 25, 30),
            (4, 0.73, 0.18, 25, 24),
        ],
        [
            (1, "Station selector: confirm the target profile first."),
            (2, "Browse folders opens the native Windows picker."),
            (3, "Exact replacement keeps active music inside the chosen folder."),
            (4, "Sync and verify scans audio, validates files, and refreshes the queue."),
        ],
        spec,
    )
    callout(
        c,
        42,
        72,
        W - 84,
        96,
        "Recommended folder practice",
        "Use one folder per station and enable subfolders only when their contents share the same format. Unreadable files should be corrected or explicitly skipped and reviewed.",
        accent=BLUE,
        kind="PRACTICE",
    )


def queue_worker(c: canvas.Canvas, page_no: int) -> None:
    spec = WORKER
    new_page(c, spec, page_no, "Media")
    page_title(
        c,
        "Playout control",
        "Build and verify the queue",
        "The queue is a visible program plan. Search, add, reorder, and remove items with the mouse; verify the list after every edit.",
        spec,
    )
    draw_flow(
        c,
        [
            ("SEARCH", "Find by title, artist, album, or filter.", BLUE),
            ("QUEUE", "Add one intended item. Existing pending items are not duplicated.", GREEN),
            ("ORDER", "Use arrows to place the item in the exact sequence.", PURPLE),
            ("VERIFY", "Read the refreshed queue and forecast.", AMBER),
        ],
        42,
        H - 330,
        W - 84,
    )
    y = H - 380
    sections = [
        ("Before a format change", ["Stop the current station if required by policy.", "Select the target station profile.", "Assign and verify the target station's exact folder."]),
        ("During queue editing", ["Add only items visible in the selected library.", "Move one item at a time and wait for the refreshed order.", "Use Remove only on the intended row."]),
        ("Before going live", ["Confirm the next item, total forecast, and jingle rule.", "Confirm output and monitor readiness.", "Start from the On Air workspace."]),
    ]
    for title, bullets in sections:
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 10.5)
        c.drawString(42, y, title)
        y = draw_bullets(c, bullets, 48, y - 21, W - 96, bullet_color=spec.accent, size=8.5, gap=5) - 6
    callout(
        c,
        42,
        75,
        W - 84,
        88,
        "A normal stop is non-destructive",
        "The interrupted track, jingle, advertisement, or scheduled item stays pending and restarts from its beginning on resume.",
        accent=GREEN,
        kind="BEHAVIOR",
    )


def automation_worker(c: canvas.Canvas, page_no: int) -> None:
    spec = WORKER
    new_page(c, spec, page_no, "Automation")
    page_title(
        c,
        "Deterministic rules",
        "Set the jingle interval you want",
        "Automatic jingles are optional and station-specific. The operator chooses the interval and selection order.",
        spec,
    )
    annotated_screenshot(
        c,
        ASSET_DIR / "automation-jingles.png",
        330,
        315,
        [
            (1, 0.50, 0.41, -35, 30),
            (2, 0.50, 0.30, 35, 25),
            (3, 0.50, 0.22, -35, -22),
            (4, 0.50, 0.12, 35, 20),
        ],
        [
            (1, "Enable or disable automatic jingles."),
            (2, "Songs between jingles: enter 2, 3, or another value from 1 to 100."),
            (3, "Choose random or rotation order."),
            (4, "Save and verify; the green activity line confirms the active rule."),
        ],
        spec,
        crop_browser=False,
    )
    callout(
        c,
        42,
        70,
        W - 84,
        100,
        "Counting rule",
        "Only completed songs increment the interval. A stopped or interrupted song does not count until it finishes. The current song always finishes before the inserted jingle.",
        accent=AMBER,
        kind="RULE",
    )


def emergency_worker(c: canvas.Canvas, page_no: int) -> None:
    spec = WORKER
    new_page(c, spec, page_no, "Emergency")
    page_title(
        c,
        "Priority takeover",
        "Broadcast approved browser-tab audio",
        "Emergency audio is independent of the normal music mount. Preview first, share only the intended tab audio, then confirm takeover.",
        spec,
    )
    annotated_screenshot(
        c,
        ASSET_DIR / "emergency.png",
        350,
        294,
        [
            (1, 0.61, 0.64, 15, 25),
            (2, 0.88, 0.54, -45, 28),
            (3, 0.51, 0.28, -25, 30),
            (4, 0.75, 0.17, 15, 25),
        ],
        [
            (1, "Choose an approved preset or enter an approved public-service page."),
            (2, "Open and preview before takeover."),
            (3, "Arm, then confirm within 20 seconds after sharing tab audio."),
            (4, "Stop emergency audio to restore the saved normal playlist and mix."),
        ],
        spec,
    )
    callout(
        c,
        42,
        70,
        W - 84,
        98,
        "Emergency checklist",
        "Verify the page is official, start its player, select the opened tab, enable Share tab audio, confirm audible monitoring, arm takeover, and verify PROGRAM: Emergency.",
        accent=RED,
        kind="CRITICAL",
    )


def services_worker(c: canvas.Canvas, page_no: int) -> None:
    spec = WORKER
    new_page(c, spec, page_no, "Services")
    page_title(
        c,
        "Optional systems",
        "See health and control optional services",
        "Music continuity does not depend on AI. Optional services may be enabled, tested, updated, backed up, or disabled without giving them broadcast authority.",
        spec,
    )
    annotated_screenshot(
        c,
        ASSET_DIR / "services.png",
        350,
        294,
        [
            (1, 0.10, 0.63, 35, 25),
            (2, 0.87, 0.72, -40, 20),
            (3, 0.36, 0.41, 30, 25),
            (4, 0.58, 0.16, 30, 25),
        ],
        [
            (1, "Services workspace keeps optional systems away from core playout."),
            (2, "Read the enabled or disabled badge before changing state."),
            (3, "Enable and Disable are explicit operator controls."),
            (4, "AI configuration is saved and tested here; it cannot start or stop a stream."),
        ],
        spec,
    )
    callout(
        c,
        42,
        70,
        W - 84,
        98,
        "Safe database maintenance",
        "Run a service health check first, create a backup, apply only the fixed update action, then run health again. Do not expose tokens, passwords, or database files in screenshots or support tickets.",
        accent=PURPLE,
        kind="MAINTENANCE",
    )


def settings_worker(c: canvas.Canvas, page_no: int) -> None:
    spec = WORKER
    new_page(c, spec, page_no, "Settings")
    page_title(
        c,
        "Station administration",
        "Configure output without guesswork",
        "Settings are station-scoped. Save and apply stores the values; Test stream destination proves the destination accepts them.",
        spec,
    )
    annotated_screenshot(
        c,
        ASSET_DIR / "settings.png",
        350,
        294,
        [
            (1, 0.32, 0.53, 25, 25),
            (2, 0.71, 0.54, 20, 25),
            (3, 0.81, 0.31, -40, 26),
            (4, 0.71, 0.23, 25, 25),
        ],
        [
            (1, "Station identity: verify before editing output."),
            (2, "Output gain: change conservatively and monitor."),
            (3, "Local monitor and device selection are independent of the stream."),
            (4, "Save, apply, and verify before running the destination test."),
        ],
        spec,
    )
    callout(
        c,
        42,
        70,
        W - 84,
        98,
        "Credential handling",
        "Enter source credentials only in the protected Settings fields. Never place a password in the station name, mount label, screenshot, log excerpt, or public issue.",
        accent=RED,
        kind="SECURITY",
    )


def diagnostics_worker(c: canvas.Canvas, page_no: int) -> None:
    spec = WORKER
    new_page(c, spec, page_no, "Diagnostics")
    page_title(
        c,
        "Reliability",
        "Readiness is a list, not a feeling",
        "The installation self-check explains each required and optional dependency. Green is ready, amber needs review, and red needs operator action.",
        spec,
    )
    annotated_screenshot(
        c,
        ASSET_DIR / "diagnostics.png",
        350,
        294,
        [
            (1, 0.86, 0.73, -45, 22),
            (2, 0.45, 0.58, 28, 20),
            (3, 0.45, 0.34, 28, 20),
            (4, 0.45, 0.13, 28, 22),
        ],
        [
            (1, "Action count summarizes unresolved checks."),
            (2, "Green checks are ready."),
            (3, "Amber checks are optional or require review."),
            (4, "Red checks include a direct corrective action."),
        ],
        spec,
    )
    callout(
        c,
        42,
        70,
        W - 84,
        98,
        "Go-live threshold",
        "Do not start a production stream while a required output, media, or station-profile check is red. Optional AI checks may remain disabled without blocking music continuity.",
        accent=GREEN,
        kind="GATE",
    )


def shift_checklist_worker(c: canvas.Canvas, page_no: int) -> None:
    spec = WORKER
    new_page(c, spec, page_no, "Operations")
    page_title(
        c,
        "Routine",
        "Shift checklist",
        "A repeatable checklist reduces state ambiguity and makes handovers fast.",
        spec,
    )
    columns = [
        (
            "Start of shift",
            [
                "Sign in and select the station.",
                "Run installation self-check.",
                "Verify managed folder and queue.",
                "Verify jingle interval and order.",
                "Verify monitor and destination.",
                "Read current On Air state.",
            ],
            GREEN,
        ),
        (
            "During shift",
            [
                "Watch now-playing and forecast.",
                "Verify every queue edit.",
                "Keep optional services visibly healthy.",
                "Record unusual warnings.",
                "Use two-step stop for planned silence.",
                "Use priority audio only when approved.",
            ],
            BLUE,
        ),
        (
            "End of shift",
            [
                "Confirm intended live or stopped state.",
                "Confirm queue order.",
                "Confirm priority audio is off.",
                "Review operator activity.",
                "Write the handover note.",
                "Sign out or lock the workstation.",
            ],
            PURPLE,
        ),
    ]
    for i, (title, items, color) in enumerate(columns):
        x = 42 + i * 172
        rounded_card(c, x, 230, 158, 420, fill=white, stroke=tint(color, 0.65))
        c.setFillColor(color)
        c.rect(x, 614, 158, 36, stroke=0, fill=1)
        c.setFillColor(white)
        c.setFont("Helvetica-Bold", 10)
        c.drawString(x + 14, 627, title)
        yy = 585
        for item in items:
            c.setStrokeColor(tint(color, 0.4))
            c.setLineWidth(1)
            c.rect(x + 14, yy - 2, 10, 10, stroke=1, fill=0)
            yy = draw_text(c, item, x + 34, yy + 7, 106, size=7.7, color=INK, leading=10.5) - 14
    callout(
        c,
        42,
        80,
        W - 84,
        94,
        "Handover minimum",
        "State the selected station, live status, next three queue items, active automation rule, unresolved health items, and whether any priority source was used.",
        accent=AMBER,
        kind="HANDOVER",
    )


def troubleshooting_page(c: canvas.Canvas, spec: ManualSpec, page_no: int, internal: bool) -> None:
    new_page(c, spec, page_no, "Troubleshooting")
    page_title(
        c,
        "Recovery",
        "Symptom-to-action matrix",
        "Use visible status and bounded corrective actions. Do not repeatedly click a control while the result is unknown.",
        spec,
    )
    rows = [
        ("Dashboard will not open", "Wait for startup, then reopen the tray action.", "Login screen or readiness page appears."),
        ("Stream rejected", "Recheck destination, mount, user, password, and codec; run destination test.", "Test reports accepted."),
        ("Media missing", "Confirm folder access; sync and verify; review unreadable files.", "Expected library count appears."),
        ("Jingle did not run", "Check enabled state, interval, completed-song count, and selection order.", "Verified rule appears in activity."),
        ("Priority audio is silent", "Select the correct browser tab and enable shared tab audio.", "Signal and buffer become active."),
        ("Optional service unavailable", "Disable it, preserve music continuity, then run service health.", "Core playout remains healthy."),
    ]
    x, y = 42, H - 205
    widths = [126, 240, 145]
    headers = ["SYMPTOM", "OPERATOR ACTION", "VERIFICATION"]
    c.setFillColor(spec.accent)
    c.rect(x, y, sum(widths), 28, stroke=0, fill=1)
    cursor_x = x
    c.setFillColor(white)
    c.setFont("Helvetica-Bold", 7)
    for header, width in zip(headers, widths):
        c.drawString(cursor_x + 8, y + 10, header)
        cursor_x += width
    y -= 64
    for idx, row in enumerate(rows):
        fill = white if idx % 2 == 0 else PALE
        c.setFillColor(fill)
        c.rect(x, y, sum(widths), 64, stroke=0, fill=1)
        cursor_x = x
        for text_value, width in zip(row, widths):
            draw_text(c, text_value, cursor_x + 8, y + 49, width - 16, size=7.3, color=INK, leading=9.6)
            cursor_x += width
        c.setStrokeColor(LINE)
        c.line(x, y, x + sum(widths), y)
        y -= 64
    callout(
        c,
        42,
        70,
        W - 84,
        88,
        "Escalate with evidence",
        (
            "Record the time, selected station, visible status, exact action, and exact error. "
            + ("Never include passwords, protected configuration, or database files." if internal else "Remove credentials and organization-specific details before sharing.")
        ),
        accent=RED,
        kind="ESCALATE",
    )


def incident_worker(c: canvas.Canvas, page_no: int) -> None:
    spec = WORKER
    new_page(c, spec, page_no, "Incident response")
    page_title(
        c,
        "When seconds matter",
        "Priority broadcast response",
        "Use this page as the operator's compact decision sequence. Local policy and authorized editorial direction always take precedence.",
        spec,
    )
    flow = [
        ("1", "AUTHORIZE", "Confirm the instruction and approved source."),
        ("2", "PREVIEW", "Open the page, start playback, and verify audible content."),
        ("3", "SHARE", "Select only the intended browser tab and enable tab audio."),
        ("4", "TAKE OVER", "Arm, confirm, and verify Emergency program state."),
        ("5", "RESTORE", "Stop priority audio and verify the saved playlist returns."),
    ]
    y = H - 220
    for number, title, body in flow:
        c.setFillColor(spec.accent)
        c.roundRect(42, y - 14, 45, 45, 12, stroke=0, fill=1)
        c.setFillColor(white)
        c.setFont("Helvetica-Bold", 15)
        c.drawCentredString(64.5, y + 1, number)
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 11)
        c.drawString(106, y + 12, title)
        draw_text(c, body, 106, y - 4, W - 154, size=8.5, color=MUTED, leading=11.2)
        if number != "5":
            draw_arrow(c, 64.5, y - 22, 64.5, y - 48, MUTED, width=1.2)
        y -= 92
    callout(
        c,
        42,
        76,
        W - 84,
        96,
        "If the browser source fails",
        "Do not improvise an unapproved page. Restore normal program audio, record the failure, and follow the station's authorized backup-source procedure.",
        accent=AMBER,
        kind="FALLBACK",
    )


def quick_reference(c: canvas.Canvas, spec: ManualSpec, page_no: int, internal: bool) -> None:
    new_page(c, spec, page_no, "Quick reference")
    page_title(
        c,
        "One page",
        "Operator quick reference",
        "The shortest safe path for common actions.",
        spec,
    )
    actions = [
        ("START", "Select station -> On Air -> read state -> Start / resume -> verify ON AIR", GREEN),
        ("STOP", "On Air -> Stop -> Confirm within 20 seconds -> verify OFF AIR and unchanged queue", RED),
        ("IMPORT", "Media -> Browse -> choose folder -> set scope -> Sync and verify", BLUE),
        ("JINGLES", "Automation -> enable -> set song interval -> choose order -> Save and verify", PURPLE),
        ("PRIORITY", "Preview -> share tab audio -> Arm -> Confirm -> verify signal -> Restore", AMBER),
        ("HEALTH", "Diagnostics or Health -> run checks -> resolve required red items -> re-run", CYAN),
    ]
    y = H - 210
    for title, body, color in actions:
        rounded_card(c, 42, y - 45, W - 84, 55, fill=white, stroke=tint(color, 0.65))
        c.setFillColor(color)
        c.roundRect(54, y - 30, 74, 27, 13.5, stroke=0, fill=1)
        c.setFillColor(white)
        c.setFont("Helvetica-Bold", 8)
        c.drawCentredString(91, y - 20, title)
        draw_text(c, body, 145, y - 11, W - 205, size=8.4, color=INK, leading=11)
        y -= 72
    callout(
        c,
        42,
        76,
        W - 84,
        92,
        "The golden rule",
        "Select, act, verify. If the visible result is not the expected result, stop clicking and open the readiness page.",
        accent=spec.accent,
        kind="REMEMBER",
    )


def public_interface(c: canvas.Canvas, page_no: int) -> None:
    spec = PUBLIC
    new_page(c, spec, page_no, "Interface tour")
    page_title(
        c,
        "Orientation",
        "A consistent control surface",
        "The neutral example below shows the standard pattern without organization-specific names, endpoints, or integrations.",
        spec,
    )
    neutral_figure_with_legend(
        c,
        "onair",
        350,
        [
            (1, 0.09, 0.64, 30, 22),
            (2, 0.64, 0.91, -25, -32),
            (3, 0.89, 0.77, -35, 24),
            (4, 0.74, 0.22, 20, 30),
        ],
        [
            (1, "Workspace navigation groups tasks by purpose."),
            (2, "System-ready badge confirms application connectivity."),
            (3, "Live state remains visible inside the active workspace."),
            (4, "Primary actions use explicit verbs and a visible result."),
        ],
    )
    callout(
        c,
        42,
        82,
        W - 84,
        82,
        "No hidden control path",
        "Normal broadcast operation is available entirely through the visible interface. Automation does not replace operator authorization.",
        accent=BLUE,
        kind="DESIGN",
    )


def public_task_page(
    c: canvas.Canvas,
    page_no: int,
    section: str,
    kicker: str,
    title: str,
    intro: str,
    variant: str,
    markers: Sequence[tuple[int, float, float, float, float]],
    legend: Sequence[tuple[int, str]],
    note_title: str,
    note_body: str,
    note_color: Color,
) -> None:
    spec = PUBLIC
    new_page(c, spec, page_no, section)
    page_title(c, kicker, title, intro, spec)
    neutral_figure_with_legend(c, variant, 350, markers, legend)
    callout(c, 42, 72, W - 84, 96, note_title, note_body, accent=note_color, kind="VERIFY")


def public_daily_page(c: canvas.Canvas, page_no: int) -> None:
    spec = PUBLIC
    new_page(c, spec, page_no, "Daily operation")
    page_title(
        c,
        "Routine",
        "A complete mouse-driven shift",
        "Use the same predictable sequence at startup, during operation, and at handover.",
        spec,
    )
    draw_flow(
        c,
        [
            ("CHECK", "Open Health and clear required issues.", GREEN),
            ("PREPARE", "Review media, queue, and automation.", BLUE),
            ("OPERATE", "Start, monitor, and verify each change.", PURPLE),
            ("HAND OVER", "Record state and sign out.", AMBER),
        ],
        42,
        H - 315,
        W - 84,
    )
    checklists = [
        ("Before live", ["Select the intended profile.", "Confirm ready status.", "Review queue and inserts.", "Confirm monitor and output."]),
        ("While live", ["Watch current item and forecast.", "Edit one queue item at a time.", "Use two-step stop.", "Keep priority audio off unless authorized."]),
        ("At handover", ["State live status.", "List next queue items.", "Record warnings.", "Lock or sign out."]),
    ]
    y = H - 375
    for title, items in checklists:
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 10.5)
        c.drawString(42, y, title)
        y = draw_bullets(c, items, 48, y - 21, W - 96, bullet_color=BLUE, size=8.4, gap=4) - 5


def glossary_page(c: canvas.Canvas, page_no: int) -> None:
    spec = PUBLIC
    new_page(c, spec, page_no, "Reference")
    page_title(
        c,
        "Terms",
        "Glossary and operating language",
        "Consistent language helps operators make precise decisions.",
        spec,
    )
    terms = [
        ("Station profile", "A self-contained set of media, queue, automation, output, and monitor settings."),
        ("Managed folder", "The approved file-system location used to build one profile's media library."),
        ("Queue", "The ordered list of pending program items."),
        ("Current item", "The item playing now or waiting to restart after a preserved stop."),
        ("Insert interval", "The number of completed songs between automatic jingles or other configured inserts."),
        ("Priority audio", "Approved external browser-tab audio that temporarily replaces normal program audio."),
        ("Readiness check", "A visible list of required, optional, ready, warning, and failed components."),
        ("Verified action", "A control action followed by a returned state that confirms the intended result."),
    ]
    y = H - 190
    for index, (term, definition) in enumerate(terms):
        x = 42 if index % 2 == 0 else 306
        row = index // 2
        yy = y - row * 126
        rounded_card(c, x, yy - 92, 247, 103, fill=white, stroke=LINE)
        c.setFillColor(BLUE)
        c.setFont("Helvetica-Bold", 10)
        c.drawString(x + 14, yy - 17, term)
        draw_text(c, definition, x + 14, yy - 38, 219, size=8.1, color=MUTED, leading=11)
    callout(
        c,
        42,
        74,
        W - 84,
        90,
        "Document boundary",
        "This edition intentionally contains no organization-specific branding, station names, server addresses, mount labels, credentials, or private service details.",
        accent=BLUE,
        kind="PRIVACY",
    )


def build_worker(path: Path) -> int:
    c = canvas.Canvas(str(path), pagesize=A4, pageCompression=1, pdfVersion=(1, 7))
    cover(c, WORKER)
    principles_page(c, WORKER, 2)
    toc_page(
        c,
        WORKER,
        3,
        [
            ("Operator authority and interface tour", 4),
            ("Start-of-shift readiness", 5),
            ("Start, stop, and playlist continuity", 6),
            ("Managed media and queue control", 7),
            ("Queue planning and format isolation", 8),
            ("Adjustable jingle automation", 9),
            ("Emergency browser-audio takeover", 10),
            ("Optional services and database health", 11),
            ("Station and output settings", 12),
            ("Diagnostics and readiness gates", 13),
            ("Shift checklist", 14),
            ("Troubleshooting", 15),
            ("Priority incident response", 16),
            ("Operator quick reference", 17),
        ],
    )
    interface_tour_worker(c, 4)
    first_shift_page(c, WORKER, 5, True)
    onair_worker(c, 6)
    media_worker(c, 7)
    queue_worker(c, 8)
    automation_worker(c, 9)
    emergency_worker(c, 10)
    services_worker(c, 11)
    settings_worker(c, 12)
    diagnostics_worker(c, 13)
    shift_checklist_worker(c, 14)
    troubleshooting_page(c, WORKER, 15, True)
    incident_worker(c, 16)
    quick_reference(c, WORKER, 17, True)
    c.save()
    return 17


def build_public(path: Path) -> int:
    c = canvas.Canvas(str(path), pagesize=A4, pageCompression=1, pdfVersion=(1, 7))
    cover(c, PUBLIC)
    principles_page(c, PUBLIC, 2)
    toc_page(
        c,
        PUBLIC,
        3,
        [
            ("Interface tour", 4),
            ("Start-of-shift readiness", 5),
            ("Start, stop, and queue continuity", 6),
            ("Managed media and queue", 7),
            ("Adjustable automation", 8),
            ("Priority external audio", 9),
            ("Output settings", 10),
            ("System health", 11),
            ("Daily operating checklist", 12),
            ("Troubleshooting", 13),
            ("Glossary", 14),
            ("Quick reference", 15),
        ],
    )
    public_interface(c, 4)
    first_shift_page(c, PUBLIC, 5, False)
    public_task_page(
        c,
        6,
        "On Air",
        "Core operation",
        "Start, stop, and keep the queue",
        "A normal stop is a controlled pause, not a playlist reset.",
        "onair",
        [(1, 0.88, 0.75, -35, 20), (2, 0.44, 0.44, 25, 22), (3, 0.74, 0.21, 20, 30), (4, 0.44, 0.21, -20, 30)],
        [
            (1, "Read the live state first."),
            (2, "Confirm the current item and timeline."),
            (3, "Stop requires a deliberate confirmation and preserves the queue."),
            (4, "Start / resume restarts the interrupted item from the beginning."),
        ],
        "Expected stop result",
        "The live badge changes to off, the engine and scheduler stop, and the pending queue remains in the same order.",
        GREEN,
    )
    public_task_page(
        c,
        7,
        "Media",
        "Library and queue",
        "Import only the intended program library",
        "Use the native folder picker, define scope, sync, and verify before adding tracks.",
        "media",
        [(1, 0.42, 0.63, 22, 25), (2, 0.89, 0.62, -35, 25), (3, 0.62, 0.39, 24, 22), (4, 0.86, 0.28, -35, 22)],
        [
            (1, "Managed folder is visible before scanning."),
            (2, "Browse opens the operating-system picker."),
            (3, "Library rows show what is available."),
            (4, "Add one intended item to the queue."),
        ],
        "After each queue edit",
        "Wait for the refreshed order, then read the next item and forecast. Do not make a second edit while the first result is unknown.",
        BLUE,
    )
    public_task_page(
        c,
        8,
        "Automation",
        "Deterministic rules",
        "Choose the insert interval",
        "Automatic jingles are optional. Set the number of completed songs between inserts and choose the selection order.",
        "automation",
        [(1, 0.89, 0.67, -35, 22), (2, 0.61, 0.53, 24, 22), (3, 0.62, 0.39, -30, 20), (4, 0.62, 0.24, 24, 24)],
        [
            (1, "Enable or disable the rule."),
            (2, "Enter 2, 3, or another allowed interval."),
            (3, "Choose random or rotation order."),
            (4, "Save and verify the returned rule."),
        ],
        "How the counter works",
        "Only completed songs count. An interrupted song does not increment the interval until it finishes, and the current song finishes before the insert plays.",
        AMBER,
    )
    public_task_page(
        c,
        9,
        "Priority audio",
        "External source",
        "Take over with approved browser-tab audio",
        "Preview the source, share only its tab audio, arm the takeover, and verify the returned signal.",
        "emergency",
        [(1, 0.58, 0.58, 20, 25), (2, 0.59, 0.42, -25, 25), (3, 0.62, 0.28, 24, 22), (4, 0.60, 0.20, -25, 22)],
        [
            (1, "Enter an approved HTTP or HTTPS page."),
            (2, "Preview and confirm shared tab audio."),
            (3, "Arm the priority takeover."),
            (4, "Restore normal audio when the event ends."),
        ],
        "Safe source rule",
        "Use only approved official sources. If the source is silent or unclear, restore normal program audio and follow the authorized fallback procedure.",
        RED,
    )
    public_task_page(
        c,
        10,
        "Settings",
        "Output",
        "Save, apply, test",
        "Output settings are scoped to the selected profile and should be changed only with a verification plan.",
        "settings",
        [(1, 0.55, 0.65, 24, 22), (2, 0.55, 0.51, -30, 22), (3, 0.55, 0.37, 24, 22), (4, 0.59, 0.22, -25, 22)],
        [
            (1, "Confirm the profile identity."),
            (2, "Adjust gain conservatively."),
            (3, "Enable only the intended output path."),
            (4, "Save, apply, and verify before testing."),
        ],
        "Protect credentials",
        "Keep stream passwords and tokens only in protected input fields. Remove organization-specific details from screenshots and support reports.",
        PURPLE,
    )
    public_task_page(
        c,
        11,
        "Health",
        "Readiness",
        "Resolve required checks before live use",
        "The health page is the authoritative view of application, media, profile, monitor, and output readiness.",
        "health",
        [(1, 0.49, 0.60, 24, 22), (2, 0.49, 0.50, -28, 22), (3, 0.49, 0.40, 24, 22), (4, 0.49, 0.30, -28, 22)],
        [
            (1, "Green means ready."),
            (2, "Amber means optional or review."),
            (3, "Red means operator action is required."),
            (4, "Re-run checks after each correction."),
        ],
        "Production gate",
        "Do not start while a required media, profile, or output check is red. Optional features may remain disabled when core playout is healthy.",
        GREEN,
    )
    public_daily_page(c, 12)
    troubleshooting_page(c, PUBLIC, 13, False)
    glossary_page(c, 14)
    quick_reference(c, PUBLIC, 15, False)
    c.save()
    return 15


def validate_pdf(path: Path, expected_pages: int, forbidden: Iterable[str] = ()) -> dict[str, object]:
    reader = PdfReader(str(path))
    if len(reader.pages) != expected_pages:
        raise AssertionError(f"{path.name}: expected {expected_pages} pages, found {len(reader.pages)}")
    # Vector-only manuals compress extremely well; 25 KB still catches empty output.
    if path.stat().st_size < 25_000:
        raise AssertionError(f"{path.name}: unexpectedly small PDF")
    text = "\n".join((page.extract_text() or "") for page in reader.pages)
    if len(text.strip()) < 5_000:
        raise AssertionError(f"{path.name}: insufficient extractable text")
    lower = text.lower()
    found = [word for word in forbidden if word.lower() in lower]
    if found:
        raise AssertionError(f"{path.name}: forbidden public-edition terms found: {found}")
    return {
        "file": path.name,
        "pages": len(reader.pages),
        "bytes": path.stat().st_size,
        "text_chars": len(text),
    }


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    worker_path = OUTPUT_DIR / "RadioTEDU-OnAir-Worker-Operations-Manual.pdf"
    public_path = OUTPUT_DIR / "Deterministic-Broadcast-Console-User-Manual.pdf"
    worker_pages = build_worker(worker_path)
    public_pages = build_public(public_path)
    reports = [
        validate_pdf(worker_path, worker_pages),
        validate_pdf(
            public_path,
            public_pages,
            forbidden=[
                "radiotedu",
                "rtai",
                "lofi",
                "trt",
                "ollama",
                "icecast",
                "localhost",
                "127.0.0.1",
                "stream.radiotedu.com",
            ],
        ),
    ]
    for report in reports:
        print(
            f"{report['file']}: {report['pages']} pages, "
            f"{report['bytes']} bytes, {report['text_chars']} text characters"
        )


if __name__ == "__main__":
    main()
