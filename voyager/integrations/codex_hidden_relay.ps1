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

# Resolve the interpreter at run time.  This used to be the hardcoded
# "C:\Python314\python.exe", which is true on exactly one machine: anywhere else
# (a GitHub runner, a user who installed python elsewhere or into a venv) the
# Start-Process below failed, the relay fell into its fail-open catch, emitted
# nothing, and the SessionStart hook silently did nothing.
#
# VOYAGER_PYTHON wins when set, then the first python on PATH.  The hook command
# itself stays untouched: its shape is what was live-verified (see
# CodexIntegration.hook_command), so the interpreter cannot be appended to it.
$py = $env:VOYAGER_PYTHON
if (-not $py -or -not (Test-Path -LiteralPath $py)) {
    foreach ($cand in @("python.exe", "python")) {
        $found = Get-Command $cand -ErrorAction SilentlyContinue
        if ($found -and $found.Source) { $py = $found.Source; break }
    }
}
if (-not $py) { $py = "python" }

$handler = Join-Path $PSScriptRoot "codex_session_start.py"
$pf = $null; $of = $null; $ef = $null
$utf8 = New-Object System.Text.UTF8Encoding($false)   # never a BOM

try {
    # Read the hook payload as UTF-8 rather than through [Console]::In, whose
    # decoding follows the console code page -- a cwd containing non-ASCII (a
    # Chinese path, say) would otherwise be mangled before the handler saw it.
    $inStream = [Console]::OpenStandardInput()
    $reader = New-Object System.IO.StreamReader($inStream, $utf8)
    $pl = $reader.ReadToEnd()
    $reader.Dispose()

    $stamp = Get-Date -Format "yyyyMMddHHmmssfff"
    # per-run unique names (millisecond stamp + PID): concurrent SessionStarts
    # never share temp files
    $pf = Join-Path $env:TEMP ("voy_pl_" + $stamp + "_" + $PID + ".json")
    $of = Join-Path $env:TEMP ("voy_out_" + $stamp + "_" + $PID + ".txt")
    $ef = Join-Path $env:TEMP ("voy_err_" + $stamp + "_" + $PID + ".txt")
    [IO.File]::WriteAllText($pf, $pl, $utf8)

    # hidden + wait: synchronous transport without a visible console; the
    # 30s hook timeout in hooks.json is the overall upper bound.
    Start-Process -WindowStyle Hidden -Wait -FilePath $py `
        -ArgumentList ('"' + $handler + '"') `
        -RedirectStandardInput $pf -RedirectStandardOutput $of `
        -RedirectStandardError $ef

    # relay the captured protocol stdout only; child stderr (if any) is kept
    # out of the protocol channel, and a non-zero child exit still fails open
    # Relay the captured protocol stdout as raw bytes.  Going through
    # [Console]::Out would re-encode the text with [Console]::OutputEncoding --
    # the console code page on Windows PowerShell 5.1 -- so any non-ASCII
    # character in the protocol JSON (a Chinese session title, an emoji, a
    # cp1252 ellipsis) reached Codex as mojibake.  Copying bytes cannot
    # transcode anything, and cannot introduce a BOM.
    if ([IO.File]::Exists($of)) {
        $bytes = [IO.File]::ReadAllBytes($of)
        $stdout = [Console]::OpenStandardOutput()
        $stdout.Write($bytes, 0, $bytes.Length)
        $stdout.Flush()
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
