"""出版：把 deck.json 轉成單一檔案的 HTML 簡報（可離線開啟、可列印），以及選用的 PowerPoint。

版面由這裡決定，agent 只負責內容。所有文字都經過跳脫；SVG 會再清洗一次。
"""
from __future__ import annotations

import base64
import html
import json
import re
from pathlib import Path

E = html.escape
from .svgsafe import sanitize_svg  # noqa: E402  （白名單清洗；render 的使用者也從這裡匯入）


def _safe_url(url: str) -> str:
    return url if re.match(r"^https?://", url or "", re.I) else "#"


def _image_data_uri(pdir: Path, rel: str) -> str:
    p = (pdir / rel).resolve()
    if pdir.resolve() not in p.parents or not p.is_file() or p.stat().st_size > 4_000_000:
        return ""
    mime = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}.get(p.suffix.lower())
    if not mime:
        return ""
    return f"data:{mime};base64,{base64.b64encode(p.read_bytes()).decode()}"


def _fmt_time(sec: int) -> str:
    return f"{sec // 60}:{sec % 60:02d}"


def _visual_html(v: dict | None, pdir: Path, slide_id: str = "") -> str:
    if not v:
        return ""
    t = v.get("type")
    cap = f'<figcaption>{E(v.get("caption", ""))}</figcaption>' if v.get("caption") else ""
    if t == "svg":
        svg = sanitize_svg(v.get("svg", ""), id_prefix=f"fig-{re.sub(r'[^A-Za-z0-9]', '', slide_id)}")
        return f'<figure class="vis vis-svg">{svg}{cap}</figure>' if svg else ""
    if t == "image":
        uri = _image_data_uri(pdir, v.get("path", ""))
        return f'<figure class="vis vis-img"><img src="{uri}" alt="{E(v.get("caption", ""))}">{cap}</figure>' if uri else ""
    if t in ("video", "link"):
        url = _safe_url(v.get("url", ""))
        start = v.get("start_seconds") or 0
        if t == "video" and start and "youtu" in url and "t=" not in url:
            url += ("&" if "?" in url else "?") + f"t={int(start)}s"
        kind = "影片" if t == "video" else "連結"
        icon = "▶" if t == "video" else "↗"
        when = f'<span class="when">從 {_fmt_time(int(start))} 開始</span>' if t == "video" and start else ""
        host = re.sub(r"^https?://(www\.)?", "", url).split("/")[0]
        return (f'<figure class="vis vis-link"><a href="{E(url)}" target="_blank" rel="noopener noreferrer">'
                f'<span class="play">{icon}</span><span class="lt"><b>{E(v.get("title") or url)}</b>'
                f'<small>{kind}｜{E(host)} {when}</small></span></a>{cap}</figure>')
    return ""


def _bullets(items: list[str] | None) -> str:
    if not items:
        return ""
    return "<ul>" + "".join(f"<li>{E(b)}</li>" for b in items) + "</ul>"


def _slide_html(s: dict, idx: int, total: int, pdir: Path, sec_titles: dict) -> str:
    lay = s["layout"]
    title = E(s["title"])
    vis = _visual_html(s.get("visual"), pdir, str(s.get("id", idx)))
    sec = sec_titles.get(s.get("section_id"), "")
    kicker = f'<p class="kicker">{E(sec)}</p>' if sec and lay not in ("title",) else ""
    notes = f'<aside class="notes"><b>講解備註</b>{E(s.get("notes", ""))}</aside>' if s.get("notes") else ""
    if lay == "title":
        body = f'<div class="cover"><h1>{title}</h1>{"<p class=sub>" + E(s["subtitle"]) + "</p>" if s.get("subtitle") else ""}</div>'
    elif lay == "comparison":
        cols = "".join(f'<div class="col"><h3>{E(c["heading"])}</h3>{_bullets(c["bullets"])}</div>' for c in s.get("columns", []))
        body = f'{kicker}<h2>{title}</h2><div class="cols n{len(s.get("columns", []))}">{cols}</div>'
    elif lay == "quiz":
        opts = "".join(
            f'<li><button type="button" class="opt" data-ok="{int(i == s.get("answer_index"))}">'
            f'<span class="mark">{chr(65 + i)}</span>{E(o)}</button></li>' for i, o in enumerate(s.get("options", [])))
        body = (f'{kicker}<h2>{title}</h2><p class="question">{E(s.get("question", ""))}</p><ol class="opts">{opts}</ol>'
                f'<p class="explain" hidden>{E(s.get("explanation", ""))}</p>')
    elif lay == "references":
        refs = "".join(
            f'<li>{E(r["title"])}' + (f'<br><a href="{E(_safe_url(r["url"]))}" target="_blank" rel="noopener noreferrer">{E(r["url"])}</a>' if r.get("url") else "") + "</li>"
            for r in s.get("references", []))
        body = f'<h2>{title}</h2><ol class="refs">{refs}</ol>'
    elif lay == "diagram" and vis and s.get("bullets"):
        # 示意圖是主角：佔左邊三分之二，條列縮在右側。放在圖的上下會把圖擠到看不清楚
        body = f'{kicker}<h2>{title}</h2><div class="split wide-vis">{vis}{_bullets(s.get("bullets"))}</div>'
    elif lay in ("diagram", "media") or (vis and not s.get("bullets")):
        body = f'{kicker}<h2>{title}</h2><div class="stage">{vis}</div>{_bullets(s.get("bullets"))}'
    elif vis:
        body = f'{kicker}<h2>{title}</h2><div class="split">{_bullets(s.get("bullets"))}{vis}</div>'
    else:
        body = f'{kicker}<h2>{title}</h2>{_bullets(s.get("bullets"))}'
    return (f'<section class="slide l-{lay}" id="{E(s["id"])}" data-i="{idx}">'
            f'<div class="inner">{body}</div>{notes}<footer><span>{idx + 1} / {total}</span></footer></section>')


