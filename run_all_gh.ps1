# Test the autobid against every Greenhouse URL in applied_jobs.jsonl.
# Reads each line, parses out external_url, keeps only Greenhouse ones,
# and passes them all in one batched test_autobid run.

$pattern = '"external_url":\s*"(?<url>https://job-boards\.greenhouse\.io[^"]+)"'
$urls = Get-Content applied_jobs.jsonl |
    Select-String -Pattern $pattern |
    ForEach-Object { $_.Matches[0].Groups['url'].Value } |
    Select-Object -Unique

Write-Host "Found $($urls.Count) unique Greenhouse URL(s) to test." -ForegroundColor Cyan

# Build the --url args. Review prompt is on by default (Windows alert
# after each fill); pass --no-review to fall back to a fixed --hold.
$argList = @('-m', 'greezik.test_autobid')
foreach ($u in $urls) { $argList += '--url'; $argList += $u }
foreach ($extra in $args) { $argList += $extra }

& ".\.venv\Scripts\python.exe" @argList
