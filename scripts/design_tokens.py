"""Generate CSS from the maintained DESIGN.md tokens; --check detects drift."""

import json
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
data = json.loads((root / "DESIGN.md").read_text().split("---", 2)[1])
lines = ["/* Generated from DESIGN.md; use scripts/design_tokens.py to update. */", ":root {"]
for name, value in data["colors"].items():
    lines.append(f"  --color-{name}: {value};")
for name, value in data["typography"].items():
    lines.append(f"  --font-{name}: {value['fontFamily']};")
for name, value in data["spacing"].items():
    lines.append(f"  --space-{name}: {value};")
lines.extend(["  --radius: " + data["rounded"]["DEFAULT"] + ";", "}", ""])
text = "\n".join(lines)
destination = root / "frontend" / "src" / "tokens.css"
if "--check" in sys.argv:
    if not destination.exists() or destination.read_text() != text:
        raise SystemExit("Generated design tokens are stale")
else:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text)