CSS = """
:root{--ink:#16324F;--body:#24384D;--muted:#5D7186;--line:#D5DEE8;--paper:#FFFFFF;--wash:#EEF3F8;
--accent:#1F8A70;--warn:#B7791F;--bg:#DCE4EC;
--font:"Noto Sans TC","PingFang TC","Microsoft JhengHei","Heiti TC",system-ui,sans-serif}
*{box-sizing:border-box}
html,body{margin:0;height:100%;background:var(--bg);color:var(--body);font-family:var(--font)}
.deck{height:100%;display:flex;align-items:center;justify-content:center;padding:52px 2vmin 14px}
.slide{display:none;position:relative;width:min(96vw,calc((100vh - 70px)*16/9));aspect-ratio:16/9;background:var(--paper);
border-radius:.6vmin;box-shadow:0 1.2vmin 4vmin rgba(22,50,79,.18);overflow:hidden;
font-size:clamp(11px,min(2.05vw,3.65vh),34px);container-type:size}
.slide.on{display:block}
.inner{position:absolute;inset:0;padding:6% 7% 9%;display:flex;flex-direction:column}
.kicker{margin:0 0 .5em;font-size:.6em;color:var(--accent);font-weight:700;letter-spacing:.04em}
h1{font-size:2.3em;line-height:1.25;margin:0;color:var(--ink);font-weight:800;text-wrap:balance}
h2{font-size:1.35em;line-height:1.3;margin:0 0 .7em;color:var(--ink);font-weight:800;text-wrap:balance;
padding-bottom:.45em;border-bottom:.14em solid var(--ink)}
h3{font-size:.95em;margin:0 0 .5em;color:var(--ink)}
ul{margin:0;padding:0;list-style:none;display:flex;flex-direction:column;gap:.62em}
ul li{position:relative;padding-left:1.15em;font-size:.86em;line-height:1.5}
ul li::before{content:"";position:absolute;left:.12em;top:.56em;width:.42em;height:.42em;background:var(--accent);border-radius:50%}
.cover{margin:auto 0;border-left:.5em solid var(--accent);padding-left:1.1em}
.cover .sub{font-size:.95em;color:var(--muted);margin:.8em 0 0}
.l-title{background:linear-gradient(180deg,var(--paper) 62%,var(--wash) 62%)}
.split{display:grid;grid-template-columns:1.05fr .95fr;grid-template-rows:minmax(0,1fr);gap:5%;flex:1;min-height:0}
.split>ul{align-self:center}
.split.wide-vis{grid-template-columns:2.2fr 1fr;gap:4%}
.split.wide-vis>ul li{font-size:.76em}
.stage{flex:1;min-height:0;display:flex;flex-direction:column;justify-content:center;margin-bottom:.5em}
.vis{margin:0;display:flex;flex-direction:column;align-items:center;gap:.4em;min-height:0;width:100%}
.vis-svg,.vis-img{flex:1;height:100%}
.vis svg,.vis img{flex:1;min-height:0;width:100%;max-width:100%;object-fit:contain}
.vis-link{align-self:center}
figcaption{font-size:.56em;color:var(--muted);text-align:center}
.vis-link a{display:flex;align-items:center;gap:1em;text-decoration:none;color:var(--ink);background:var(--wash);
border:.08em solid var(--line);border-radius:.5em;padding:1em 1.3em;width:min(100%,30em)}
.vis-link a:hover,.vis-link a:focus-visible{border-color:var(--accent);outline:none}
.vis-link .play{flex:none;width:2.6em;height:2.6em;border-radius:50%;background:var(--accent);color:#fff;
display:grid;place-items:center;font-size:1.05em}
.vis-link .lt{display:flex;flex-direction:column;gap:.25em;min-width:0}
.vis-link b{font-size:.84em;line-height:1.35}
.vis-link small{font-size:.56em;color:var(--muted)}
.cols{display:grid;gap:4%;flex:1;min-height:0}
.cols.n2{grid-template-columns:1fr 1fr}.cols.n3{grid-template-columns:1fr 1fr 1fr}
.col{background:var(--wash);border-top:.3em solid var(--ink);padding:1em 1.1em;border-radius:0 0 .4em .4em}
.col:nth-child(2){border-top-color:var(--accent)}.col:nth-child(3){border-top-color:var(--warn)}
.question{font-size:.98em;line-height:1.5;margin:0 0 .9em;font-weight:700;color:var(--ink)}
.opts{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:.5em}
.opt{width:100%;text-align:left;font:inherit;font-size:.82em;line-height:1.4;color:var(--body);background:var(--paper);
border:.1em solid var(--line);border-radius:.45em;padding:.6em .9em;display:flex;gap:.8em;align-items:center;cursor:pointer}
.opt:hover,.opt:focus-visible{border-color:var(--ink);outline:none}
.opt .mark{flex:none;width:1.7em;height:1.7em;border-radius:50%;background:var(--wash);display:grid;place-items:center;font-weight:700;color:var(--ink)}
.opt.right{border-color:var(--accent);background:#E6F4EF}.opt.right .mark{background:var(--accent);color:#fff}
.opt.wrong{border-color:#C0473B;background:#FBECEA}.opt.wrong .mark{background:#C0473B;color:#fff}
.explain{font-size:.74em;line-height:1.55;margin:.9em 0 0;padding:.7em 1em;background:var(--wash);border-left:.3em solid var(--accent)}
.refs{margin:0;padding-left:1.4em;font-size:.66em;line-height:1.5;display:flex;flex-direction:column;gap:.6em;overflow:auto}
.refs a{color:var(--accent);word-break:break-all}
footer{position:absolute;left:7%;right:7%;bottom:3.5%;display:flex;justify-content:flex-end;font-size:.5em;color:var(--muted)}
.notes{display:none}
body.show-notes .slide.on .notes{display:block;position:absolute;left:0;right:0;bottom:0;max-height:38%;overflow:auto;
background:rgba(22,50,79,.96);color:#fff;font-size:.6em;line-height:1.6;padding:1em 7%}
.notes b{display:block;font-size:.85em;opacity:.7;margin-bottom:.3em}
.bar{position:fixed;left:0;right:0;bottom:0;height:4px;background:rgba(22,50,79,.15)}
.bar i{display:block;height:100%;background:var(--accent);width:0;transition:width .2s}
.ctl{position:fixed;right:14px;top:12px;display:flex;gap:6px}
.ctl button{font:inherit;font-size:13px;color:var(--ink);background:rgba(255,255,255,.85);border:1px solid var(--line);
border-radius:6px;padding:6px 10px;cursor:pointer}
.ctl button:hover,.ctl button:focus-visible{border-color:var(--ink);outline:none}
body.grid .deck{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:18px;height:auto;padding:60px 18px 30px;align-items:start}
body.grid .slide{display:block;width:100%;font-size:11px;cursor:pointer}
body.grid .slide.on{outline:3px solid var(--accent)}
@media (prefers-reduced-motion:reduce){.bar i{transition:none}}
@media print{
 @page{size:297mm 167mm;margin:0}
 html,body{background:#fff;height:auto}.ctl,.bar{display:none}
 .deck{display:block;padding:0;height:auto}
 .slide{display:block!important;width:297mm;height:167mm;aspect-ratio:auto;box-shadow:none;border-radius:0;page-break-after:always;font-size:19px}
}
"""

