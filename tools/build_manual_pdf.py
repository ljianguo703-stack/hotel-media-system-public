#!/usr/bin/env python3
"""Render the generic first-install manual; no site state or evidence is imported."""
import argparse
import html
from pathlib import Path
import re

from pypdf import PdfReader
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate, Flowable, Frame, PageBreak, PageTemplate, Paragraph,
    Spacer, Table, TableStyle,
)
from reportlab.platypus.tableofcontents import TableOfContents

INK = colors.HexColor('#20313e')
TEAL = colors.HexColor('#126d79')
MUTED = colors.HexColor('#5a6d78')
LINE = colors.HexColor('#d6e2e6')


def styles():
    body = ParagraphStyle('Body', fontName='CN', fontSize=10.2, leading=16.2,
                          textColor=INK, spaceAfter=8, wordWrap='CJK')
    return {
        'body': body,
        'h1': ParagraphStyle('H1', parent=body, fontName='CNBold', fontSize=21,
                             leading=28, spaceAfter=16, keepWithNext=True),
        'h2': ParagraphStyle('H2', parent=body, fontName='CNBold', fontSize=12,
                             leading=19, textColor=TEAL, spaceBefore=10,
                             spaceAfter=6, keepWithNext=True),
        'step': ParagraphStyle('Step', parent=body, leftIndent=18,
                               firstLineIndent=-18, spaceAfter=7),
        'small': ParagraphStyle('Small', parent=body, fontSize=8.6, leading=13,
                                textColor=MUTED),
        'cell': ParagraphStyle('Cell', parent=body, fontSize=8.7, leading=13,
                               spaceAfter=0),
        'toc': ParagraphStyle('TOC', parent=body, fontSize=9.8, leading=16,
                              spaceBefore=2, spaceAfter=2),
        'cover': ParagraphStyle('Cover', parent=body, fontName='CNBold',
                                fontSize=33, leading=45, spaceAfter=18),
    }


def markup(text):
    text = html.escape(text)
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'`([^`]+)`', r'<font color="#126d79">\1</font>', text)
    def link(match):
        url = match.group(0).rstrip('。；，')
        return '<link href="%s" color="#126d79">%s</link>%s' % (
            url, url, match.group(0)[len(url):])
    return re.sub(r'https?://[^\s]+', link, text)


class CodeBox(Flowable):
    """Keep command lines literal; never insert display wraps into a command."""
    def __init__(self, lines, size=8.5):
        Flowable.__init__(self)
        self.lines = lines
        self.size = size
        self.spaceBefore = 3
        self.spaceAfter = 10

    def wrap(self, width, height):
        maximum = max((pdfmetrics.stringWidth(s, 'CN', self.size)
                       for s in self.lines), default=1)
        if maximum > width - 20:
            self.size *= (width - 20) / maximum
        if self.size < 7.3:
            raise ValueError('Command too wide; add a valid shell continuation')
        self.width = width
        self.leading = self.size * 1.6
        self.height = self.leading * len(self.lines) + 18
        return self.width, self.height

    def split(self, width, height):
        self.wrap(width, height)
        count = int((height - 18) / self.leading)
        if count < 1:
            return []
        if count >= len(self.lines):
            return [self]
        return [CodeBox(self.lines[:count], self.size),
                CodeBox(self.lines[count:], self.size)]

    def draw(self):
        c = self.canv
        c.setFillColor(colors.HexColor('#eff5f6'))
        c.roundRect(0, 0, self.width, self.height, 4, fill=1, stroke=0)
        c.setFillColor(INK)
        c.setFont('CN', self.size)
        for index, line in enumerate(self.lines):
            c.drawString(10, self.height - 10 - self.size - index*self.leading, line)


class Manual(BaseDocTemplate):
    def afterFlowable(self, item):
        if isinstance(item, Paragraph) and hasattr(item, 'chapter'):
            title = item.getPlainText()
            key = 'chapter-%d' % item.chapter
            self.canv.bookmarkPage(key)
            self.canv.addOutlineEntry(title, key, 0, False)
            self.notify('TOCEntry', (0, title, self.page, key))


def decorate(c, doc):
    c.saveState()
    c.setFont('CN', 8)
    c.setFillColor(MUTED)
    c.drawString(19*mm, 12*mm, '酒店影院系统 / 全新部署与运营 / IN16-DOC1')
    c.drawRightString(A4[0]-19*mm, 12*mm, '%d / %s' % (doc.page, doc.total or '…'))
    if doc.page > 1:
        c.setStrokeColor(LINE)
        c.line(19*mm, A4[1]-18*mm, A4[0]-19*mm, A4[1]-18*mm)
        c.drawString(19*mm, A4[1]-14*mm, '全新酒店 · Ubuntu + 电视首次安装')
        c.drawRightString(A4[0]-19*mm, A4[1]-14*mm, '2026-11-01')
    c.restoreState()


