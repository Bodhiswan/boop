"""Portable local report/recap renderers; all graphs use canonical daily metrics."""
from __future__ import annotations

import html
import io
import math


METRICS = [("charge", "Charge", " / 100"), ("effort", "Effort", " / 100"),
           ("hrv", "Night HRV", " ms"), ("resting_hr", "Resting HR", " bpm"),
           ("rest", "Rest", " / 100"), ("sleep", "Sleep", " h")]


def value(row, key):
    if key == "sleep":
        result = (row.get("sleep", {}).get("main") or {}).get("total_sleep_min")
        return result / 60 if isinstance(result, (int, float)) else None
    result = row.get(key, {})
    return result.get("value") if isinstance(result, dict) else result


def points(rows, key):
    return [(i, v) for i, row in enumerate(rows) if isinstance((v := value(row, key)), (int, float)) and math.isfinite(v)]


def report_svg(bundle):
    rows = bundle.get("days", [])
    start, end = (rows[0]["day"], rows[-1]["day"]) if rows else ("", "")
    output = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1000 1120" role="img" aria-label="BOOP local trends report">',
              '<rect width="1000" height="1120" fill="#f5f3ec"/>',
              '<g fill="#252d29" font-family="Arial,sans-serif">',
              '<text x="60" y="75" font-size="35" font-weight="700">boop.</text>',
              '<text x="60" y="126" font-size="32">Your rhythm, over time.</text>',
              f'<text x="60" y="164" font-size="17">{html.escape(start)} to {html.escape(end)} · Local NOOP estimates</text>']
    for index, (key, title, unit) in enumerate(METRICS):
        x, y = 60 + index % 2 * 460, 216 + index // 2 * 256
        samples = points(rows, key)
        mean = sum(v for _, v in samples) / len(samples) if samples else None
        output.extend([f'<rect x="{x}" y="{y}" width="420" height="232" rx="16" fill="#fffdf7"/>',
                       f'<text x="{x+24}" y="{y+34}" font-size="16">{title}</text>',
                       f'<text x="{x+24}" y="{y+70}" font-size="28">{f"{mean:.1f}" if mean is not None else "—"}{html.escape(unit)}</text>'])
        if samples:
            lo, hi = min(v for _, v in samples), max(v for _, v in samples)
            # Split paths on missing calendar days; no interpolation over missing data.
            d, previous = [], -2
            for i, v in samples:
                px, py = x+24+i/max(1,len(rows)-1)*370, y+180-(v-lo)/max(1,hi-lo)*70
                d.append(f'{"L" if i == previous+1 else "M"}{px:.2f},{py:.2f}')
                output.append(f'<circle cx="{px:.2f}" cy="{py:.2f}" r="2.5" fill="#507663"/>')
                previous = i
            output.append(f'<path d="{" ".join(d)}" fill="none" stroke="#507663" stroke-width="2.5"/>')
        else:
            output.append(f'<text x="{x+24}" y="{y+152}" font-size="14" fill="#6d766e">More covered observations needed</text>')
        output.append(f'<text x="{x+24}" y="{y+214}" font-size="13" fill="#6d766e">{len(samples)} of {len(rows)} days observed · gaps preserved</text>')
    output.extend(['<text x="60" y="1044" font-size="14">Computed metrics are estimates. Imported official scores retain their source.</text>',
                   '<text x="60" y="1071" font-size="14">Your data remains on this laptop. No invented values for missing days.</text>', '</g></svg>'])
    return "".join(output)


def report_pdf(bundle):
    from reportlab.pdfgen import canvas
    from reportlab.lib.colors import HexColor
    from reportlab.lib.pagesizes import A4
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    c.setTitle("BOOP - Your local rhythm")
    c.setAuthor("BOOP local companion")
    c.setFillColor(HexColor("#f5f3ec")); c.rect(0, 0, width, height, fill=1, stroke=0)
    c.setFillColor(HexColor("#252d29")); c.setFont("Helvetica-Bold", 24); c.drawString(36, height-54, "boop.")
    c.setFont("Times-Roman", 25); c.drawString(36, height-98, "Your rhythm, over time.")
    rows = bundle.get("days", [])
    date_range = rows[0]["day"] + " to " + rows[-1]["day"] if rows else "No observed range"
    c.setFont("Helvetica", 10); c.drawString(36, height-124, date_range + " | Local NOOP estimates")
    for index, (key, title, unit) in enumerate(METRICS):
        x, y = 36 + index % 2 * 267, height-316-index//2*196
        c.setFillColor(HexColor("#fffdf7")); c.roundRect(x,y,255,172,10,fill=1,stroke=0)
        c.setFillColor(HexColor("#252d29")); c.setFont("Helvetica",11); c.drawString(x+16,y+145,title)
        samples = points(rows,key)
        mean = sum(v for _,v in samples)/len(samples) if samples else None
        c.setFont("Helvetica-Bold",21); c.drawString(x+16,y+113,(f"{mean:.1f}" if mean is not None else "--")+unit)
        if samples:
            lo,hi=min(v for _,v in samples),max(v for _,v in samples)
            previous=None
            c.setStrokeColor(HexColor("#507663")); c.setFillColor(HexColor("#507663")); c.setLineWidth(1.5)
            for i,v in samples:
                px,py=x+16+i/max(1,len(rows)-1)*221,y+42+(v-lo)/max(1,hi-lo)*50
                if previous and previous[0]==i-1:
                    c.line(previous[1],previous[2],px,py)
                c.circle(px,py,1.8,fill=1,stroke=0); previous=(i,px,py)
        else:
            c.setFont("Helvetica",9); c.drawString(x+16,y+60,"More covered observations needed")
        c.setFillColor(HexColor("#6d766e")); c.setFont("Helvetica",8)
        c.drawString(x+16,y+17,f"{len(samples)} of {len(rows)} days observed | gaps preserved")
    c.setFillColor(HexColor("#6d766e")); c.setFont("Helvetica",8)
    c.drawString(36,60,"Computed metrics are estimates. Imported official scores retain their source.")
    c.drawString(36,44,"Your data remains on this laptop. Missing days are unknown.")
    c.showPage(); c.save()
    return buffer.getvalue()


def report_png(bundle):
    import pymupdf
    with pymupdf.open(stream=report_pdf(bundle),filetype="pdf") as document:
        return document[0].get_pixmap(matrix=pymupdf.Matrix(2,2),alpha=False).tobytes("png")