JS = """
(function(){
 var slides=[].slice.call(document.querySelectorAll('.slide')),i=0,bar=document.querySelector('.bar i');
 function show(n){i=Math.max(0,Math.min(slides.length-1,n));slides.forEach(function(s,k){s.classList.toggle('on',k===i)});
  bar.style.width=((i+1)/slides.length*100)+'%';try{history.replaceState(null,'','#'+slides[i].id)}catch(e){}}
 var start=slides.findIndex(function(s){return '#'+s.id===location.hash});show(start<0?0:start);
 document.addEventListener('keydown',function(e){
  if(e.target.closest&&e.target.closest('.opt')&&(e.key===' '||e.key==='Enter'))return;
  if(['ArrowRight','PageDown',' '].indexOf(e.key)>=0){e.preventDefault();show(i+1)}
  else if(['ArrowLeft','PageUp'].indexOf(e.key)>=0){e.preventDefault();show(i-1)}
  else if(e.key==='Home')show(0);else if(e.key==='End')show(slides.length-1);
  else if(e.key==='n'||e.key==='N')document.body.classList.toggle('show-notes');
  else if(e.key==='g'||e.key==='G')document.body.classList.toggle('grid');
  else if(e.key==='Escape')document.body.classList.remove('grid');
 });
 document.getElementById('prev').onclick=function(){show(i-1)};
 document.getElementById('next').onclick=function(){show(i+1)};
 document.getElementById('notes').onclick=function(){document.body.classList.toggle('show-notes')};
 document.getElementById('grid').onclick=function(){document.body.classList.toggle('grid')};
 document.getElementById('print').onclick=function(){window.print()};
 slides.forEach(function(s,k){s.addEventListener('click',function(){if(document.body.classList.contains('grid')){document.body.classList.remove('grid');show(k)}})});
 document.querySelectorAll('.opt').forEach(function(b){b.addEventListener('click',function(e){
  e.stopPropagation();var box=b.closest('.inner');
  box.querySelectorAll('.opt').forEach(function(o){o.classList.remove('wrong');if(o.dataset.ok==='1')o.classList.add('right')});
  if(b.dataset.ok!=='1')b.classList.add('wrong');box.querySelector('.explain').hidden=false})});
 var x0=null;document.addEventListener('touchstart',function(e){x0=e.touches[0].clientX},{passive:true});
 document.addEventListener('touchend',function(e){if(x0===null)return;var dx=e.changedTouches[0].clientX-x0;
  if(Math.abs(dx)>50)show(i+(dx<0?1:-1));x0=null},{passive:true});
})();
"""


