#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

from pathlib import Path
from flask import Flask, abort, send_file, Response, request, redirect
import mimetypes
import sqlite3
import json
import html
import config as cfg

ROOT_DIR = Path(__file__).resolve().parent

# --- config ---
DOWNLOAD_KEY = str(getattr(cfg, "DOWNLOAD_KEY", "") or "").strip()
if not DOWNLOAD_KEY:
    raise SystemExit("config.py 里没有配置 DOWNLOAD_KEY")

DB_PATH = Path(str(getattr(cfg, "DB_PATH", "./photos.db") or "./photos.db")).expanduser()
if not DB_PATH.is_absolute():
    DB_PATH = (ROOT_DIR / DB_PATH).resolve()

IMAGE_DIR = Path(str(getattr(cfg, "IMAGE_DIR", "") or "")).expanduser()
if not IMAGE_DIR.is_absolute():
    IMAGE_DIR = (ROOT_DIR / IMAGE_DIR).resolve()

BIN_OUTPUT_DIR = Path(str(getattr(cfg, "BIN_OUTPUT_DIR", "./output") or "./output")).expanduser()
if not BIN_OUTPUT_DIR.is_absolute():
    BIN_OUTPUT_DIR = (ROOT_DIR / BIN_OUTPUT_DIR).resolve()
BIN_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

FLASK_HOST = str(getattr(cfg, "FLASK_HOST", "0.0.0.0") or "0.0.0.0")
FLASK_PORT = int(getattr(cfg, "FLASK_PORT", 8765) or 8765)

# 是否开启照片库 WebUI（跑通后建议关闭，只保留 ESP32 下载接口）
ENABLE_REVIEW_WEBUI = bool(getattr(cfg, "ENABLE_REVIEW_WEBUI", True))

DAILY_PHOTO_QUANTITY = int(getattr(cfg, "DAILY_PHOTO_QUANTITY", 5) or 5)
if DAILY_PHOTO_QUANTITY < 1:
    DAILY_PHOTO_QUANTITY = 1

# review 分页：每页 100 张
REVIEW_PAGE_SIZE = 100

app = Flask(__name__)
def _require_webui_enabled() -> None:
    if not ENABLE_REVIEW_WEBUI:
        abort(404)


def _safe_join(base: Path, rel: str) -> Path:
    """防目录穿越：只允许 base 下的相对路径"""
    p = (base / rel).resolve()
    if not str(p).startswith(str(base.resolve())):
        raise ValueError("path traversal blocked")
    return p


def _send_static_file(p: Path) -> Response:
    if not p.exists() or not p.is_file():
        abort(404)

    if p.suffix.lower() == ".bin":
        return send_file(p, mimetype="application/octet-stream", as_attachment=False)

    mt, _ = mimetypes.guess_type(str(p))
    if mt:
        return send_file(p, mimetype=mt, as_attachment=False)
    return send_file(p, as_attachment=False)


def _make_image_url(path_str: str) -> str:
    """
    把数据库里的本地图片路径转换成 HTTP 可访问的 /images/... 路径。
    要求图片在 IMAGE_DIR 目录下；不在则返回空，避免 file:// 污染与 canvas 跨域。
    """
    try:
        p = Path(path_str).expanduser().resolve()
        rel = p.relative_to(IMAGE_DIR.resolve())
        return "/images/" + str(rel).replace("\\", "/")
    except Exception:
        return ""


# --------------------------
# DB helpers
# --------------------------

def load_rows(page: int = 1, page_size: int = REVIEW_PAGE_SIZE):
    """分页读取 review 数据。返回 (rows, total_count)."""
    if not DB_PATH.exists():
        raise SystemExit(f"找不到数据库文件: {DB_PATH}")

    if page < 1:
        page = 1
    if page_size < 1:
        page_size = REVIEW_PAGE_SIZE

    offset = (page - 1) * page_size

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # 表名统一为 photo_scores（模型无关）
    total_count = c.execute("SELECT COUNT(1) FROM photo_scores").fetchone()[0]

    base_sql = """
        SELECT path,
               caption,
               type,
               memory_score,
               beauty_score,
               reason,
               exif_json,
               width,
               height,
               orientation,
               used_at,
               side_caption
        FROM photo_scores
        ORDER BY COALESCE(memory_score, -1) DESC,
                 COALESCE(beauty_score, -1) DESC,
                 path
        LIMIT ? OFFSET ?
    """

    rows = c.execute(base_sql, (page_size, offset)).fetchall()

    conn.close()
    return rows, int(total_count)


def load_sim_rows():
    if not DB_PATH.exists():
        raise SystemExit(f"找不到数据库文件: {DB_PATH}")

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    rows = c.execute(
        """
        SELECT path,
               caption,
               type,
               memory_score,
               beauty_score,
               reason,
               side_caption,
               exif_json,
               width,
               height,
               orientation,
               used_at,
               exif_gps_lat,
               exif_gps_lon,
               exif_city
        FROM photo_scores
        """
    ).fetchall()

    conn.close()
    return rows


def summarize_exif(exif_json: str | None) -> str:
    if not exif_json:
        return ""

    try:
        data = json.loads(exif_json)
    except Exception:
        return ""

    dtv = data.get("datetime")
    make = data.get("make")
    model = data.get("model")
    iso = data.get("iso")
    exp = data.get("exposure_time")
    fnum = data.get("f_number")
    fl = data.get("focal_length")
    lat = data.get("gps_lat")
    lon = data.get("gps_lon")

    parts = []
    if dtv:
        parts.append(f"时间: {dtv}")
    if make or model:
        cam = f"{make or ''} {model or ''}".strip()
        if cam:
            parts.append(f"设备: {cam}")
    exp_parts = []
    if iso:
        exp_parts.append(f"ISO {iso}")
    if exp:
        exp_parts.append(f"快门 {exp}")
    if fnum:
        exp_parts.append(f"光圈 {fnum}")
    if fl:
        exp_parts.append(f"焦距 {fl}")
    if exp_parts:
        parts.append(" / ".join(exp_parts))
    if lat is not None and lon is not None:
        try:
            parts.append(f"GPS: {float(lat):.5f}, {float(lon):.5f}")
        except Exception:
            parts.append(f"GPS: {lat}, {lon}")

    return "；".join(str(p) for p in parts if p)


