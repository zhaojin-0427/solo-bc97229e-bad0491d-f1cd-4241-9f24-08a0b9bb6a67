"""页面模板（纯字符串拼接，所有动态值经 html.escape，无模板引擎依赖）。"""
from __future__ import annotations

import html
from typing import List, Optional

from .service import WindowResult

_BASE_CSS = """
:root { --line:#d7dde5; --blue:#1f6feb; --bg:#f6f8fa; --ok:#1a7f37; --err:#cf222e; }
* { box-sizing: border-box; }
body { font-family: -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
       margin: 0; color: #1f2328; background: var(--bg); }
header { background: #0d1b2a; color: #fff; padding: 16px 28px; }
header h1 { margin: 0; font-size: 20px; }
header p { margin: 6px 0 0; color: #b8c2cc; font-size: 13px; }
main { max-width: 1100px; margin: 24px auto; padding: 0 20px; }
.card { background: #fff; border: 1px solid var(--line); border-radius: 10px;
        padding: 20px 24px; margin-bottom: 22px; }
.card h2 { margin: 0 0 14px; font-size: 16px; }
label { display: block; font-size: 13px; margin: 10px 0 4px; color: #57606a; }
input[type=number], input[type=file] { width: 100%; max-width: 360px; padding: 8px 10px;
        border: 1px solid var(--line); border-radius: 6px; font-size: 14px; }
button, .btn { display: inline-block; margin-top: 14px; background: var(--blue); color: #fff;
        border: 0; border-radius: 6px; padding: 9px 18px; font-size: 14px; cursor: pointer;
        text-decoration: none; }
.btn.secondary { background: #57606a; }
table.grid { border-collapse: collapse; font-size: 12px; max-width: 100%; }
table.grid th, table.grid td { border: 1px solid var(--line); padding: 3px 7px;
        text-align: right; min-width: 44px; }
table.grid th { background: #eef1f4; position: sticky; top: 0; }
table.grid td.empty { color: #9aa4ae; text-align: center; background: #fafbfc; }
.row { display: flex; gap: 18px; flex-wrap: wrap; }
.row > div { flex: 1; min-width: 220px; }
.flash { padding: 10px 14px; border-radius: 6px; margin-bottom: 16px; font-size: 14px; }
.flash.ok { background: #dafbe1; color: var(--ok); border: 1px solid #aceebb; }
.flash.err { background: #ffebe9; color: var(--err); border: 1px solid #ffabaf; }
dl.stats { display: grid; grid-template-columns: max-content 1fr; gap: 6px 18px; margin: 0; }
dl.stats dt { color: #57606a; font-size: 13px; }
dl.stats dd { margin: 0; font-weight: 600; }
table.commits { width: 100%; border-collapse: collapse; font-size: 13px; }
table.commits th, table.commits td { border-bottom: 1px solid var(--line); padding: 7px 8px;
        text-align: left; }
.small { font-size: 12px; color: #6e7781; }
.scroll { overflow: auto; max-height: 520px; border: 1px solid var(--line); border-radius: 8px; }
code { background: #eef1f4; padding: 1px 5px; border-radius: 4px; }
"""


def _shell(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} - 高程网格仓库</title>
<style>{_BASE_CSS}</style>
</head>
<body>
<header>
  <h1>水利研究所 · 高程网格分块仓库</h1>
  <p>64×64 不可变块存储 · SQLite 索引 · 事务化上传 · 快照隔离窗口取数</p>