def render_html(deck: dict, pdir: Path, outline: dict | None = None) -> str:
    slides = deck["slides"]
    sec_titles = {s["id"]: s["title"] for s in (outline or {}).get("sections", [])}
    body = "".join(_slide_html(s, i, len(slides), pdir, sec_titles) for i, s in enumerate(slides))
    title = E(deck.get("meta", {}).get("title", "簡報"))
    return (
        '<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{title}</title><style>{CSS}</style></head><body>"
        '<div class="ctl"><button id="prev" type="button" aria-label="上一張">上一張</button>'
        '<button id="next" type="button" aria-label="下一張">下一張</button>'
        '<button id="notes" type="button" title="快捷鍵 N">講解備註</button>'
        '<button id="grid" type="button" title="快捷鍵 G">總覽</button>'
        '<button id="print" type="button">列印／存成 PDF</button></div>'
        f'<main class="deck">{body}</main><div class="bar"><i></i></div><script>{JS}</script></body></html>'
    )


# ---- PowerPoint（選用） ----------------------------------------------------

def render_pptx(deck: dict, pdir: Path, out: Path) -> list[str]:
    """輸出可編輯的 .pptx。回傳警告清單（例如示意圖無法轉檔）。未安裝 python-pptx 時丟 ImportError。"""
    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.util import Emu, Inches, Pt

    warnings: list[str] = []
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    blank = prs.slide_layouts[6]
    INK, BODY, ACCENT = RGBColor(0x16, 0x32, 0x4F), RGBColor(0x24, 0x38, 0x4D), RGBColor(0x1F, 0x8A, 0x70)

    def text_box(slide, x, y, w, h, lines, size, color=BODY, bold=False, bullet=False):
        tb = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
        tf = tb.text_frame
        tf.word_wrap = True
        for i, line in enumerate(lines):
            p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            run = p.add_run()
            run.text = ("• " if bullet else "") + line
            run.font.size, run.font.bold, run.font.color.rgb = Pt(size), bold, color
            p.space_after = Pt(size * 0.5)
        return tb

    def svg_to_png(svg: str, dest: Path) -> bool:
        try:
            import cairosvg  # type: ignore
            cairosvg.svg2png(bytestring=svg.encode("utf-8"), write_to=str(dest), output_width=1600)
            return True
        except Exception:
            return False

    tmp = out.parent / ".pptx_tmp"
    tmp.mkdir(exist_ok=True)
    for s in deck["slides"]:
        slide = prs.slides.add_slide(blank)
        lay = s["layout"]
        if lay == "title":
            text_box(slide, 0.9, 2.4, 11.5, 1.8, [s["title"]], 40, INK, True)
            if s.get("subtitle"):
                text_box(slide, 0.9, 4.3, 11.5, 1, [s["subtitle"]], 20)
        else:
            text_box(slide, 0.7, 0.45, 11.9, 1.1, [s["title"]], 28, INK, True)
            line = slide.shapes.add_shape(1, Inches(0.7), Inches(1.5), Inches(11.9), Emu(38100))
            line.fill.solid()
            line.fill.fore_color.rgb = INK
            line.line.fill.background()
        v = s.get("visual") or {}
        has_pic, pic_path = False, None
        if v.get("type") == "svg":
            png = tmp / f"{s['id']}.png"
            if svg_to_png(sanitize_svg(v.get("svg", "")), png):
                pic_path = png
            else:
                warnings.append(f"{s['id']}：示意圖無法轉成圖片放進 PowerPoint（需安裝 cairosvg），請參考 HTML 版")
        elif v.get("type") == "image":
            p = (pdir / v.get("path", "")).resolve()
            if p.is_file() and pdir.resolve() in p.parents:
                pic_path = p
        bullets = s.get("bullets") or []
        if pic_path:
            try:
                if bullets:
                    slide.shapes.add_picture(str(pic_path), Inches(6.9), Inches(1.9), width=Inches(5.7))
                else:
                    slide.shapes.add_picture(str(pic_path), Inches(2.2), Inches(1.8), height=Inches(5.0))
                has_pic = True
            except Exception:
                warnings.append(f"{s['id']}：圖片無法放進 PowerPoint")
        y = 1.9
        if lay == "quiz":
            text_box(slide, 0.9, y, 11.5, 1.2, [s.get("question", "")], 22, INK, True)
            text_box(slide, 1.1, y + 1.3, 11, 3, [f"{chr(65 + i)}. {o}" for i, o in enumerate(s.get("options", []))], 20)
        elif lay == "comparison":
            cols = s.get("columns", [])
            w = 11.9 / max(len(cols), 1)
            for i, c in enumerate(cols):
                text_box(slide, 0.7 + i * w, y, w - 0.3, 0.6, [c["heading"]], 22, ACCENT, True)
                text_box(slide, 0.7 + i * w, y + 0.7, w - 0.3, 4, c["bullets"], 18, bullet=True)
        elif lay == "references":
            text_box(slide, 0.9, y, 11.5, 5, [f"{r['title']}  {r.get('url', '')}".strip() for r in s.get("references", [])], 14)
        elif bullets:
            text_box(slide, 0.9, y, 5.7 if has_pic else 11.5, 4.8, bullets, 20, bullet=True)
        if v.get("type") in ("video", "link"):
            tb = text_box(slide, 0.9, 5.9, 11.5, 0.9, [f"{'▶ 影片' if v['type'] == 'video' else '↗ 連結'}：{v.get('title', '')}"], 18, ACCENT, True)
            tb.text_frame.paragraphs[0].runs[0].hyperlink.address = _safe_url(v.get("url", ""))
        note = s.get("notes", "")
        if lay == "quiz":
            note = f"答案：{chr(65 + int(s.get('answer_index', 0)))}。{s.get('explanation', '')}"
        if note:
            slide.notes_slide.notes_text_frame.text = note
    prs.save(str(out))
    for f in tmp.glob("*"):
        f.unlink()
    tmp.rmdir()
    return warnings


def references_md(pdir: Path) -> str:
    """彙整查證與補充階段的所有出處。"""
    seen, lines = set(), ["# 參考資料與出處\n"]
    meta = json.loads((pdir / "project.json").read_text(encoding="utf-8"))
    lines.append("## 上傳的來源\n")
    for s in meta.get("sources", []):
        lines.append(f"- [{s['id']}] {s['filename']}（{'教材' if s.get('role') == 'material' else '參考資料'}）")
    lines.append("\n## 查證與補充引用的外部來源\n")
    for name in ("factcheck.json", "enrich.json"):
        try:
            data = json.loads((pdir / "work" / name).read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            continue
        for item in data.get("claims", []) + data.get("supplements", []):
            for src in item.get("sources", []) or []:
                url = src.get("url")
                if url and url not in seen:
                    seen.add(url)
                    lines.append(f"- {src.get('title', url)} —— {url}")
        for m in data.get("media", []) or []:
            if m.get("url") and m["url"] not in seen:
                seen.add(m["url"])
                lines.append(f"- {m.get('title', m['url'])}（{m.get('kind')}）—— {m['url']}")
    return "\n".join(lines) + "\n"
