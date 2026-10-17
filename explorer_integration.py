"""Explorer integration for the current user: `venv\\Scripts\\python explorer_integration.py install|uninstall [--dry-run]`.

Writes only under HKCU\\Software\\Classes (no admin rights):
  - ProgID RSVPReader.Book with an `open` command -> «Відкрити за допомогою → RSVP читалка»;
  - <ext>\\OpenWithProgids\\RSVPReader.Book (empty REG_NONE) -> the app is offered for the type,
    while the extension's default program stays as it was;
  - SystemFileAssociations\\<ext>\\shell\\RSVPReader -> «Читати в RSVP» in the context menu
    (Windows 11: under «Показати додаткові параметри»).
Before writing, explorer_install.json records every key it creates and every value it sets,
with the previous value; `uninstall` undoes exactly that list.
"""
from __future__ import annotations

import argparse
import json
import sys
import winreg
from pathlib import Path
from typing import NamedTuple, Protocol, TextIO

ROOT = Path(__file__).resolve().parent
MANIFEST_PATH = ROOT / "explorer_install.json"
PYTHONW = ROOT / "venv" / "Scripts" / "pythonw.exe"
LAUNCHER = ROOT / "open_book.py"

PROGID = "RSVPReader.Book"
PROGID_NAME = "Книга (RSVP читалка)"
APP_NAME = "RSVP читалка"
VERB_KEY = "RSVPReader"
VERB_NAME = "Читати в RSVP"
EXTENSIONS = (".pdf", ".djvu", ".djv", ".epub", ".fb2", ".mobi")
SHCNE_ASSOCCHANGED = 0x08000000
MANIFEST_VERSION = 1


class Entry(NamedTuple):
    key: str  # relative to HKCU\Software\Classes
    name: str  # "" = the key's default value
    kind: int
    data: object


class Registry(Protocol):
    def key_exists(self, path: str) -> bool: ...
    def create_key(self, path: str) -> None: ...
    def get_value(self, path: str, name: str) -> tuple[int, object] | None: ...
    def set_value(self, path: str, name: str, kind: int, data: object) -> None: ...
    def delete_value(self, path: str, name: str) -> None: ...
    def is_empty(self, path: str) -> bool: ...
    def delete_key(self, path: str) -> None: ...


class WinRegistry:
    """The real HKCU\\Software\\Classes."""

    def __init__(self, root=winreg.HKEY_CURRENT_USER, base: str = r"Software\Classes"):
        self.root = root
        self.base = base

    def _full(self, path: str) -> str:
        return f"{self.base}\\{path}"

    def key_exists(self, path: str) -> bool:
        try:
            winreg.OpenKey(self.root, self._full(path), 0, winreg.KEY_READ).Close()
            return True
        except FileNotFoundError:
            return False

    def create_key(self, path: str) -> None:
        winreg.CreateKeyEx(self.root, self._full(path), 0, winreg.KEY_WRITE).Close()

    def get_value(self, path: str, name: str) -> tuple[int, object] | None:
        try:
            with winreg.OpenKey(self.root, self._full(path), 0, winreg.KEY_READ) as key:
                data, kind = winreg.QueryValueEx(key, name)
        except FileNotFoundError:
            return None
        return kind, data

    def set_value(self, path: str, name: str, kind: int, data: object) -> None:
        with winreg.CreateKeyEx(self.root, self._full(path), 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, name, 0, kind, data)

    def delete_value(self, path: str, name: str) -> None:
        with winreg.OpenKey(self.root, self._full(path), 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, name)

    def is_empty(self, path: str) -> bool:
        with winreg.OpenKey(self.root, self._full(path), 0, winreg.KEY_READ) as key:
            subkeys, values, _ = winreg.QueryInfoKey(key)
        return subkeys == 0 and values == 0

    def delete_key(self, path: str) -> None:
        winreg.DeleteKey(self.root, self._full(path))


def notify_shell() -> None:
    """Tell Explorer that associations changed, so menus update without a re-login."""
    import ctypes

    ctypes.windll.shell32.SHChangeNotify(SHCNE_ASSOCCHANGED, 0, None, None)


