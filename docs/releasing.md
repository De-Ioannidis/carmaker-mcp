# Releasing

A release consists of three things, all produced by `.github/workflows/release.yml` when a tag `v<version>`
is pushed:

1. the package on [PyPI](https://pypi.org/project/carmaker-mcp/), so that users can run `uvx carmaker-mcp`;
2. the entry in the [MCP registry](https://registry.modelcontextprotocol.io) (`server.json`), so that clients
   can find the server by name;
3. a **draft** GitHub release with the one-click bundles (`.mcpb`) attached, which you review and publish by hand.

PyPI and the registry authenticate the workflow through OIDC ("trusted publishing"): GitHub proves which
repository and workflow is uploading, so no token is stored anywhere.

## One-time setup

1. The repository must be on GitHub at the URL used in `pyproject.toml` (`[project.urls]`), `README.md`,
   `server.json` and `packaging/build_mcpb.py`.
2. On <https://pypi.org/manage/account/publishing/> add a *pending publisher*: PyPI project name
   `carmaker-mcp`, your GitHub owner and repository, workflow `release.yml`, environment `pypi`.
   The project is created by the first upload.
3. Optional, for rehearsals: the same on <https://test.pypi.org/manage/account/publishing/> with
   environment `testpypi`.
4. In the GitHub repository settings create the environments `pypi` (and `testpypi`). Adding yourself as a
   required reviewer on `pypi` makes every upload wait for a click.
5. The registry needs no setup: the namespace `io.github.<owner>` is proven by the workflow's GitHub identity,
   and the PyPI package is tied to it by the `mcp-name:` comment at the top of `README.md`.

## Each release

1. Set the version in **two files**: `src/carmaker_mcp/__init__.py` and `server.json` (`version` and
   `packages[0].version`). A unit test fails if they differ. Move the `[Unreleased]` section of `CHANGELOG.md`
   under the new version with the date.
2. Check locally:
   ```bash
   uv run ruff check src tests packaging scripts && uv run pyright && uv run pytest
   uv build && uvx twine check --strict dist/*
   uvx --from dist/carmaker_mcp-<version>-py3-none-any.whl carmaker-mcp doctor
   python packaging/build_mcpb.py        # needs Node.js; validates the manifest and packs the bundle
   ```
3. Rehearsal (optional): run the `release` workflow by hand (*Run workflow*); it uploads to TestPyPI only. Then
   ```bash
   uvx --index https://test.pypi.org/simple/ --index-strategy unsafe-best-match carmaker-mcp --version
   ```
4. Commit, tag and push. The tag must be `v` + the version, otherwise the workflow stops before uploading:
   ```bash
   git tag v<version> && git push origin main v<version>
   ```
5. After the workflow has finished: `uvx carmaker-mcp@latest --version`; open the draft release on GitHub,
   replace the generated notes with the changelog section, and publish it.

A version number can be uploaded to PyPI only once. If a release is broken, yank it on PyPI and publish the
next patch version.

## What the package does not contain

`matlabengine` and IPG's `cmapi` are not dependencies of the wheel: the first must match the user's MATLAB
release (`uvx --with "matlabengine==24.2.*" ...`; `carmaker-mcp doctor` prints the pin), the second is loaded
from the user's CarMaker install.

## The one-click bundle

`packaging/build_mcpb.py` builds an [MCPB](https://github.com/modelcontextprotocol/mcpb) bundle of the "uv"
type: the server's source plus a small `pyproject.toml`; the host (for example Claude Desktop) creates the
Python environment. Because the MATLAB engine package is version-bound, there is one bundle per MATLAB release
(`--matlab R2024b`) and one without MATLAB (`--no-matlab`, standalone runs only). The workflow builds the
R2024b and the no-MATLAB bundle; build others by hand if needed.

## The single-file executable

`packaging/build_exe.ps1` (PyInstaller) is built by hand per CarMaker / MATLAB combination and can be attached
to the GitHub release. It is not part of the workflow.
