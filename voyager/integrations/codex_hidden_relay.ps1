# Voyager Codex SessionStart hidden relay (G3-B popup fix, candidate).
#
# Codex dispatches hook commands through PowerShell, but PowerShell does not
# collect stdout of GUI-subsystem children (pythonw), and a console python
# pops a visible window when the dispatch parent is windowless.  This relay
# runs as the hook command and:
#   1. reads the hook payload from stdin (codex's pipe),
#   2. writes it to a per-run temp file,
#   3. Start-Process -WindowStyle Hidden -Wait runs the real handler
#      (python.exe) with stdin/stdout/stderr redirected to per-run files,
#   4. relays the captured stdout back to its own stdout for Codex,
#   5. cleans up the temp files.
# Synchronous, hidden, and byte-preserving for the hookSpecificOutput JSON.

$ErrorActionPreference = "Stop"
$py = "C:\Python314\python.exe"
$handler = Join-Path $PSScriptRoot "codex_session_start.py"
$pf = $null; $of = $null; $ef = $null

try {
    $pl = [Console]::In.ReadToEnd()
    $stamp = Get-Date -Format "yyyyMMddHHmmssfff"
    # per-run unique names (millisecond stamp + PID): concurrent SessionStarts
    # never share temp files
    $pf = Join-Path $env:TEMP ("voy_pl_" + $stamp + "_" + $PID + ".json")
    $of = Join-Path $env:TEMP ("voy_out_" + $stamp + "_" + $PID + ".txt")
    $ef = Join-Path $env:TEMP ("voy_err_" + $stamp + "_" + $PID + ".txt")
    [IO.File]::WriteAllText($pf, $pl)

    # hidden + wait: synchronous transport without a visible console; the
    # 30s hook timeout in hooks.json is the overall upper bound.
    Start-Process -WindowStyle Hidden -Wait -FilePath $py `
        -ArgumentList ('"' + $handler + '"') `
        -RedirectStandardInput $pf -RedirectStandardOutput $of `
        -RedirectStandardError $ef

    # relay the captured protocol stdout only; child stderr (if any) is kept
    # out of the protocol channel, and a non-zero child exit still fails open
    if ([IO.File]::Exists($of)) {
        [Console]::Out.Write([IO.File]::ReadAllText($of))
    }
} catch {
    # fail-open: never break a Codex session
    try {
        $lg = Join-Path $env:USERPROFILE ".voyager\logs\codex-hooks.jsonl"
        $lg | Out-Null
        [IO.File]::AppendAllText($lg, (Get-Date -Format o) + " relay error: " +
            $_.Exception.Message + "`n")
    } catch {}
} finally {
    Remove-Item $pf, $of, $ef -ErrorAction SilentlyContinue
}
exit 0
