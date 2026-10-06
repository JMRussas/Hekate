# On-demand local Hekate: plan-only profile (Postgres+AGE container + context-store Api).
#
# Entry point: scripts/local/hekate-local.ps1. Every external effect goes through a
# small Invoke-HL*/Test-HL*/Get-HL* wrapper so the tests can mock it.
#
# Safety rules this module enforces:
#   - only resources recorded in .hekate-local/state.json AND re-verified at use time
#     are ever stopped (process start time + expected command line; container compose
#     project + workspace label). A reused PID or a foreign container is left alone.
#   - a resource that could not be stopped stays in the ledger so the next stop retries.
#   - never `docker compose down`, never `-v`, never removes volumes.
#   - binds DB and Api to loopback; refuses a non-loopback connection string.
#   - occupied ports are reported, never freed by stopping someone else's process.
#   - environment for the Api is passed to that child process only.
#   - start/stop/backup/restore are serialized by a lock file; state writes are atomic.
#   - restore only ever targets a NEW, explicitly named database.

#Requires -Version 7.5
# 7.5+: ConvertFrom-Json -DateKind String keeps recorded process start times as
# strings (otherwise they come back as DateTime and ownership checks fail).

Set-StrictMode -Version Latest

$script:ComposeProject = 'hekate-local'
$script:ReservedDatabases = @('code_storage', 'orchestration', 'postgres', 'template0', 'template1')
$script:LoopbackHosts = @('127.0.0.1', 'localhost', '::1', '[::1]')

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

function Get-HLEnvValue([string]$Name, [string]$Default) {
    $v = [Environment]::GetEnvironmentVariable($Name)
    if ([string]::IsNullOrWhiteSpace($v)) { return $Default }
    return $v
}

function ConvertTo-HLInt([string]$Name, [string]$Value) {
    $n = 0
    if (-not [int]::TryParse($Value, [ref]$n)) { throw "$Name must be an integer (got '$Value')." }
    return $n
}

function Get-HLConfig {
    param([string]$RepoRoot)
    if (-not $RepoRoot) { $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path }
    $stateDir = Join-Path $RepoRoot '.hekate-local'
    $dbPort = ConvertTo-HLInt 'HEKATE_LOCAL_DB_PORT' (Get-HLEnvValue 'HEKATE_LOCAL_DB_PORT' '5434')
    $apiPort = ConvertTo-HLInt 'HEKATE_LOCAL_API_PORT' (Get-HLEnvValue 'HEKATE_LOCAL_API_PORT' '5103')
    [pscustomobject]@{
        RepoRoot           = $RepoRoot
        ComposeFile        = Join-Path $RepoRoot 'context-store\docker-compose.local.yml'
        ComposeProject     = $script:ComposeProject
        StateDir           = $stateDir
        StateFile          = Join-Path $stateDir 'state.json'
        LockFile           = Join-Path $stateDir 'lock'
        EnvFile            = Join-Path $stateDir 'compose.env'
        LogDir             = Join-Path $stateDir 'logs'
        BackupDir          = Join-Path $stateDir 'backups'
        ApiBinDir          = Join-Path $stateDir 'api-bin'
        ApiDll             = Join-Path $stateDir 'api-bin\Api.dll'
        ApiProject         = Join-Path $RepoRoot 'context-store\Api\Api.csproj'
        # The Api resolves tools/skills relative to its working directory.
        ApiWorkDir         = Join-Path $RepoRoot 'context-store\Api'
        DbHost             = '127.0.0.1'
        DbPort             = $dbPort
        DbName             = 'code_storage'
        PgPassword         = Get-HLEnvValue 'HEKATE_LOCAL_PG_PASSWORD' 'postgres'
        ApiPort            = $apiPort
        ApiUrl             = "http://127.0.0.1:$apiPort"
        HealthTimeoutSec   = ConvertTo-HLInt 'HEKATE_LOCAL_TIMEOUT_SEC' (Get-HLEnvValue 'HEKATE_LOCAL_TIMEOUT_SEC' '180')
        ShutdownTimeoutSec = 20
        PollIntervalSec    = 2
    }
}

function Assert-HLConfig($Config) {
    foreach ($p in @(@('DB port', $Config.DbPort), @('Api port', $Config.ApiPort))) {
        if ($p[1] -lt 1 -or $p[1] -gt 65535) { throw "$($p[0]) $($p[1]) is outside 1..65535." }
    }
    if ($Config.DbPort -eq $Config.ApiPort) { throw "DB port and Api port must differ (both $($Config.DbPort))." }
    foreach ($t in 'HealthTimeoutSec', 'ShutdownTimeoutSec', 'PollIntervalSec') {
        if ($Config.$t -le 0) { throw "$t must be a positive number of seconds." }
    }
    # The password lands in a connection string and a compose env file; restrict it
    # rather than escape it, so it can never inject Host=/Database= or a new env line.
    if ($Config.PgPassword -cnotmatch '^[A-Za-z0-9_.@%+:-]{1,128}$') {
        throw 'HEKATE_LOCAL_PG_PASSWORD may only contain letters, digits and _ . @ % + : - (1-128 chars).'
    }
    Assert-HLLoopbackConnectionString (Get-HLConnectionString $Config)
}

function Get-HLConnectionString($Config) {
    "Host=$($Config.DbHost);Port=$($Config.DbPort);Database=$($Config.DbName);Username=postgres;Password=$($Config.PgPassword);Maximum Pool Size=20"
}

function Assert-HLLoopbackConnectionString([string]$ConnectionString) {
    $hosts = @($ConnectionString -split ';' | Where-Object { $_ -match '^\s*(Host|Server)\s*=' })
    if ($hosts.Count -ne 1) { throw 'Connection string must contain exactly one Host; refusing to start the local profile.' }
    $h = ($hosts[0] -split '=', 2)[1].Trim()
    if ($script:LoopbackHosts -notcontains $h.ToLowerInvariant()) {
        throw "Connection string host '$h' is not loopback. The local profile only talks to its own 127.0.0.1 database; refusing."
    }
}

