# Contributing

Bug reports, test results on other CarMaker / MATLAB versions and pull requests are welcome.

## Development setup

```bash
git clone https://github.com/De-Ioannidis/carmaker-mcp
cd carmaker-mcp
uv sync --extra dev                 # or: python -m venv .venv && .venv\Scripts\pip install -e ".[dev]"
uv run pytest                       # unit tests: no CarMaker, no MATLAB needed
uv run ruff check src tests packaging scripts
uv run pyright
```

To work against a real MATLAB add the engine for your release to the environment, for example
`uv pip install "matlabengine==24.2.*"`.

## What the checks are

| Check | Command | Notes |
|---|---|---|
| Unit tests | `pytest` | mock backend, a fake MATLAB engine and a fake `cmapi`; coverage must stay at 85 % or more (`--cov=carmaker_mcp`) |
| Lint | `ruff check src tests packaging scripts` | line length 110 |
| Types | `pyright` | must be clean (run it on Windows: the code uses `winreg`) |
| Tools reference | `python scripts/gen_tools_doc.py` | regenerates `docs/tools.md`; a unit test fails if it is stale |
| Older FastMCP | `uv run --no-project --isolated --with-editable . --with pytest --with "fastmcp==2.14.*" pytest` | use `--with-editable`, otherwise uv may test a cached build |

## Live tests

They act on an open CarMaker / MATLAB session and are therefore opt-in through environment variables; see
[docs/verification.md](docs/verification.md). If you run them on a setup that is not listed there, please open
an issue or pull request with the printed output: that is the most useful contribution at this stage.

## Guidelines

* Keep the repository generic: no project, team or company names, no local paths, no IPG or MathWorks files
  or documentation text.
* A tool that changes something must log the change with the old value and be covered by `cm_revert_all`, or
  say clearly in its description why it cannot be.
* State-changing work goes through `Session` (it holds the lock and the idle check); `server.py` only declares
  tools.
* Every tool parameter needs a description; a unit test checks this.
* Say in the pull request what you verified live and on which versions, and what you did not.
* New behaviour needs a unit test and a line in `CHANGELOG.md`.

## Releases

See [docs/releasing.md](docs/releasing.md).
