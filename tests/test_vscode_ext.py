import json
import zipfile

from money import vscode_ext
from tests.helpers import TempDBTest


class VsixBuildTest(TempDBTest):
    def setUp(self):
        super().setUp()
        self._out = vscode_ext.OUT_DIR
        vscode_ext.OUT_DIR = self.tmp / "vscode"

    def tearDown(self):
        vscode_ext.OUT_DIR = self._out
        super().tearDown()

    def test_vsix_contains_extension_and_baked_config(self):
        vsix = vscode_ext.build(python="/usr/bin/python3")
        with zipfile.ZipFile(vsix) as z:
            names = set(z.namelist())
            self.assertTrue({"extension.vsixmanifest", "[Content_Types].xml", "extension/package.json",
                             "extension/extension.js", "extension/media/money.svg",
                             "extension/config.json"} <= names)
            pkg = json.loads(z.read("extension/package.json"))
            baked = json.loads(z.read("extension/config.json"))
            manifest = z.read("extension.vsixmanifest").decode()
        self.assertEqual(pkg["main"], "./extension.js")
        self.assertEqual(baked["python"], "/usr/bin/python3")
        self.assertTrue((vscode_ext.config.ROOT / "money" / "__main__.py").exists())
        self.assertEqual(baked["root"], str(vscode_ext.config.ROOT))
        self.assertIn(f'Id="{pkg["name"]}"', manifest)
        self.assertIn(f'Publisher="{pkg["publisher"]}"', manifest)