function Get-HLApiEnvironment($Config, [string]$ShutdownToken) {
    @{
        CODESTORAGE_CONNSTR         = Get-HLConnectionString $Config
        HEKATE_API_URLS             = $Config.ApiUrl
        # Plan-only profile: node edits must not spawn claude/gemini CLI runs.
        HEKATE_DISABLE_DISPATCHER   = '1'
        # Enables the loopback-only graceful shutdown endpoint for this process.
        HEKATE_LOCAL_SHUTDOWN_TOKEN = $ShutdownToken
    }
}

function New-HLToken {
    $bytes = [byte[]]::new(32)
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    [Convert]::ToHexString($bytes)
}

function Get-HLComposeArgs($Config) {
    @('compose', '-p', $Config.ComposeProject, '-f', $Config.ComposeFile, '--env-file', $Config.EnvFile)
}

# ---------------------------------------------------------------------------
# External-effect wrappers (mocked in tests)
# ---------------------------------------------------------------------------

function Test-HLCommand([string]$Name) {
    [bool](Get-Command $Name -CommandType Application -ErrorAction SilentlyContinue)
}

# Callers pass ONE array argument, e.g. Invoke-HLDocker @('ps', '-q'). That is an
# array literal bound to -Arguments, not splatting; the forwarding test verifies argv.
function Invoke-HLDocker([string[]]$Arguments) { Invoke-HLNative 'docker' $Arguments }

function Invoke-HLDotnet([string[]]$Arguments) { Invoke-HLNative 'dotnet' $Arguments }

# Never trust a stale or unset $LASTEXITCODE (StrictMode throws on unset): clear it,
# run, and report 127 if the command could not run or did not set an exit code.
function Invoke-HLNative([string]$Command, [string[]]$Arguments) {
    $global:LASTEXITCODE = $null
    try { $out = & $Command @Arguments 2>&1 }
    catch { return [pscustomobject]@{ ExitCode = 127; Output = "'$Command' could not be executed: $($_.Exception.Message)" } }
    $code = if ($null -eq $global:LASTEXITCODE) { 127 } else { [int]$global:LASTEXITCODE }
    [pscustomobject]@{ ExitCode = $code; Output = ($out | Out-String).Trim() }
}

function Test-HLPortInUse([int]$Port) {
    if (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue) {
        if (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue) { return $true }
    }
    $listener = $null
    try {
        $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $Port)
        $listener.ExclusiveAddressUse = $true
        $listener.Start()
        return $false
    }
    catch { return $true }
    finally { if ($listener) { $listener.Stop() } }
}

function Get-HLProcessInfo([int]$ProcessId) {
    $p = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction SilentlyContinue
    if (-not $p) { return $null }
    [pscustomobject]@{
        Id          = [int]$ProcessId
        StartTime   = $p.CreationDate.ToUniversalTime().ToString('o')
        CommandLine = [string]$p.CommandLine
    }
}

function Stop-HLProcessById([int]$ProcessId) {
    Stop-Process -Id $ProcessId -Force -ErrorAction Stop
}

# Start-Process with redirection creates the child with handle inheritance on, so the
# Api would also inherit THIS process's std handles. When a caller captures the
# launcher's output through a pipe, that pipe then stays open until the Api exits and
# the caller hangs. Clearing HANDLE_FLAG_INHERIT on our own std handles (and giving the
# Api explicit stdin/stdout/stderr files) leaves the Api holding only its log files.
function Disable-HLStdHandleInheritance {
    if (-not $IsWindows) { return }
    if (-not ('HekateLocal.NativeHandles' -as [type])) {
        Add-Type -Namespace HekateLocal -Name NativeHandles -MemberDefinition @'
[DllImport("kernel32.dll", SetLastError = true)] public static extern IntPtr GetStdHandle(int nStdHandle);
[DllImport("kernel32.dll", SetLastError = true)] public static extern bool SetHandleInformation(IntPtr hObject, uint dwMask, uint dwFlags);
'@
    }
    foreach ($std in -10, -11, -12) {   # STD_INPUT, STD_OUTPUT, STD_ERROR
        $h = [HekateLocal.NativeHandles]::GetStdHandle($std)
        if ($h -eq [IntPtr]::Zero -or $h -eq [IntPtr]::new(-1)) { continue }   # none / invalid
        # HANDLE_FLAG_INHERIT = 1. Fails harmlessly for console pseudo-handles.
        [void][HekateLocal.NativeHandles]::SetHandleInformation($h, 1, 0)
    }
}

