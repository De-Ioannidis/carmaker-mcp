<#
Builds dist\carmaker-mcp.exe, a single-file Windows executable of the server (PyInstaller).

  .\packaging\build_exe.ps1                                   # with the MATLAB engine (needs MATLAB installed)
  .\packaging\build_exe.ps1 -MatlabEngine "matlabengine==24.2.*"
  .\packaging\build_exe.ps1 -NoMatlab                         # standalone instances and --mock only

The executable embeds the Python it was built with. Two things stay outside and are loaded from the
user's installs at run time, so the build is tied to them:
  * IPG's cmapi (compiled per Python minor version): the CarMaker install must ship
    Python\python3.<minor of the build Python>.
  * MATLAB's engine binaries: the bundled matlabengine package records the MATLAB folder it was
    installed against, so the executable expects that MATLAB release in the same location.
#>
param(
    [string]$Python = "python",
    [string]$MatlabEngine = "matlabengine",
    [switch]$NoMatlab
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$venv = Join-Path $root "build\exe-venv"

& $Python -m venv $venv
$py = Join-Path $venv "Scripts\python.exe"
& $py -m pip install --quiet --upgrade pip
& $py -m pip install --quiet $root pyinstaller
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

$opts = @(
    "--noconfirm", "--onefile", "--console", "--name", "carmaker-mcp",
    "--distpath", (Join-Path $root "dist"),
    "--workpath", (Join-Path $root "build\pyinstaller"),
    "--specpath", (Join-Path $root "build"),
    "--copy-metadata", "fastmcp", "--copy-metadata", "mcp", "--copy-metadata", "carmaker-mcp"
)
if (-not $NoMatlab) {
    & $py -m pip install --quiet $MatlabEngine
    if ($LASTEXITCODE -ne 0) { throw "could not install $MatlabEngine (it must match the installed MATLAB release)" }
    # _arch.txt (where MATLAB lives) and the licence text are data files, not modules
    $opts += @("--hidden-import", "matlab.engine", "--collect-data", "matlab", "--copy-metadata", "matlabengine")
}

& $py -m PyInstaller @opts (Join-Path $PSScriptRoot "entry.py")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

$exe = Join-Path $root "dist\carmaker-mcp.exe"
& $exe --version
if ($LASTEXITCODE -ne 0) { throw "the built executable does not start" }
Write-Host "built $exe"
