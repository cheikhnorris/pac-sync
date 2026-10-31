<#
.SYNOPSIS
  One-step PAC Sync proof of concept: checks prerequisites, prepares .venv, config and .env, starts the
  ServiceNow mock, runs doctor and a first sync, opens the portal.
.DESCRIPTION
  Usually started through start-poc.cmd (no ExecutionPolicy change needed). Each step prints one line and the
  script stops at the first failure with a remedy.

  Config: -Config (default config\config.yaml). When that file is missing it is created from the local mock profile
  (pac-sync setup --profile mock). To run the local-only demo whatever config\config.yaml says, use
  -Config config\config.mock.yaml (history in pac-history-mock, simulated ZIA, nothing pushed).
.EXAMPLE
  .\start-poc.cmd
  .\start-poc.cmd -Config config\config.mock.yaml -NoBrowser
  .\start-poc.cmd -Reset        # fresh demo: mock database, sim-zia and pac-history-mock are deleted
#>
param(
    [string]$Config = "config\config.yaml",
    [switch]$Reset,
    [switch]$NoBrowser,
    [switch]$SkipSync,
    [string]$MockPath = (Join-Path $PSScriptRoot "..\snow-mock"),
    [string]$SdkPath = (Join-Path $PSScriptRoot "..\zscaler-sdk-python")
)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$MockUrl = "http://127.0.0.1:8090"

function Step([string]$Message) { Write-Host "==> $Message" }
function Info([string]$Message) { Write-Host "    $Message" }
function Fail([string]$Message, [string]$Remedy) {
    Write-Host "FAILED: $Message" -ForegroundColor Red
    if ($Remedy) { Write-Host "  Fix: $Remedy" -ForegroundColor Yellow }
    exit 1
}
function Invoke-Native([string]$Exe, [string[]]$Arguments) {
    # Native tools log to stderr: do not let PowerShell turn that into terminating errors.
    $ErrorActionPreference = "Continue"
    & $Exe @Arguments | Out-Host
    return $LASTEXITCODE
}
function Get-PythonVersion([string]$Exe, [string[]]$Prefix) {
    $ErrorActionPreference = "Continue"
    try {
        $out = & $Exe @($Prefix + @("-c", "import sys; print(sys.version_info[0]*100+sys.version_info[1])")) 2>$null
        if ($LASTEXITCODE -eq 0 -and $out) { return [int]("$out".Trim()) }
    } catch { }
    return 0
}
function Remove-DemoPath([string]$Path) {
    # Never touches the owner's pac-history clone, whatever the arguments say.
    if (-not (Test-Path -LiteralPath $Path)) { return }
    $full = (Resolve-Path -LiteralPath $Path).Path
    if ((Split-Path $full -Leaf) -eq "pac-history") { Fail "refusing to delete $full" "this is the GitHub-backed history" }
    Remove-Item -LiteralPath $full -Recurse -Force -Confirm:$false
    Info "deleted $full"
}

# 1. git -------------------------------------------------------------------------------------------------------
Step "Checking git"
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Fail "git not found" "winget install Git.Git, then open a new terminal" }

# 2. Python 3.10 - 3.12 and the virtual environment ----------------------------------------------------------------
$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (Test-Path $venvPy) {
    $v = Get-PythonVersion $venvPy @()
    if ($v -lt 310 -or $v -gt 312) { Fail ".venv uses Python $v (need 3.10 - 3.12)" "delete the .venv folder and run start-poc again" }
    Step "Python virtual environment .venv (Python $([int]($v / 100)).$($v % 100))"
} else {
    Step "Creating .venv"
    $candidates = @(
        @{ Exe = "py"; Prefix = @("-3.11") }, @{ Exe = "py"; Prefix = @("-3.12") }, @{ Exe = "py"; Prefix = @("-3.10") },
        @{ Exe = "python"; Prefix = @() }, @{ Exe = "python3"; Prefix = @() }
    )
    $found = $null
    foreach ($c in $candidates) {
        if (-not (Get-Command $c.Exe -ErrorAction SilentlyContinue)) { continue }
        $v = Get-PythonVersion $c.Exe $c.Prefix
        if ($v -ge 310 -and $v -le 312) { $found = $c; break }
    }
    if (-not $found) { Fail "no Python 3.10 - 3.12 found" "winget install Python.Python.3.11, then open a new terminal" }
    $rc = Invoke-Native $found.Exe ($found.Prefix + @("-m", "venv", ".venv"))
    if ($rc -ne 0 -or -not (Test-Path $venvPy)) { Fail "python -m venv failed" "check the Python installation" }
}

