# Contributing

[简体中文](CONTRIBUTING.md) | English

Contributions to Harness Shell are welcome through bug reports, documentation, translations, tests, and code improvements.

## Bug Reports and Feature Requests

- Search [existing issues](https://github.com/Franc1s396/harness-shell/issues) first. Add reproduction details to a related issue when appropriate.
- When [opening an issue](https://github.com/Franc1s396/harness-shell/issues/new/choose), choose the bug report or feature request template. Chinese and English are both welcome.
- For bugs, include the application version or commit, Windows version, installed or source-run setup, reproduction steps, and expected and actual behavior. Include provider, model, or SSH environment details only when relevant.
- For features, describe the use case, current difficulty, and desired behavior. Discuss larger features, architecture changes, or protocol changes in an issue before implementing them.
- Sanitize logs and screenshots. Do not upload API keys, passwords, private keys, runtime databases, or sensitive server information. Do not publish exploitable vulnerability details in public issues.

## Local Development

See the [README](README.en.md#local-development-quick-start) for prerequisites and startup instructions. Read [AGENTS.md](AGENTS.md) before making changes, then follow its routing to the relevant domain guides and directory rules.

External contributors can fork the repository, make changes on their own branch, and open a pull request against the upstream repository. Keep each PR focused; avoid unrelated formatting, generated files, or dependency upgrades.

## Verify Your Changes

Start with tests directly related to the change, then expand verification according to its impact. The [Testing Guide](docs/agents/testing.md) is the source of truth for commands, environment requirements, and acceptance boundaries.

- Frontend changes: related tests, plus `npm.cmd --prefix frontend run test` and `npm.cmd --prefix frontend run build`.
- Python changes: run related tests using `backend\.venv\Scripts\python.exe -m pytest`; use `backend\.venv\Scripts\python.exe -m pytest backend -q` for the full backend suite.
- Launcher, Tauri, protocol, packaging, or SSH/SFTP changes: select the relevant tests and gates from the Testing Guide.
- Documentation or template changes: check links, commands, formatting, and consistency between Chinese and English content.
- Run `git diff --check` before submitting.

Add meaningful tests for behavior changes. Update the relevant implementations, tests, and domain documentation when contracts or lasting architectural facts change. Explain any verification you could not perform. Passing automated tests or builds does not establish acceptance with real providers, installed Desktop applications, or production SSH environments.

## Submit a Pull Request

Use the PR template to explain the problem, resulting behavior, related issues, verification commands and results, and unverified areas. Include sanitized screenshots for UI changes and upgrade implications for persistence or compatibility changes.

Do not commit credentials, runtime databases, `.runtime/`, dependency directories, caches, or build outputs. Keep both README language versions in sync.

Maintainers review contributions against the project scope and verification results; submitting a PR does not guarantee a merge. Small, complete changes that are easy to reproduce and verify are easier to review.
