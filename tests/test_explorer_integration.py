"""Explorer integration on a fake registry: the real HKCU is never touched here."""
import io
import json
import tempfile
import unittest
import winreg
from pathlib import Path

import explorer_integration as ei

PYTHONW = Path(r"C:\Apps\rsvp\venv\Scripts\pythonw.exe")
SCRIPT = Path(r"C:\Apps\rsvp\open_book.py")
COMMAND = r'"C:\Apps\rsvp\venv\Scripts\pythonw.exe" "C:\Apps\rsvp\open_book.py" "%1"'


class FakeRegistry:
    """Keys under HKCU\\Software\\Classes; names and paths are case-insensitive like the real one."""

    def __init__(self):
        self.keys: dict[str, dict[str, tuple[int, object]]] = {}
        self.fail_after: int | None = None  # set_value calls allowed before an OSError

    @staticmethod
    def _k(path):
        return path.lower()

    def key_exists(self, path):
        return self._k(path) in self.keys

    def create_key(self, path):
        parts = path.split("\\")
        for i in range(1, len(parts) + 1):
            self.keys.setdefault(self._k("\\".join(parts[:i])), {})

    def get_value(self, path, name):
        return self.keys.get(self._k(path), {}).get(name.lower())

    def set_value(self, path, name, kind, data):
        if self.fail_after is not None:
            if self.fail_after == 0:
                raise OSError("simulated registry failure")
            self.fail_after -= 1
        self.create_key(path)
        self.keys[self._k(path)][name.lower()] = (kind, data)

    def delete_value(self, path, name):
        del self.keys[self._k(path)][name.lower()]

    def is_empty(self, path):
        k = self._k(path)
        return not self.keys[k] and not any(other.startswith(k + "\\") for other in self.keys)

    def delete_key(self, path):
        if not self.is_empty(path):
            raise OSError("key has subkeys")
        del self.keys[self._k(path)]

    def snapshot(self):
        return json.dumps(self.keys, sort_keys=True, default=repr)


class ExplorerIntegrationTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.manifest = Path(self._tmp.name) / "explorer_install.json"
        self.reg = FakeRegistry()
        self.notified = 0
        self.out = io.StringIO()

    def tearDown(self):
        self._tmp.cleanup()

    def notify(self):
        self.notified += 1

    def install(self, dry_run=False):
        entries = ei.plan_entries(PYTHONW, SCRIPT)
        return ei.install(self.reg, self.manifest, entries, notify=self.notify, dry_run=dry_run, out=self.out)

    def uninstall(self, dry_run=False):
        return ei.uninstall(self.reg, self.manifest, notify=self.notify, dry_run=dry_run, out=self.out)

    def preexisting(self):
        """What a typical machine already has: an Acrobat default for .pdf and another OpenWith app."""
        self.reg.set_value(".pdf", "", winreg.REG_SZ, "AcroExch.Document.DC")
        self.reg.set_value(r".pdf\OpenWithProgids", "MSEdgePDF", winreg.REG_NONE, b"")
        self.reg.set_value(r"SystemFileAssociations\.pdf\shell\print", "", winreg.REG_SZ, "Друк")

    # ---------- what install writes ----------
    def test_command_quotes_interpreter_script_and_file(self):
        self.assertEqual(ei.command_line(PYTHONW, SCRIPT), COMMAND)

    def test_install_registers_progid(self):
        self.install()
        self.assertEqual(self.reg.get_value(ei.PROGID, ""), (winreg.REG_SZ, "Книга (RSVP читалка)"))
        self.assertEqual(self.reg.get_value(rf"{ei.PROGID}\shell\open", "FriendlyAppName"),
                         (winreg.REG_SZ, "RSVP читалка"))
        self.assertEqual(self.reg.get_value(rf"{ei.PROGID}\shell\open\command", ""), (winreg.REG_SZ, COMMAND))

    def test_install_adds_open_with_and_context_verb_for_each_extension(self):
        self.install()
        self.assertEqual(ei.EXTENSIONS, (".pdf", ".djvu", ".djv", ".epub", ".fb2", ".mobi"))
        for ext in ei.EXTENSIONS:
            with self.subTest(ext):
                self.assertEqual(self.reg.get_value(rf"{ext}\OpenWithProgids", ei.PROGID), (winreg.REG_NONE, b""))
                verb = rf"SystemFileAssociations\{ext}\shell\RSVPReader"
                self.assertEqual(self.reg.get_value(verb, "MUIVerb"), (winreg.REG_SZ, "Читати в RSVP"))
                self.assertEqual(self.reg.get_value(rf"{verb}\command", ""), (winreg.REG_SZ, COMMAND))

    def test_install_never_sets_extension_default(self):
        self.preexisting()
        self.install()
        self.assertEqual(self.reg.get_value(".pdf", ""), (winreg.REG_SZ, "AcroExch.Document.DC"))
        for ext in ei.EXTENSIONS[1:]:
            with self.subTest(ext):
                self.assertIsNone(self.reg.get_value(ext, ""))

    def test_install_notifies_shell(self):
        self.install()
        self.assertEqual(self.notified, 1)

    # ---------- symmetry ----------
    def test_uninstall_restores_registry_exactly(self):
        self.preexisting()
        before = self.reg.snapshot()
        self.install()
        self.assertNotEqual(self.reg.snapshot(), before)
        self.uninstall()
        self.assertEqual(self.reg.snapshot(), before)
        self.assertEqual(self.notified, 2)

    def test_uninstall_on_empty_registry_leaves_nothing(self):
        self.install()
        self.uninstall()
        self.assertEqual(self.reg.keys, {})

    def test_uninstall_keeps_keys_that_existed_before(self):
        self.preexisting()
        self.install()
        self.uninstall()
        self.assertTrue(self.reg.key_exists(r".pdf\OpenWithProgids"))
        self.assertEqual(self.reg.get_value(r".pdf\OpenWithProgids", "MSEdgePDF"), (winreg.REG_NONE, b""))
        self.assertTrue(self.reg.key_exists(r"SystemFileAssociations\.pdf\shell"))

    def test_uninstall_restores_a_value_we_overwrote(self):
        verb = r"SystemFileAssociations\.pdf\shell\RSVPReader"
        self.reg.set_value(verb, "MUIVerb", winreg.REG_SZ, "стара назва")
        before = self.reg.snapshot()
        self.install()
        self.uninstall()
        self.assertEqual(self.reg.snapshot(), before)

    def test_reinstall_then_uninstall_restores_original(self):
        self.preexisting()
        before = self.reg.snapshot()
        self.install()
        self.install()
        self.uninstall()
        self.assertEqual(self.reg.snapshot(), before)

    def test_manifest_is_saved_before_writes_so_a_failed_install_can_be_undone(self):
        self.preexisting()
        before = self.reg.snapshot()
        self.reg.fail_after = 3
        with self.assertRaises(OSError):
            self.install()
        self.assertTrue(self.manifest.exists())
        self.reg.fail_after = None
        self.uninstall()
        self.assertEqual(self.reg.snapshot(), before)

    def test_key_with_foreign_subkey_is_kept_and_reported(self):
        self.install()
        foreign = rf"{ei.PROGID}\shell\edit"
        self.reg.create_key(foreign)
        self.uninstall()
        self.assertTrue(self.reg.key_exists(foreign))
        self.assertIn(ei.PROGID, self.out.getvalue())
        self.assertIsNone(self.reg.get_value(rf"{ei.PROGID}\shell\open\command", ""))

    def test_uninstall_removes_manifest(self):
        self.install()
        self.uninstall()
        self.assertFalse(self.manifest.exists())

    # ---------- dry run and edge cases ----------
    def test_dry_run_install_writes_nothing(self):
        self.preexisting()
        before = self.reg.snapshot()
        self.install(dry_run=True)
        self.assertEqual(self.reg.snapshot(), before)
        self.assertFalse(self.manifest.exists())
        self.assertEqual(self.notified, 0)
        self.assertIn(COMMAND, self.out.getvalue())

    def test_dry_run_uninstall_writes_nothing(self):
        self.install()
        after_install = self.reg.snapshot()
        self.uninstall(dry_run=True)
        self.assertEqual(self.reg.snapshot(), after_install)
        self.assertTrue(self.manifest.exists())
        self.assertEqual(self.notified, 1)

    def test_uninstall_without_manifest_does_nothing(self):
        self.preexisting()
        before = self.reg.snapshot()
        self.assertEqual(self.uninstall(), 1)
        self.assertEqual(self.reg.snapshot(), before)
        self.assertEqual(self.notified, 0)


class CliTest(unittest.TestCase):
    def test_parse_args(self):
        self.assertEqual(ei.parse_args(["install", "--dry-run"]).dry_run, True)
        self.assertEqual(ei.parse_args(["uninstall"]).action, "uninstall")
        with self.assertRaises(SystemExit):
            ei.parse_args(["remove-everything"])


if __name__ == "__main__":
    unittest.main()