def command_line(pythonw: Path, script: Path) -> str:
    return f'"{pythonw}" "{script}" "%1"'


def plan_entries(pythonw: Path, script: Path) -> list[Entry]:
    command = command_line(pythonw, script)
    entries = [
        Entry(PROGID, "", winreg.REG_SZ, PROGID_NAME),
        Entry(rf"{PROGID}\shell\open", "FriendlyAppName", winreg.REG_SZ, APP_NAME),
        Entry(rf"{PROGID}\shell\open\command", "", winreg.REG_SZ, command),
    ]
    for ext in EXTENSIONS:
        verb = rf"SystemFileAssociations\{ext}\shell\{VERB_KEY}"
        entries += [
            Entry(rf"{ext}\OpenWithProgids", PROGID, winreg.REG_NONE, b""),
            Entry(verb, "MUIVerb", winreg.REG_SZ, VERB_NAME),
            Entry(rf"{verb}\command", "", winreg.REG_SZ, command),
        ]
    return entries


# ---------- manifest ----------

def _encode(value: tuple[int, object] | None) -> dict | None:
    if value is None:
        return None
    kind, data = value
    if data is None or isinstance(data, (bytes, bytearray)):
        # winreg reads a zero-length binary/REG_NONE value back as None, not b"".
        return {"type": kind, "hex": bytes(data or b"").hex()}
    return {"type": kind, "data": data}  # str, int or list[str]


def _own_key(key: str) -> bool:
    """Keys that only this program uses: whatever is in them came from an install of ours."""
    k = key.lower()
    progid = PROGID.lower()
    if k == progid or k.startswith(progid + "\\"):
        return True
    parts = k.split("\\")
    return len(parts) >= 4 and parts[0] == "systemfileassociations" and parts[2] == "shell" \
        and parts[3] == VERB_KEY.lower()


def _own_value(key: str, name: str) -> bool:
    return _own_key(key) or (key.lower().endswith("\\openwithprogids") and name.lower() == PROGID.lower())


def _decode(stored: dict) -> tuple[int, object]:
    if "hex" in stored:
        return stored["type"], bytes.fromhex(stored["hex"])
    return stored["type"], stored["data"]