def extract_date_from_exif(exif_json: str | None) -> str:
    if not exif_json:
        return ""
    try:
        data = json.loads(exif_json)
    except Exception:
        return ""
    dtv = data.get("datetime")
    if not dtv:
        return ""
    try:
        date_part = str(dtv).split()[0]  # "2018:03:18"
        parts = date_part.replace(":", "-").split("-")
        if len(parts) >= 3:
            return f"{parts[0]}-{parts[1]}-{parts[2]}"
    except Exception:
        return ""
    return ""


# --------------------------
# HTML builders
# --------------------------

def build_html(rows, page: int, page_size: int, total_count: int):
    items_html = []

    for path, caption, ptype, m_score, b_score, reason, exif_json, width, height, orientation, used_at, side_caption in rows:
        safe_caption = html.escape(caption or "").replace("\n", "<br>")
        safe_side = html.escape(side_caption or "").replace("\n", "<br>")
        safe_type = html.escape(ptype or "")
        safe_reason = html.escape(reason or "")
        exif_summary = summarize_exif(exif_json)
        safe_exif = html.escape(exif_summary or "")

        date_str = extract_date_from_exif(exif_json)
        safe_date = html.escape(date_str or "")

        md_str = ""
        if date_str and len(date_str) >= 10:
            md_str = date_str[5:10]
        safe_md = html.escape(md_str or "")

        res_str = ""
        if width and height:
            try:
                res_str = f"{int(width)} x {int(height)}"
            except Exception:
                res_str = f"{width} x {height}"
        orient_str = orientation or ""
        used_str = used_at or ""

        img_uri = _make_image_url(str(path))
        if not img_uri:
            continue

        score_html = ""
        if m_score is not None or b_score is not None:
            parts = []
            if m_score is not None:
                parts.append(f"回忆度: {m_score:.1f}")
            if b_score is not None:
                parts.append(f"美观度: {b_score:.1f}")
            score_line = " / ".join(parts)
            score_html = f'<div class="score">{score_line}</div>'

        type_html = f'<div class="type">类型: {safe_type}</div>' if safe_type else ""
        exif_html = f'<div class="exif">{safe_exif}</div>' if safe_exif else ""
        reason_html = f'<div class="reason">理由: {safe_reason}</div>' if safe_reason else ""

        items_html.append(f"""
        <div class="item"
             data-date="{safe_date}"
             data-md="{safe_md}"
             data-memory="{m_score if m_score is not None else ''}"
             data-beauty="{b_score if b_score is not None else ''}">
            <div class="img-wrap">
                <a class="img-link" href="/sim?img={html.escape(img_uri)}" title="打开该照片的模拟器">
                    <img src="{img_uri}" loading="lazy">
                </a>
            </div>
            {f'<div class="side-under">{safe_side}</div>' if safe_side else ''}
            <div class="meta">
                <div class="path">{html.escape(str(path))}</div>
                {type_html}
                {score_html}
                {reason_html}
                {exif_html}
                <div class="extra">
                    {f"拍摄日期: {safe_date}" if safe_date else ""}
                    {(" · 分辨率: " + html.escape(res_str)) if res_str else ""}
                    {(" · 方向: " + html.escape(orient_str)) if orient_str else ""}
                    {(" · 已上屏: " + html.escape(used_str)) if used_str else ""}
                </div>
                <div class="caption">{safe_caption}</div>
            </div>
        </div>
        """)

    items_str = "\n".join(items_html)
    total_pages = (total_count + page_size - 1) // page_size

    html_str = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <title>InkTime照片数据库</title>
  <style>
    :root{{
      --bg: #0b0c10;
      --panel: rgba(255,255,255,0.06);
      --card: rgba(255,255,255,0.10);
      --card2: rgba(255,255,255,0.08);
      --text: rgba(255,255,255,0.92);
      --muted: rgba(255,255,255,0.62);
      --muted2: rgba(255,255,255,0.48);
      --line: rgba(255,255,255,0.14);
      --accent: #8ab4ff;
      --accent2:#9cffd6;
      --shadow: 0 18px 60px rgba(0,0,0,0.45);
      --shadow2: 0 10px 28px rgba(0,0,0,0.35);
      --radius: 14px;
    }}
    body{{
      margin:0;
      padding:0;
      font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", system-ui, sans-serif;
      background: radial-gradient(1200px 800px at 20% 0%, rgba(138,180,255,0.18), transparent 45%),
                  radial-gradient(900px 700px at 90% 20%, rgba(156,255,214,0.14), transparent 55%),
                  linear-gradient(180deg, #07080b 0%, #0b0c10 40%, #0b0c10 100%);
      color: var(--text);
    }}
    .container{{
      max-width: 1320px;
      margin: 26px auto 60px;
      padding: 0 18px;
    }}
    h1{{
      font-size: 22px;
      margin: 0 0 8px;
      letter-spacing: 0.2px;
    }}
    .subtitle{{
      font-size: 13px;
      color: var(--muted);
      margin: 0 0 14px;
      line-height: 1.35;
    }}

    .controls{{
      display:flex;
      flex-wrap:wrap;
      gap: 10px;
      align-items:center;
      margin: 12px 0 14px;
      font-size: 13px;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: var(--radius);
      padding: 10px 12px;
      box-shadow: var(--shadow2);
      backdrop-filter: blur(10px);
    }}
    .controls label{{
      display:inline-flex;
      align-items:center;
      gap: 8px;
      color: var(--muted);
      white-space: nowrap;
    }}
    .controls select{{
      padding: 7px 10px;
      font-size: 13px;
      color: var(--text);
      background: rgba(255,255,255,0.08);
      border: 1px solid rgba(255,255,255,0.16);
      border-radius: 10px;
      outline: none;
    }}
    .controls select:focus{{
      border-color: rgba(138,180,255,0.7);
      box-shadow: 0 0 0 3px rgba(138,180,255,0.16);
    }}
    .controls button{{
      padding: 7px 12px;
      font-size: 13px;
      cursor: pointer;
      color: var(--text);
      background: rgba(255,255,255,0.10);
      border: 1px solid rgba(255,255,255,0.16);
      border-radius: 10px;
      transition: transform .08s ease, background .15s ease, border-color .15s ease, opacity .15s ease;
    }}
    .controls button:hover{{
      background: rgba(255,255,255,0.14);
      border-color: rgba(255,255,255,0.26);
    }}
    .controls button:active{{
      transform: translateY(1px);
    }}
    .controls button:disabled{{
      opacity: 0.45;
      cursor: not-allowed;
    }}
    .controls.pager{{
      background: rgba(255,255,255,0.05);
    }}

    .status{{
      font-size: 12px;
      color: var(--muted);
      margin: 8px 0 12px;
    }}

    .grid{{
      display:grid;
      grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
      gap: 16px;
    }}
    .item{{
      background: linear-gradient(180deg, var(--card) 0%, var(--card2) 100%);
      border: 1px solid rgba(255,255,255,0.14);
      border-radius: var(--radius);
      overflow: hidden;
      box-shadow: var(--shadow2);
      display:flex;
      flex-direction:column;
      transition: transform .12s ease, border-color .15s ease, box-shadow .15s ease;
    }}
    .item:hover{{
      transform: translateY(-2px);
      border-color: rgba(138,180,255,0.38);
      box-shadow: var(--shadow);
    }}

    .img-wrap{{
      width:100%;
      background: rgba(0,0,0,0.55);
      display:flex;
      align-items:center;
      justify-content:center;
      max-height: 260px;
      overflow:hidden;
    }}
    .img-wrap img{{
      width:100%;
      height:auto;
      display:block;
      object-fit: cover;
      filter: saturate(1.04) contrast(1.02);
    }}
    .img-link{{ display:block; width:100%; }}
    .img-link:link, .img-link:visited{{ text-decoration:none; }}

    .side-under{{
      padding: 10px 12px 0;
      font-size: 12px;
      color: var(--text);
      line-height: 1.45;
      word-break: break-word;
      opacity: 0.92;
    }}

    .meta{{
      padding: 10px 12px 12px;
      font-size: 13px;
      color: var(--text);
    }}
    .path{{
      font-size: 11px;
      color: var(--muted2);
      margin-bottom: 6px;
      word-break: break-all;
    }}
    .type{{
      font-size: 12px;
      color: var(--muted);
      margin-bottom: 4px;
    }}
    .score{{
      font-size: 13px;
      font-weight: 650;
      margin-bottom: 6px;
      color: var(--accent2);
    }}
    .reason{{
      font-size: 12px;
      color: var(--muted);
      margin-bottom: 6px;
      line-height: 1.45;
    }}
    .exif{{
      font-size: 11px;
      color: var(--muted2);
      margin-bottom: 8px;
      line-height: 1.45;
    }}
    .extra{{
      font-size: 11px;
      color: var(--muted2);
      margin-bottom: 8px;
      line-height: 1.45;
    }}
    .caption{{
      margin-top: 6px;
      font-size: 13px;
      line-height: 1.55;
      color: var(--text);
    }}

    @media (max-width: 560px){{
      .container{{ padding: 0 14px; }}
      .grid{{ grid-template-columns: 1fr; }}
      .controls{{ gap: 8px; }}
    }}
  </style>
</head>
<body>
  <div class="container">
    <h1>InkTime照片数据库</h1>
    <div class="subtitle">
      数据库：{html.escape(str(DB_PATH))} · 当前页 {page} · 本页 {len(rows)} 张 · 总计 {total_count} 张（每页 {page_size} 张）
    </div>

    <div class="controls">
      <label>
        月份：
        <select id="monthFilter">
          <option value="">全部</option>
          <option value="01">1 月</option><option value="02">2 月</option><option value="03">3 月</option>
          <option value="04">4 月</option><option value="05">5 月</option><option value="06">6 月</option>
          <option value="07">7 月</option><option value="08">8 月</option><option value="09">9 月</option>
          <option value="10">10 月</option><option value="11">11 月</option><option value="12">12 月</option>
        </select>
      </label>
      <label>
        日期：
        <select id="dayFilter">
          <option value="">全部</option>
          {''.join([f'<option value="{i:02d}">{i} 日</option>' for i in range(1, 32)])}
        </select>
      </label>
      <label>
        排序：
        <select id="sortBy">
          <option value="memory">按回忆度</option>
          <option value="beauty">按美观度</option>
        </select>
      </label>
      <button type="button" id="randomDateBtn">随机一天</button>
    </div>

    <div class="controls pager" style="justify-content: space-between;">
      <div>
        <button type="button" id="prevPageBtn">上一页</button>
        <button type="button" id="nextPageBtn">下一页</button>
      </div>
      <div class="subtitle" style="margin:0;">第 <span id="pageNum">{page}</span> 页 / 共 <span id="pageTotal">{total_pages}</span> 页</div>
    </div>

    <div class="status" id="statusLine"></div>

    <div class="grid">
      {items_str}
    </div>

    <div class="controls pager" style="justify-content: space-between; margin-top: 18px;">
      <div>
        <button type="button" id="prevPageBtnBottom">上一页</button>
        <button type="button" id="nextPageBtnBottom">下一页</button>
      </div>
      <div class="subtitle" style="margin:0;">第 <span>{page}</span> 页 / 共 <span>{total_pages}</span> 页</div>
    </div>
  </div>

  <script>
    document.addEventListener('DOMContentLoaded', function () {{
      const monthSelect = document.getElementById('monthFilter');
      const daySelect = document.getElementById('dayFilter');
      const sortSelect = document.getElementById('sortBy');
      const statusLine = document.getElementById('statusLine');
      const randomBtn = document.getElementById('randomDateBtn');
      const grid = document.querySelector('.grid');
      const items = Array.from(grid.children);

      function mdToDayOfYear(md) {{
        const parts = md.split("-");
        if (parts.length !== 2) return null;
        const m = parseInt(parts[0], 10);
        const d = parseInt(parts[1], 10);
        if (!m || !d) return null;
        const daysBefore = [0, 0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334];
        if (m < 1 || m > 12) return null;
        return daysBefore[m] + d;
      }}

      function applyFilterSort() {{
        const mVal = monthSelect.value;
        const dVal = daySelect.value;
        const sortBy = sortSelect.value;

        let filterMd = "";
        if (mVal && dVal) filterMd = mVal + "-" + dVal;

        let visibleItems = items.filter(function (item) {{
          if (!filterMd) return true;
          const mdAttr = item.getAttribute('data-md') || "";
          return mdAttr === filterMd;
        }});

        items.forEach(function (it) {{ it.style.display = 'none'; }});

        if (sortBy === 'memory' || sortBy === 'beauty') {{
          const key = sortBy === 'memory' ? 'data-memory' : 'data-beauty';
          visibleItems.sort(function (a, b) {{
            const av = parseFloat(a.getAttribute(key) || '-1');
            const bv = parseFloat(b.getAttribute(key) || '-1');
            return bv - av;
          }});
        }}

        visibleItems.forEach(function (it) {{
          grid.appendChild(it);
          it.style.display = '';
        }});

        if (!filterMd) {{ statusLine.textContent = ''; return; }}

        if (visibleItems.length > 0) {{
          statusLine.textContent = '找到 ' + visibleItems.length + ' 张 ' + parseInt(mVal, 10) + ' 月 ' + parseInt(dVal, 10) + ' 日 的照片（仅本页范围）。';
        }} else {{
          const targetDay = mdToDayOfYear(filterMd);
          if (!targetDay) {{ statusLine.textContent = '日期格式无效。'; return; }}

          let bestItem = null;
          let bestDiff = Infinity;

          items.forEach(function (item) {{
            const mdAttr = item.getAttribute('data-md') || '';
            const day = mdToDayOfYear(mdAttr);
            if (!day) return;
            const diff = Math.abs(day - targetDay);
            if (diff < bestDiff) {{ bestDiff = diff; bestItem = item; }}
          }});

          if (!bestItem) {{ statusLine.textContent = '没有找到任何带日期的照片（仅本页范围）。'; return; }}

          const closestDate = bestItem.getAttribute('data-date') || '';
          let countSame = 0;
          items.forEach(function (item) {{
            if (item.getAttribute('data-date') === closestDate) countSame += 1;
          }});

          statusLine.textContent = '本页没有 ' + parseInt(mVal, 10) + ' 月 ' + parseInt(dVal, 10) +
            ' 日 的照片。最近的是 ' + closestDate + '，本页共有 ' + countSame + ' 张。';
        }}
      }}

      function pickRandomDate() {{
        const allMd = items
          .map(function (it) {{ return it.getAttribute('data-md') || ''; }})
          .filter(function (md) {{ return md && md.length === 5 && md.indexOf('-') === 2; }});

        const uniqueMd = Array.from(new Set(allMd));
        if (uniqueMd.length === 0) {{
          statusLine.textContent = '没有任何带日期的照片（仅本页范围），无法随机选择。';
          return;
        }}

        const idx = Math.floor(Math.random() * uniqueMd.length);
        const md = uniqueMd[idx];
        const parts = md.split('-');
        if (parts.length !== 2) {{ statusLine.textContent = '随机日期解析失败。'; return; }}

        monthSelect.value = parts[0];
        daySelect.value = parts[1];
        applyFilterSort();
        statusLine.textContent = '随机跳转到 ' + parseInt(parts[0], 10) + ' 月 ' + parseInt(parts[1], 10) + ' 日 的照片（仅本页范围）。';
      }}

      // 分页按钮
      const currentPage = {page};
      const totalPages = {total_pages};
      const prevBtn = document.getElementById('prevPageBtn');
      const nextBtn = document.getElementById('nextPageBtn');
      const prevBtnBottom = document.getElementById('prevPageBtnBottom');
      const nextBtnBottom = document.getElementById('nextPageBtnBottom');

      function goPage(p) {{
        const url = new URL(window.location.href);
        url.searchParams.set('page', String(p));
        window.location.href = url.toString();
      }}

      if (prevBtn) {{
        prevBtn.disabled = currentPage <= 1;
        prevBtn.addEventListener('click', () => goPage(Math.max(1, currentPage - 1)));
      }}
      if (nextBtn) {{
        nextBtn.disabled = currentPage >= totalPages;
        nextBtn.addEventListener('click', () => goPage(Math.min(totalPages, currentPage + 1)));
      }}
      if (prevBtnBottom) {{
        prevBtnBottom.disabled = currentPage <= 1;
        prevBtnBottom.addEventListener('click', () => goPage(Math.max(1, currentPage - 1)));
      }}
      if (nextBtnBottom) {{
        nextBtnBottom.disabled = currentPage >= totalPages;
        nextBtnBottom.addEventListener('click', () => goPage(Math.min(totalPages, currentPage + 1)));
      }}

      monthSelect.addEventListener('change', applyFilterSort);
      daySelect.addEventListener('change', applyFilterSort);
      sortSelect.addEventListener('change', applyFilterSort);
      randomBtn.addEventListener('click', pickRandomDate);

      applyFilterSort();
    }});
  </script>
</body>
</html>
"""
    return html_str


def build_simulator_html(sim_rows, selected_img: str = ""):
    items = []
    for (
        path,
        caption,
        ptype,
        memory_score,
        beauty_score,
        reason,
        side_caption,
        exif_json,
        width,
        height,
        orientation,
        used_at,
        gps_lat,
        gps_lon,
        exif_city,
    ) in sim_rows:
        date_str = extract_date_from_exif(exif_json)
        if not date_str:
            continue
        img_uri = _make_image_url(str(path))
        if not img_uri:
            continue

        items.append({
            "path": img_uri,
            "date": date_str,
            "memory": float(memory_score) if memory_score is not None else None,
            "beauty": float(beauty_score) if beauty_score is not None else None,
            "city": exif_city or "",
            "lat": gps_lat,
            "lon": gps_lon,
            "side": side_caption or "",
            "caption": caption or "",
            "type": ptype or "",
            "reason": reason or "",
            "exif_json": exif_json or "",
            "exif_summary": summarize_exif(exif_json) if exif_json else "",
            "width": width if width is not None else "",
            "height": height if height is not None else "",
            "orientation": orientation or "",
            "used_at": used_at or "",
        })

    data_json = json.dumps(items, ensure_ascii=False).replace("</", "<\\/") if items else "[]"
    selected_json = json.dumps(selected_img or "", ensure_ascii=False).replace("</", "<\\/")

    html_str = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <title>墨水屏模拟渲染图</title>
  <style>
    :root {{
      --bg: #0b0c10;
      --panel: rgba(255,255,255,0.06);
      --line: rgba(255,255,255,0.14);
      --text: rgba(255,255,255,0.92);
      --muted: rgba(255,255,255,0.62);
      --muted2: rgba(255,255,255,0.48);
      --accent: #8ab4ff;
      --accent2: #9cffd6;
      --shadow: 0 18px 60px rgba(0,0,0,0.45);
      --shadow2: 0 10px 28px rgba(0,0,0,0.35);
      --radius: 14px;
    }}
    body {{
      margin:0; padding:0;
      font-family:-apple-system,BlinkMacSystemFont,system-ui,sans-serif;
      background: radial-gradient(1200px 800px at 20% 0%, rgba(138,180,255,0.18), transparent 45%),
                  radial-gradient(900px 700px at 90% 20%, rgba(156,255,214,0.14), transparent 55%),
                  linear-gradient(180deg, #07080b 0%, #0b0c10 40%, #0b0c10 100%);
      color: var(--text);
    }}
    .container {{
      max-width: 1120px;
      margin: 22px auto 42px;
      padding: 0 16px;
    }}
    a.back {{
      display:inline-block;
      margin-bottom: 10px;
      color: var(--accent);
      text-decoration: none;
    }}
    h1 {{
      font-size: 22px;
      margin: 0 0 8px;
      letter-spacing: 0.2px;
    }}
    .subtitle {{
      font-size: 13px;
      color: var(--muted);
      margin-bottom: 14px;
      line-height: 1.45;
    }}
    .controls {{
      display:flex;
      align-items:center;
      gap: 10px;
      margin-bottom: 14px;
      font-size: 13px;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: var(--radius);
      padding: 10px 12px;
      box-shadow: var(--shadow2);
      backdrop-filter: blur(10px);
    }}
    .controls button {{
      padding: 7px 12px;
      font-size: 13px;
      cursor: pointer;
      color: var(--text);
      background: rgba(255,255,255,0.10);
      border: 1px solid rgba(255,255,255,0.16);
      border-radius: 10px;
      transition: transform .08s ease, background .15s ease, border-color .15s ease, opacity .15s ease;
    }}
    .controls button:hover {{
      background: rgba(255,255,255,0.14);
      border-color: rgba(255,255,255,0.26);
    }}
    .controls button:active {{
      transform: translateY(1px);
    }}

    .status {{
      font-size: 12px;
      color: var(--muted);
      margin: 6px 0 10px;
      min-height: 16px;
    }}

    .preview-wrap {{
      display:flex;
      flex-wrap:wrap;
      gap: 16px;
      align-items: flex-start;
    }}
    .canvas-box {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: var(--radius);
      padding: 10px;
      box-shadow: var(--shadow2);
      backdrop-filter: blur(10px);
    }}
    .canvas-box h2 {{
      font-size: 13px;
      margin: 0 0 8px;
      color: rgba(255,255,255,0.78);
    }}
    #previewCanvas {{
      display:block;
      background:#fff;
      border: 1px solid rgba(255,255,255,0.18);
      border-radius: 10px;
    }}

    .meta-box {{
      flex: 1;
      min-width: 320px;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: var(--radius);
      padding: 12px;
      box-shadow: var(--shadow2);
      backdrop-filter: blur(10px);
    }}
    .meta-title {{
      font-size: 13px;
      color: rgba(255,255,255,0.78);
      margin: 0 0 10px;
    }}
    .kpi {{
      display:grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
      margin-bottom: 12px;
      padding-bottom: 10px;
      border-bottom: 1px solid rgba(255,255,255,0.10);
    }}
    .kpi .cell {{
      background: rgba(255,255,255,0.06);
      border: 1px solid rgba(255,255,255,0.10);
      border-radius: 12px;
      padding: 10px;
    }}
    .kpi .label {{
      font-size: 11px;
      color: var(--muted2);
      margin-bottom: 4px;
    }}
    .kpi .value {{
      font-size: 16px;
      font-weight: 700;
      color: var(--text);
      line-height: 1.2;
      word-break: break-word;
    }}
    .kpi .value.accent {{
      color: var(--accent2);
    }}

    .field {{
      display:flex;
      gap: 10px;
      margin-bottom: 8px;
      line-height: 1.45;
      font-size: 12px;
    }}
    .field .label {{
      width: 92px;
      flex: 0 0 92px;
      color: var(--muted2);
    }}
    .field .value {{
      flex: 1;
      color: var(--text);
      word-break: break-word;
    }}
    .mono {{
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
      font-size: 11px;
      color: rgba(255,255,255,0.80);
      white-space: pre-wrap;
      word-break: break-word;
      background: rgba(0,0,0,0.22);
      border: 1px solid rgba(255,255,255,0.10);
      border-radius: 12px;
      padding: 10px;
    }}

    @media (max-width: 560px) {{
      .kpi {{ grid-template-columns: 1fr; }}
      .meta-box {{ min-width: 0; }}
    }}
  </style>
</head>
<body>
  <div class="container">
    <a class="back" href="/review">← 返回 Review</a>
    <h1>墨水屏模拟渲染图</h1>
    <div class="subtitle">屏幕尺寸：480 x 800。</div>

    <div class="controls">
      <button type="button" id="rerollBtn">同一天换一张</button>
    </div>

    <div class="status" id="statusLine"></div>

    <div class="preview-wrap">
      <div class="canvas-box">
        <h2>预览（渲染到 480 x 800）</h2>
        <canvas id="previewCanvas" width="480" height="800"></canvas>
      </div>

      <div class="meta-box">
        <div class="meta-title">数据库信息</div>

        <div class="kpi">
          <div class="cell">
            <div class="label">日期</div>
            <div class="value" id="kpiDate"></div>
          </div>
          <div class="cell">
            <div class="label">地点</div>
            <div class="value" id="kpiLocation"></div>
          </div>
          <div class="cell">
            <div class="label">回忆度</div>
            <div class="value accent" id="kpiMemory"></div>
          </div>
          <div class="cell">
            <div class="label">美观度</div>
            <div class="value accent" id="kpiBeauty"></div>
          </div>
          <div class="cell" style="grid-column: 1 / -1;">
            <div class="label">文案</div>
            <div class="value" id="kpiSide"></div>
          </div>
        </div>

        <div class="field"><div class="label">图片URL</div><div class="value" id="fieldPath"></div></div>
        <div class="field"><div class="label">原始路径</div><div class="value" id="fieldOrigPath"></div></div>
        <div class="field"><div class="label">类型</div><div class="value" id="fieldType"></div></div>
        <div class="field"><div class="label">主caption</div><div class="value" id="fieldCaption"></div></div>
        <div class="field"><div class="label">理由</div><div class="value" id="fieldReason"></div></div>
        <div class="field"><div class="label">分辨率</div><div class="value" id="fieldRes"></div></div>
        <div class="field"><div class="label">方向</div><div class="value" id="fieldOrientation"></div></div>
        <div class="field"><div class="label">已上屏</div><div class="value" id="fieldUsedAt"></div></div>
        <div class="field"><div class="label">EXIF摘要</div><div class="value" id="fieldExifSummary"></div></div>

        <div class="field" style="margin-top:10px; margin-bottom:6px;">
          <div class="label">EXIF JSON</div>
          <div class="value"></div>
        </div>
        <div class="mono" id="fieldExifJson"></div>
      </div>
    </div>
  </div>

  <script>
    const PHOTOS = {data_json};
    const SELECTED_IMG = {selected_json};

    const byDate = new Map();
    for (const p of PHOTOS) {{
      if (!p.date) continue;
      if (!byDate.has(p.date)) byDate.set(p.date, []);
      byDate.get(p.date).push(p);
    }}
    for (const [d, arr] of byDate.entries()) {{
      arr.sort((a, b) => ((b.memory ?? -1) - (a.memory ?? -1)));
    }}

    const canvas = document.getElementById('previewCanvas');
    const ctx = canvas.getContext('2d');
    const statusLine = document.getElementById('statusLine');

    const kpiDate = document.getElementById('kpiDate');
    const kpiLocation = document.getElementById('kpiLocation');
    const kpiMemory = document.getElementById('kpiMemory');
    const kpiBeauty = document.getElementById('kpiBeauty');
    const kpiSide = document.getElementById('kpiSide');

    const fieldPath = document.getElementById('fieldPath');
    const fieldOrigPath = document.getElementById('fieldOrigPath');
    const fieldType = document.getElementById('fieldType');
    const fieldCaption = document.getElementById('fieldCaption');
    const fieldReason = document.getElementById('fieldReason');
    const fieldRes = document.getElementById('fieldRes');
    const fieldOrientation = document.getElementById('fieldOrientation');
    const fieldUsedAt = document.getElementById('fieldUsedAt');
    const fieldExifSummary = document.getElementById('fieldExifSummary');
    const fieldExifJson = document.getElementById('fieldExifJson');

    let currentDate = null;
    let currentPhoto = null;

    function formatLocation(lat, lon, city) {{
      const c = (city || '').trim();
      if (c.length > 0) return c;
      if (lat == null || lon == null) return '';
      try {{
        return Number(lat).toFixed(5) + ', ' + Number(lon).toFixed(5);
      }} catch (e) {{
        return String(lat) + ', ' + String(lon);
      }}
    }}

    function formatDateDisplay(dateStr) {{
      if (!dateStr) return '';
      const parts = dateStr.split('-');
      if (parts.length < 3) return dateStr;
      const y = parts[0];
      const m = String(parseInt(parts[1], 10));
      const d = String(parseInt(parts[2], 10));
      return y + '.' + m + '.' + d;
    }}

    function safeText(v) {{
      if (v === null || v === undefined) return '';
      return String(v);
    }}

    function wrapText(ctx, text, x, y, maxWidth, lineHeight, maxLines) {{
      if (!text) return;
      const words = text.split(/\\s+/);
      let line = '';
      let lineCount = 0;
      for (let n = 0; n < words.length; n++) {{
        const testLine = line ? (line + ' ' + words[n]) : words[n];
        const metrics = ctx.measureText(testLine);
        if (metrics.width > maxWidth && n > 0) {{
          ctx.fillText(line, x, y);
          line = words[n];
          y += lineHeight;
          lineCount++;
          if (lineCount >= maxLines) break;
        }} else {{
          line = testLine;
        }}
      }}
      if (line && lineCount < maxLines) ctx.fillText(line, x, y);
    }}

    function applyFourColorDither() {{
      const w = canvas.width, h = canvas.height;
      let imgData;
      try {{
        imgData = ctx.getImageData(0, 0, w, h);
      }} catch (e) {{
        statusLine.textContent = '无法从画布读取像素（跨域或图片未走 /images）：' + e;
        return;
      }}
      const data = imgData.data;

      const palette = [
        {{ r: 0, g: 0, b: 0 }},
        {{ r: 255, g: 255, b: 255 }},
        {{ r: 200, g: 0, b: 0 }},
        {{ r: 220, g: 180, b: 0 }}
      ];

      const errR = new Float32Array(w);
      const errG = new Float32Array(w);
      const errB = new Float32Array(w);
      const nextErrR = new Float32Array(w);
      const nextErrG = new Float32Array(w);
      const nextErrB = new Float32Array(w);

      function nearestColor(r, g, b) {{
        let bestIndex = 0;
        let bestDist = Infinity;
        for (let i = 0; i < palette.length; i++) {{
          const pr = palette[i].r, pg = palette[i].g, pb = palette[i].b;
          const dr = r - pr, dg = g - pg, db = b - pb;
          const dist = dr*dr + dg*dg + db*db;
          if (dist < bestDist) {{ bestDist = dist; bestIndex = i; }}
        }}
        return palette[bestIndex];
      }}

      for (let y = 0; y < h; y++) {{
        for (let x = 0; x < w; x++) {{
          const idx = (y * w + x) * 4;

          let r = data[idx] + errR[x];
          let g = data[idx + 1] + errG[x];
          let b = data[idx + 2] + errB[x];

          r = r < 0 ? 0 : (r > 255 ? 255 : r);
          g = g < 0 ? 0 : (g > 255 ? 255 : g);
          b = b < 0 ? 0 : (b > 255 ? 255 : b);

          const nc = nearestColor(r, g, b);

          data[idx] = nc.r;
          data[idx + 1] = nc.g;
          data[idx + 2] = nc.b;

          const er = r - nc.r, eg = g - nc.g, eb = b - nc.b;

          if (x + 1 < w) {{
            errR[x + 1] += er * (7 / 16);
            errG[x + 1] += eg * (7 / 16);
            errB[x + 1] += eb * (7 / 16);
          }}
          if (y + 1 < h) {{
            if (x > 0) {{
              nextErrR[x - 1] += er * (3 / 16);
              nextErrG[x - 1] += eg * (3 / 16);
              nextErrB[x - 1] += eb * (3 / 16);
            }}
            nextErrR[x] += er * (5 / 16);
            nextErrG[x] += eg * (5 / 16);
            nextErrB[x] += eb * (5 / 16);
            if (x + 1 < w) {{
              nextErrR[x + 1] += er * (1 / 16);
              nextErrG[x + 1] += eg * (1 / 16);
              nextErrB[x + 1] += eb * (1 / 16);
            }}
          }}
        }}

        if (y + 1 < h) {{
          for (let i = 0; i < w; i++) {{
            errR[i] = nextErrR[i]; errG[i] = nextErrG[i]; errB[i] = nextErrB[i];
            nextErrR[i] = 0; nextErrG[i] = 0; nextErrB[i] = 0;
          }}
        }}
      }}

      ctx.putImageData(imgData, 0, 0);
    }}

    function updateMeta(photo) {{
      if (!photo) {{
        kpiDate.textContent = '';
        kpiLocation.textContent = '';
        kpiMemory.textContent = '';
        kpiBeauty.textContent = '';
        kpiSide.textContent = '';

        fieldPath.textContent = '';
        fieldOrigPath.textContent = '';
        fieldType.textContent = '';
        fieldCaption.textContent = '';
        fieldReason.textContent = '';
        fieldRes.textContent = '';
        fieldOrientation.textContent = '';
        fieldUsedAt.textContent = '';
        fieldExifSummary.textContent = '';
        fieldExifJson.textContent = '';
        return;
      }}

      const loc = formatLocation(photo.lat, photo.lon, photo.city);
      const mem = (photo.memory === null || photo.memory === undefined) ? '' : Number(photo.memory).toFixed(1);
      const bea = (photo.beauty === null || photo.beauty === undefined) ? '' : Number(photo.beauty).toFixed(1);

      kpiDate.textContent = safeText(photo.date);
      kpiLocation.textContent = safeText(loc);
      kpiMemory.textContent = safeText(mem);
      kpiBeauty.textContent = safeText(bea);
      kpiSide.textContent = safeText(photo.side);

      fieldPath.textContent = safeText(photo.path);
      fieldOrigPath.textContent = safeText(photo.orig_path || '');
      fieldType.textContent = safeText(photo.type);
      fieldCaption.textContent = safeText(photo.caption);
      fieldReason.textContent = safeText(photo.reason);

      const res = (safeText(photo.width) || safeText(photo.height)) ? (safeText(photo.width) + ' x ' + safeText(photo.height)) : '';
      fieldRes.textContent = res;
      fieldOrientation.textContent = safeText(photo.orientation);
      fieldUsedAt.textContent = safeText(photo.used_at);

      fieldExifSummary.textContent = safeText(photo.exif_summary);
      fieldExifJson.textContent = safeText(photo.exif_json);
    }}

    function drawPreview(photo) {{
      if (!photo) {{
        statusLine.textContent = '未指定照片。请从 /review 点击某张照片进入模拟器。';
        return;
      }}

      statusLine.textContent = ''; // 正常情况不显示废话

      canvas.width = 480;
      canvas.height = 800;

      ctx.fillStyle = '#FFFFFF';
      ctx.fillRect(0, 0, canvas.width, canvas.height);

      const img = new Image();
      img.onload = function() {{
        const textAreaHeight = 100;
        const imgAreaWidth = canvas.width;
        const imgAreaHeight = canvas.height - textAreaHeight;

        const scale = Math.max(imgAreaWidth / img.width, imgAreaHeight / img.height);
        const drawW = img.width * scale;
        const drawH = img.height * scale;
        const dx = (imgAreaWidth - drawW) / 2;
        const dy = (imgAreaHeight - drawH) / 2;

        ctx.save();
        ctx.beginPath();
        ctx.rect(0, 0, imgAreaWidth, imgAreaHeight);
        ctx.clip();
        ctx.drawImage(img, dx, dy, drawW, drawH);
        ctx.restore();

        const paddingX = 24;
        const textAreaTop = canvas.height - textAreaHeight + 10;
        const textAreaWidth = canvas.width - 2 * paddingX;

        ctx.fillStyle = '#000';
        ctx.textBaseline = 'top';

        ctx.font = 'bold 22px sans-serif';
        wrapText(ctx, photo.side || '', paddingX, textAreaTop, textAreaWidth, 24, 2);

        const dateDisplay = formatDateDisplay(photo.date);
        const locDisplay = formatLocation(photo.lat, photo.lon, photo.city);

        ctx.font = '16px sans-serif';
        const secondLineY = textAreaTop + 54;
        ctx.fillText(dateDisplay, paddingX, secondLineY);

        const locWidth = ctx.measureText(locDisplay).width;
        let locX = paddingX + textAreaWidth - locWidth;
        if (locX < paddingX) locX = paddingX;
        ctx.fillText(locDisplay, locX, secondLineY);

        applyFourColorDither();
      }};
      img.onerror = function() {{
        statusLine.textContent = '图片加载失败：' + photo.path;
      }};
      img.src = photo.path;
    }}

    function pickPhotoFromDate(date) {{
      const arr = byDate.get(date) || [];
      if (!arr.length) return null;

      const THRESHOLD = 60;
      const candidates = arr.filter(p => p.memory != null && p.memory > THRESHOLD);
      if (candidates.length > 0) {{
        const idx = Math.floor(Math.random() * candidates.length);
        return {{ photo: candidates[idx], dateUsed: date }};
      }}

      // 兜底：当天随便挑
      const idx = Math.floor(Math.random() * arr.length);
      return {{ photo: arr[idx], dateUsed: date, fallbackNoThreshold: true }};
    }}

    function getPreviousDateStr(dateStr) {{
      if (!dateStr) return null;
      const parts = dateStr.split('-');
      if (parts.length < 3) return null;
      const y = parseInt(parts[0], 10);
      const m = parseInt(parts[1], 10);
      const d = parseInt(parts[2], 10);
      if (!y || !m || !d) return null;
      const dt = new Date(y, m - 1, d);
      dt.setDate(dt.getDate() - 1);
      const yy = dt.getFullYear();
      const mm = String(dt.getMonth() + 1).padStart(2, '0');
      const dd = String(dt.getDate()).padStart(2, '0');
      return yy + '-' + mm + '-' + dd;
    }}

    function pickPhotoWithLookback(baseDate) {{
      if (!baseDate) return null;
      let date = baseDate;
      const MAX_LOOKBACK = 30;

      for (let i = 0; i < MAX_LOOKBACK; i++) {{
        const picked = pickPhotoFromDate(date);
        if (picked && picked.photo) return picked;
        const prev = getPreviousDateStr(date);
        if (!prev) break;
        date = prev;
      }}

      // 最终兜底：目标日期没找到 map，啥也不干
      return null;
    }}

    function findSelectedPhoto() {{
      if (!SELECTED_IMG) return null;
      for (const p of PHOTOS) {{
        if (p.path === SELECTED_IMG) return p;
      }}
      return null;
    }}

    function onRerollSameDay() {{
      if (!currentDate) {{
        statusLine.textContent = '请从 /review 点击某张照片进入模拟器。';
        return;
      }}

      const pick = pickPhotoWithLookback(currentDate);
      if (!pick || !pick.photo) {{
        statusLine.textContent = '该日期及向前 30 天内没有可用照片。';
        return;
      }}

      // 如果刚好又抽到自己，尝试再抽几次
      let tries = 0;
      let chosen = pick;
      while (tries < 6 && chosen && chosen.photo && currentPhoto && chosen.photo.path === currentPhoto.path) {{
        const again = pickPhotoWithLookback(currentDate);
        if (!again || !again.photo) break;
        chosen = again;
        tries++;
      }}

      currentPhoto = chosen.photo;
      updateMeta(currentPhoto);
      drawPreview(currentPhoto);
    }}

    document.getElementById('rerollBtn').addEventListener('click', onRerollSameDay);

    // 默认进入：如果从 review 点进来，则显示该照片；否则提示用户从 review 进入
    const initPhoto = findSelectedPhoto();
    if (!initPhoto) {{
      updateMeta(null);
      drawPreview(null);
    }} else {{
      currentDate = initPhoto.date;
      currentPhoto = initPhoto;
      updateMeta(currentPhoto);
      drawPreview(currentPhoto);
    }}
  </script>
</body>
</html>
"""
    return html_str


# --------------------------
# Routes
# --------------------------

@app.get("/")
def index():
    if ENABLE_REVIEW_WEBUI:
        return redirect("/review")
    return Response("InkTime server running. WebUI disabled.", mimetype="text/plain; charset=utf-8")


@app.get("/review")
def review():
    _require_webui_enabled()
    try:
        page = int(request.args.get("page", "1"))
    except Exception:
        page = 1

    rows, total_count = load_rows(page=page, page_size=REVIEW_PAGE_SIZE)
    if not rows:
        return Response(
            "数据库里没有可展示的数据。请先运行你的分析脚本生成评分与文案。",
            status=404,
            mimetype="text/plain; charset=utf-8",
        )

    html_str = build_html(rows, page=page, page_size=REVIEW_PAGE_SIZE, total_count=total_count)
    return Response(html_str, mimetype="text/html; charset=utf-8")


@app.get("/sim")
def sim():
    _require_webui_enabled()
    selected_img = request.args.get("img", "")
    sim_rows = load_sim_rows()

    html_str = build_simulator_html(sim_rows, selected_img=selected_img)
    return Response(html_str, mimetype="text/html; charset=utf-8")


@app.get("/images/<path:subpath>")
def images(subpath: str):
    _require_webui_enabled()
    try:
        p = _safe_join(IMAGE_DIR, subpath)
    except Exception:
        abort(400)
    return _send_static_file(p)


@app.get("/static/inktime/<key>/photo_<int:idx>.bin")
def esp_photo(key: str, idx: int):
    if key != DOWNLOAD_KEY:
        abort(404)
    if idx < 0 or idx >= DAILY_PHOTO_QUANTITY:
        abort(404)
    p = BIN_OUTPUT_DIR / f"photo_{idx}.bin"
    return _send_static_file(p)


@app.get("/static/inktime/<key>/latest.bin")
def esp_latest(key: str):
    if key != DOWNLOAD_KEY:
        abort(404)
    p = BIN_OUTPUT_DIR / "latest.bin"
    return _send_static_file(p)


@app.get("/static/inktime/<key>/preview.png")
def esp_preview(key: str):
    if key != DOWNLOAD_KEY:
        abort(404)
    p = BIN_OUTPUT_DIR / "preview.png"
    return _send_static_file(p)


@app.get("/files/")
@app.get("/files/<path:subpath>")
def browse(subpath: str = ""):
    _require_webui_enabled()
    try:
        p = _safe_join(BIN_OUTPUT_DIR, subpath)
    except Exception:
        abort(400)

    if p.is_file():
        return _send_static_file(p)

    if not p.exists() or not p.is_dir():
        abort(404)

    items = []
    for child in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
        name = child.name + ("/" if child.is_dir() else "")
        rel = child.relative_to(BIN_OUTPUT_DIR)
        href = "/files/" + str(rel).replace("\\", "/")
        items.append(f'<li><a href="{html.escape(href)}">{html.escape(name)}</a></li>')

    up = ""
    if p != BIN_OUTPUT_DIR:
        parent_rel = p.parent.relative_to(BIN_OUTPUT_DIR)
        up_href = "/files/" + str(parent_rel).replace("\\", "/")
        up = f'<a href="{html.escape(up_href)}">⬅ 返回上级</a><br><br>'

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>InkTime Files</title>
<style>
body {{ font-family: -apple-system,BlinkMacSystemFont,system-ui,sans-serif; padding: 24px; }}
ul {{ line-height: 1.8; }}
code {{ background:#f2f2f2; padding:2px 6px; border-radius:4px; }}
</style>
</head>
<body>
<h3>输出目录浏览</h3>
<p>当前：<code>{html.escape(str(p.relative_to(BIN_OUTPUT_DIR) if p != BIN_OUTPUT_DIR else "."))}</code></p>
{up}
<ul>
{''.join(items)}
</ul>
</body>
</html>
"""


if __name__ == "__main__":
    mimetypes.add_type("application/octet-stream", ".bin")
    print(f"[InkTime] DB: {DB_PATH}")
    print(f"[InkTime] IMAGE_DIR: {IMAGE_DIR}")
    print(f"[InkTime] OUT: {BIN_OUTPUT_DIR}")
    print(f"[InkTime] key: {DOWNLOAD_KEY}")
    print(f"[InkTime] listen: {FLASK_HOST}:{FLASK_PORT}")
    print(f"[InkTime] open: http://127.0.0.1:{FLASK_PORT}/  (本机)")
    app.run(host=FLASK_HOST, port=FLASK_PORT, debug=False)