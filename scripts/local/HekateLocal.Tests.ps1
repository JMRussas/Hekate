# Pester 3.4 tests for HekateLocal.psm1 (Pester 3.4 ships with Windows; no install needed).
#   pwsh -NoProfile -Command "Import-Module Pester -RequiredVersion 3.4.0; Invoke-Pester scripts/local/HekateLocal.Tests.ps1"
#
# Every external effect (docker, dotnet, ports, processes, HTTP, sleep) is mocked in
# the main Describe; these tests never start or stop anything real. The argv
# forwarding Describe runs the real wrapper against a fake docker.cmd.

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Import-Module (Join-Path $here 'HekateLocal.psm1') -Force

function New-TestConfig {
    $root = Join-Path $TestDrive ([guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Force -Path (Join-Path $root 'context-store') | Out-Null
    Set-Content -Path (Join-Path $root 'context-store\docker-compose.local.yml') -Value 'services: {}'
    $c = Get-HLConfig -RepoRoot $root
    $c.HealthTimeoutSec = 6
    $c.ShutdownTimeoutSec = 4
    $c.PollIntervalSec = 2
    return $c
}

function New-Container([string]$Id, [bool]$Running, [string]$Health, [string]$Project, [string]$Workspace) {
    @{
        Id     = $Id
        State  = @{ Running = $Running; Health = @{ Status = $Health } }
        Config = @{ Labels = @{ 'com.docker.compose.project' = $Project; 'com.hekate.local.workspace' = $Workspace } }
    }
}

function Reset-World($Config) {
    $global:HL = @{
        Calls            = New-Object System.Collections.ArrayList
        Missing          = @()
        InfoExit         = 0
        UpExit           = 0
        StopExit         = 0
        BusyPorts        = @()
        Inspect          = @{}
        PsId             = 'cid1'
        AfterUp          = (New-Container 'cid1' $true 'healthy' 'hekate-local' $Config.RepoRoot)
        Procs            = @{}
        Stopped          = New-Object System.Collections.ArrayList
        StopFails        = $false
        Ready            = $true
        ApiStarts        = 0
        CpExit           = 0
        RepairExit       = 0
        RepairOutput     = 'NOTICE:  hekate-local age-repair: repaired'
        DbExists         = $false
        NextPid          = 4242
        Labels           = @{ code_storage = @('CALLS', 'CodeNode'); restore_check = @('CALLS', 'CodeNode') }
        AuthFail         = $false
        ShutdownCode     = 202
        ExitsOnShutdown  = $true
        ShutdownRequests = 0
    }
}

function New-ApiState($Config, [int]$ProcessId = 4242, [string]$StartTime = '2026-10-06T12:00:00.1234567Z') {
    @{ pid = $ProcessId; startTime = $StartTime; dll = $Config.ApiDll; shutdownToken = 'tok' }
}

function Add-ApiProc($Config, [int]$ProcessId = 4242, [string]$StartTime = '2026-10-06T12:00:00.1234567Z') {
    $global:HL.Procs[[string]$ProcessId] = @{ StartTime = $StartTime; CommandLine = "dotnet `"$($Config.ApiDll)`"" }
}

function Write-TestState($Config, $Container, $Api) {
    New-Item -ItemType Directory -Force -Path $Config.StateDir | Out-Null
    @{ version = 1; workspace = $Config.RepoRoot; composeProject = 'hekate-local'; container = $Container; api = $Api } |
        ConvertTo-Json -Depth 5 | Set-Content $Config.StateFile
}

function Read-TestState($Config) { Get-Content -Raw $Config.StateFile | ConvertFrom-Json -AsHashtable -DateKind String }

function Get-DockerCalls([string]$Like) { @($global:HL.Calls | Where-Object { $_ -like $Like }) }

function Get-DestructiveDockerCalls {
    @($global:HL.Calls | Where-Object { $_ -match '(^| )down( |$)|(^| )-v( |$)|volume|(^| )rm( |$)|(^| )kill( |$)' -and $_ -notmatch 'rm -f /tmp/hekate-local-' })
}

Describe 'HekateLocal launcher' {

    Mock -ModuleName HekateLocal Test-HLCommand { $global:HL.Missing -notcontains $Name }
    Mock -ModuleName HekateLocal Test-HLPortInUse { $global:HL.BusyPorts -contains $Port }
    Mock -ModuleName HekateLocal Wait-HLInterval { }
    Mock -ModuleName HekateLocal Invoke-HLDotnet { [pscustomobject]@{ ExitCode = 0; Output = '' } }
    Mock -ModuleName HekateLocal Invoke-HLHttpGet {
        if ($global:HL.AuthFail) {
            # The Api logs 28P01 from Schema.Initialize and exits before serving.
            Set-Content -Path $global:HL.ApiErrLog -Value 'Npgsql.PostgresException: 28P01: password authentication failed for user "postgres"'
            $global:HL.Procs.Clear()
            return [pscustomobject]@{ StatusCode = 0; Body = $null }
        }
        if ($global:HL.Ready) { [pscustomobject]@{ StatusCode = 200; Body = [pscustomobject]@{ ready = $true } } }
        else { [pscustomobject]@{ StatusCode = 503; Body = [pscustomobject]@{ ready = $false } } }
    }
    Mock -ModuleName HekateLocal Get-HLProcessInfo {
        $p = $global:HL.Procs[[string]$ProcessId]
        if ($p) { [pscustomobject]@{ Id = $ProcessId; StartTime = $p.StartTime; CommandLine = $p.CommandLine } }
    }
    Mock -ModuleName HekateLocal Stop-HLProcessById {
        if ($global:HL.StopFails) { throw 'Access is denied' }
        [void]$global:HL.Stopped.Add($ProcessId)
        $global:HL.Procs.Remove([string]$ProcessId)
    }
    Mock -ModuleName HekateLocal Start-HLApiProcess {
        $global:HL.ApiStarts++
        $global:HL.ApiErrLog = $Logs.err
        $global:HL.Procs[[string]$global:HL.NextPid] = @{ StartTime = '2026-10-06T12:00:00.1234567Z'; CommandLine = "dotnet `"$($Config.ApiDll)`"" }
        $global:HL.NextPid
    }
    Mock -ModuleName HekateLocal Invoke-HLShutdownRequest {
        $global:HL.ShutdownRequests++
        if ($global:HL.ShutdownCode -eq 202 -and $global:HL.ExitsOnShutdown) { $global:HL.Procs.Clear() }
        $global:HL.ShutdownCode
    }
    Mock -ModuleName HekateLocal Invoke-HLDocker {
        $a = $Arguments -join ' '
        [void]$global:HL.Calls.Add($a)
        $res = { param($code, $o) [pscustomobject]@{ ExitCode = $code; Output = $o } }
        if ($a -like 'info*') { return & $res $global:HL.InfoExit 'Cannot connect to the Docker daemon' }
        if ($a -eq 'compose version') { return & $res 0 'Docker Compose version v2' }
        if ($a -like '* up -d postgres') { $global:HL.Inspect[$global:HL.PsId] = $global:HL.AfterUp; return & $res $global:HL.UpExit 'up output' }
        if ($a -like '* ps -a -q postgres') {
            if ($global:HL.Inspect.ContainsKey($global:HL.PsId)) { return & $res 0 $global:HL.PsId }
            return & $res 0 ''
        }
        if ($a -like 'inspect *') {
            $c = $global:HL.Inspect[$Arguments[-1]]
            if ($c) { return & $res 0 ($c | ConvertTo-Json -Depth 6 -Compress) }
            return & $res 1 'Error: No such object'
        }
        if ($a -like 'cp *') {
            if ($global:HL.CpExit -ne 0) { return & $res $global:HL.CpExit 'cp failed' }
            if ($Arguments[-1] -notmatch '^[^:]+:/') { Set-Content -Path $Arguments[-1] -Value 'dumpdata' }   # copy-out
            return & $res 0 ''
        }
        if ($a -like '*age-repair*') { return & $res $global:HL.RepairExit $global:HL.RepairOutput }
        if ($a -like '*pg_database*') { return & $res 0 $(if ($global:HL.DbExists) { '1' } else { '' }) }
        if ($a -like '*cypher*') { return & $res 0 '3' }
        if ($a -like '*ag_label*') {
            $db = $Arguments[[array]::IndexOf($Arguments, '-d') + 1]
            return & $res 0 ($global:HL.Labels[$db] -join "`n")
        }
        if ($a -like 'stop *') {
            if ($global:HL.StopExit -ne 0) { return & $res $global:HL.StopExit 'cannot stop' }
            $global:HL.Inspect[$Arguments[-1]].State.Running = $false
            return & $res 0 ''
        }
        return & $res 0 ''
    }

    Context 'config and preflight' {
        It 'gives an actionable error and starts nothing when docker is missing' {
            $cfg = New-TestConfig; Reset-World $cfg; $global:HL.Missing = @('docker')
            { Start-HekateLocal -Config $cfg } | Should Throw 'Docker-compatible container runtime'
            $global:HL.Calls.Count | Should Be 0
            Assert-MockCalled Start-HLApiProcess -ModuleName HekateLocal -Times 0 -Exactly -Scope It
            Test-Path $cfg.StateFile | Should Be $false
        }

        It 'reports an unreachable docker engine instead of false readiness' {
            $cfg = New-TestConfig; Reset-World $cfg; $global:HL.InfoExit = 1
            { Start-HekateLocal -Config $cfg } | Should Throw 'engine is not reachable'
            (Get-DockerCalls '* up *').Count | Should Be 0
        }

        It 'refuses a non-loopback database host' {
            $cfg = New-TestConfig; Reset-World $cfg; $cfg.DbHost = '192.168.1.164'
            { Start-HekateLocal -Config $cfg } | Should Throw 'not loopback'
            $global:HL.Calls.Count | Should Be 0
        }

        It 'rejects a password that could inject connection-string keys' {
            $cfg = New-TestConfig; Reset-World $cfg; $cfg.PgPassword = 'x;Host=192.168.1.164'
            { Start-HekateLocal -Config $cfg } | Should Throw 'HEKATE_LOCAL_PG_PASSWORD may only contain'
            $global:HL.Calls.Count | Should Be 0
        }

        It 'rejects out-of-range, equal ports and non-positive timeouts' {
            $cfg = New-TestConfig; Reset-World $cfg; $cfg.DbPort = 70000
            { Start-HekateLocal -Config $cfg } | Should Throw 'outside 1..65535'
            $cfg = New-TestConfig; $cfg.ApiPort = $cfg.DbPort
            { Start-HekateLocal -Config $cfg } | Should Throw 'must differ'
            $cfg = New-TestConfig; $cfg.HealthTimeoutSec = 0
            { Start-HekateLocal -Config $cfg } | Should Throw 'positive'
            $global:HL.Calls.Count | Should Be 0
        }

        It 'refuses an occupied database port without stopping anything' {
            $cfg = New-TestConfig; Reset-World $cfg; $global:HL.BusyPorts = @(5434)
            { Start-HekateLocal -Config $cfg } | Should Throw '5434 (local database) is already in use'
            (Get-DockerCalls '* up *').Count + (Get-DockerCalls 'stop *').Count | Should Be 0
            Assert-MockCalled Stop-HLProcessById -ModuleName HekateLocal -Times 0 -Exactly -Scope It
        }

        It 'refuses an occupied Api port without stopping anything' {
            $cfg = New-TestConfig; Reset-World $cfg; $global:HL.BusyPorts = @(5103)
            { Start-HekateLocal -Config $cfg } | Should Throw '5103 (local Api) is already in use'
            (Get-DockerCalls '* up *').Count | Should Be 0
            Assert-MockCalled Stop-HLProcessById -ModuleName HekateLocal -Times 0 -Exactly -Scope It
        }

        It 'refuses a malformed or foreign state file without touching anything' {
            $cfg = New-TestConfig; Reset-World $cfg
            New-Item -ItemType Directory -Force -Path $cfg.StateDir | Out-Null
            Set-Content $cfg.StateFile '{ not json'
            { Stop-HekateLocal -Config $cfg } | Should Throw 'not valid JSON'
            @{ version = 2; workspace = $cfg.RepoRoot; composeProject = 'hekate-local' } | ConvertTo-Json | Set-Content $cfg.StateFile
            { Stop-HekateLocal -Config $cfg } | Should Throw 'unexpected version/project'
            @{ version = 1; workspace = 'D:\elsewhere'; composeProject = 'hekate-local' } | ConvertTo-Json | Set-Content $cfg.StateFile
            { Stop-HekateLocal -Config $cfg } | Should Throw 'belongs to workspace'
            $global:HL.Calls.Count | Should Be 0
        }

        It 'reports a lock file that cannot be opened as itself, not as a concurrent operation' {
            $cfg = New-TestConfig; Reset-World $cfg
            New-Item -ItemType Directory -Force -Path $cfg.LockFile | Out-Null   # a directory where the lock file belongs
            { Stop-HekateLocal -Config $cfg } | Should Throw 'Could not open lock file'
        }

        It 'inspects by container type only' {
            $cfg = New-TestConfig; Reset-World $cfg
            Start-HekateLocal -Config $cfg | Out-Null
            $inspects = Get-DockerCalls 'inspect *'
            $inspects.Count | Should BeGreaterThan 0
            @($inspects | Where-Object { $_ -notlike 'inspect --type container *' }).Count | Should Be 0
        }

        It 'serializes operations with a lock file' {
            $cfg = New-TestConfig; Reset-World $cfg
            New-Item -ItemType Directory -Force -Path $cfg.StateDir | Out-Null
            $held = [System.IO.File]::Open($cfg.LockFile, 'OpenOrCreate', 'ReadWrite', 'None')
            try { { Stop-HekateLocal -Config $cfg } | Should Throw 'Another hekate-local operation is in progress' }
            finally { $held.Dispose() }
            (Stop-HekateLocal -Config $cfg).Result | Should Be 'nothing-to-stop'
        }
    }

    Context 'start' {
        It 'starts the owned container and Api and records verified ownership' {
            $cfg = New-TestConfig; Reset-World $cfg
            (Start-HekateLocal -Config $cfg).Result | Should Be 'started'
            $s = Read-TestState $cfg
            $s.container.id | Should Be 'cid1'
            $s.api.pid | Should Be 4242
            $s.api.startTime | Should Be '2026-10-06T12:00:00.1234567Z'
            $s.api.shutdownToken.Length | Should Be 64
            $s.api.logs.err | Should Match 'api-\d{8}-\d{6}\.err\.log$'
            (Get-DockerCalls '*-p hekate-local*up -d postgres').Count | Should Be 1
            (Get-DestructiveDockerCalls).Count | Should Be 0
        }

        It 'still recognizes its own Api after the ledger round-trips through disk (ISO start time)' {
            $cfg = New-TestConfig; Reset-World $cfg
            (Start-HekateLocal -Config $cfg).Result | Should Be 'started'
            (Get-HekateLocalStatus -Config $cfg).Api | Should Match '^running'
            (Start-HekateLocal -Config $cfg).Result | Should Be 'already-running'
            $stop = Stop-HekateLocal -Config $cfg
            ($stop.Actions -join ' ') | Should Match 'stopped Api pid 4242 \(graceful\)'
            $global:HL.Procs.ContainsKey('4242') | Should Be $false
        }

        It 'always sets HEKATE_PLAN_CONTRACT explicitly so an ambient flag cannot leak into the Api' {
            $cfg = New-TestConfig
            $off = & (Get-Module HekateLocal) { param($c) Get-HLApiEnvironment $c 'tok' } $cfg
            $off.HEKATE_PLAN_CONTRACT | Should Be '0'
            $cfg.PlanContract = $true
            $on = & (Get-Module HekateLocal) { param($c) Get-HLApiEnvironment $c 'tok' } $cfg
            $on.HEKATE_PLAN_CONTRACT | Should Be '1'
            $on.HEKATE_DISABLE_DISPATCHER | Should Be '1'
            $on.HEKATE_API_URLS | Should Be 'http://127.0.0.1:5103'
        }

        It 'is idempotent when already running and ready' {
            $cfg = New-TestConfig; Reset-World $cfg
            $global:HL.Inspect['cid1'] = $global:HL.AfterUp
            Add-ApiProc $cfg
            Write-TestState $cfg @{ id = 'cid1' } (New-ApiState $cfg)
            (Start-HekateLocal -Config $cfg).Result | Should Be 'already-running'
            (Get-DockerCalls '* up *').Count | Should Be 0
            $global:HL.ApiStarts | Should Be 0
        }

        It 'rolls back only newly started resources on partial startup and keeps the volume' {
            $cfg = New-TestConfig; Reset-World $cfg; $global:HL.Ready = $false
            { Start-HekateLocal -Config $cfg } | Should Throw 'Rollback'
            $global:HL.ShutdownRequests | Should Be 1
            $global:HL.Procs.ContainsKey('4242') | Should Be $false
            (Get-DockerCalls 'stop cid1').Count | Should Be 1
            (Get-DestructiveDockerCalls).Count | Should Be 0
            $s = Read-TestState $cfg
            $s.api | Should BeNullOrEmpty
            $s.container.id | Should Be 'cid1'
        }

        It 'records and rolls back a container that compose created before exiting non-zero' {
            $cfg = New-TestConfig; Reset-World $cfg; $global:HL.UpExit = 1
            { Start-HekateLocal -Config $cfg } | Should Throw 'docker compose up failed'
            (Get-DockerCalls 'stop cid1').Count | Should Be 1
            (Read-TestState $cfg).container.id | Should Be 'cid1'
            $global:HL.ApiStarts | Should Be 0
        }

        It 'keeps an Api it failed to stop in the ledger so stop can retry' {
            $cfg = New-TestConfig; Reset-World $cfg
            $global:HL.Ready = $false; $global:HL.ExitsOnShutdown = $false; $global:HL.StopFails = $true
            { Start-HekateLocal -Config $cfg } | Should Throw 'FAILED to stop Api pid 4242, kept in ledger'
            (Read-TestState $cfg).api.pid | Should Be 4242
        }

        It 'reports a forced stop honestly when graceful shutdown times out' {
            $cfg = New-TestConfig; Reset-World $cfg
            $global:HL.Ready = $false; $global:HL.ExitsOnShutdown = $false
            { Start-HekateLocal -Config $cfg } | Should Throw 'forced (still running 4s after graceful request)'
            $global:HL.Stopped -contains 4242 | Should Be $true
        }

        It 'does not stop a database container that was already running before this start' {
            $cfg = New-TestConfig; Reset-World $cfg; $global:HL.Ready = $false
            $global:HL.Inspect['cid1'] = $global:HL.AfterUp
            Write-TestState $cfg @{ id = 'cid1' } $null
            { Start-HekateLocal -Config $cfg } | Should Throw 'Rollback'
            $global:HL.Procs.ContainsKey('4242') | Should Be $false
            (Get-DockerCalls 'stop *').Count | Should Be 0
        }

        It 'refuses to continue when the compose container fails ownership verification' {
            $cfg = New-TestConfig; Reset-World $cfg
            $global:HL.AfterUp = New-Container 'cid1' $true 'healthy' 'hekate-local' 'D:\some\other\workspace'
            { Start-HekateLocal -Config $cfg } | Should Throw 'ownership verification'
            (Get-DockerCalls 'stop *').Count | Should Be 0
            $global:HL.ApiStarts | Should Be 0
        }

        It 'launches a fresh Api instead of adopting a reused PID' {
            $cfg = New-TestConfig; Reset-World $cfg
            $global:HL.Inspect['cid1'] = $global:HL.AfterUp
            $global:HL.Procs['4242'] = @{ StartTime = 'T-OTHER'; CommandLine = 'notepad.exe' }
            Write-TestState $cfg @{ id = 'cid1' } (New-ApiState $cfg 4242 'T0')
            $global:HL.NextPid = 5151
            (Start-HekateLocal -Config $cfg).Result | Should Be 'started'
            $global:HL.ApiStarts | Should Be 1
            $global:HL.Stopped.Count | Should Be 0
            $global:HL.Procs.ContainsKey('4242') | Should Be $true
            (Read-TestState $cfg).api.pid | Should Be 5151
        }
    }

    Context 'stop' {
        It 'is a no-op with no state' {
            $cfg = New-TestConfig; Reset-World $cfg
            (Stop-HekateLocal -Config $cfg).Result | Should Be 'nothing-to-stop'
            $global:HL.Calls.Count | Should Be 0
        }

        It 'stops the owned Api gracefully and the container, keeps the volume, and is idempotent' {
            $cfg = New-TestConfig; Reset-World $cfg
            $global:HL.Inspect['cid1'] = $global:HL.AfterUp
            Add-ApiProc $cfg
            Write-TestState $cfg @{ id = 'cid1' } (New-ApiState $cfg)

            $first = Stop-HekateLocal -Config $cfg
            $first.Result | Should Be 'stopped'
            ($first.Actions -join ' ') | Should Match 'stopped Api pid 4242 \(graceful\)'
            $global:HL.Stopped.Count | Should Be 0
            (Get-DockerCalls 'stop cid1').Count | Should Be 1

            $second = Stop-HekateLocal -Config $cfg
            $second.Result | Should Be 'stopped'
            ($second.Actions -join ' ') | Should Match 'already stopped'
            (Get-DockerCalls 'stop cid1').Count | Should Be 1
            (Get-DestructiveDockerCalls).Count | Should Be 0
            (Read-TestState $cfg).container.id | Should Be 'cid1'
        }

        It 'forces and says so when no shutdown token was recorded' {
            $cfg = New-TestConfig; Reset-World $cfg
            Add-ApiProc $cfg
            $api = New-ApiState $cfg; $api.Remove('shutdownToken')
            Write-TestState $cfg $null $api
            ($r = Stop-HekateLocal -Config $cfg) | Out-Null
            ($r.Actions -join ' ') | Should Match 'forced \(no shutdown token recorded\)'
            $global:HL.Stopped -contains 4242 | Should Be $true
        }

        It 'never kills a reused PID (start time differs)' {
            $cfg = New-TestConfig; Reset-World $cfg
            Add-ApiProc $cfg 4242 'T2'
            Write-TestState $cfg $null (New-ApiState $cfg 4242 '2026-10-06T12:00:00.1234567Z')
            $r = Stop-HekateLocal -Config $cfg
            $global:HL.Stopped.Count | Should Be 0
            $global:HL.ShutdownRequests | Should Be 0
            ($r.Actions -join ' ') | Should Match 'left pid 4242 running'
            (Read-TestState $cfg).api | Should BeNullOrEmpty
        }

        It 'never kills a process whose command line is not the expected Api, even if state says so' {
            $cfg = New-TestConfig; Reset-World $cfg
            $global:HL.Procs['4242'] = @{ StartTime = '2026-10-06T12:00:00.1234567Z'; CommandLine = 'dotnet C:\elsewhere\Other.dll' }
            $api = New-ApiState $cfg; $api.dll = 'C:\elsewhere\Other.dll'
            Write-TestState $cfg $null $api
            Stop-HekateLocal -Config $cfg | Out-Null
            $global:HL.Stopped.Count | Should Be 0
            $global:HL.ShutdownRequests | Should Be 0
        }

        It 'reports partial and keeps the Api in the ledger when it cannot be stopped' {
            $cfg = New-TestConfig; Reset-World $cfg
            $global:HL.ExitsOnShutdown = $false; $global:HL.StopFails = $true
            Add-ApiProc $cfg
            Write-TestState $cfg $null (New-ApiState $cfg)
            (Stop-HekateLocal -Config $cfg).Result | Should Be 'partial'
            (Read-TestState $cfg).api.pid | Should Be 4242
        }

        It 'reports partial and keeps the ledger when docker stop fails' {
            $cfg = New-TestConfig; Reset-World $cfg; $global:HL.StopExit = 1
            $global:HL.Inspect['cid1'] = $global:HL.AfterUp
            Write-TestState $cfg @{ id = 'cid1' } $null
            (Stop-HekateLocal -Config $cfg).Result | Should Be 'partial'
            (Read-TestState $cfg).container.id | Should Be 'cid1'
        }

        It 'leaves a container alone when its labels do not match this workspace' {
            $cfg = New-TestConfig; Reset-World $cfg
            $global:HL.Inspect['cid1'] = New-Container 'cid1' $true 'healthy' 'context-store' 'C:\Hekate'
            Write-TestState $cfg @{ id = 'cid1' } $null
            $r = Stop-HekateLocal -Config $cfg
            (Get-DockerCalls 'stop *').Count | Should Be 0
            ($r.Actions -join ' ') | Should Match 'left container'
        }

        It 'reports partial, not stopped, and keeps the ledger when docker is unavailable' {
            $cfg = New-TestConfig; Reset-World $cfg; $global:HL.Missing = @('docker')
            Write-TestState $cfg @{ id = 'cid1' } $null
            $r = Stop-HekateLocal -Config $cfg
            $r.Result | Should Be 'partial'
            ($r.Actions -join ' ') | Should Match 'ledger kept'
            (Read-TestState $cfg).container.id | Should Be 'cid1'
        }
    }

    Context 'status' {
        It 'reports unknown, not stopped or ready, when docker is missing' {
            $cfg = New-TestConfig; Reset-World $cfg; $global:HL.Missing = @('docker')
            Add-ApiProc $cfg
            Write-TestState $cfg @{ id = 'cid1' } (New-ApiState $cfg)
            $s = Get-HekateLocalStatus -Config $cfg
            $s.Status | Should Be 'unknown'
            $s.Database | Should Match 'docker not found'
        }

        It 'reports ready only with owned DB, owned Api and a ready probe' {
            $cfg = New-TestConfig; Reset-World $cfg
            $global:HL.Inspect['cid1'] = $global:HL.AfterUp
            Add-ApiProc $cfg
            Write-TestState $cfg @{ id = 'cid1' } (New-ApiState $cfg)
            (Get-HekateLocalStatus -Config $cfg).Status | Should Be 'ready'
            $global:HL.Ready = $false
            (Get-HekateLocalStatus -Config $cfg).Status | Should Be 'degraded'
            $global:HL.Ready = $true
            $global:HL.Inspect['cid1'] = New-Container 'cid1' $true 'healthy' 'other' 'C:\Hekate'
            (Get-HekateLocalStatus -Config $cfg).Status | Should Not Be 'ready'
        }
    }

    Context 'backup and restore safety' {
        It 'requires an explicit target' {
            $cfg = New-TestConfig; Reset-World $cfg
            { Restore-HekateLocal -Config $cfg -BackupFile 'x.dump' -TargetDatabase '' } | Should Throw 'explicit -TargetDatabase'
        }

        It 'refuses live and system databases' {
            $cfg = New-TestConfig; Reset-World $cfg
            foreach ($db in 'code_storage', 'orchestration', 'postgres', 'template1') {
                { Restore-HekateLocal -Config $cfg -BackupFile 'x.dump' -TargetDatabase $db } | Should Throw 'live/system database'
            }
            $global:HL.Calls.Count | Should Be 0
        }

        It 'refuses invalid names' {
            $cfg = New-TestConfig; Reset-World $cfg
            { Restore-HekateLocal -Config $cfg -BackupFile 'x.dump' -TargetDatabase 'Bad;Name' } | Should Throw 'Invalid target database name'
        }

        It 'refuses an existing database and never creates or restores into it' {
            $cfg = New-TestConfig; Reset-World $cfg
            $global:HL.Inspect['cid1'] = $global:HL.AfterUp
            Write-TestState $cfg @{ id = 'cid1' } $null
            $backup = Join-Path $TestDrive 'b.dump'; Set-Content $backup 'x'
            $global:HL.DbExists = $true
            { Restore-HekateLocal -Config $cfg -BackupFile $backup -TargetDatabase 'restore_check' } | Should Throw 'already exists'
            (Get-DockerCalls '*createdb*').Count + (Get-DockerCalls '*pg_restore*').Count | Should Be 0
        }

        It 'uses a unique container temp path, cleans it up, and never overwrites a backup' {
            $cfg = New-TestConfig; Reset-World $cfg
            $global:HL.Inspect['cid1'] = $global:HL.AfterUp
            Write-TestState $cfg @{ id = 'cid1' } $null
            $out = Join-Path $TestDrive 'out.dump'
            $b = Backup-HekateLocal -Config $cfg -OutFile $out
            $b.Result | Should Be 'backed-up'
            $b.Sha256 | Should Match '^[0-9A-F]{64}$'
            $b.Bytes | Should BeGreaterThan 0
            @(Get-DockerCalls '*pg_dump*')[0] | Should Match '/tmp/hekate-local-backup-[0-9a-f]{32}\.dump$'
            (Get-DockerCalls '*pg_restore -l /tmp/hekate-local-backup-*').Count | Should Be 1
            @(Get-DockerCalls 'cp cid1:*')[0] | Should Match '\.[0-9a-f]{32}\.partial$'
            (Get-DockerCalls 'exec cid1 rm -f /tmp/hekate-local-backup-*').Count | Should Be 1
            @(Get-ChildItem $TestDrive -Filter '*.partial').Count | Should Be 0
            { Backup-HekateLocal -Config $cfg -OutFile $out } | Should Throw 'refusing to overwrite'
        }

        It 'leaves neither a partial nor a final backup file when the copy fails' {
            $cfg = New-TestConfig; Reset-World $cfg; $global:HL.CpExit = 1
            $global:HL.Inspect['cid1'] = $global:HL.AfterUp
            Write-TestState $cfg @{ id = 'cid1' } $null
            $out = Join-Path $TestDrive 'fail.dump'
            { Backup-HekateLocal -Config $cfg -OutFile $out } | Should Throw 'docker cp of the backup failed'
            Test-Path $out | Should Be $false
            @(Get-ChildItem $TestDrive -Filter 'fail.dump*').Count | Should Be 0
        }

        It 'restores into a new database and proves the AGE graph with Cypher and labels' {
            $cfg = New-TestConfig; Reset-World $cfg
            $global:HL.Inspect['cid1'] = $global:HL.AfterUp
            Write-TestState $cfg @{ id = 'cid1' } $null
            $backup = Join-Path $TestDrive 'r.dump'; Set-Content $backup 'x'
            $r = Restore-HekateLocal -Config $cfg -BackupFile $backup -TargetDatabase 'restore_check'
            $r.Result | Should Be 'restored'
            $r.AgeGraph.Ok | Should Be $true
            $r.Note | Should Match "Disposable database 'restore_check'"
            (Get-DockerCalls "*-d restore_check*LOAD 'age'*MATCH (n) RETURN count(n)*").Count | Should Be 1
            (Get-DockerCalls '*-d restore_check*MATCH ()-`[r`]->()*').Count | Should Be 1
        }

        It 'repairs AGE catalog OIDs on the new target only, and reports the outcome' {
            $cfg = New-TestConfig; Reset-World $cfg
            $global:HL.Inspect['cid1'] = $global:HL.AfterUp
            Write-TestState $cfg @{ id = 'cid1' } $null
            $backup = Join-Path $TestDrive 'rr.dump'; Set-Content $backup 'x'
            $r = Restore-HekateLocal -Config $cfg -BackupFile $backup -TargetDatabase 'restore_check'
            $r.Result | Should Be 'restored'
            $r.AgeRepair | Should Be 'repaired'
            $repairs = Get-DockerCalls '*age-repair*'
            $repairs.Count | Should Be 1
            @($repairs)[0] | Should Match ' -d restore_check '
            @($repairs | Where-Object { $_ -match ' -d code_storage ' }).Count | Should Be 0
            @($repairs)[0] | Should Match 'ON_ERROR_STOP=1'
        }

        It 'accepts an already-consistent catalog' {
            $cfg = New-TestConfig; Reset-World $cfg; $global:HL.RepairOutput = 'NOTICE:  hekate-local age-repair: already-consistent'
            $global:HL.Inspect['cid1'] = $global:HL.AfterUp
            Write-TestState $cfg @{ id = 'cid1' } $null
            $backup = Join-Path $TestDrive 'rc.dump'; Set-Content $backup 'x'
            $r = Restore-HekateLocal -Config $cfg -BackupFile $backup -TargetDatabase 'restore_check'
            $r.Result | Should Be 'restored'
            $r.AgeRepair | Should Be 'already-consistent'
        }

        It 'fails closed when the repair fails or reports no outcome' {
            $cfg = New-TestConfig; Reset-World $cfg
            $global:HL.RepairExit = 1; $global:HL.RepairOutput = 'ERROR:  hekate-local age-repair: unsupported shape, expected exactly 1 graph, found 2'
            $global:HL.Inspect['cid1'] = $global:HL.AfterUp
            Write-TestState $cfg @{ id = 'cid1' } $null
            $backup = Join-Path $TestDrive 'rf.dump'; Set-Content $backup 'x'
            $r = Restore-HekateLocal -Config $cfg -BackupFile $backup -TargetDatabase 'restore_check'
            $r.Result | Should Be 'restored-with-errors'
            $r.AgeRepair | Should Match '^failed: .*unsupported shape'

            Reset-World $cfg; $global:HL.RepairOutput = ''   # exit 0 but no outcome notice
            $global:HL.Inspect['cid1'] = $global:HL.AfterUp
            $r2 = Restore-HekateLocal -Config $cfg -BackupFile $backup -TargetDatabase 'restore_check2'
            $r2.Result | Should Be 'restored-with-errors'
            $r2.AgeRepair | Should Match '^failed: no repair outcome'
        }

        It 'reports restored-with-errors when the restored graph is missing labels' {
            $cfg = New-TestConfig; Reset-World $cfg
            $global:HL.Labels.restore_check = @('CodeNode')
            $global:HL.Inspect['cid1'] = $global:HL.AfterUp
            Write-TestState $cfg @{ id = 'cid1' } $null
            $backup = Join-Path $TestDrive 'r2.dump'; Set-Content $backup 'x'
            $r = Restore-HekateLocal -Config $cfg -BackupFile $backup -TargetDatabase 'restore_check'
            $r.Result | Should Be 'restored-with-errors'
            @($r.AgeGraph.MissingLabels) -contains 'CALLS' | Should Be $true
        }

        It 'names the retained disposable database when a step after createdb fails' {
            $cfg = New-TestConfig; Reset-World $cfg; $global:HL.CpExit = 1
            $global:HL.Inspect['cid1'] = $global:HL.AfterUp
            Write-TestState $cfg @{ id = 'cid1' } $null
            $backup = Join-Path $TestDrive 'r3.dump'; Set-Content $backup 'x'
            { Restore-HekateLocal -Config $cfg -BackupFile $backup -TargetDatabase 'restore_check' } |
                Should Throw "Disposable database 'restore_check' was created and left in place"
        }
    }

    Context 'password diagnostics' {
        It 'refuses to start when the password differs from the one last verified' {
            $cfg = New-TestConfig; Reset-World $cfg
            New-Item -ItemType Directory -Force -Path $cfg.StateDir | Out-Null
            @{ version = 1; workspace = $cfg.RepoRoot; composeProject = 'hekate-local'; container = $null; api = $null; pgPasswordSha256 = 'DEADBEEF' } |
                ConvertTo-Json | Set-Content $cfg.StateFile
            { Start-HekateLocal -Config $cfg } | Should Throw 'differs from the password last verified'
            (Get-DockerCalls '* up *').Count | Should Be 0
        }

        It 'records the fingerprint only after a successful start' {
            $cfg = New-TestConfig; Reset-World $cfg
            Start-HekateLocal -Config $cfg | Out-Null
            (Read-TestState $cfg).pgPasswordSha256 | Should Match '^[0-9A-F]{64}$'
        }

        It 'turns a 28P01 login failure into a specific message instead of a timeout' {
            $cfg = New-TestConfig; Reset-World $cfg; $global:HL.AuthFail = $true
            { Start-HekateLocal -Config $cfg } | Should Throw 'password authentication failed'
            (Read-TestState $cfg).ContainsKey('pgPasswordSha256') | Should Be $false
        }
    }
}

Describe 'HekateLocal docker argv forwarding (real wrapper, fake docker.cmd)' {
    It 'passes each array element as its own argument' {
        $bin = Join-Path $TestDrive 'fakebin'
        New-Item -ItemType Directory -Force -Path $bin | Out-Null
        Set-Content -Path (Join-Path $bin 'docker.cmd') -Value "@echo ARGV:%*`r`n@exit /b 7" -Encoding ascii
        $oldPath = $env:PATH
        try {
            $env:PATH = "$bin;$oldPath"
            $r = & (Get-Module HekateLocal) { Invoke-HLDocker @('inspect', '--format', '{{json .}}', 'cid1') }
            $r.ExitCode | Should Be 7
            $r.Output | Should Be 'ARGV:inspect --format "{{json .}}" cid1'
            $r2 = & (Get-Module HekateLocal) { Invoke-HLDocker ((Get-HLComposeArgs ([pscustomobject]@{ ComposeProject = 'hekate-local'; ComposeFile = 'C:\r\c.yml'; EnvFile = 'C:\r\e.env' })) + @('ps', '-a', '-q', 'postgres')) }
            $r2.Output | Should Be 'ARGV:compose -p hekate-local -f C:\r\c.yml --env-file C:\r\e.env ps -a -q postgres'
        }
        finally { $env:PATH = $oldPath }
    }

    It 'reports 127 instead of throwing when the command does not set an exit code or cannot run' {
        $bin = Join-Path $TestDrive 'scriptbin'
        New-Item -ItemType Directory -Force -Path $bin | Out-Null
        # A PowerShell script named docker runs in-process and never sets $LASTEXITCODE.
        Set-Content -Path (Join-Path $bin 'docker.ps1') -Value "'from script'"
        $oldPath = $env:PATH
        try {
            $env:PATH = "$bin;$oldPath"
            $r = & (Get-Module HekateLocal) { Set-StrictMode -Version Latest; Invoke-HLNative (Join-Path $args[0] 'docker.ps1') @('info') } $bin
            $r.ExitCode | Should Be 127
            $r.Output | Should Be 'from script'
            $missing = & (Get-Module HekateLocal) { Invoke-HLNative 'definitely-not-a-command-hl' @('x') }
            $missing.ExitCode | Should Be 127
            $missing.Output | Should Match 'could not be executed'
        }
        finally { $env:PATH = $oldPath }
    }
}