# 3. Dependencies (skipped when pyproject.toml and the SDK source did not change) -------------------------------
$useLocalSdk = Test-Path (Join-Path $SdkPath "pyproject.toml")
$sdkSource = if ($useLocalSdk) { "editable:" + (Resolve-Path $SdkPath).Path } else { "pypi:zscaler-sdk-python>=1.11,<2" }
$stampFile = Join-Path $PSScriptRoot ".venv\.install-stamp"
$stamp = (Get-FileHash (Join-Path $PSScriptRoot "pyproject.toml") -Algorithm SHA256).Hash + "|" + $sdkSource
if ((Test-Path $stampFile) -and ((Get-Content $stampFile -Raw).Trim() -eq $stamp)) {
    Step "Dependencies up to date"
} else {
    Step "Installing dependencies ($sdkSource)"
    $rc = Invoke-Native $venvPy @("-m", "pip", "install", "-q", "--upgrade", "pip")
    if ($useLocalSdk) {
        $rc = Invoke-Native $venvPy @("-m", "pip", "install", "-q", "-e", (Resolve-Path $SdkPath).Path)
    } else {
        $rc = Invoke-Native $venvPy @("-m", "pip", "install", "-q", "zscaler-sdk-python>=1.11,<2")
    }
    if ($rc -ne 0) { Fail "installing zscaler-sdk-python failed" "check the network / proxy, then run start-poc again" }
    $rc = Invoke-Native $venvPy @("-m", "pip", "install", "-q", "-e", ".[dev]")
    if ($rc -ne 0) { Fail "installing pac-sync failed" "see the pip output above" }
    Set-Content -Path $stampFile -Value $stamp -Encoding ascii
}

# 4. Config and credentials ------------------------------------------------------------------------------------
if (-not (Test-Path $Config)) {
    if ($PSBoundParameters.ContainsKey("Config")) { Fail "config $Config not found" "omit -Config to create config\config.yaml from the mock profile" }
    Step "Creating $Config and .env from the mock profile"
    $rc = Invoke-Native $venvPy @("-m", "pac_sync", "--config", $Config, "setup", "--profile", "mock", "--non-interactive", "--no-doctor")
    if ($rc -ne 0) { Fail "pac-sync setup failed" "see the message above" }
} else {
    Step "Using config $Config"
}
$ErrorActionPreference = "Continue"
$settings = & $venvPy -c "import sys, yaml; c = yaml.safe_load(open(sys.argv[1], encoding='utf-8')); print(c['servicenow'].get('instance_url', '')); print(c['history']['repo_path']); print(bool(c['history'].get('push')))" $Config 2>$null
$ErrorActionPreference = "Stop"
if (-not $settings -or $settings.Count -lt 3) { Fail "cannot read $Config" "run .venv\Scripts\pac-sync --config $Config doctor for details" }
$instanceUrl, $repoPath, $push = $settings[0], $settings[1], $settings[2]
$usesMock = $instanceUrl -match '^https?://(127\.0\.0\.1|localhost)(:|/|$)'
Info "ServiceNow $instanceUrl, history $repoPath, push $push"
if ($usesMock -and -not (Test-Path ".env") -and -not $env:SNOW_CLIENT_ID) {
    Info "no .env: using the mock's public demo OAuth client for this session"
    $env:SNOW_CLIENT_ID = "pac-sync"
    $env:SNOW_CLIENT_SECRET = "pac-sync-secret"
}

