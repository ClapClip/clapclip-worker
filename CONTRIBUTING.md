# Contributing

Please use GitHub issues for bugs and feature requests. Include the OS, Python
version, mode, model names and a minimal reproduction using synthetic or
consented media. Redact logs first; never attach `.env`, tokens, signed URLs or
other people's photos. See [SECURITY.md](SECURITY.md) for private reports.

Contributions to project source are under [MIT](LICENSE). Submit only material
you can license; keep third-party notices and do not add model weights. Use a
GitHub noreply commit email if you do not want your personal address published.
Maintainers integrate changes into `main`.

Use Python 3.13 and an isolated environment. Installation is described in
[local mode](docs/local-mode.md) and [distributed mode](docs/distributed-mode.md).
After choosing the appropriate runtime dependencies, install development tools
with `pip install build pip-audit`. Windows DirectML users must retain the
documented `--no-deps` project/InsightFace installation order; do not reinstall
the CPU ONNX Runtime into that environment just to add development tools.

```bash
python scripts/check-release.py
MEDIAWORKER_LOAD_DOTENV=0 python -m unittest discover -s tests -v
python -m build
python scripts/check-release.py --archive dist/mediaworker-0.1.0.tar.gz
python scripts/check-release.py --archive dist/mediaworker-0.1.0-py3-none-any.whl
python -m pip_audit --skip-editable
```

Tests use generated media, temporary directories and mock APIs. Never run a
production queue task as a default test. Report untested GPU/model/platform
paths honestly. Preserve API, lease, cancellation, download-policy and shared
three-slot behavior. A source commit does not switch an existing deployment.

Before a source release, check the actual Git history and archives, license
metadata, dependency audit and CI result. Create archives from Git or the build
backend, never from a zip of the working directory. Do not include Python
environments, weights or private model releases. A version tag must identify
the reviewed commit; publishing packages to an index is a separate maintainer
action and is not performed automatically by CI.
