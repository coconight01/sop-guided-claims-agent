"""Build a token-free GitHub Pages demo from the same Python SOP engine."""
from pathlib import Path
from hashlib import sha256
import re
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
    adapter_path = OUT / "demo-engine.js"
    adapter = adapter_path.read_text(encoding="utf-8")
    engine_revision = sha256((OUT / "engine.py").read_bytes()).hexdigest()[:10]
    adapter = re.sub(r'const ASSET_VERSION = "[^"]*";',
                     f'const ASSET_VERSION = "{engine_revision}";', adapter)
    adapter_path.write_text(adapter, encoding="utf-8")
    adapter_revision = sha256(adapter.encode()).hexdigest()[:10]
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    css_revision = sha256((OUT / "style.css").read_bytes()).hexdigest()[:10]
    app_revision = sha256((ROOT / "web" / "app.js").read_bytes()).hexdigest()[:10]
    html = html.replace('href="/style.css"', f'href="style.css?v={css_revision}"')
    html = html.replace('<script src="/app.js" defer></script>',
        '<script src="https://cdn.jsdelivr.net/pyodide/v314.0.7/full/pyodide.js"></script>\n'
        f'  <script src="demo-engine.js?v={adapter_revision}"></script>\n'
        f'  <script src="app.js?v={app_revision}" defer></script>')
    (OUT / "index.html").write_text(html, encoding="utf-8")
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8").replace("fetch(", "demoFetch(")
    (OUT / "app.js").write_text(app, encoding="utf-8")
    (OUT / ".nojekyll").write_text("", encoding="utf-8")


if __name__ == "__main__":
    build()