</header>
<main>{body}</main>
</body></html>"""


def index_page(stats: dict, commits: list, flash: Optional[tuple[str, str]] = None) -> str:
    flash_html = ""
    if flash:
        kind, msg = flash
        flash_html = f'<div class="flash {kind}">{html.escape(msg)}</div>'

    commit_rows = "".join(
        f"<tr><td>{c['commit_id']}</td><td>{html.escape(c['created_at'])}</td>"
        f"<td>({c['row_offset']}, {c['col_offset']})</td>"
        f"<td>{c['rows']}×{c['cols']}</td><td>{c['cell_count']}</td></tr>"
        for c in commits) or '<tr><td colspan="5" class="small">尚无成功提交</td></tr>'

    body = f"""
{flash_html}
<div class="card">
  <h2>仓库状态</h2>
  <dl class="stats">
    <dt>逻辑网格</dt><dd>{stats['grid_rows']} × {stats['grid_cols']}（厘米，不预分配）</dd>
    <dt>块大小</dt><dd>{stats['block_size']} × {stats['block_size']}</dd>
    <dt>已填格总数</dt><dd>{stats['filled_cells']}</dd>
    <dt>已存块数</dt><dd>{stats['stored_blocks']}</dd>
    <dt>成功提交数</dt><dd>{stats['commit_count']}</dd>
    <dt>当前快照</dt><dd>commit #{stats['snapshot_commit_id']}</dd>
  </dl>
</div>

<div class="card">
  <h2>上传高程 CSV（最多 256×256，无表头整数，单位厘米）</h2>
  <form method="post" action="/upload" enctype="multipart/form-data">
    <div class="row">
      <div>
        <label>CSV 文件</label>
        <input type="file" name="csv" accept=".csv,text/csv" required>
      </div>
      <div>
        <label>左上角行号（从 0 起）</label>
        <input type="number" name="row_offset" min="0" value="0" required>
        <label>左上角列号（从 0 起）</label>
        <input type="number" name="col_offset" min="0" value="0" required>
      </div>
    </div>
    <button type="submit">提交上传（事务化，异值/越界整次拒绝）</button>
    <a class="btn secondary" href="/sample.csv">下载示例 CSV</a>
  </form>
</div>

<div class="card">
  <h2>窗口预览 / 下载（最多 256×256，未填格为空）</h2>
  <form method="get" action="/window">
    <div class="row">
      <div>
        <label>起始行</label><input type="number" name="row" min="0" value="0" required>
        <label>行数</label><input type="number" name="rows" min="1" max="256" value="8" required>
      </div>
      <div>
        <label>起始列</label><input type="number" name="col" min="0" value="0" required>
        <label>列数</label><input type="number" name="cols" min="1" max="256" value="8" required>
      </div>
    </div>
    <button type="submit">预览窗口</button>
  </form>
</div>

<div class="card">
  <h2>最近提交</h2>
  <table class="commits">
    <thead><tr><th>#</th><th>时间 (UTC)</th><th>左上 (行,列)</th><th>尺寸</th><th>格数</th></tr></thead>
    <tbody>{commit_rows}</tbody>
  </table>
  <p class="small">接口：<code>GET /api/stats</code> · <code>GET /health</code> ·
     <code>GET /download?row=&col=&rows=&cols=</code></p>
</div>
"""
    return _shell("首页", body)


def window_page(result: WindowResult) -> str:
    head = "".join(
        f"<th>{result.col_offset + c}</th>" for c in range(result.cols))
    body_rows: List[str] = []
    for r in range(result.rows):
        tds = [f"<th>{result.row_offset + r}</th>"]
        for c in range(result.cols):
            v = result.values[r][c]
            if v is None:
                tds.append('<td class="empty">·</td>')
            else:
                tds.append(f"<td>{v}</td>")
        body_rows.append("<tr>" + "".join(tds) + "</tr>")

    qs = (f"row={result.row_offset}&col={result.col_offset}"
          f"&rows={result.rows}&cols={result.cols}")
    body = f"""
<div class="card">
  <h2>窗口预览：({result.row_offset}, {result.col_offset}) 起
      {result.rows}×{result.cols}</h2>
  <p class="small">读取自同一完整提交快照 commit #<strong>{result.snapshot_commit_id}</strong>
     · 已填 {result.filled_cells} 格 · 仅访问相交块 {result.blocks_read} 个 ·
     未填格以 <code>·</code> 表示，下载 CSV 时为空字段</p>
  <div class="scroll">
    <table class="grid">
      <thead><tr><th>行\\列</th>{head}</tr></thead>
      <tbody>{''.join(body_rows)}</tbody>
    </table>
  </div>
  <a class="btn" href="/download?{qs}">下载该窗口 CSV</a>
  <a class="btn secondary" href="/">返回首页</a>
</div>
"""
    return _shell("窗口预览", body)
