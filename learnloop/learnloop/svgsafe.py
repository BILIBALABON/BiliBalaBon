"""示意圖（SVG）的安全處理：只允許白名單內的元素與屬性。

SVG 會被原樣嵌進 HTML 簡報，而它是模型產生的內容（模型又讀過不受信任的教材與網頁），
所以不能用「擋掉已知危險標籤」的方式——HTML 解析器遇到 <div>、<meta>、<form> 之類非 SVG 元素
會跳出 SVG，黑名單擋不完。這裡改成只留下畫圖需要的東西。

  check_svg()     給閘門用：不合格就回傳訊息，讓 agent 改
  sanitize_svg()  給出版用：無論閘門結果如何，輸出前再清一次
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET

SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"

ALLOWED_TAGS = {
    "svg", "g", "defs", "marker", "symbol", "use", "title", "desc",
    "path", "rect", "circle", "ellipse", "line", "polyline", "polygon",
    "text", "tspan", "linearGradient", "radialGradient", "stop", "clipPath",
}

ALLOWED_ATTRS = {
    "id", "viewBox", "preserveAspectRatio", "role", "aria-label", "aria-hidden",
    "x", "y", "x1", "y1", "x2", "y2", "cx", "cy", "r", "rx", "ry", "width", "height", "d", "points",
    "dx", "dy", "rotate", "textLength", "lengthAdjust", "transform",
    "fill", "fill-opacity", "fill-rule", "stroke", "stroke-width", "stroke-opacity", "stroke-dasharray",
    "stroke-dashoffset", "stroke-linecap", "stroke-linejoin", "stroke-miterlimit", "opacity", "vector-effect",
    "font-size", "font-family", "font-weight", "font-style", "text-anchor", "dominant-baseline",
    "alignment-baseline", "letter-spacing", "word-spacing", "text-decoration",
    "marker-start", "marker-mid", "marker-end", "markerWidth", "markerHeight", "markerUnits", "refX", "refY", "orient",
    "offset", "stop-color", "stop-opacity", "gradientUnits", "gradientTransform", "spreadMethod",
    "fx", "fy", "clip-path", "clipPathUnits", "clip-rule", "href",
}

MAX_CHARS = 40_000
_URL_REF = re.compile(r"url\(\s*(?!#)", re.I)  # url(...) 只能指向圖內元素 url(#id)


def _split(tag: str) -> tuple[str, str]:
    if tag.startswith("{"):
        ns, _, local = tag[1:].partition("}")
        return ns, local
    return "", tag


def _bad_value(name: str, value: str) -> bool:
    v = value.strip().lower()
    if name == "href":
        return not v.startswith("#")
    return "javascript:" in v or "data:" in v or bool(_URL_REF.search(v)) or "expression(" in v or "@import" in v


def _parse(svg: str) -> ET.Element | None:
    try:
        root = ET.fromstring(svg)
    except ET.ParseError:
        return None
    return root


def check_svg(svg: str) -> str | None:
    """回傳第一個問題的說明；None 表示合格。"""
    if not isinstance(svg, str) or not svg.strip():
        return "需要完整的 <svg>…</svg>"
    if len(svg) > MAX_CHARS:
        return f"SVG 太大（{len(svg)} 字元，上限 {MAX_CHARS}），請簡化圖形"
    try:
        root = ET.fromstring(svg)
    except ET.ParseError as e:
        return f"SVG 不是合法的 XML：{e}"
    ns, local = _split(root.tag)
    if local != "svg" or ns not in ("", SVG_NS):
        return "最外層必須是 <svg>"
    vb = root.attrib.get("viewBox", "")
    nums = re.findall(r"-?\d+(?:\.\d+)?", vb)
    if len(nums) != 4 or float(nums[2]) <= 0 or float(nums[3]) <= 0:
        return "<svg> 必須有 viewBox 屬性（例如 viewBox=\"0 0 960 540\"），才能縮放"
    width = float(nums[2])
    for el in root.iter():
        ns, local = _split(el.tag)
        if ns not in ("", SVG_NS) or local not in ALLOWED_TAGS:
            return (f"SVG 不可包含 <{local}>。只能使用：rect、circle、ellipse、line、polyline、polygon、path、"
                    "text、tspan、g、defs、marker、linearGradient、stop")
        for k, v in el.attrib.items():
            _, name = _split(k)
            if name.lower().startswith("on"):
                return f"SVG 不可包含事件屬性 {name}"
            if name == "style":
                return "SVG 不可使用 style 屬性，請直接寫成屬性（fill、stroke、font-size、font-weight…）"
            if name in ALLOWED_ATTRS and _bad_value(name, str(v)):
                return f"SVG 的 {name} 只能指向圖內元素（#id 或 url(#id)），不可使用外部連結"

    sizes: list[float] = []

    def walk(el: ET.Element, inherited: float) -> None:  # 字級會從外層元素繼承；都沒寫就是瀏覽器預設的 16
        m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*(px)?\s*$", str(el.attrib.get("font-size", "")))
        size = float(m.group(1)) if m else inherited
        if _split(el.tag)[1] in ("text", "tspan") and ("".join(el.itertext())).strip():
            sizes.append(size)
        for child in el:
            walk(child, size)

    walk(root, 16.0)
    has_text, smallest = bool(sizes), (min(sizes) if sizes else None)
    if not has_text:
        return "示意圖裡沒有任何文字標籤（<text>），請為圖形加上說明文字"
    need = width / 48  # viewBox 寬 960 時約 20：再小，投影到螢幕上就看不清楚了
    if smallest is not None and smallest < need:
        return (f"示意圖的文字太小（最小字級 {smallest:g}，viewBox 寬 {width:g} 時至少要 {need:.0f}）。"
                "請放大文字、減少圖中的字數，把細節移到條列或講解備註")
    return None


def sanitize_svg(svg: str, id_prefix: str = "") -> str:
    """移除白名單以外的一切，回傳可安全嵌入 HTML 的 SVG；無法解析時回傳空字串。

    id_prefix：替圖內所有 id 加上前綴（並同步改寫 #id 與 url(#id) 的引用），
    避免不同投影片的圖互相撞名，也避免蓋掉頁面本身的元素 id。
    """
    if not isinstance(svg, str) or len(svg) > MAX_CHARS * 2:
        return ""
    root = _parse(svg)
    if root is None:
        return ""
    ns, local = _split(root.tag)
    if local != "svg" or ns not in ("", SVG_NS):
        return ""

    def clean(el: ET.Element) -> None:
        for child in list(el):
            cns, clocal = _split(child.tag)
            if not isinstance(child.tag, str) or cns not in ("", SVG_NS) or clocal not in ALLOWED_TAGS:
                el.remove(child)  # 連同子孫一起丟掉
                continue
            clean(child)
        for k in list(el.attrib):
            kns, name = _split(k)
            if kns not in ("", XLINK_NS) or name not in ALLOWED_ATTRS or _bad_value(name, str(el.attrib[k])):
                del el.attrib[k]
        el.tag = f"{{{SVG_NS}}}{_split(el.tag)[1]}"  # 統一命名空間，序列化時不會出現奇怪的前綴

    clean(root)
    if id_prefix:
        ids = {el.attrib["id"] for el in root.iter() if "id" in el.attrib}
        for el in root.iter():
            for k, v in list(el.attrib.items()):
                name = _split(k)[1]
                if name == "id":
                    el.attrib[k] = f"{id_prefix}-{v}"
                elif name == "href" and v[1:] in ids:
                    el.attrib[k] = f"#{id_prefix}-{v[1:]}"
                elif "url(" in v:
                    el.attrib[k] = re.sub(r"url\(\s*#([^)\s]+)\s*\)",
                                          lambda m: f"url(#{id_prefix}-{m.group(1)})" if m.group(1) in ids else m.group(0), v)
    root.attrib.pop("width", None)
    root.attrib.pop("height", None)
    root.set("role", "img")
    ET.register_namespace("", SVG_NS)
    ET.register_namespace("xlink", XLINK_NS)
    return ET.tostring(root, encoding="unicode")