def _load_manifest(path: Path) -> dict | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _save_manifest(path: Path, manifest: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def _ancestors(path: str) -> list[str]:
    parts = path.split("\\")
    return ["\\".join(parts[:i]) for i in range(1, len(parts) + 1)]


def _show(name: str) -> str:
    return name or "(за замовчуванням)"


# ---------- install / uninstall ----------

def install(reg: Registry, manifest_path: Path, entries: list[Entry], notify=notify_shell,
            dry_run: bool = False, out: TextIO = sys.stdout) -> int:
    manifest = _load_manifest(manifest_path) or {"version": MANIFEST_VERSION, "created_keys": [], "values": []}
    created = {k.lower() for k in manifest.setdefault("created_keys", [])}
    records = {(v["key"].lower(), v["name"].lower()): v for v in manifest.setdefault("values", [])}
    exists: dict[str, bool] = {}  # answers for keys this run will create, so a dry run sees them too

    for entry in entries:
        for key in _ancestors(entry.key):
            lower = key.lower()
            if lower not in exists:
                exists[lower] = reg.key_exists(key)
            # Our own keys are listed even if they exist: they are left over from an install
            # whose manifest was lost, and uninstall must still remove them.
            if (not exists[lower] or _own_key(key)) and lower not in created:
                created.add(lower)
                manifest["created_keys"].append(key)
                print(f"+ розділ  HKCU\\Software\\Classes\\{key}", file=out)
            exists[lower] = True
        record = records.get((entry.key.lower(), entry.name.lower()))
        if record is None:
            # A value in our own namespace is never the user's: nothing to restore later (review M-1).
            previous = None if _own_value(entry.key, entry.name) else reg.get_value(entry.key, entry.name)
            record = {"key": entry.key, "name": entry.name, "previous": _encode(previous)}
            records[(entry.key.lower(), entry.name.lower())] = record
            manifest["values"].append(record)
        record["written"] = _encode((entry.kind, entry.data))
        shown = entry.data if isinstance(entry.data, str) else "(порожнє REG_NONE)"
        print(f"= значення {entry.key} [{_show(entry.name)}] = {shown}", file=out)

    if dry_run:
        print("Пробний прогін: реєстр не змінено, маніфест не записано.", file=out)
        return 0
    _save_manifest(manifest_path, manifest)  # first: a failed write below can still be undone
    for entry in entries:
        reg.set_value(entry.key, entry.name, entry.kind, entry.data)
    notify()
    print(f"Готово. Скасувати: explorer_integration.py uninstall (маніфест {manifest_path.name}).", file=out)
    return 0


def uninstall(reg: Registry, manifest_path: Path, notify=notify_shell,
              dry_run: bool = False, out: TextIO = sys.stdout) -> int:
    manifest = _load_manifest(manifest_path)
    if manifest is None:
        print(f"Немає {manifest_path.name}: нічого скасовувати.", file=out)
        return 1

    changed = []
    for value in reversed(manifest["values"]):
        key, name = value["key"], value["name"]
        previous, written = value.get("previous"), value.get("written")  # older manifests lack "written"
        current = _encode(reg.get_value(key, name))
        if current is None or current == previous:
            continue  # already undone: a repeated uninstall after a partial failure
        if written is not None and current != written:
            # Someone (an install from another folder) wrote it after us: not ours to undo.
            changed.append(key)
            print(f"! значення {key} [{_show(name)}] змінене після нашого install, лишаю як є", file=out)
            continue
        if previous is not None:
            print(f"~ значення {key} [{_show(name)}] -> попереднє", file=out)
            if not dry_run:
                reg.set_value(key, name, *_decode(previous))
        else:
            print(f"- значення {key} [{_show(name)}]", file=out)
            if not dry_run:
                reg.delete_value(key, name)

    kept = []
    for key in reversed(manifest.get("created_keys", [])):  # children were created after their parents
        if not reg.key_exists(key):
            continue
        if dry_run:
            print(f"- розділ  {key} (якщо порожній)", file=out)
            continue
        if reg.is_empty(key):
            reg.delete_key(key)
            print(f"- розділ  {key}", file=out)
        else:
            kept.append(key)
            print(f"! розділ  {key} лишився: у ньому є чужі записи", file=out)

    if dry_run:
        print("Пробний прогін: реєстр не змінено.", file=out)
        return 0
    notify()
    # Kept as a record of what was undone; a later install starts a fresh manifest.
    manifest_path.replace(manifest_path.with_name(f"{manifest_path.stem}.uninstalled.json"))
    notes = [f"лишилось розділів із чужими записами: {len(kept)}" if kept else "",
             f"значень, змінених іншим install: {len(changed)}" if changed else ""]
    print("Готово." + "".join(f" {n}." for n in notes if n), file=out)
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="RSVP reader in Explorer's «Open with» and context menu")
    parser.add_argument("action", choices=["install", "uninstall"])
    parser.add_argument("--dry-run", action="store_true", help="show the changes, write nothing")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    for stream in (sys.stdout, sys.stderr):  # a cp1251/cp866 console must not crash on a symbol
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    reg = WinRegistry()
    try:
        if args.action == "uninstall":
            return uninstall(reg, MANIFEST_PATH, dry_run=args.dry_run)
        missing = [p for p in (PYTHONW, LAUNCHER) if not p.is_file()]
        if missing:
            print("Не знайдено: " + ", ".join(map(str, missing)), file=sys.stderr)
            return 1
        return install(reg, MANIFEST_PATH, plan_entries(PYTHONW, LAUNCHER), dry_run=args.dry_run)
    except (KeyError, TypeError, ValueError) as exc:  # json.JSONDecodeError is a ValueError
        print(f"Пошкоджений {MANIFEST_PATH.name}: {type(exc).__name__}: {exc}. "
              f"Перевірте файл у редакторі (він у теці проєкту) і повторіть команду.", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"Помилка реєстру або файлу: {exc}. Повторіть команду: вона продовжить з місця збою.",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
