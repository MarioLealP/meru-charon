# charon

Meru's purge - clears what a retired app left after its retention

A Meru app, made from the [Meru/nuwa-python](https://git.clockmost.com/Meru/nuwa-python) template, turned into a
daily job (D107): every day at 11:00 Lisbon a CronJob runs one pass and exits. An app whose request was removed more
than its retention ago (`metadata.removedAt`, stamped by Xolotl) loses what the retire only revoked - namespaces,
volumes, OpenBao entries, Zot images, runner records - and its request file goes last. Exit 0 = the pass succeeded;
Zabbix watches the CronJob's last successful run. Shikigami never deletes (D79, D84): Charon is the only part of Meru
that does, with credentials of its own.

## Run locally

```sh
uv sync
PYTHONPATH=src uv run python -m charon
```

## Development

```sh
scripts/check.sh   # lint, format, types, lockfile, tests - exactly what CI runs
```

## Deploy

`deploy/meru.yaml` makes this repo a Meru app: [Meru/meru](https://git.clockmost.com/Meru/meru) finds it and deploys it
into namespace `charon`. Shikigami adds it once the app's fence, signing key and build credentials exist,
from `deploy/meru.yaml.in` (the template's chart release and check image) with the owner from the request.
`deploy/values.yaml` is the app's whole deployment, rendered with the `nuwa-app` chart from
[Meru/nuwa](https://git.clockmost.com/Meru/nuwa). Merging to `main` runs the checks, builds the image, signs it and rolls
it out; builds run in namespace `charon-build` on the runner from [Meru/sindri](https://git.clockmost.com/Meru/sindri).
No secrets live in this repo: they come from OpenBao, through the External Secrets Operator or the build's own login.

## License

MIT - see `LICENSE`.
