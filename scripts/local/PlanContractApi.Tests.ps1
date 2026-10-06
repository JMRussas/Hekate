#Requires -Version 7.5
<#
.SYNOPSIS
    Live process-level checks for the plan-contract API (context-store Api).

.DESCRIPTION
    Runs against the RUNNING hekate-local container of this workspace (verified by its
    compose project and workspace labels). Creates a NEW disposable database
    hekate_plan_api_<timestamp>_<random>, builds the Api to a private folder, and starts its
    own Api processes on 127.0.0.1:<port>. Checks:
      A  flag off: plan-contract endpoints absent (404), no managed schema created.
      B  HEKATE_PLAN_CONTRACT=1 with an unsafe bind: process exits 78 before any managed DDL.
      C  flag on, loopback: HTTP lifecycle, readiness, error mapping, and that legacy generic
         writers get 409 managed_plan_protected with data unchanged.
    Cleans up only the processes it started and the database it created (try/finally).
    Exit 0 only when every assertion passed; never skips.

.EXAMPLE
    pwsh scripts/local/PlanContractApi.Tests.ps1
#>
[CmdletBinding()]
param([int]$Port = 5105, [switch]$KeepDatabase)

$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
# Per-run folder: concurrent invocations never share logs or the built Api.
$work = Join-Path $repo ".hekate-local\plan-contract-api-test\$([guid]::NewGuid().ToString('N'))"
New-Item -ItemType Directory -Force -Path $work | Out-Null

$failures = [System.Collections.Generic.List[string]]::new()
$passes = 0
function Check([string]$name, [bool]$ok, [string]$detail = '') {
    if ($ok) { $script:passes++; Write-Host "PASS  $name" }
    else { $script:failures.Add("$name $detail"); Write-Host "FAIL  $name $detail" -ForegroundColor Red }
}

# --- Owned container only --------------------------------------------------
if (-not (Get-Command docker -CommandType Application -ErrorAction SilentlyContinue)) { throw "docker not found on PATH." }
$container = docker ps -q --no-trunc --filter 'label=com.docker.compose.project=hekate-local' --filter "label=com.hekate.local.workspace=$repo"
if (-not $container) { throw "The hekate-local container for workspace '$repo' is not running. Run 'hekate-local.ps1 start' first." }
$dbPort = [int](docker inspect --type container --format '{{(index (index .NetworkSettings.Ports "5432/tcp") 0).HostPort}}' $container)
$hostIp = docker inspect --type container --format '{{(index (index .NetworkSettings.Ports "5432/tcp") 0).HostIp}}' $container
if ($hostIp -ne '127.0.0.1') { throw "Container database is published on '$hostIp', not loopback; refusing." }

$db = "hekate_plan_api_$(Get-Date -Format 'yyyyMMddHHmmss')_$([guid]::NewGuid().ToString('N').Substring(0, 8))"
if ($db -notmatch '^hekate_plan_api_[0-9]{14}_[0-9a-f]{8}$') { throw "Database name guard failed: $db" }
$conn = "Host=127.0.0.1;Port=$dbPort;Database=$db;Username=postgres;Password=postgres"
$base = "http://127.0.0.1:$Port"
$bin = Join-Path $work 'api-bin'
# Process OBJECTS (holding their OS handles), never bare PIDs: a reused PID can't be killed.
$ownedProcesses = [System.Collections.Generic.List[System.Diagnostics.Process]]::new()
$dbCreated = $false

function Psql([string]$sql) {
    $out = docker exec $container psql -U postgres -d $db -v ON_ERROR_STOP=1 -qtAc $sql
    if ($LASTEXITCODE -ne 0) { throw "psql failed: $sql" }
    return ($out | Out-String).Trim()
}
function ManagedSchemaExists { (Psql "select to_regclass('public.managed_plans') is not null") -eq 't' }

