"""Interface texts: collect them from the sources and keep the catalogues complete.

    uv run python scripts/i18n.py check de     # missing / unused translations (exit 1 if missing)
    uv run python scripts/i18n.py update de    # rewrite locale/de/messages.po (sorted by source)

Texts are collected from ``_()``, ``N_()``, ``gettext()``, ``ngettext()``, ``pgettext()`` in the
Python code, the same calls and ``{% trans %}`` blocks in the templates, and ``t()``/``tn()`` in
the browser scripts. ``update`` keeps every existing translation and adds new texts with an
empty translation (shown in English until translated).
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

from jinja2 import Environment

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "src" / "heftig"
sys.path.insert(0, str(ROOT / "src"))

from heftig import i18n  # noqa: E402

FUNCS = {"_": 1, "N_": 1, "gettext": 1, "ngettext": 2, "pgettext": 2}

# key -> (msgid, plural or None, context or None, [locations])
Entry = tuple[str, "str | None", "str | None", list[str]]


def _add(out: dict[str, Entry], msgid: str, plural, ctx, where: str) -> None:
    key = f"{ctx}\x04{msgid}" if ctx else msgid
    if key not in out:
        out[key] = (msgid, plural, ctx, [])
    out[key][3].append(where)


def from_python(out: dict[str, Entry]) -> None:
    for f in sorted(PKG.rglob("*.py")):
        rel = f.relative_to(ROOT)
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = (
                fn.id
                if isinstance(fn, ast.Name)
                else fn.attr
                if isinstance(fn, ast.Attribute)
                else ""
            )
            if name not in FUNCS:
                continue
            args = [a.value if isinstance(a, ast.Constant) and isinstance(a.value, str) else None
                    for a in node.args[: FUNCS[name]]]  # fmt: skip
            if len(args) < FUNCS[name] or None in args:
                continue
            where = f"{rel}:{node.lineno}"
            if name == "ngettext":
                _add(out, args[0], args[1], None, where)
            elif name == "pgettext":
                _add(out, args[1], None, args[0], where)
            else:
                _add(out, args[0], None, None, where)


def from_templates(out: dict[str, Entry]) -> None:
    env = Environment(extensions=["jinja2.ext.i18n"])
    for f in sorted((PKG / "web" / "templates").glob("*.html")):
        rel = f.relative_to(ROOT)
        for line, func, msg in env.extract_translations(
            f.read_text(encoding="utf-8"),
            gettext_functions=("_", "gettext", "ngettext", "pgettext"),
        ):
            where = f"{rel}:{line}"
            if isinstance(msg, str):
                _add(out, msg, None, None, where)
            elif func == "ngettext" and msg[0] and msg[1]:
                _add(out, msg[0], msg[1], None, where)
            elif func == "pgettext" and msg[0] and msg[1]:
                _add(out, msg[1], None, msg[0], where)
            elif msg and msg[0]:
                _add(out, msg[0], None, None, where)


# tn("one", "many", n): the plural form too (double quotes only)
_JS_PLURAL = re.compile(r'\btn\(\s*"((?:[^"\\\n]|\\.)*)"\s*,\s*"((?:[^"\\\n]|\\.)*)"')


def from_scripts(out: dict[str, Entry]) -> None:
    static = PKG / "web" / "static"
    for f in sorted(static.glob("*.js")):
        if f.name == "i18n.js":
            continue
        rel = f.relative_to(ROOT)
        text = f.read_text(encoding="utf-8")
        plurals = {m.start(): json.loads(f'"{m.group(2)}"') for m in _JS_PLURAL.finditer(text)}
        for m in i18n._JS_CALL.finditer(text):
            raw = m.group(1)
            if raw is None:
                raw = m.group(2).replace("\\'", "'").replace('"', '\\"')
            line = text.count("\n", 0, m.start()) + 1
            _add(out, json.loads(f'"{raw}"'), plurals.get(m.start()), None, f"{rel}:{line}")


def collect() -> dict[str, Entry]:
    out: dict[str, Entry] = {}
    from_python(out)
    from_templates(out)
    from_scripts(out)
    return out


def _q(s: str) -> str:
    body = json.dumps(s, ensure_ascii=False)
    if "\\n" not in body[1:-1] or len(body) < 70:
        return body
    parts = s.split("\n")
    lines = [json.dumps(p + "\n", ensure_ascii=False) for p in parts[:-1]]
    if parts[-1]:
        lines.append(json.dumps(parts[-1], ensure_ascii=False))
    return '""\n' + "\n".join(lines)


def write_po(lang: str, entries: dict[str, Entry]) -> Path:
    cat = i18n.catalogue.__wrapped__(lang)  # all current translations, uncached
    lines = [
        f"# Heftig – interface texts in {i18n.LANGUAGES[lang]} ({lang}).",
        "# Update with: uv run python scripts/i18n.py update " + lang,
        'msgid ""',
        'msgstr ""',
        '"Content-Type: text/plain; charset=UTF-8\\n"',
        f'"Language: {lang}\\n"',
        '"Plural-Forms: nplurals=2; plural=(n != 1);\\n"',
        "",
    ]
    for key, (msgid, plural, ctx, where) in sorted(entries.items(), key=lambda kv: kv[1][3][0]):
        lines.append("#: " + " ".join(sorted(set(where))[:6]))
        if ctx:
            lines.append(f"msgctxt {_q(ctx)}")
        lines.append(f"msgid {_q(msgid)}")
        tr = cat.get(key)
        if plural:
            forms = tr if isinstance(tr, list) else ["", ""]
            lines.append(f"msgid_plural {_q(plural)}")
            lines += [f"msgstr[{i}] {_q(forms[i])}" for i in range(2)]
        else:
            lines.append(f"msgstr {_q(tr if isinstance(tr, str) else '')}")
        lines.append("")
    path = i18n.LOCALE_DIR / lang / "messages.po"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] not in ("check", "update") or argv[1] not in i18n.LANGUAGES:
        print(__doc__)
        return 2
    cmd, lang = argv
    entries = collect()
    if cmd == "update":
        print(write_po(lang, entries))
        return 0
    cat = i18n.catalogue.__wrapped__(lang)
    missing = [(e[0], e[3][0]) for k, e in entries.items()
               if not (isinstance(cat.get(k), list) if e[1] else isinstance(cat.get(k), str))]  # fmt: skip
    unused = sorted(set(cat) - set(entries))
    for msgid, where in missing:
        print(f"missing  {where}  {msgid[:90]!r}")
    for msgid in unused:
        print(f"unused   {msgid[:90]!r}")
    print(f"{len(entries)} texts, {len(missing)} missing, {len(unused)} unused")
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
