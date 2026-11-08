#!/usr/bin/env python3
"""Package the reviewed scripture into public reader assets and an AI quote corpus."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def build(source: Path, repo: Path = REPO) -> dict[str, int]:
    document = json.loads((source / "逐节对应数据.json").read_text(encoding="utf-8"))
    items = document["items"]
    if len(items) != 2029 or len({item["key"] for item in items}) != 2029:
        raise ValueError("the complete reviewed edition must contain 2029 unique verses")
    records = []
    for item in items:
        text = item["parody"].strip()
        if not text or len(text) > 500:
            raise ValueError("invalid scripture quote")
        records.append({key: item[key] for key in ("key", "book", "chapter", "verse")} | {
            "text": text,
        })
    html = (source / "牛牛圣经_逐节对照.html").read_text(encoding="utf-8")
    styles = re.findall(r"<style>([\s\S]*?)</style>", html)
    scripts = re.findall(r"<script>([\s\S]*?)</script>", html)
    if len(styles) != 1 or len(scripts) != 1 or html.count('<article class="verse"') != 2029:
        raise ValueError("unexpected reviewed reader structure")
    html = html.replace(f"<style>{styles[0]}</style>",
                        '<link rel="stylesheet" href="./scripture.css">')
    html = html.replace(f"<script>{scripts[0]}</script>",
                        '<script src="./scripture.js" defer></script>')
    html = html.replace('<main><header class="cover">',
                        '<main><nav class="reader-nav" aria-label="阅读导航">'
                        '<a href="./">← 牛牛主页</a>'
                        '<a href="./scripture.md" download="牛牛圣经_群译本.md">下载群译全文 ↓</a>'
                        '</nav><header class="cover">')
    html = html.replace('style="position:absolute;left:-10000px"', 'class="sr-only"')
    html = html.replace('<details class="preface" open>', '<details class="preface">')
    html = html.replace('红字群译本 · 逐节对照版</p>',
                        '红字群译本 · 逐节对照版 · 又名《牛子圣经》</p>')
    html = html.replace('本文件可离线阅读，不访问服务器、不收集阅读记录。',
                        '可搜索经节、展开原典，也可下载群译全文。本站不收集阅读记录。')
    html = html.replace(
        '此处内容尚未推送群公告，也未作为机器人规则。条数、字数与覆盖以随书校验记录为准。',
        '愿每句“我的”，不只说给战绩看；愿每句“下把”，真有人一起。',
    )
    css = styles[0] + (
        '\n.reader-nav{display:flex;justify-content:space-between;gap:16px;'
        'font:14px/1.8 system-ui;padding-block:6px 18px;border-bottom:1px solid var(--line)}'
        '.reader-nav a{text-decoration:none}.reader-nav a:hover{text-decoration:underline}'
        '.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;'
        'clip:rect(0,0,0,0);white-space:nowrap;border:0}'
        '@media print{.reader-nav{display:none}}\n'
    )
    public = repo / "src/pwcs2_bot/assets/community_home"
    private_corpus = repo / "src/pwcs2_bot/assets/scripture"
    public.mkdir(parents=True, exist_ok=True)
    private_corpus.mkdir(parents=True, exist_ok=True)
    (public / "scripture.html").write_text(html, encoding="utf-8", newline="\n")
    (public / "scripture.css").write_text(css, encoding="utf-8", newline="\n")
    (public / "scripture.js").write_text(scripts[0], encoding="utf-8", newline="\n")
    reading = (source / "牛牛圣经_群译本.md").read_text(encoding="utf-8")
    (public / "scripture.md").write_text(reading, encoding="utf-8", newline="\n")
    corpus = {"title": "牛牛圣经", "edition": document["date"], "items": records}
    (private_corpus / "verses.json").write_text(
        json.dumps(corpus, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8", newline="\n",
    )
    return {"verses": len(records), "public_files": 4}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True,
                        help="directory containing the reviewed final files")
    args = parser.parse_args()
    print(json.dumps(build(args.source), ensure_ascii=False))