function Start-Api([hashtable]$envVars, [string]$name) {
    $stdin = Join-Path $work 'empty.stdin'
    if (-not (Test-Path $stdin)) { New-Item -ItemType File -Path $stdin | Out-Null }
    $p = Start-Process dotnet -ArgumentList "`"$bin\Api.dll`"" -WorkingDirectory (Join-Path $repo 'context-store\Api') -Environment $envVars `
        -RedirectStandardInput $stdin -RedirectStandardOutput (Join-Path $work "$name.out.log") -RedirectStandardError (Join-Path $work "$name.err.log") `
        -WindowStyle Hidden -PassThru
    $script:ownedProcesses.Add($p)
    return $p
}
function Wait-Ready($p) {
    for ($i = 0; $i -lt 90; $i++) {
        if ($p.HasExited) { return $false }
        try { if ((Invoke-WebRequest "$base/api/health/ready" -TimeoutSec 2 -SkipHttpErrorCheck).StatusCode -eq 200) { return $true } } catch { }
        Start-Sleep -Milliseconds 500
    }
    return $false
}
function Stop-Owned([System.Diagnostics.Process]$p) { if ($p -and -not $p.HasExited) { $p.Kill(); $p.WaitForExit(10000) | Out-Null } }
function Call([string]$method, [string]$path, $body = $null) {
    $a = @{ Method = $method; Uri = "$base$path"; SkipHttpErrorCheck = $true; TimeoutSec = 15 }
    if ($null -ne $body) { $a.Body = ($body | ConvertTo-Json -Depth 10); $a.ContentType = 'application/json' }
    $r = Invoke-WebRequest @a
    [pscustomobject]@{ Status = [int]$r.StatusCode; Json = $(try { $r.Content | ConvertFrom-Json -DateKind String } catch { $null }) }
}
function Key { [guid]::NewGuid().ToString('N') }

# The flag is always explicit for the child (Start-Process -Environment inherits anything unset).
$baseEnv = @{ CODESTORAGE_CONNSTR = $conn; HEKATE_API_URLS = $base; HEKATE_DISABLE_DISPATCHER = '1'; HEKATE_PLAN_CONTRACT = '0' }
$ambientFlag = $env:HEKATE_PLAN_CONTRACT
try {
    if (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue) { throw "Port $Port is in use; choose another with -Port." }
    docker exec $container createdb -U postgres $db
    if ($LASTEXITCODE -ne 0) { throw "createdb failed" }
    $dbCreated = $true
    Psql "CREATE EXTENSION IF NOT EXISTS vector; CREATE EXTENSION IF NOT EXISTS age; LOAD 'age'; SET search_path = ag_catalog, `"`$user`", public; SELECT create_graph('code_graph'); SELECT create_vlabel('code_graph','CodeNode'); SELECT create_elabel('code_graph','DEPENDS_ON');" | Out-Null
    dotnet build (Join-Path $repo 'context-store\Api\Api.csproj') -c Debug -o $bin --nologo -v q | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Api build failed' }

    # A: flag off for the child, even with HEKATE_PLAN_CONTRACT=1 in THIS (parent) process.
    $env:HEKATE_PLAN_CONTRACT = '1'
    $a = Start-Api $baseEnv 'a-flag-off'
    $env:HEKATE_PLAN_CONTRACT = $ambientFlag
    Check 'A api ready (flag off)' (Wait-Ready $a)
    Check 'A endpoints absent' ((Call GET "/api/plan-contract/v1/plans/$([guid]::NewGuid())").Status -eq 404)
    Check 'A list route absent' ((Call GET '/api/plan-contract/v1/plans').Status -eq 404)
    Check 'A no managed schema' (-not (ManagedSchemaExists))
    Check 'A ambient flag did not leak' ([bool](Select-String -Path (Join-Path $work 'a-flag-off.out.log') -Pattern "Plan contract: Disabled \(HEKATE_PLAN_CONTRACT='0'" -Quiet))
    Stop-Owned $a

    # B: explicit flag with unsafe bind
    $unsafe = $baseEnv.Clone(); $unsafe.HEKATE_PLAN_CONTRACT = '1'; $unsafe.HEKATE_API_URLS = "http://0.0.0.0:$Port"
    $b = Start-Api $unsafe 'b-unsafe'
    $exited = $b.WaitForExit(60000)
    Check 'B exits' $exited
    Check 'B exit code 78' ($exited -and $b.ExitCode -eq 78) "exit=$(if ($exited) { $b.ExitCode })"
    Check 'B refusal logged' ([bool](Select-String -Path (Join-Path $work 'b-unsafe.err.log') -Pattern 'Refusing to start' -Quiet))
    Check 'B no managed schema' (-not (ManagedSchemaExists))
    Stop-Owned $b

    # C: enabled
    $safe = $baseEnv.Clone(); $safe.HEKATE_PLAN_CONTRACT = '1'
    $c = Start-Api $safe 'c-enabled'
    Check 'C api ready (enabled)' (Wait-Ready $c)
    Check 'C managed schema created' (ManagedSchemaExists)
    $project = [guid]::NewGuid(); $root = [guid]::NewGuid(); $t1 = [guid]::NewGuid(); $t2 = [guid]::NewGuid()
    Psql "INSERT INTO projects (id, name, root_path) VALUES ('$project', 'plan-contract-api-test', 'disposable://plan-contract-api-test')" | Out-Null

    $r = Call POST '/api/plan-contract/v1/plans' @{ rootId = $root; projectId = $project; name = 'API plan'; operationKey = (Key); expectedStateRevision = 0; actor = 'api-test' }
    Check 'C create plan' ($r.Status -eq 200 -and $r.Json.outcome -eq 'applied' -and $r.Json.contractVersion -eq 'plan-contract/v1' -and $r.Json.defaultGate -eq 'accepted') "status=$($r.Status)"
    $r = Call POST "/api/plan-contract/v1/nodes/$root/children" @{ childId = $t1; nodeType = 'task'; name = 'T1'; value = 'spec'; attributes = @{ acceptance_criteria = 'green' }; operationKey = (Key); expectedStateRevision = 1; actor = 'api-test' }
    Check 'C add child T1' ($r.Status -eq 200) "status=$($r.Status)"
    $r = Call POST "/api/plan-contract/v1/nodes/$root/children" @{ childId = $t2; nodeType = 'task'; name = 'T2'; operationKey = (Key); expectedStateRevision = 2; actor = 'api-test' }
    Check 'C add child T2' ($r.Status -eq 200) "status=$($r.Status)"
    $r = Call POST "/api/plan-contract/v1/nodes/$t2/dependencies" @{ predecessorId = $t1; operationKey = (Key); expectedStateRevision = 0; actor = 'api-test' }
    Check 'C add dependency' ($r.Status -eq 200) "status=$($r.Status)"
    $r = Call GET "/api/plan-contract/v1/plans/$root/readiness"
    Check 'C T2 blocked before T1 accepted' ((($r.Json.leaves | Where-Object nodeId -eq "$t2").ready) -eq $false)
    $s1 = Call POST "/api/plan-contract/v1/nodes/$t1/transition" @{ to = 'in_progress'; attemptId = 'attempt-1'; executorRef = 'gods:engine_tasks:42'; operationKey = (Key); expectedStateRevision = 0; actor = 'worker' }
    $s2 = Call POST "/api/plan-contract/v1/nodes/$t1/transition" @{ to = 'done'; attemptId = 'attempt-1'; attemptEpoch = 1; artifactRef = 'sha-1'; operationKey = (Key); expectedStateRevision = 1; actor = 'worker' }
    $s3 = Call POST "/api/plan-contract/v1/nodes/$t1/decide" @{ decision = 'accepted'; reviewedContentRevision = 1; reviewedArtifactRef = 'sha-1'; reviewedAttemptEpoch = 1; evidenceRef = 'review-1'; operationKey = (Key); expectedStateRevision = 2; actor = 'reviewer' }
    Check 'C start/finish/accept' ($s1.Status -eq 200 -and $s2.Status -eq 200 -and $s3.Status -eq 200) "$($s1.Status)/$($s2.Status)/$($s3.Status)"
    $r = Call GET "/api/plan-contract/v1/plans/$root/readiness"
    Check 'C T2 ready after T1 accepted' ((($r.Json.leaves | Where-Object nodeId -eq "$t2").ready) -eq $true)
    $r = Call GET "/api/plan-contract/v1/plans/$root"
    $n1 = $r.Json.nodes | Where-Object id -eq "$t1"
    Check 'C view has named states and content' ($n1.work -eq 'done' -and $n1.effectiveAcceptance -eq 'accepted' -and $n1.value -eq 'spec' -and $n1.contentAttributes.acceptance_criteria -eq 'green')
    Check 'C view keeps executorRef after finish' ($n1.executorRef -eq 'gods:engine_tasks:42') "executorRef=$($n1.executorRef)"

    # Read-only provenance history (plan 016).
    $ev = Call GET "/api/plan-contract/v1/plans/$root/events"
    $kinds = @($ev.Json.events | ForEach-Object kind)
    Check 'E plan events ordered' ($ev.Status -eq 200 -and ($kinds -join ',') -eq 'attempt_started,attempt_finished,decision_recorded' -and (@($ev.Json.events | ForEach-Object seq) -join ',') -eq '1,2,3') "kinds=$($kinds -join ',')"
    Check 'E event fields' ($ev.Json.events[0].executorRef -eq 'gods:engine_tasks:42' -and $ev.Json.events[0].attemptEpoch -eq 1 -and $ev.Json.events[2].decision -eq 'accepted' -and $ev.Json.events[2].evidenceRef -eq 'review-1')
    Check 'E history markers' ($ev.Json.historyStartsAtSeq -eq 1 -and $ev.Json.historyBackfilled -eq $false -and $null -eq $ev.Json.nextAfterSeq -and $ev.Json.contractVersion -eq 'plan-contract/v1')
    $p1 = Call GET "/api/plan-contract/v1/plans/$root/events?limit=2"
    $p2 = Call GET "/api/plan-contract/v1/plans/$root/events?afterSeq=$($p1.Json.nextAfterSeq)&limit=2"
    Check 'E pagination cursor' ($p1.Json.nextAfterSeq -eq 2 -and @($p1.Json.events).Count -eq 2 -and @($p2.Json.events).Count -eq 1 -and $p2.Json.events[0].seq -eq 3 -and $null -eq $p2.Json.nextAfterSeq)
    $nodeEv = Call GET "/api/plan-contract/v1/nodes/$t1/events"
    Check 'E node events filtered' ($nodeEv.Status -eq 200 -and @($nodeEv.Json.events).Count -eq 3 -and @($nodeEv.Json.events | Where-Object nodeId -ne "$t1").Count -eq 0)
    $empty = Call GET "/api/plan-contract/v1/nodes/$t2/events"
    Check 'E existing node with no history is 200 empty' ($empty.Status -eq 200 -and @($empty.Json.events).Count -eq 0 -and $null -eq $empty.Json.historyStartsAtSeq)
    Check 'E missing plan 404' ((Call GET "/api/plan-contract/v1/plans/$([guid]::NewGuid())/events").Status -eq 404)
    Check 'E missing node 404' ((Call GET "/api/plan-contract/v1/nodes/$([guid]::NewGuid())/events").Status -eq 404)
    $bad = @('afterSeq=-1', 'afterSeq=abc', 'limit=0', 'limit=501', 'limit=x') | ForEach-Object { (Call GET "/api/plan-contract/v1/plans/$root/events?$_").Status }
    Check 'E invalid query 400' (@($bad | Where-Object { $_ -ne 400 }).Count -eq 0) "statuses=$($bad -join ',')"
    $badRef = Call POST "/api/plan-contract/v1/nodes/$t2/transition" @{ to = 'in_progress'; attemptId = 'x'; executorRef = 'has space'; operationKey = (Key); expectedStateRevision = 1; actor = 'w' }
    Check 'E invalid executorRef 400' ($badRef.Status -eq 400 -and $badRef.Json.code -eq 'invalid_executor_ref') "status=$($badRef.Status)"

    $r = Call POST "/api/plan-contract/v1/nodes/$t2/transition" @{ to = 'in_progress'; attemptId = 'x'; operationKey = (Key); expectedStateRevision = 99; actor = 'w' }
    Check 'C stale revision 409' ($r.Status -eq 409 -and $r.Json.code -eq 'stale_revision') "status=$($r.Status) code=$($r.Json.code)"
    $r = Call POST "/api/plan-contract/v1/nodes/$t2/transition" @{ to = 'in_progress'; attemptId = 'x'; actor = 'w' }
    Check 'C missing precondition 400' ($r.Status -eq 400 -and $r.Json.code -eq 'missing_field') "status=$($r.Status) code=$($r.Json.code)"
    $r = Call POST "/api/plan-contract/v1/nodes/$t2/transition" @{ to = 'finished'; operationKey = (Key); expectedStateRevision = 1; actor = 'w' }
    Check 'C invalid enum 400' ($r.Status -eq 400 -and $r.Json.code -eq 'invalid_enum') "status=$($r.Status)"
    $r = Call POST "/api/plan-contract/v1/nodes/$t1/dependencies" @{ predecessorId = $t2; operationKey = (Key); expectedStateRevision = 3; actor = 'w' }
    Check 'C dependency cycle 422' ($r.Status -eq 422 -and $r.Json.code -eq 'dependency_cycle') "status=$($r.Status) code=$($r.Json.code)"

    $r = Call PUT "/api/node/$t1/attributes" @{ attributes = @{ status = 'completed' } }
    Check 'C legacy attribute write 409' ($r.Status -eq 409 -and $r.Json.code -eq 'managed_plan_protected') "status=$($r.Status)"
    $r = Call PUT "/api/node/$t1" @{ name = 'T1'; value = 'overwritten' }
    Check 'C legacy value write 409' ($r.Status -eq 409 -and $r.Json.code -eq 'managed_plan_protected') "status=$($r.Status)"
    Check 'C value unchanged' ((Psql "select value from nodes where id = '$t1'") -eq 'spec')
    Check 'C acceptance_criteria unchanged' ((Psql "select value from node_attributes where node_id = '$t1' and key = 'acceptance_criteria'") -eq 'green')
    $edges = Psql "LOAD 'age'; SET search_path = ag_catalog, `"`$user`", public; SELECT count::text FROM cypher('code_graph', `$`$ MATCH ()-[e:DEPENDS_ON {plan_root: '$root'}]->() RETURN count(e) `$`$) AS (count agtype)"
    Check 'C one tagged projected edge' ($edges -eq '1') "edges=$edges"
    Check 'C dispatcher disabled' ([bool](Select-String -Path (Join-Path $work 'c-enabled.out.log') -Pattern 'Agent dispatcher disabled' -Quiet))

    # K: durable claim receipts and pins (plan 019). T1 is done+accepted, T2 is ready.
    function Receipts { [int](Psql "select count(*) from plan_claim_receipts where root_node_id = '$root'") }
    $claimBody = @{ claimKey = 'x._~-123'; attemptId = 'k-att'; executorRef = 'run-k'; actor = 'worker' }
    $k1 = Call POST "/api/plan-contract/v1/plans/$root/claims" $claimBody
    $rc = $k1.Json.receipt
    Check 'K claim 200 claimed T2' ($k1.Status -eq 200 -and $rc.outcome -eq 'claimed' -and $rc.nodeId -eq "$t2" -and $rc.attemptEpoch -eq 1 -and $rc.executorRef -eq 'run-k' -and $k1.Json.replayed -eq $false -and $k1.Json.stillCurrent -eq $true -and $k1.Json.current.work -eq 'in_progress') "status=$($k1.Status) code=$($k1.Json.code)"
    $t1Snap = $rc.prereqSnapshot.nodes | Where-Object id -eq "$t1"
    Check 'K prereq snapshot has named states' ($rc.prereqSnapshot.digest -eq $rc.prereqDigest -and $t1Snap.work -eq 'done' -and $t1Snap.acceptance.decision -eq 'accepted' -and $t1Snap.acceptance.evidenceRef -eq 'review-1' -and ($rc.prereqSnapshot.declared | Where-Object predecessorId -eq "$t1").gate -eq 'accepted')
    Check 'K content snapshot' ($rc.contentRevision -eq 1 -and $null -eq $rc.contentSnapshot.value -and $rc.contentDigest -match '^[0-9a-f]+$')
    $t2View = (Call GET "/api/plan-contract/v1/plans/$root").Json.nodes | Where-Object id -eq "$t2"
    Check 'K node view shows pins' ($t2View.attemptContentRevision -eq 1 -and $t2View.attemptPrereqDigest -eq $rc.prereqDigest)
    $receiptJson = $rc | ConvertTo-Json -Depth 20 -Compress
    $k2 = Call GET "/api/plan-contract/v1/plans/$root/claims/x._~-123"
    Check 'K GET same receipt' ($k2.Status -eq 200 -and ($k2.Json.receipt | ConvertTo-Json -Depth 20 -Compress) -eq $receiptJson -and $k2.Json.stillCurrent -eq $true) "status=$($k2.Status)"
    $k3 = Call POST "/api/plan-contract/v1/plans/$root/claims" $claimBody
    Check 'K replay same receipt' ($k3.Status -eq 200 -and $k3.Json.replayed -eq $true -and ($k3.Json.receipt | ConvertTo-Json -Depth 20 -Compress) -eq $receiptJson -and (Receipts) -eq 1)
    $k4 = Call POST "/api/plan-contract/v1/plans/$root/claims" @{ claimKey = 'x._~-123'; attemptId = 'other'; executorRef = 'run-k'; actor = 'worker' }
    Check 'K payload conflict 409' ($k4.Status -eq 409 -and $k4.Json.code -eq 'operation_key_reused') "status=$($k4.Status)"
    $k5 = Call POST "/api/plan-contract/v1/plans/$root/claims" @{ claimKey = 'k-none'; attemptId = 'k-att-2'; actor = 'worker' }
    Check 'K no_ready_work 200' ($k5.Status -eq 200 -and $k5.Json.receipt.outcome -eq 'no_ready_work' -and $k5.Json.stillCurrent -eq $false -and $null -eq $k5.Json.current -and $null -eq $k5.Json.receipt.nodeId) "status=$($k5.Status)"
    Check 'K GET missing claim 404' ((Call GET "/api/plan-contract/v1/plans/$root/claims/never").Json.code -eq 'claim_not_found')
    Check 'K GET missing plan 404' ((Call GET "/api/plan-contract/v1/plans/$([guid]::NewGuid())/claims/x").Json.code -eq 'plan_not_found')
    $beforeBad = Receipts
    $badKeys = @('a/b', 'a%2Fb', 'a?b', 'a#b', '.', '..', 'hekate-claim:x') | ForEach-Object {
        $r = Call POST "/api/plan-contract/v1/plans/$root/claims" @{ claimKey = $_; attemptId = 'a'; actor = 'w' }
        "$($r.Status):$($r.Json.code)"
    }
    Check 'K invalid claim keys 400 invalid_input' (@($badKeys | Where-Object { $_ -ne '400:invalid_input' }).Count -eq 0 -and (Receipts) -eq $beforeBad) "got=$($badKeys -join ',')"
    $mf = Call POST "/api/plan-contract/v1/plans/$root/claims" @{ attemptId = 'a'; actor = 'w' }
    Check 'K missing claimKey 400' ($mf.Status -eq 400 -and $mf.Json.code -eq 'missing_field')
    $br = Call POST "/api/plan-contract/v1/plans/$root/claims" @{ claimKey = 'k-badref'; attemptId = 'a'; executorRef = 'has space'; actor = 'w' }
    Check 'K invalid executorRef 400' ($br.Status -eq 400 -and $br.Json.code -eq 'invalid_executor_ref' -and (Receipts) -eq $beforeBad)
    $rp = Call POST "/api/plan-contract/v1/nodes/$t1/transition" @{ to = 'in_progress'; attemptId = 'r'; operationKey = 'hekate-claim:forged'; expectedStateRevision = 3; actor = 'w' }
    Check 'K reserved operation key 400' ($rp.Status -eq 400 -and $rp.Json.code -eq 'invalid_operation_key') "status=$($rp.Status) code=$($rp.Json.code)"
    $kev = (Call GET "/api/plan-contract/v1/nodes/$t2/events").Json.events
    Check 'K claim event carries key and pins' ($kev[-1].kind -eq 'attempt_started' -and $kev[-1].claimKey -eq 'x._~-123' -and $kev[-1].operationKey -eq 'hekate-claim:x._~-123' -and $kev[-1].attemptPrereqDigest -eq $rc.prereqDigest -and $kev[-1].seq -eq $rc.eventSeq)

    # Pinned finish: upstream content change -> 409 stale_prerequisites; own content change -> 409 stale_content.
    $rv = Call PUT "/api/plan-contract/v1/nodes/$t1/content" @{ value = 'spec v2'; expectedContentRevision = 1; operationKey = (Key); expectedStateRevision = 3; actor = 'planner' }
    Check 'K revise T1' ($rv.Status -eq 200) "status=$($rv.Status)"
    $f1 = Call POST "/api/plan-contract/v1/nodes/$t2/transition" @{ to = 'done'; attemptId = 'k-att'; attemptEpoch = 1; artifactRef = 'sha-k'; operationKey = (Key); expectedStateRevision = 2; actor = 'worker' }
    Check 'K finish stale_prerequisites 409' ($f1.Status -eq 409 -and $f1.Json.code -eq 'stale_prerequisites') "status=$($f1.Status) code=$($f1.Json.code)"
    Check 'K stillCurrent false after drift' ((Call GET "/api/plan-contract/v1/plans/$root/claims/x._~-123").Json.stillCurrent -eq $false)
    $rv2 = Call PUT "/api/plan-contract/v1/nodes/$t2/content" @{ value = 't2 v2'; expectedContentRevision = 1; operationKey = (Key); expectedStateRevision = 2; actor = 'planner' }
    $f2 = Call POST "/api/plan-contract/v1/nodes/$t2/transition" @{ to = 'done'; attemptId = 'k-att'; attemptEpoch = 1; artifactRef = 'sha-k'; operationKey = (Key); expectedStateRevision = 3; actor = 'worker' }
    Check 'K finish stale_content 409' ($rv2.Status -eq 200 -and $f2.Status -eq 409 -and $f2.Json.code -eq 'stale_content') "status=$($rv2.Status)/$($f2.Status) code=$($f2.Json.code)"
    Check 'K receipt unchanged after drift' (((Call GET "/api/plan-contract/v1/plans/$root/claims/x._~-123").Json.receipt | ConvertTo-Json -Depth 20 -Compress) -eq $receiptJson)

    # L: read-only managed-plan discovery (plan 021).
    $lProject = [guid]::NewGuid()
    Psql "INSERT INTO projects (id, name, root_path) VALUES ('$lProject', 'plan-listing-test', 'disposable://plan-listing-test')" | Out-Null
    $lRoots = @(1..3 | ForEach-Object {
        $id = [guid]::NewGuid()
        $r = Call POST '/api/plan-contract/v1/plans' @{ rootId = $id; projectId = $lProject; name = "L$_"; operationKey = (Key); expectedStateRevision = 0; actor = 'api-test' }
        if ($r.Status -ne 200) { throw "L plan create failed: $($r.Status)" }
        "$id"
    })
    $legacyRootResp = Call POST "/api/project/$lProject/nodes" @{ nodeType = 'plan'; name = 'legacy root' }
    $legacyRoot = "$($legacyRootResp.Json.id)"
    $legacyChild = Call POST "/api/node/$legacyRoot/children" @{ nodeType = 'plan'; name = 'legacy child plan' }
    $legacyChildId = if ($legacyChild.Json.node) { "$($legacyChild.Json.node.id)" } else { "$($legacyChild.Json.id)" }
    Check 'L legacy unmanaged plans created' ($legacyRootResp.Status -eq 200 -and $legacyChild.Status -eq 200 -and $legacyRoot -match '^[0-9a-f-]{36}$' -and $legacyChildId -match '^[0-9a-f-]{36}$') "status=$($legacyRootResp.Status)/$($legacyChild.Status)"
    $all = Call GET "/api/plan-contract/v1/plans?projectId=$lProject"
    $ids = @($all.Json.plans | ForEach-Object { "$($_.rootId)" })
    $pgOrder = @((Psql "select root_node_id from managed_plans where project_id = '$lProject' order by root_node_id") -split "`n" | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    Check 'L list 200 managed only, database order' ($all.Status -eq 200 -and $all.Json.contractVersion -eq 'plan-contract/v1' -and ($ids -join ',') -eq ($pgOrder -join ',') -and $ids.Count -eq 3 -and $ids -notcontains "$legacyRoot" -and $ids -notcontains $legacyChildId -and $null -eq $all.Json.nextAfterRootId) "ids=$($ids -join ',')"
    $item = $all.Json.plans | Where-Object rootId -eq $lRoots[0]
    Check 'L item shape' ($item.name -eq 'L1' -and $item.supported -eq $true -and $item.contractVersion -eq 'plan-contract/v1' -and $item.defaultGate -eq 'accepted' -and $item.eventSeq -eq 0 -and "$($item.projectId)" -eq "$lProject" -and $null -eq $all.Json.supported -and $null -eq $all.Json.supportedContractVersion)
    $p1 = Call GET "/api/plan-contract/v1/plans?projectId=$lProject&limit=2"
    $p2 = Call GET "/api/plan-contract/v1/plans?projectId=$lProject&limit=2&afterRootId=$($p1.Json.nextAfterRootId)"
    $paged = @($p1.Json.plans + $p2.Json.plans | ForEach-Object { "$($_.rootId)" })
    Check 'L cursor pages' (@($p1.Json.plans).Count -eq 2 -and "$($p1.Json.nextAfterRootId)" -eq $pgOrder[1] -and @($p2.Json.plans).Count -eq 1 -and $null -eq $p2.Json.nextAfterRootId -and ($paged -join ',') -eq ($pgOrder -join ','))
    Psql "SET hekate.plan_contract = 'on'; UPDATE managed_plans SET contract_version = 'plan-contract/v9' WHERE root_node_id = '$($lRoots[2])'" | Out-Null
    $unsup = (Call GET "/api/plan-contract/v1/plans?projectId=$lProject").Json.plans | Where-Object rootId -eq $lRoots[2]
    Check 'L unsupported version listed as metadata' ($unsup.supported -eq $false -and $unsup.contractVersion -eq 'plan-contract/v9')
    $badQ = @('limit=0', 'limit=501', 'limit=x', 'afterRootId=nope', 'projectId=123') | ForEach-Object { $r = Call GET "/api/plan-contract/v1/plans?$_"; "$($r.Status):$($r.Json.code)" }
    Check 'L invalid query 400' (@($badQ | Where-Object { $_ -ne '400:invalid_query' }).Count -eq 0) "got=$($badQ -join ',')"
    Stop-Owned $c
}
catch {
    $failures.Add("ERROR $($_.Exception.Message)")
    Write-Host "ERROR $($_.Exception.Message)" -ForegroundColor Red
}
finally {
    $env:HEKATE_PLAN_CONTRACT = $ambientFlag
    # Cleanup is verified, and any cleanup problem counts as a failure.
    foreach ($p in $ownedProcesses) {
        try { Stop-Owned $p } catch { }
        if (-not $p.HasExited) { $failures.Add("CLEANUP owned Api process $($p.Id) did not exit") }
    }
    $dbState = if (-not $dbCreated) { 'not created' } elseif ($KeepDatabase) { 'kept (requested)' } else { 'unknown' }
    if ($dbCreated -and -not $KeepDatabase) {
        if ($db -notmatch '^hekate_plan_api_[0-9]{14}_[0-9a-f]{8}$') { $failures.Add("CLEANUP refused to drop unexpected database name $db"); $dbState = 'NOT dropped (name guard)' }
        else {
            docker exec $container dropdb -U postgres --force $db 2>$null | Out-Null
            $dropExit = $LASTEXITCODE
            $still = docker exec $container psql -U postgres -d postgres -qtAc "SELECT 1 FROM pg_database WHERE datname = '$db'"
            $verifyExit = $LASTEXITCODE
            if ($dropExit -ne 0 -or $verifyExit -ne 0 -or ($still | Out-String).Trim() -eq '1') {
                $failures.Add("CLEANUP dropdb $db failed (drop exit $dropExit, verification exit $verifyExit)"); $dbState = 'NOT dropped'
            }
            else { $dbState = 'dropped' }
        }
    }
}

Write-Host "`n$passes passed, $($failures.Count) failed (database $db $dbState)"
$failures | Where-Object { $_ -like 'CLEANUP*' } | ForEach-Object { Write-Host "FAIL  $_" -ForegroundColor Red }
if ($failures.Count -gt 0) { exit 1 }
exit 0
