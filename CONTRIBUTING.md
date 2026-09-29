# Contributing

Thanks for looking. Heftig is deliberately small, so the most useful contributions are fixes,
better defaults, translations and support for more scanners, mail providers and document types –
less so new areas of features. If you plan something bigger, open an issue first and describe
what you want to do; that saves both of us time.

## Getting started

```sh
git clone https://github.com/jo-tud/heftig.git && cd heftig
uv sync
make test && make lint
```

[AGENTS.md](AGENTS.md) is a short map of the code with the rules that are easy to break – useful
whether you work with a coding agent or not. [docs/architecture.md](docs/architecture.md) has the
details.

## Pull requests

- One topic per pull request, with a test for the behaviour you add or fix.
- `make test`, `make lint` and `uv run python scripts/i18n.py check de` pass.
- New interface texts in English, with a German translation (or say that you need help with it).
- No personal data in tests or fixtures – invent names, numbers and addresses.

## Translations

The interface is English, translations live in `src/heftig/locale/<language>/messages.po`
(standard gettext format, any .po editor works). To add a language: add it to `LANGUAGES` in
`src/heftig/i18n.py` and to the `language` setting in `src/heftig/config.py`, run
`uv run python scripts/i18n.py update <code>` and translate the file. The AI prompts have their
examples and language names in `src/heftig/providers/prompt.py`; a language missing there falls
back to English examples and English summaries.

## Security issues

Please don't open a public issue – see [SECURITY.md](SECURITY.md).