def parse_body(lines, story, s, width, table_padding=7, table_widths=None):
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if not line:
            index += 1
            continue
        if line.startswith('```'):
            code = []
            index += 1
            while index < len(lines) and not lines[index].startswith('```'):
                code.append(lines[index])
                index += 1
            if index == len(lines):
                raise ValueError('Unclosed command fence')
            story.append(CodeBox(code))
            index += 1
            continue
        if line.startswith('### '):
            story.append(Paragraph(markup(line[4:]), s['h2']))
            index += 1
            continue
        if line.startswith('|'):
            rows = []
            while index < len(lines) and lines[index].strip().startswith('|'):
                row = [x.strip() for x in lines[index].strip().strip('|').split('|')]
                if not all(re.fullmatch(r':?-+:?', x) for x in row):
                    rows.append([Paragraph(markup(x), s['cell']) for x in row])
                index += 1
            cols = len(rows[0])
            weights = [0.36, 0.64] if cols == 2 else [1/cols]*cols
            if cols == 2 and rows[0][0].getPlainText() == '文件':
                weights = [0.58, 0.42]
            weights = (table_widths or {}).get(rows[0][0].getPlainText(), weights)
            table = Table(rows, colWidths=[width*x for x in weights], repeatRows=1,
                          hAlign='LEFT', spaceBefore=3, spaceAfter=12)
            table.setStyle(TableStyle([
                ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#dcecef')),
                ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, colors.HexColor('#f7fafb')]),
                ('VALIGN', (0,0), (-1,-1), 'TOP'),
                ('LEFTPADDING', (0,0), (-1,-1), 8),
                ('RIGHTPADDING', (0,0), (-1,-1), 8),
                ('TOPPADDING', (0,0), (-1,-1), table_padding),
                ('BOTTOMPADDING', (0,0), (-1,-1), table_padding),
                ('LINEBELOW', (0,0), (-1,0), .6, LINE),
            ]))
            story.append(table)
            continue
        story.append(Paragraph(markup(line), s['step'] if re.match(r'^\d+\. ',line) else s['body']))
        index += 1


def render(source, output, total=None):
    s = styles()
    doc = Manual(str(output), pagesize=A4, leftMargin=19*mm, rightMargin=19*mm,
                 topMargin=25*mm, bottomMargin=23*mm,
                 title='酒店影院系统完整使用说明 - 全新酒店首次安装与运营',
                 author='酒店影院项目', pageCompression=1)
    doc.total = total
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height,
                  id='body', leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    doc.addPageTemplates(PageTemplate(id='main', frames=frame, onPage=decorate))
    story = [Spacer(1, 20*mm), Paragraph('酒店影院系统<br/>完整使用说明', s['cover']),
             Spacer(1, 5*mm), Paragraph('全新酒店首次安装与运营', s['h1']),
             Paragraph('Ubuntu 系统安装 · 影片盘 · 一键首装<br/>电视逐间部署 · 后台纳管 · 日常使用', s['body']),
             Spacer(1, 12*mm), Paragraph('后台 v1-0-0-rc4-in15 / 执行器 1.0.0~rc4.1<br/>电视 0.1.11 / 员工工具 1.3.8', s['body']),
             Spacer(1, 10*mm), Paragraph('从空白主机和未安装影院的电视开始。<br/>按步骤执行，命令、参数、预期结果与注意事项随章节说明。<br/>酒店地址、账号和磁盘信息由施工前填写。', s['body']),
             Spacer(1, 10*mm), Paragraph('2026-11-01 · IN16-DOC1 文档修订版<br/>本手册提供通用流程，现场验收结果由各酒店独立记录。', s['small']),
             PageBreak(), Paragraph('目录', s['h1'])]
    toc = TableOfContents()
    toc.levelStyles = [s['toc']]
    toc.dotsMinLevel = 0
    story.append(toc)
    sections = re.split(r'^## ', source.read_text(), flags=re.M)[1:]
    for number, section in enumerate(sections,1):
        title, _, body = section.partition('\n')
        story.append(PageBreak())
        heading = Paragraph('%02d  %s' % (number, markup(title.strip())), s['h1'])
        heading.chapter = number
        story.append(heading)
        parse_body(body.splitlines(), story, s, doc.width)
    doc.multiBuild(story)
    count = len(PdfReader(output).pages)
    if total is None:
        return render(source, output, count)
    if count != total:
        raise ValueError('Pagination changed between final passes')
    return count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[1]/'docs/完整使用说明.md')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--font', type=Path, default=Path('/System/Library/Fonts/STHeiti Light.ttc'))
    parser.add_argument('--bold-font', type=Path, default=Path('/System/Library/Fonts/STHeiti Medium.ttc'))
    args = parser.parse_args()
    for name,path in [('CN',args.font),('CNBold',args.bold_font)]:
        if not path.is_file():
            raise SystemExit('Provide an embeddable Chinese TrueType font with --font / --bold-font')
        pdfmetrics.registerFont(TTFont(name,str(path),subfontIndex=0))
    pdfmetrics.registerFontFamily('CN',normal='CN',bold='CNBold',italic='CN',boldItalic='CNBold')
    args.output.parent.mkdir(parents=True,exist_ok=True)
    print({'output':str(args.output),'pages':render(args.source,args.output)})


if __name__ == '__main__':
    main()