function Start-HLApiProcess($Config, [hashtable]$Environment, $Logs) {
    $stdin = Join-Path $Config.StateDir 'api-stdin.empty'
    if (-not (Test-Path $stdin)) { New-Item -ItemType File -Path $stdin -Force | Out-Null }
    Disable-HLStdHandleInheritance
    $p = Start-Process -FilePath 'dotnet' -ArgumentList @("`"$($Config.ApiDll)`"") `
        -WorkingDirectory $Config.ApiWorkDir -Environment $Environment `
        -RedirectStandardInput $stdin -RedirectStandardOutput $Logs.out -RedirectStandardError $Logs.err `
        -WindowStyle Hidden -PassThru
    return [int]$p.Id
}

function Invoke-HLHttpGet([string]$Url) {
    try {
        $r = Invoke-WebRequest -Uri $Url -TimeoutSec 5 -SkipHttpErrorCheck -ErrorAction Stop
        $body = $null
        try { $body = $r.Content | ConvertFrom-Json } catch { }
        [pscustomobject]@{ StatusCode = [int]$r.StatusCode; Body = $body }
    }
    catch { [pscustomobject]@{ StatusCode = 0; Body = $null } }
}

function Invoke-HLShutdownRequest([string]$Url, [string]$Token) {
    try {
        $r = Invoke-WebRequest -Uri $Url -Method Post -Headers @{ 'X-Hekate-Local-Token' = $Token } -TimeoutSec 5 -SkipHttpErrorCheck -ErrorAction Stop
        return [int]$r.StatusCode
    }
    catch { return 0 }
}

function Wait-HLInterval([int]$Seconds) { Start-Sleep -Seconds $Seconds }

# ---------------------------------------------------------------------------
# State ledger + lock
# ---------------------------------------------------------------------------

function Invoke-HLLocked($Config, [scriptblock]$Action) {
    New-Item -ItemType Directory -Force -Path $Config.StateDir | Out-Null
    try { $lock = [System.IO.File]::Open($Config.LockFile, 'OpenOrCreate', 'ReadWrite', 'None') }
    catch {
        $ex = $_.Exception
        while ($ex.InnerException -and -not ($ex -is [System.IO.IOException])) { $ex = $ex.InnerException }
        # Only a sharing violation (Win32 error 32) means another operation holds the lock.
        if ($ex -is [System.IO.IOException] -and ($ex.HResult -band 0xFFFF) -eq 32) {
            throw "Another hekate-local operation is in progress ($($Config.LockFile) is held). Retry when it finishes."
        }
        throw "Could not open lock file $($Config.LockFile): $($ex.Message)"
    }
    try { & $Action }
    finally { $lock.Dispose() }
}

function New-HLState($Config) {
    @{ version = 1; workspace = $Config.RepoRoot; composeProject = $Config.ComposeProject; container = $null; api = $null }
}

function Read-HLState($Config) {
    if (-not (Test-Path $Config.StateFile)) { return $null }
    try { $s = Get-Content -Raw $Config.StateFile | ConvertFrom-Json -AsHashtable -DateKind String -ErrorAction Stop }
    catch { throw "State file $($Config.StateFile) is not valid JSON; inspect or delete it by hand. Nothing was touched." }
    if (-not ($s -is [hashtable]) -or $s.version -ne 1 -or $s.composeProject -ne $Config.ComposeProject) {
        throw "State file $($Config.StateFile) has an unexpected version/project. Nothing was touched."
    }
    if ($s.workspace -ne $Config.RepoRoot) {
        throw "State file $($Config.StateFile) belongs to workspace '$($s.workspace)', not '$($Config.RepoRoot)'. Refusing to act on it."
    }
    foreach ($k in 'container', 'api') { if (-not $s.ContainsKey($k)) { $s[$k] = $null } }
    if ($s.api -and (-not ($s.api -is [hashtable]) -or -not $s.api.pid -or -not $s.api.startTime)) {
        throw "State file $($Config.StateFile) has a malformed api entry. Nothing was touched."
    }
    if ($s.container -and (-not ($s.container -is [hashtable]) -or -not $s.container.id)) {
        throw "State file $($Config.StateFile) has a malformed container entry. Nothing was touched."
    }
    return $s
}

function Write-HLState($Config, $State) {
    New-Item -ItemType Directory -Force -Path $Config.StateDir | Out-Null
    $tmp = "$($Config.StateFile).$([guid]::NewGuid().ToString('N')).tmp"
    $State | ConvertTo-Json -Depth 5 | Set-Content -Path $tmp -Encoding utf8
    [System.IO.File]::Move($tmp, $Config.StateFile, $true)
}

function Write-HLComposeEnv($Config) {
    New-Item -ItemType Directory -Force -Path $Config.StateDir | Out-Null
    @(
        "HEKATE_LOCAL_DB_PORT=$($Config.DbPort)"
        "HEKATE_LOCAL_WORKSPACE=$($Config.RepoRoot)"
        "HEKATE_LOCAL_PG_PASSWORD=$($Config.PgPassword)"
    ) | Set-Content -Path $Config.EnvFile -Encoding utf8
}

# ---------------------------------------------------------------------------
# Ownership verification
# ---------------------------------------------------------------------------

function Test-HLOwnedApi($Config, $ApiState) {
    if (-not $ApiState) { return $false }
    $info = Get-HLProcessInfo ([int]$ApiState.pid)
    if (-not $info) { return $false }
    if ($info.StartTime -ne $ApiState.startTime) { return $false }   # PID reused
    # Compare against the path this launcher computes, never one read from state.
    if ($info.CommandLine.IndexOf($Config.ApiDll, [StringComparison]::OrdinalIgnoreCase) -lt 0) { return $false }
    return $true
}

function Stop-HLApi($Config, $ApiState) {
    # Graceful first: the Api's local-only shutdown endpoint runs ApplicationStopping
    # (outbox timer, dispatcher disposal). Force only after a bounded wait, and say so.
    $mode = 'forced (no shutdown token recorded)'
    if ($ApiState.ContainsKey('shutdownToken') -and $ApiState.shutdownToken) {
        $code = Invoke-HLShutdownRequest "$($Config.ApiUrl)/api/local/shutdown" $ApiState.shutdownToken
        $mode = "forced (graceful shutdown request returned $code)"
        if ($code -eq 202) {
            $attempts = [Math]::Max(1, [int][Math]::Ceiling($Config.ShutdownTimeoutSec / $Config.PollIntervalSec))
            for ($i = 0; $i -lt $attempts; $i++) {
                if (-not (Test-HLOwnedApi $Config $ApiState)) { return 'graceful' }
                Wait-HLInterval $Config.PollIntervalSec
            }
            $mode = "forced (still running $($Config.ShutdownTimeoutSec)s after graceful request)"
        }
    }
    if (Test-HLOwnedApi $Config $ApiState) { Stop-HLProcessById ([int]$ApiState.pid); return $mode }
    return 'graceful'
}

function Get-HLContainer($Config, [string]$ContainerId) {
    if ([string]::IsNullOrWhiteSpace($ContainerId)) { return $null }
    $r = Invoke-HLDocker @('inspect', '--type', 'container', '--format', '{{json .}}', $ContainerId)
    if ($r.ExitCode -ne 0 -or -not $r.Output) { return $null }
    $j = $r.Output | ConvertFrom-Json -AsHashtable -DateKind String
    $labels = if ($j.Config.Labels) { $j.Config.Labels } else { @{} }
    $health = if ($j.State.ContainsKey('Health') -and $j.State.Health) { $j.State.Health.Status } else { $null }
    [pscustomobject]@{
        Id        = $j.Id
        Running   = [bool]$j.State.Running
        Health    = $health
        Project   = $labels['com.docker.compose.project']
        Workspace = $labels['com.hekate.local.workspace']
    }
}

function Find-HLComposeContainer($Config) {
    # -a: include a container compose created but that is not (or no longer) running.
    $ps = Invoke-HLDocker ((Get-HLComposeArgs $Config) + @('ps', '-a', '-q', 'postgres'))
    if ($ps.ExitCode -ne 0) { return $null }
    $id = @($ps.Output -split "\r?\n" | Where-Object { $_.Trim() }) | Select-Object -First 1
    if (-not $id) { return $null }
    return Get-HLContainer $Config $id.Trim()
}

function Format-HLShortId([string]$Id) { if ($Id.Length -gt 12) { $Id.Substring(0, 12) } else { $Id } }

function Get-HLApiLogHint($ApiState) {
    if ($ApiState -and $ApiState.ContainsKey('logs') -and $ApiState.logs) { "$($ApiState.logs.err) and $($ApiState.logs.out)" }
    else { 'the Api logs under .hekate-local/logs' }
}

function Test-HLAuthFailureLogged($ApiState) {
    if (-not ($ApiState -and $ApiState.ContainsKey('logs') -and $ApiState.logs)) { return $false }
    foreach ($f in $ApiState.logs.err, $ApiState.logs.out) {
        if ($f -and (Test-Path $f) -and (Select-String -Path $f -Pattern '28P01|password authentication failed' -Quiet)) { return $true }
    }
    return $false
}

function Get-HLPasswordFingerprint($Config) {
    [Convert]::ToHexString([System.Security.Cryptography.SHA256]::HashData([System.Text.Encoding]::UTF8.GetBytes($Config.PgPassword)))
}

function Test-HLOwnedContainer($Config, $Container) {
    [bool]($Container -and $Container.Project -eq $Config.ComposeProject -and $Container.Workspace -eq $Config.RepoRoot)
}

# ---------------------------------------------------------------------------
# Preflight + readiness
# ---------------------------------------------------------------------------

function Assert-HLPreflight($Config) {
    Assert-HLConfig $Config
    if (-not (Test-HLCommand 'docker')) {
        throw ("No 'docker' CLI found on PATH. The local profile runs Postgres+AGE from context-store/Dockerfile.postgres " +
            "and needs a Docker-compatible container runtime with Compose v2 that provides the 'docker' command " +
            "(for example Docker Desktop). Install and start one, then re-run 'hekate-local.ps1 start'. Nothing was started.")
    }
    $info = Invoke-HLDocker @('info', '--format', '{{.ServerVersion}}')
    if ($info.ExitCode -ne 0) {
        throw "A 'docker' CLI was found but its container engine is not reachable ('docker info' failed: $($info.Output)). Start the runtime and retry. Nothing was started."
    }
    $compose = Invoke-HLDocker @('compose', 'version')
    if ($compose.ExitCode -ne 0) {
        throw "The 'docker compose' plugin is not available ($($compose.Output)). Install/enable Docker Compose v2 and retry. Nothing was started."
    }
    if (-not (Test-HLCommand 'dotnet')) {
        throw 'dotnet SDK not found on PATH; the context-store Api targets net10.0. Install the .NET 10 SDK and retry. Nothing was started.'
    }
    if (-not (Test-Path $Config.ComposeFile)) { throw "Missing $($Config.ComposeFile)." }
}

function Get-HLReadiness($Config) {
    $r = Invoke-HLHttpGet "$($Config.ApiUrl)/api/health/ready"
    $ready = $r.StatusCode -eq 200 -and $r.Body -and $r.Body.ready -eq $true
    [pscustomobject]@{ Ready = [bool]$ready; StatusCode = $r.StatusCode; Detail = $r.Body }
}

function Get-HLAttempts($Config, [int]$TimeoutSec) {
    [Math]::Max(1, [int][Math]::Ceiling($TimeoutSec / $Config.PollIntervalSec))
}

function Wait-HLContainerHealthy($Config, [string]$ContainerId) {
    for ($i = 0; $i -lt (Get-HLAttempts $Config $Config.HealthTimeoutSec); $i++) {
        $c = Get-HLContainer $Config $ContainerId
        if (-not $c) { throw "Local database container $ContainerId disappeared during startup." }
        if (-not $c.Running) { throw "Local database container exited during startup. Inspect with: docker logs $ContainerId" }
        if ($c.Health -eq 'healthy') { return }
        Wait-HLInterval $Config.PollIntervalSec
    }
    throw "Local database container did not become healthy within $($Config.HealthTimeoutSec)s. Inspect with: docker logs $ContainerId"
}

function Wait-HLApiReady($Config, $ApiState) {
    $last = $null
    for ($i = 0; $i -lt (Get-HLAttempts $Config $Config.HealthTimeoutSec); $i++) {
        if (-not (Test-HLOwnedApi $Config $ApiState)) {
            if (Test-HLAuthFailureLogged $ApiState) {
                throw ("The Api could not log in to the local database (password authentication failed). " +
                    "Postgres keeps the password from when the hekate-local volume was first initialized; " +
                    "set HEKATE_LOCAL_PG_PASSWORD back to that value. See $(Get-HLApiLogHint $ApiState).")
            }
            throw "Context-store Api exited during startup. See $(Get-HLApiLogHint $ApiState)."
        }
        $last = Get-HLReadiness $Config
        if ($last.Ready) { return $last }
        Wait-HLInterval $Config.PollIntervalSec
    }
    $detail = if ($last -and $last.Detail) { ($last.Detail | ConvertTo-Json -Compress -Depth 4) } else { 'no response' }
    throw "Context-store Api not ready within $($Config.HealthTimeoutSec)s at $($Config.ApiUrl)/api/health/ready (last: $detail). Logs: $(Get-HLApiLogHint $ApiState)"
}

# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

function Undo-HLStart($Config, $State, $Attempted) {
    # Roll back only what THIS invocation started. Never removes the volume. Anything
    # that cannot be stopped stays in the ledger so `stop` can retry it.
    $notes = @()
    if ($Attempted.api -and $State.api) {
        if (Test-HLOwnedApi $Config $State.api) {
            try {
                $mode = Stop-HLApi $Config $State.api
                $notes += "stopped Api pid $($State.api.pid) ($mode)"
                $State.api = $null
            }
            catch { $notes += "FAILED to stop Api pid $($State.api.pid), kept in ledger: $($_.Exception.Message)" }
        }
        else { $State.api = $null }
    }
    if ($Attempted.container) {
        $c = if ($State.container) { Get-HLContainer $Config $State.container.id } else { Find-HLComposeContainer $Config }
        if (Test-HLOwnedContainer $Config $c) {
            $State.container = @{ id = $c.Id }
            if ($c.Running) {
                $r = Invoke-HLDocker @('stop', $c.Id)
                $notes += if ($r.ExitCode -eq 0) { "stopped container $((Format-HLShortId $c.Id)) (volume kept)" }
                          else { "FAILED to stop container $((Format-HLShortId $c.Id)), kept in ledger: $($r.Output)" }
            }
        }
        elseif (-not $c) { $notes += 'no hekate-local container found to roll back' }
    }
    Write-HLState $Config $State
    return $notes
}

function Start-HekateLocal {
    [CmdletBinding()]
    param($Config = (Get-HLConfig))

    Assert-HLPreflight $Config
    Invoke-HLLocked $Config {
        $state = Read-HLState $Config
        if (-not $state) { $state = New-HLState $Config }

        # Reconcile the ledger with reality; drop entries we can no longer prove we own.
        $container = $null
        if ($state.container) {
            $c = Get-HLContainer $Config $state.container.id
            if (Test-HLOwnedContainer $Config $c) { $container = $c } else { $state.container = $null }
        }
        $apiOwned = Test-HLOwnedApi $Config $state.api
        if (-not $apiOwned) { $state.api = $null }

        # The volume keeps the password it was initialized with; catch a changed
        # HEKATE_LOCAL_PG_PASSWORD up front instead of as a readiness timeout.
        if ($state.ContainsKey('pgPasswordSha256') -and $state.pgPasswordSha256 -and $state.pgPasswordSha256 -ne (Get-HLPasswordFingerprint $Config)) {
            throw ("HEKATE_LOCAL_PG_PASSWORD differs from the password last verified against the hekate-local volume. " +
                "Postgres only applies the password at first initialization; restore the previous value. Nothing was started.")
        }

        if ($container -and $container.Running -and $apiOwned) {
            $ready = Get-HLReadiness $Config
            if ($ready.Ready) {
                Write-HLState $Config $state
                return [pscustomobject]@{ Result = 'already-running'; ApiUrl = $Config.ApiUrl; DbPort = $Config.DbPort }
            }
        }

        $attempted = @{ container = $false; api = $false }
        try {
            $dbRunning = [bool]($container -and $container.Running)
            if (-not $dbRunning -and (Test-HLPortInUse $Config.DbPort)) {
                throw "Port $($Config.DbPort) (local database) is already in use by another process. Free it or set HEKATE_LOCAL_DB_PORT; nothing else was stopped."
            }
            if (-not $apiOwned -and (Test-HLPortInUse $Config.ApiPort)) {
                throw "Port $($Config.ApiPort) (local Api) is already in use by another process. Free it or set HEKATE_LOCAL_API_PORT; nothing else was stopped."
            }

            if (-not $dbRunning) {
                Write-HLComposeEnv $Config
                $attempted.container = $true
                $up = Invoke-HLDocker ((Get-HLComposeArgs $Config) + @('up', '-d', 'postgres'))
                # Record whatever compose created even if `up` failed part-way.
                $c = Find-HLComposeContainer $Config
                if ($c) {
                    if (-not (Test-HLOwnedContainer $Config $c)) {
                        throw 'Compose container failed ownership verification (project/workspace labels); refusing to continue.'
                    }
                    $state.container = @{ id = $c.Id }
                    Write-HLState $Config $state
                    $container = $c
                }
                if ($up.ExitCode -ne 0) { throw "docker compose up failed: $($up.Output)" }
                if (-not $c) { throw 'docker compose up reported success but no hekate-local container was found.' }
            }

            Wait-HLContainerHealthy $Config $container.Id

            if (-not $apiOwned) {
                New-Item -ItemType Directory -Force -Path $Config.LogDir | Out-Null
                $build = Invoke-HLDotnet @('build', $Config.ApiProject, '-c', 'Debug', '-o', $Config.ApiBinDir, '--nologo', '-v', 'q')
                if ($build.ExitCode -ne 0) { throw "dotnet build of the context-store Api failed:`n$($build.Output)" }
                $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
                $logs = @{ out = (Join-Path $Config.LogDir "api-$stamp.out.log"); err = (Join-Path $Config.LogDir "api-$stamp.err.log") }
                $token = New-HLToken
                $attempted.api = $true
                $apiPid = Start-HLApiProcess $Config (Get-HLApiEnvironment $Config $token) $logs
                $info = Get-HLProcessInfo $apiPid
                $state.api = @{ pid = $apiPid; startTime = if ($info) { $info.StartTime } else { '' }; dll = $Config.ApiDll; shutdownToken = $token; logs = $logs }
                if (-not $info) {
                    $hint = if (Test-HLAuthFailureLogged $state.api) { ' Password authentication failed: HEKATE_LOCAL_PG_PASSWORD must match the value the volume was initialized with.' } else { '' }
                    $state.api = $null
                    throw "Context-store Api exited immediately.$hint See $($logs.err)."
                }
                Write-HLState $Config $state
            }

            $ready = Wait-HLApiReady $Config $state.api
            $state.pgPasswordSha256 = Get-HLPasswordFingerprint $Config   # proven to work against this volume
            Write-HLState $Config $state
            return [pscustomobject]@{ Result = 'started'; ApiUrl = $Config.ApiUrl; DbPort = $Config.DbPort; Readiness = $ready.Detail }
        }
        catch {
            $err = $_
            $notes = Undo-HLStart $Config $state $attempted
            $suffix = if ($notes) { ' Rollback: ' + ($notes -join '; ') + '.' } else { '' }
            throw "$($err.Exception.Message)$suffix"
        }
    }
}

function Get-HekateLocalStatus {
    [CmdletBinding()]
    param($Config = (Get-HLConfig))

    $state = Read-HLState $Config
    $dbState = 'none'      # none | running | stopped | missing | not-owned | unknown
    $dbDetail = 'none'
    $apiState = 'none'     # none | running | stale | exited

    if ($state -and $state.container) {
        if (-not (Test-HLCommand 'docker')) { $dbState = 'unknown'; $dbDetail = 'unknown (docker not found)' }
        else {
            $c = Get-HLContainer $Config $state.container.id
            if (-not $c) { $dbState = 'missing'; $dbDetail = 'missing' }
            elseif (-not (Test-HLOwnedContainer $Config $c)) { $dbState = 'not-owned'; $dbDetail = 'not owned by this workspace' }
            elseif ($c.Running) { $dbState = 'running'; $dbDetail = "running ($($c.Health))" }
            else { $dbState = 'stopped'; $dbDetail = 'stopped (volume kept)' }
        }
    }
    if ($state -and $state.api) {
        if (Test-HLOwnedApi $Config $state.api) { $apiState = 'running' }
        elseif (Get-HLProcessInfo ([int]$state.api.pid)) { $apiState = 'stale' }
        else { $apiState = 'exited' }
    }
    $apiDetail = switch ($apiState) {
        'running' { "running (pid $($state.api.pid))" }
        'stale' { "stale (pid $($state.api.pid) now belongs to another process)" }
        default { $apiState }
    }

    $readiness = if ($apiState -eq 'running') { Get-HLReadiness $Config } else { $null }
    # Ready needs all three: owned running DB, owned running Api, Api readiness probe.
    $overall = if ($dbState -eq 'running' -and $apiState -eq 'running' -and $readiness -and $readiness.Ready) { 'ready' }
        elseif ($dbState -eq 'unknown') { 'unknown' }
        elseif ($dbState -eq 'running' -or $apiState -eq 'running') { 'degraded' }
        else { 'stopped' }

    [pscustomobject]@{
        Status    = $overall
        Database  = $dbDetail
        Api       = $apiDetail
        ApiUrl    = $Config.ApiUrl
        DbPort    = $Config.DbPort
        Readiness = if ($readiness) { $readiness.Detail } else { $null }
        Logs      = if ($state -and $state.api -and $state.api.ContainsKey('logs')) { $state.api.logs } else { $null }
        StateFile = $Config.StateFile
    }
}

function Stop-HekateLocal {
    [CmdletBinding()]
    param($Config = (Get-HLConfig))

    Invoke-HLLocked $Config {
        $state = Read-HLState $Config
        if (-not $state -or (-not $state.api -and -not $state.container)) {
            return [pscustomobject]@{ Result = 'nothing-to-stop'; Actions = @() }
        }
        $actions = @()
        $complete = $true

        if ($state.api) {
            $apiPid = [int]$state.api.pid
            if (Test-HLOwnedApi $Config $state.api) {
                try {
                    $mode = Stop-HLApi $Config $state.api
                    $actions += "stopped Api pid $apiPid ($mode)"
                    $state.api = $null
                }
                catch { $complete = $false; $actions += "FAILED to stop Api pid ${apiPid}, kept in ledger: $($_.Exception.Message)" }
            }
            elseif (Get-HLProcessInfo $apiPid) {
                $actions += "left pid $apiPid running: it is not the Api this launcher started (start time/command differ)"
                $state.api = $null
            }
            else { $actions += 'Api already exited'; $state.api = $null }
            Write-HLState $Config $state
        }

        if ($state.container) {
            if (-not (Test-HLCommand 'docker')) {
                $complete = $false
                $actions += 'docker not found: database container state unknown and not stopped; ledger kept'
            }
            else {
                $c = Get-HLContainer $Config $state.container.id
                if (-not $c) { $actions += 'database container no longer exists'; $state.container = $null }
                elseif (-not (Test-HLOwnedContainer $Config $c)) {
                    $actions += "left container $((Format-HLShortId $c.Id)) alone: labels do not match this workspace's hekate-local project"
                    $state.container = $null
                }
                elseif ($c.Running) {
                    $r = Invoke-HLDocker @('stop', $c.Id)
                    if ($r.ExitCode -eq 0) { $actions += "stopped database container $((Format-HLShortId $c.Id)) (volume kept)" }
                    else { $complete = $false; $actions += "FAILED to stop database container, kept in ledger: $($r.Output)" }
                }
                else { $actions += 'database container already stopped (volume kept)' }
            }
        }
        Write-HLState $Config $state
        [pscustomobject]@{ Result = if ($complete) { 'stopped' } else { 'partial' }; Actions = $actions }
    }
}

function Get-HLOwnedRunningContainer($Config) {
    if (-not (Test-HLCommand 'docker')) { throw 'Docker CLI not found on PATH.' }
    $state = Read-HLState $Config
    if (-not $state -or -not $state.container) { throw "The local profile is not running. Run 'hekate-local.ps1 start' first." }
    $c = Get-HLContainer $Config $state.container.id
    if (-not (Test-HLOwnedContainer $Config $c) -or -not $c.Running) {
        throw "The local database container is not running (or not owned by this workspace). Run 'hekate-local.ps1 start' first."
    }
    return $c
}

function New-HLContainerTempPath([string]$Kind) { "/tmp/hekate-local-$Kind-$([guid]::NewGuid().ToString('N')).dump" }

function Backup-HekateLocal {
    [CmdletBinding()]
    param($Config = (Get-HLConfig), [string]$OutFile)

    Invoke-HLLocked $Config {
        $c = Get-HLOwnedRunningContainer $Config
        if (-not $OutFile) {
            New-Item -ItemType Directory -Force -Path $Config.BackupDir | Out-Null
            $OutFile = Join-Path $Config.BackupDir ("code_storage-{0}.dump" -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
        }
        if (Test-Path $OutFile) { throw "Backup target $OutFile already exists; refusing to overwrite." }
        # Scope: the code_storage database only (plans/nodes + AGE graph). The
        # orchestration database in the same container is not used by this profile.
        $tmp = New-HLContainerTempPath 'backup'
        # Copy to a unique .partial next to the target, then move into place without
        # overwrite, so an interrupted backup never looks like a finished one.
        $partial = "$OutFile.$([guid]::NewGuid().ToString('N')).partial"
        try {
            $dump = Invoke-HLDocker @('exec', $c.Id, 'pg_dump', '-U', 'postgres', '-d', $Config.DbName, '-Fc', '-f', $tmp)
            if ($dump.ExitCode -ne 0) { throw "pg_dump failed: $($dump.Output)" }
            $list = Invoke-HLDocker @('exec', $c.Id, 'pg_restore', '-l', $tmp)
            if ($list.ExitCode -ne 0) { throw "pg_dump produced an unreadable archive: $($list.Output)" }
            $cp = Invoke-HLDocker @('cp', "$($c.Id):$tmp", $partial)
            if ($cp.ExitCode -ne 0) { throw "docker cp of the backup failed: $($cp.Output)" }
            [System.IO.File]::Move($partial, $OutFile, $false)
        }
        finally {
            Invoke-HLDocker @('exec', $c.Id, 'rm', '-f', $tmp) | Out-Null
            if (Test-Path $partial) { Remove-Item -Force $partial }
        }
        [pscustomobject]@{
            Result   = 'backed-up'
            File     = $OutFile
            Bytes    = (Get-Item $OutFile).Length
            Sha256   = (Get-FileHash -Algorithm SHA256 $OutFile).Hash
            Database = $Config.DbName
        }
    }
}

function Assert-HLRestoreTarget([string]$TargetDatabase) {
    if ([string]::IsNullOrWhiteSpace($TargetDatabase)) {
        throw 'Restore requires an explicit -TargetDatabase naming a NEW disposable database.'
    }
    if ($TargetDatabase -cnotmatch '^[a-z_][a-z0-9_]{0,62}$') {
        throw "Invalid target database name '$TargetDatabase' (lowercase letters, digits, underscore)."
    }
    if ($script:ReservedDatabases -contains $TargetDatabase) {
        throw "Refusing to restore into '$TargetDatabase': it is a live/system database. Restore only targets a new disposable database."
    }
}

function Invoke-HLPsql($ContainerId, [string]$Database, [string]$Sql) {
    Invoke-HLDocker @('exec', $ContainerId, 'psql', '-U', 'postgres', '-d', $Database, '-v', 'ON_ERROR_STOP=1', '-qtAc', $Sql)
}

# A logical dump/restore of an AGE database keeps the SOURCE schema OID in
# ag_graph.graphid and ag_label.graph while ag_graph.namespace remaps to the new schema,
# so Cypher fails with "graph with oid N does not exist" (apache/age#2503). This repairs
# the NEW restore target only, in one atomic statement, for the one supported shape
# (exactly one graph; fk_graph_oid exactly as AGE defines it), re-adding the exact
# captured FK definition. Anything else fails closed. Idempotent.
$script:AgeRepairSql = @'
DO $$
DECLARE
    n_graph int;
    fk_def text;
    stale boolean;
BEGIN
    SELECT count(*) INTO n_graph FROM ag_catalog.ag_graph;
    IF n_graph <> 1 THEN
        RAISE EXCEPTION 'hekate-local age-repair: unsupported shape, expected exactly 1 graph, found %', n_graph;
    END IF;
    SELECT pg_get_constraintdef(c.oid) INTO fk_def
    FROM pg_constraint c
    WHERE c.conname = 'fk_graph_oid' AND c.conrelid = 'ag_catalog.ag_label'::regclass;
    IF fk_def IS NULL OR fk_def !~ '^FOREIGN KEY \(graph\) REFERENCES ag_catalog\.ag_graph\(graphid\)' THEN
        RAISE EXCEPTION 'hekate-local age-repair: unsupported fk_graph_oid definition: %', coalesce(fk_def, '(missing)');
    END IF;
    SELECT graphid <> namespace::oid INTO stale FROM ag_catalog.ag_graph;
    IF NOT stale THEN
        RAISE NOTICE 'hekate-local age-repair: already-consistent';
        RETURN;
    END IF;
    ALTER TABLE ag_catalog.ag_label DROP CONSTRAINT fk_graph_oid;
    UPDATE ag_catalog.ag_label SET graph = (SELECT namespace::oid FROM ag_catalog.ag_graph)
        WHERE graph = (SELECT graphid FROM ag_catalog.ag_graph);
    UPDATE ag_catalog.ag_graph SET graphid = namespace::oid;
    EXECUTE 'ALTER TABLE ag_catalog.ag_label ADD CONSTRAINT fk_graph_oid ' || fk_def;
    IF EXISTS (SELECT 1 FROM ag_catalog.ag_label l
               WHERE NOT EXISTS (SELECT 1 FROM ag_catalog.ag_graph g WHERE g.graphid = l.graph)) THEN
        RAISE EXCEPTION 'hekate-local age-repair: label references still invalid after repair';
    END IF;
    RAISE NOTICE 'hekate-local age-repair: repaired';
END $$;
'@

function Repair-HLAgeCatalog($ContainerId, [string]$Database) {
    $r = Invoke-HLPsql $ContainerId $Database $script:AgeRepairSql
    $outcome = if ($r.ExitCode -ne 0) { "failed: $($r.Output)" }
        elseif ($r.Output -match 'age-repair: repaired') { 'repaired' }
        elseif ($r.Output -match 'age-repair: already-consistent') { 'already-consistent' }
        else { "failed: no repair outcome reported ($($r.Output))" }
    [pscustomobject]@{ Ok = $outcome -in 'repaired', 'already-consistent'; Outcome = $outcome }
}

function Get-HLAgeLabels($ContainerId, [string]$Database) {
    $r = Invoke-HLPsql $ContainerId $Database ('SELECT l.name FROM ag_catalog.ag_label l JOIN ag_catalog.ag_graph g ON l.graph = g.graphid ' +
        'WHERE g.name = ''code_graph'' ORDER BY 1')
    if ($r.ExitCode -ne 0) { return $null }
    , @($r.Output -split "\r?\n" | ForEach-Object { $_.Trim() } | Where-Object { $_ })
}

function Test-HLAgeGraph($ContainerId, [string]$Database, [string]$SourceDatabase) {
    # A catalog row alone does not prove the graph is usable: run real Cypher in a
    # session that does LOAD 'age' + search_path, and compare labels with the source.
    $prefix = 'LOAD ''age''; SET search_path = ag_catalog, "$user", public; '
    $v = Invoke-HLPsql $ContainerId $Database ($prefix + 'SELECT * FROM cypher(''code_graph'', $$ MATCH (n) RETURN count(n) $$) AS (c agtype);')
    $e = Invoke-HLPsql $ContainerId $Database ($prefix + 'SELECT * FROM cypher(''code_graph'', $$ MATCH ()-[r]->() RETURN count(r) $$) AS (c agtype);')
    $targetLabels = Get-HLAgeLabels $ContainerId $Database
    $sourceLabels = Get-HLAgeLabels $ContainerId $SourceDatabase
    $missing = if ($null -ne $targetLabels -and $null -ne $sourceLabels) { , @($sourceLabels | Where-Object { $targetLabels -notcontains $_ }) } else { $null }
    [pscustomobject]@{
        Ok            = $v.ExitCode -eq 0 -and $e.ExitCode -eq 0 -and $null -ne $missing -and $missing.Count -eq 0 -and $targetLabels.Count -gt 0
        Vertices      = if ($v.ExitCode -eq 0) { $v.Output.Trim() } else { "cypher failed: $($v.Output)" }
        Edges         = if ($e.ExitCode -eq 0) { $e.Output.Trim() } else { "cypher failed: $($e.Output)" }
        MissingLabels = if ($null -eq $missing) { 'unavailable' } else { $missing }
    }
}

function Restore-HekateLocal {
    [CmdletBinding()]
    param($Config = (Get-HLConfig), [Parameter(Mandatory)][string]$BackupFile, [string]$TargetDatabase)

    Assert-HLRestoreTarget $TargetDatabase
    if (-not (Test-Path $BackupFile)) { throw "Backup file not found: $BackupFile" }
    Invoke-HLLocked $Config {
        $c = Get-HLOwnedRunningContainer $Config

        $exists = Invoke-HLPsql $c.Id 'postgres' "SELECT 1 FROM pg_database WHERE datname = '$TargetDatabase'"
        if ($exists.ExitCode -ne 0) { throw "Could not check for existing database: $($exists.Output)" }
        if ($exists.Output.Trim() -eq '1') {
            throw "Database '$TargetDatabase' already exists. Restore only creates a new database; choose another name."
        }

        $create = Invoke-HLDocker @('exec', $c.Id, 'createdb', '-U', 'postgres', $TargetDatabase)
        if ($create.ExitCode -ne 0) { throw "createdb failed: $($create.Output)" }

        # From here on the disposable database exists; every outcome names it.
        $retained = "Disposable database '$TargetDatabase' was created and left in place; drop it with: docker exec $(Format-HLShortId $c.Id) dropdb -U postgres $TargetDatabase"
        $tmp = New-HLContainerTempPath 'restore'
        try {
            $cp = Invoke-HLDocker @('cp', $BackupFile, "$($c.Id):$tmp")
            if ($cp.ExitCode -ne 0) { throw "docker cp of the backup into the container failed: $($cp.Output)" }
            $restore = Invoke-HLDocker @('exec', $c.Id, 'pg_restore', '-U', 'postgres', '-d', $TargetDatabase, '--no-owner', $tmp)
            $count = Invoke-HLPsql $c.Id $TargetDatabase 'SELECT count(*) FROM public.nodes'
            # Target-only: the source database is never passed here.
            $repair = Repair-HLAgeCatalog $c.Id $TargetDatabase
            $graph = Test-HLAgeGraph $c.Id $TargetDatabase $Config.DbName
        }
        catch { throw "$($_.Exception.Message) $retained" }
        finally { Invoke-HLDocker @('exec', $c.Id, 'rm', '-f', $tmp) | Out-Null }

        $ok = $restore.ExitCode -eq 0 -and $count.ExitCode -eq 0 -and $repair.Ok -and $graph.Ok
        [pscustomobject]@{
            Result          = if ($ok) { 'restored' } else { 'restored-with-errors' }
            TargetDatabase  = $TargetDatabase
            PgRestoreExit   = $restore.ExitCode
            PgRestoreOutput = $restore.Output
            NodeCount       = if ($count.ExitCode -eq 0) { $count.Output.Trim() } else { "unavailable: $($count.Output)" }
            AgeRepair       = $repair.Outcome
            AgeGraph        = $graph
            Note            = $retained
        }
    }
}

Export-ModuleMember -Function Get-HLConfig, Start-HekateLocal, Get-HekateLocalStatus, Stop-HekateLocal, Backup-HekateLocal, Restore-HekateLocal
