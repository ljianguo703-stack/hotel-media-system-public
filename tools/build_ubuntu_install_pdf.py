#!/usr/bin/env python3
"""Render the IN20 Ubuntu tutorial using the existing manual's components."""
import argparse
import html
import re
from pathlib import Path

from pypdf import PdfReader
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import BaseDocTemplate, Frame, PageTemplate, PageBreak, Paragraph, Spacer, Image

import build_manual_pdf as shared


def markup(value):
    value = html.escape(value)
    value = re.sub(r'\[([^\]]+)\]\((https?://[^\s)]+)\)',
                   r'<link href="\2" color="#126d79">\1</link>', value)
    value = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', value)
    return re.sub(r'`([^`]+)`', r'<font color="#126d79">\1</font>', value)


class Tutorial(BaseDocTemplate):
    def afterFlowable(self, item):
        if hasattr(item, 'section_number'):
            n = item.section_number
            self.section_pages.append((n, self.page))
            self.canv.bookmarkPage(f'section-{n}')
            self.canv.addOutlineEntry(item.getPlainText(), f'section-{n}', 0, False)


def decorate(c, doc):
    c.saveState()
    c.setFillColor(shared.TEAL)
    c.rect(0, A4[1] - 4*mm, A4[0], 4*mm, fill=1, stroke=0)
    c.setFont('CN', 8.2)
    c.setFillColor(shared.MUTED)
    c.drawString(19*mm, A4[1]-15*mm, '酒店影院 / 全新 Ubuntu 安装教程')
    c.drawRightString(A4[0]-19*mm, A4[1]-15*mm, 'IN20-DOC05 · 2026-11-07')
    c.setStrokeColor(shared.LINE)
    c.line(19*mm, 18*mm, A4[0]-19*mm, 18*mm)
    c.drawString(19*mm, 12*mm, 'Ubuntu 24.04 amd64 · 在线 / 本地完整包 · 国内 / 海外')
    c.drawRightString(A4[0]-19*mm, 12*mm, f'{doc.page:02d} / {doc.expected_pages}')
    c.restoreState()


def render(source, output):
    shared.markup = markup
    for name, filename in [('CN', 'STHeiti Light.ttc'), ('CNBold', 'STHeiti Medium.ttc')]:
        pdfmetrics.registerFont(TTFont(name, f'/System/Library/Fonts/{filename}', subfontIndex=0))
    pdfmetrics.registerFontFamily('CN', normal='CN', bold='CNBold', italic='CN', boldItalic='CNBold')
    s = shared.styles()
    s['h1'].fontSize = 20
    s['h1'].leading = 27
    s['h1'].spaceAfter = 13
    s['h2'].spaceBefore = 8
    s['h2'].spaceAfter = 5
    s['intro'] = ParagraphStyle('Intro', parent=s['body'], fontSize=9, leading=14, textColor=shared.MUTED)
    s['title'] = ParagraphStyle('Title', parent=s['h1'], fontSize=28, leading=36, spaceAfter=8)
    text = source.read_text()
    introduction, *sections = re.split(r'^## ', text, flags=re.M)
    intro = '\n'.join(introduction.splitlines()[2:]).strip()
    doc = Tutorial(str(output), pagesize=A4, leftMargin=19*mm, rightMargin=19*mm,
                   topMargin=24*mm, bottomMargin=24*mm, title='全新 Ubuntu 安装教程 - IN20',
                   author='酒店影院项目', subject='Ubuntu 系统安装、影片盘、影院首装、电视接入与验收',
                   pageCompression=1)
    doc.section_pages = []
    doc.expected_pages = len(sections)
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id='body',
                  leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    doc.addPageTemplates(PageTemplate(id='main', frames=frame, onPage=decorate))
    story = []
    for index, section in enumerate(sections, 1):
        if index > 1:
            story.append(PageBreak())
        else:
            story.append(Paragraph('全新 Ubuntu 安装教程', s['title']))
            story.append(Paragraph(markup(intro), s['intro']))
            story.append(Spacer(1, 6))
        title, _, body = section.partition('\n')
        heading = Paragraph(markup(title.strip()), s['h2'] if index == 1 else s['h1'])
        heading.section_number = index
        story.append(heading)
        chunks = re.split(r'(^!\[[^\]]*\]\([^\n)]+\)\s*$)', body, flags=re.M)
        for chunk in chunks:
            picture = re.fullmatch(r'!\[([^\]]*)\]\(([^\n)]+)\)\s*', chunk)
            if picture:
                path = source.parent / picture[2]
                item = Image(str(path))
                scale = min(doc.width / item.imageWidth, 200*mm / item.imageHeight, 1)
                item.drawWidth = item.imageWidth * scale
                item.drawHeight = item.imageHeight * scale
                item.hAlign = 'LEFT'
                story.extend([item, Spacer(1, 4), Paragraph(markup(picture[1]), s['intro']), Spacer(1, 6)])
            else:
                shared.parse_body(chunk.splitlines(), story, s, doc.width, table_padding=4.5,
                                  table_widths={'顺序': [0.13, 0.87]})
    doc.build(story)
    count = len(PdfReader(output).pages)
    expected_sections = [(i, i) for i in range(1, doc.expected_pages + 1)]
    if count != doc.expected_pages or doc.section_pages != expected_sections:
        raise ValueError(f'Expected one section per page: count={count}, sections={doc.section_pages}')
    print(f'{count} pages, headings/bookmarks OK: {output}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[1]/'docs/全新Ubuntu安装教程_IN20.md')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    render(args.source, args.output)


if __name__ == '__main__':
    main()
