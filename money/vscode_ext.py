"""Package the VS Code extension in vscode/ as a .vsix (a zip) and install it with the `code` CLI.

No npm / vsce needed. The built file goes to data/vscode/ (gitignored) because it embeds this
machine's repo path and Python path in config.json.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

from money import config

EXT_DIR = config.ROOT / "vscode"
OUT_DIR = config.DATA_DIR / "vscode"

CONTENT_TYPES = """<?xml version="1.0" encoding="utf-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension=".json" ContentType="application/json"/>
  <Default Extension=".js" ContentType="application/javascript"/>
  <Default Extension=".svg" ContentType="image/svg+xml"/>
  <Default Extension=".vsixmanifest" ContentType="text/xml"/>
</Types>
"""


def _manifest(pkg: dict) -> str:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<PackageManifest Version="2.0.0" xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011">
  <Metadata>
    <Identity Language="en-US" Id="{escape(pkg['name'])}" Version="{escape(pkg['version'])}" Publisher="{escape(pkg['publisher'])}"/>
    <DisplayName>{escape(pkg['displayName'])}</DisplayName>
    <Description xml:space="preserve">{escape(pkg['description'])}</Description>
    <Categories>Other</Categories>
    <Properties>
      <Property Id="Microsoft.VisualStudio.Code.Engine" Value="{escape(pkg['engines']['vscode'])}"/>
      <Property Id="Microsoft.VisualStudio.Code.ExtensionDependencies" Value=""/>
      <Property Id="Microsoft.VisualStudio.Code.ExtensionPack" Value=""/>
      <Property Id="Microsoft.VisualStudio.Code.ExtensionKind" Value="workspace"/>
    </Properties>
  </Metadata>
  <Installation>
    <InstallationTarget Id="Microsoft.VisualStudio.Code"/>
  </Installation>
  <Dependencies/>
  <Assets>
    <Asset Type="Microsoft.VisualStudio.Code.Manifest" Path="extension/package.json" Addressable="true"/>
  </Assets>
</PackageManifest>
"""


def build(python: str | None = None) -> Path:
    pkg = json.loads((EXT_DIR / "package.json").read_text(encoding="utf-8"))
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    vsix = OUT_DIR / f"{pkg['name']}-{pkg['version']}.vsix"
    baked = {"root": str(config.ROOT), "python": python or sys.executable, "port": config.port()}
    with zipfile.ZipFile(vsix, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        z.writestr("extension.vsixmanifest", _manifest(pkg))
        for f in sorted(EXT_DIR.rglob("*")):
            if f.is_file():
                z.write(f, "extension/" + f.relative_to(EXT_DIR).as_posix())
        z.writestr("extension/config.json", json.dumps(baked, indent=2))
    return vsix


def find_code_cli() -> str | None:
    for name in ("code", "code-insiders", "codium"):
        found = shutil.which(name)
        if found:
            return found
    return None


def install() -> tuple[Path, str]:
    vsix = build()
    cli = find_code_cli()
    if not cli:
        return vsix, ("Couldn't find the `code` command. In VS Code: Extensions view > ... menu > "
                      f"Install from VSIX... > {vsix}")
    result = subprocess.run([cli, "--install-extension", str(vsix), "--force"], capture_output=True, text=True)
    if result.returncode != 0:
        return vsix, f"`code --install-extension` failed:\n{result.stdout}{result.stderr}"
    return vsix, "ok"
