# NekrozVPN

NekrozVPN — приватный Python-builder для автоматической сборки данных проекта.

## Что делает сейчас

- читает `sources.json`
- убирает дубликаты
- создаёт `output/manifest.json`
- создаёт `output/subscription.txt`

## Автозапуск

Workflow в `.github/workflows/build.yml` запускается:

- после каждого push в `main`
- каждый час
- вручную через GitHub Actions

## Структура

```text
NekrozVPN/
├── .github/
│   └── workflows/
│       └── build.yml
├── main.py
├── sources.json
├── pyproject.toml
├── README.md
└── .gitignore
