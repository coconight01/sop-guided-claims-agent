"""Build a token-free GitHub Pages demo from the same Python SOP engine."""
from pathlib import Path
import shutil

ROOT = Path(__file__).parent
OUT = ROOT / "docs"


def build() -> None:
    OUT.mkdir(exist_ok=True)
    for name in ("engine.py", "llm.py"):
        shutil.copy2(ROOT / name, OUT / name)
    fixtures = OUT / "apps" / "insurance_claims" / "fixtures"
    fixtures.mkdir(parents=True, exist_ok=True)
    for source in (ROOT / "apps" / "insurance_claims" / "fixtures").glob("*.json"):
        shutil.copy2(source, fixtures / source.name)
    shutil.copy2(ROOT / "web" / "style.css", OUT / "style.css")
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    html = html.replace('href="/style.css"', 'href="style.css"')
    html = html.replace('<script src="/app.js" defer></script>',
        '<script src="https://cdn.jsdelivr.net/pyodide/v314.0.7/full/pyodide.js"></script>\n'
        '  <script src="demo-engine.js"></script>\n'
        '  <script src="app.js" defer></script>')
    (OUT / "index.html").write_text(html, encoding="utf-8")
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8").replace("fetch(", "demoFetch(")
    (OUT / "app.js").write_text(app, encoding="utf-8")
    (OUT / ".nojekyll").write_text("", encoding="utf-8")


if __name__ == "__main__":
    build()
