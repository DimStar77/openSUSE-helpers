#!/usr/bin/env python3
"""
Geckopit Documentation Compiler & Viewer.
Transforms USER_GUIDE.md into a self-contained, responsive HTML file
with automatic dark/light mode support, and opens it in default browser.
"""

import os
import sys
import subprocess
from typing import Optional

try:
    import markdown
except ImportError:
    markdown = None

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
:root {{
    --bg-color: #ffffff;
    --text-color: #24292f;
    --border-color: #d0d7de;
    --code-bg: #f6f8fa;
    --table-zebra: #f6f8fa;
    --link-color: #0969da;
    --shadow-color: rgba(0, 0, 0, 0.08);
}}

@media (prefers-color-scheme: dark) {{
    :root {{
        --bg-color: #1e1e1e;
        --text-color: #e0e0e0;
        --border-color: #383838;
        --code-bg: #2d2d2d;
        --table-zebra: #282828;
        --link-color: #58a6ff;
        --shadow-color: rgba(0, 0, 0, 0.4);
    }}
}}

body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans", Cantarell, Ubuntu, sans-serif;
    font-size: 16px;
    line-height: 1.6;
    color: var(--text-color);
    background-color: var(--bg-color);
    margin: 0;
    padding: 0;
}}

.container {{
    max-width: 980px;
    margin: 0 auto;
    padding: 2.5rem 1.5rem;
}}

h1, h2, h3, h4, h5, h6 {{
    margin-top: 1.8rem;
    margin-bottom: 0.8rem;
    font-weight: 600;
    line-height: 1.25;
}}

h1 {{
    font-size: 2.2rem;
    padding-bottom: 0.4rem;
    border-bottom: 1px solid var(--border-color);
}}

h2 {{
    font-size: 1.5rem;
    padding-bottom: 0.3rem;
    border-bottom: 1px solid var(--border-color);
}}

h3 {{
    font-size: 1.25rem;
}}

a {{
    color: var(--link-color);
    text-decoration: none;
}}

a:hover {{
    text-decoration: underline;
}}

pre {{
    background-color: var(--code-bg);
    border: 1px solid var(--border-color);
    border-radius: 6px;
    padding: 1rem;
    overflow-x: auto;
    font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, "Liberation Mono", monospace;
    font-size: 0.9rem;
    line-height: 1.45;
}}

code {{
    background-color: var(--code-bg);
    border: 1px solid var(--border-color);
    border-radius: 4px;
    padding: 0.15em 0.35em;
    font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, "Liberation Mono", monospace;
    font-size: 0.88em;
}}

pre code {{
    background: transparent;
    border: none;
    padding: 0;
    font-size: inherit;
}}

table {{
    border-collapse: collapse;
    width: 100%;
    margin: 1.2rem 0;
    overflow-x: auto;
    display: block;
}}

th, td {{
    padding: 8px 14px;
    border: 1px solid var(--border-color);
    text-align: left;
}}

th {{
    background-color: var(--code-bg);
    font-weight: 600;
}}

tr:nth-child(even) {{
    background-color: var(--table-zebra);
}}

img {{
    max-width: 100%;
    height: auto;
    display: block;
    margin: 1.4rem auto;
    border-radius: 8px;
    box-shadow: 0 4px 16px var(--shadow-color);
    border: 1px solid var(--border-color);
}}

blockquote {{
    margin: 1rem 0;
    padding: 0 1rem;
    color: #57606a;
    border-left: 4px solid var(--border-color);
}}

hr {{
    height: 1px;
    background-color: var(--border-color);
    border: none;
    margin: 2rem 0;
}}
</style>
</head>
<body>
<div class="container">
{content}
</div>
</body>
</html>
"""


def render_markdown_to_html(md_text: str, title: str = "Geckopit User Guide") -> str:
    """Renders raw Markdown text into a complete, styled HTML document."""
    if not markdown:
        raise RuntimeError("python-markdown is required to compile documentation.")

    md = markdown.Markdown(extensions=["tables", "fenced_code", "toc"])
    body_html = md.convert(md_text)
    return HTML_TEMPLATE.format(title=title, content=body_html)


def get_doc_paths(base_dir: Optional[str] = None):
    if not base_dir:
        # Determine from current script location
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    md_path = os.path.join(base_dir, "USER_GUIDE.md")
    html_path = os.path.join(base_dir, "USER_GUIDE.html")
    return md_path, html_path


def build_user_guide_html(base_dir: Optional[str] = None) -> str:
    """Compiles USER_GUIDE.md to USER_GUIDE.html and returns the HTML string."""
    md_path, html_path = get_doc_paths(base_dir)
    if not os.path.isfile(md_path):
        raise FileNotFoundError(f"Source markdown not found: {md_path}")

    with open(md_path, "r", encoding="utf-8") as f:
        md_text = f.read()

    html_content = render_markdown_to_html(md_text, title="Geckopit User Guide: Packaging Workflow Cockpit")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    return html_content


def open_user_guide(parent_window=None, base_dir: Optional[str] = None) -> bool:
    """Opens USER_GUIDE.html in the default system browser, building it first if missing."""
    md_path, html_path = get_doc_paths(base_dir)

    # Re-compile if html is missing or older than md
    if not os.path.isfile(html_path) or (os.path.isfile(md_path) and os.path.getmtime(md_path) > os.path.getmtime(html_path)):
        try:
            build_user_guide_html(base_dir)
        except Exception:
            pass

    target_path = html_path if os.path.isfile(html_path) else md_path
    if not os.path.isfile(target_path):
        return False

    uri = f"file://{os.path.abspath(target_path)}"

    # Try GTK show_uri first if running inside GUI
    if parent_window:
        try:
            import gi
            gi.require_version("Gtk", "4.0")
            gi.require_version("Gdk", "4.0")
            from gi.repository import Gtk, Gdk
            Gtk.show_uri(parent_window, uri, Gdk.CURRENT_TIME)
            return True
        except Exception:
            pass

    # Fallback to xdg-open / python webbrowser
    try:
        import webbrowser
        webbrowser.open(uri)
        return True
    except Exception:
        try:
            subprocess.Popen(["xdg-open", target_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except Exception:
            return False


if __name__ == "__main__":
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    print("Compiling USER_GUIDE.md -> USER_GUIDE.html...")
    out_html = build_user_guide_html(base)
    _, h_path = get_doc_paths(base)
    print(f"✅ Successfully compiled {len(out_html)} bytes to {h_path}")
