"""A read-only page of the C program, for reading in a browser.

    python3 docs/make_source_page.py OUT.html

Published as the "PolyPress Source" artifact; republish to the same link.
"""
import html, os, subprocess, re, sys
ROOT = os.path.expanduser('~/Desktop/Projects/Compression/work/csrc')
OUT = sys.argv[1] if len(sys.argv) > 1 else 'polypress-source.html'
FILES = [
  ('ppz.h', 'Shared declarations: containers, tables, every function the files share.'),
  ('ppz_encode.c', 'The encoder: classify columns, derived columns, parents, 2D groups, the text pile.'),
  ('ppz_decode.c', 'The decoder. Treats every archive as hostile.'),
  ('ppz_stream.c', 'Big files: blocks read, compressed and written as a pipeline.'),
  ('ppz_io.c', 'Reading and writing tables: CSV, TSV, JSON, JSON Lines, encodings.'),
  ('ppz_util.c', 'Buffers, xz and bzip2 calls, the JSON parser, derived-column numbers.'),
  ('ppz_thread.c', 'Threads: worker count, parallel-for, background jobs.'),
  ('ppz_main.c', 'The command line: compress, restore, info, convert, stream-compress.'),
  ('build.sh', 'How the program is built.'),
]
commit = subprocess.run(['git','-C',ROOT,'log','-1','--format=%h %ad','--date=short'],capture_output=True,text=True).stdout.strip()
dirty = subprocess.run(['git','-C',ROOT,'status','--short','.'],capture_output=True,text=True).stdout.strip()
total = 0
nav, panes = [], []
for i,(name,desc) in enumerate(FILES):
    src = open(os.path.join(ROOT,name)).read().rstrip('\n')
    n = src.count('\n')+1; total += n
    fid = re.sub(r'[^a-z0-9]','-',name.lower())
    lang = 'bash' if name.endswith('.sh') else 'c'
    nums = '\n'.join(str(k) for k in range(1,n+1))
    nav.append(f'<a href="#{fid}" data-f="{fid}"><span class="fn">{name}</span><span class="ln">{n:,}</span></a>')
    panes.append(f'''<section class="file" id="f-{fid}" hidden>
  <header class="fhead"><h2>{name}</h2><p>{html.escape(desc)}</p><p class="meta">{n:,} lines</p></header>
  <div class="code"><pre class="gut" aria-hidden="true">{nums}</pre><pre class="src"><code class="language-{lang}">{html.escape(src)}</code></pre></div>
</section>''')
stamp = f'commit {commit}' + (' + uncommitted changes' if dirty else '')
page = f'''<title>PolyPress Source</title>
<style>
/* layout: file list on the left, one file at a time on the right; stacks on phones */
:root {{
  --bg: #f7f7f5; --panel: #efefec; --fg: #1d1f21; --muted: #6b6f73; --rule: #dcdcd7; --accent: #2f5d8a;
  --k: #7a3e9d; --s: #2b7a3d; --c: #8a8f94; --n: #b4561b; --t: #1f6f8b; --m: #8a6d1f;
  --mono: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  --sans: -apple-system, BlinkMacSystemFont, "Helvetica Neue", Arial, sans-serif;
}}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{
  --bg: #17191b; --panel: #1e2124; --fg: #dcdfe2; --muted: #8d949a; --rule: #2c3034; --accent: #7fb0dd;
  --k: #c39ae0; --s: #8fc79a; --c: #6f777e; --n: #e3a06a; --t: #79c0d6; --m: #d6c07a; color-scheme: dark }} }}
:root[data-theme="dark"] {{
  --bg: #17191b; --panel: #1e2124; --fg: #dcdfe2; --muted: #8d949a; --rule: #2c3034; --accent: #7fb0dd;
  --k: #c39ae0; --s: #8fc79a; --c: #6f777e; --n: #e3a06a; --t: #79c0d6; --m: #d6c07a; color-scheme: dark }}
body {{ background: var(--bg); color: var(--fg); font: 14px/1.5 var(--sans); }}
.wrap {{ display: grid; grid-template-columns: 230px minmax(0,1fr); min-height: 100vh; }}
nav {{ background: var(--panel); border-right: 1px solid var(--rule); padding: 20px 12px; position: sticky; top: env(safe-area-inset-top, 0px); align-self: start; max-height: 100vh; overflow: auto; }}
nav .title {{ font-weight: 600; padding: 0 8px; }}
nav .stamp {{ color: var(--muted); font-size: 12px; padding: 2px 8px 14px; }}
nav a {{ display: flex; justify-content: space-between; gap: 8px; padding: 5px 8px; border-radius: 4px; color: var(--fg); text-decoration: none; font-family: var(--mono); font-size: 13px; }}
nav a:hover {{ background: var(--bg); }}
nav a.on {{ background: var(--bg); color: var(--accent); }}
nav a:focus-visible {{ outline: 2px solid var(--accent); }}
nav .ln {{ color: var(--muted); font-variant-numeric: tabular-nums; }}
nav .total {{ color: var(--muted); font-size: 12px; padding: 12px 8px 0; border-top: 1px solid var(--rule); margin-top: 10px; }}
main {{ min-width: 0; padding-block: 20px; padding-inline: 24px; }}
.fhead h2 {{ font: 600 18px var(--mono); margin: 0; }}
.fhead p {{ margin: 4px 0 0; max-width: 65ch; }}
.fhead .meta {{ color: var(--muted); font-size: 12px; }}
.code {{ display: flex; margin-top: 16px; border: 1px solid var(--rule); border-radius: 4px; overflow-x: auto; background: var(--bg); }}
.code pre {{ margin: 0; padding: 12px 0; font: 12.5px/1.55 var(--mono); tab-size: 4; }}
.gut {{ text-align: right; color: var(--muted); padding: 12px 10px !important; border-right: 1px solid var(--rule); user-select: none; background: var(--panel); position: sticky; left: 0; }}
.src {{ padding-inline: 14px !important; min-width: 0; }}
.src code {{ white-space: pre; }}
.hljs-keyword, .hljs-built_in {{ color: var(--k); }}
.hljs-string {{ color: var(--s); }}
.hljs-comment {{ color: var(--c); font-style: italic; }}
.hljs-number, .hljs-literal {{ color: var(--n); }}
.hljs-type {{ color: var(--t); }}
.hljs-title {{ color: var(--accent); }}
.hljs-meta, .hljs-meta .hljs-keyword {{ color: var(--m); }}
.hljs-variable {{ color: var(--t); }}
@media (max-width: 720px) {{
  .wrap {{ grid-template-columns: minmax(0,1fr); }}
  nav {{ position: static; max-height: none; border-right: 0; border-bottom: 1px solid var(--rule); }}
  main {{ padding-inline: 16px; }}
}}
</style>
<div class="wrap">
<nav aria-label="Files">
  <div class="title">PolyPress source</div>
  <div class="stamp">{html.escape(stamp)}<br>work/csrc/</div>
  {''.join(nav)}
  <div class="total">{total:,} lines in {len(FILES)} files. Tests (csrc/tests/) not shown.</div>
</nav>
<main>
{''.join(panes)}
</main>
</div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js"></script>
<script>
(function () {{
  var done = {{}};
  function show(id) {{
    var pane = document.getElementById('f-' + id);
    if (!pane) {{ id = '{re.sub(r"[^a-z0-9]","-",FILES[0][0])}'; pane = document.getElementById('f-' + id); }}
    document.querySelectorAll('section.file').forEach(function (s) {{ s.hidden = s !== pane; }});
    document.querySelectorAll('nav a').forEach(function (a) {{ a.classList.toggle('on', a.dataset.f === id); }});
    if (!done[id] && window.hljs) {{ hljs.highlightElement(pane.querySelector('code')); done[id] = 1; }}
    try {{ localStorage.setItem('ppz-file', id); }} catch (e) {{}}
  }}
  document.querySelectorAll('nav a').forEach(function (a) {{
    a.addEventListener('click', function (e) {{ e.preventDefault(); show(a.dataset.f); window.scrollTo(0, 0); }});
  }});
  var start = (location.hash || '').slice(1);
  if (!start) {{ try {{ start = localStorage.getItem('ppz-file') || ''; }} catch (e) {{}} }}
  show(start);
}})();
</script>
'''
open(OUT,'w').write(page)
print(OUT, total, 'lines', len(page)//1024, 'KB')