# 5. Reset (optional) ------------------------------------------------------------------------------------------
if ($Reset) {
    Step "Resetting demo data"
    $stop = Join-Path $MockPath "stop.ps1"
    if (Test-Path $stop) { & $stop }
    Get-ChildItem -Path (Join-Path $MockPath "data") -Filter "snow.sqlite*" -ErrorAction SilentlyContinue |
        ForEach-Object { Remove-DemoPath $_.FullName }
    Remove-DemoPath (Join-Path $PSScriptRoot "sim-zia")
    Remove-DemoPath (Join-Path $PSScriptRoot "pac-history-mock")
    if ((Split-Path $repoPath -Leaf) -ne "pac-history-mock") {
        Write-Warning ("$Config keeps its history in $repoPath, which -Reset does not touch. Its regions/*/state.json still " +
            "record PAC file ids of the deleted sim-zia: use -Config config\config.mock.yaml for a clean demo.")
    }
}

# 6. ServiceNow mock -------------------------------------------------------------------------------------------
if ($usesMock) {
    Step "Starting the ServiceNow mock ($MockUrl)"
    $findPhp = Join-Path $MockPath "tools\find-php.ps1"
    if (-not (Test-Path $findPhp)) { Fail "snow-mock not found at $MockPath" "pass -MockPath <folder of snow-mock>" }
    . $findPhp
    try { $null = Find-Php } catch {
        $answer = ""
        try { $answer = Read-Host "PHP 8.4 is needed for the mock. Install it now with winget? [Y/n]" } catch { }
        if ($answer -notmatch '^(|y|yes)$') { Fail "PHP not found" "winget install PHP.PHP.8.4" }
        $rc = Invoke-Native "winget" @("install", "--id", "PHP.PHP.8.4", "-e", "--accept-source-agreements", "--accept-package-agreements")
        try { $null = Find-Php } catch { Fail "PHP still not found after winget" "winget install PHP.PHP.8.4, then open a new terminal" }
    }
    $global:LASTEXITCODE = 0
    try { & (Join-Path $MockPath "start.ps1") -WaitReady } catch { Fail "the mock did not start: $_" "see $MockPath\logs\server.log" }
    if ($LASTEXITCODE -ne 0) { Fail "the mock did not answer" "see $MockPath\logs\server.log" }
} else {
    Step "ServiceNow at $instanceUrl (mock not started)"
}

# 7. Doctor, then the first run ----------------------------------------------------------------------------------
Step "pac-sync doctor"
$rc = Invoke-Native $venvPy @("-m", "pac_sync", "--config", $Config, "doctor")
if ($rc -ne 0) { Fail "doctor reported failures" "fix the [FAIL] lines above, then run start-poc again" }

if ($SkipSync) {
    Step "Skipping the sync (-SkipSync)"
} else {
    Step "pac-sync run"
    $rc = Invoke-Native $venvPy @("-m", "pac_sync", "--config", $Config, "run")
    if ($rc -eq 1) {
        Write-Warning "the run finished with a failed or blocked region (exit 1): see the log above"
    } elseif ($rc -ne 0) {
        Fail "pac-sync run failed (exit $rc)" "see the log above; .venv\Scripts\pac-sync --config $Config doctor"
    }
}

# 8. Browser and next steps ----------------------------------------------------------------------------------------
if ($usesMock -and -not $NoBrowser) {
    Step "Opening $MockUrl"
    Start-Process $MockUrl
}
$cfgArg = if ($Config -eq "config\config.yaml" -or $Config -eq "config/config.yaml") { "" } else { " --config $Config" }
Write-Host ""
Write-Host "Ready. Next steps:" -ForegroundColor Green
Write-Host "  1. In the portal, act as jdoe (top right) and submit a 'Request PAC exception' for region EMEA."
Write-Host "  2. Approve it as mmanager, then nsec (and cab for a global rule)."
Write-Host "  3. .venv\Scripts\pac-sync$cfgArg run      -> the request closes, the rule shows Deployed."
Write-Host "  4. Open sim-zia\emea-proxy.pac\deployed.pac (what ZIA would serve) and git -C $repoPath log -p."
Write-Host "  .venv\Scripts\pac-sync$cfgArg status | doctor | render --region EMEA"
exit 0
