<#
.SYNOPSIS
    On-demand local Hekate (plan-only profile): Postgres+AGE container + context-store Api.

.DESCRIPTION
    start    Preflight, start the local DB container and Api, wait for /api/health/ready.
    status   Report owned resources and readiness (exit 0 only when ready).
    stop     Stop only the resources this launcher started; the DB volume is kept.
             Exit 1 when anything could not be stopped or verified ('partial').
    backup   pg_dump the local code_storage DB to .hekate-local/backups (or -OutFile).
    restore  Restore a backup into a NEW, explicitly named disposable database.

    Independent of the Sisyphus deployment: loopback only, separate ports
    (DB 5434, Api 5103 by default), separate compose project and volume, and the
    agent dispatcher disabled so plan edits never spawn model runs.

.EXAMPLE
    pwsh scripts/local/hekate-local.ps1 start
    pwsh scripts/local/hekate-local.ps1 restore -BackupFile .hekate-local/backups/x.dump -TargetDatabase restore_check
#>
#Requires -Version 7.5
[CmdletBinding()]
param(
    [Parameter(Mandatory, Position = 0)]
    [ValidateSet('start', 'status', 'stop', 'backup', 'restore')]
    [string]$Command,
    [string]$OutFile,
    [string]$BackupFile,
    [string]$TargetDatabase,
    [switch]$Json
)

$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'HekateLocal.psm1') -Force

function Write-Result($Result) {
    if ($Json) { $Result | ConvertTo-Json -Depth 6 } else { $Result | Format-List | Out-String | Write-Output }
}

try {
    switch ($Command) {
        'start' { Write-Result (Start-HekateLocal) }
        'stop' {
            $r = Stop-HekateLocal
            Write-Result $r
            if ($r.Result -notin 'stopped', 'nothing-to-stop') { exit 1 }
        }
        'backup' { Write-Result (Backup-HekateLocal -OutFile $OutFile) }
        'restore' {
            $r = Restore-HekateLocal -BackupFile $BackupFile -TargetDatabase $TargetDatabase
            Write-Result $r
            if ($r.Result -ne 'restored') { exit 1 }
        }
        'status' {
            $s = Get-HekateLocalStatus
            Write-Result $s
            if ($s.Status -ne 'ready') { exit 1 }
        }
    }
    exit 0
}
catch {
    Write-Host "hekate-local $Command failed: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
