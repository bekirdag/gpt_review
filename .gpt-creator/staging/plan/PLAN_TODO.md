# Build Plan

- Validate discovery outputs under `staging/inputs`.
- Review `routes.md` & `entities.md` for coverage and deltas.
- Implement generation steps for API, DB, Web, Admin, Docker.
- Run `gpt-creator generate all --project <path>` if not already executed.
- Bring the stack up with `gpt-creator run up` and smoke test.
- Execute `gpt-creator verify all` to satisfy acceptance & NFR gates.
- Iterate on Jira tasks using `gpt-creator iterate` until checks pass.
