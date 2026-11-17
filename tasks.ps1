<#
.SYNOPSIS
    Windows equivalent of the Makefile targets.

.EXAMPLE
    .\tasks.ps1 test
    .\tasks.ps1 dev-backend
#>
param(
    [Parameter(Position = 0)]
    [ValidateSet(
        "help", "install", "dev", "dev-backend", "dev-frontend", "test", "test-backend",
        "test-frontend", "lint", "eval", "eval-report", "migrate", "seed", "e2e",
        "docker-up", "docker-down"
    )]
    [string]$Target = "help"
)

$ErrorActionPreference = "Stop"
$repo = $PSScriptRoot
$backend = Join-Path $repo "backend"
$frontend = Join-Path $repo "frontend"
$venvPython = Join-Path $backend ".venv\Scripts\python.exe"
$python = if (Test-Path $venvPython) { $venvPython } else { "python" }

function Invoke-Backend([string[]]$Arguments) {
    Push-Location $backend
    try { & $python @Arguments; if ($LASTEXITCODE -ne 0) { throw "backend command failed: $($Arguments -join ' ')" } }
    finally { Pop-Location }
}

function Invoke-Frontend([string[]]$Arguments) {
    Push-Location $frontend
    try { & npm @Arguments; if ($LASTEXITCODE -ne 0) { throw "npm command failed: $($Arguments -join ' ')" } }
    finally { Pop-Location }
}

switch ($Target) {
    "help" {
        @"
install        Install backend + frontend dependencies
dev            Print instructions for running both dev servers
dev-backend    Run FastAPI on http://localhost:8000
dev-frontend   Run Vite on http://localhost:5173
test           Run backend + frontend test suites
test-backend   Run pytest
test-frontend  Run Vitest
lint           Run ruff + tsc --noEmit
eval           Run the v1 vs v2 evaluation harness
eval-report    Print the latest evaluation report
migrate        Apply Alembic migrations
seed           Seed policies + evaluation scenarios
e2e            Run Playwright (if installed)
docker-up      docker compose up --build
docker-down    docker compose down -v
"@
    }
    "install" {
        Invoke-Backend @("-m", "pip", "install", "-r", "requirements-dev.txt")
        Invoke-Frontend @("install")
    }
    "dev" { Write-Host "Run '.\tasks.ps1 dev-backend' and '.\tasks.ps1 dev-frontend' in two terminals." }
    "dev-backend" {
        Push-Location $backend
        try { & $python -m uvicorn app.main:app --reload --port 8000 }
        finally { Pop-Location }
    }
    "dev-frontend" { Invoke-Frontend @("run", "dev") }
    "test" { & $PSCommandPath test-backend; & $PSCommandPath test-frontend }
    "test-backend" { Invoke-Backend @("-m", "pytest", "-q") }
    "test-frontend" { Invoke-Frontend @("run", "test") }
    "lint" {
        Invoke-Backend @("-m", "ruff", "check", "app", "tests")
        Invoke-Frontend @("run", "lint")
    }
    "eval" { Invoke-Backend @("-m", "app.evaluation.cli", "run", "--versions", "v1,v2") }
    "eval-report" { Invoke-Backend @("-m", "app.evaluation.cli", "report") }
    "migrate" { Invoke-Backend @("-m", "alembic", "upgrade", "head") }
    "seed" { Invoke-Backend @("-m", "app.db.seed") }
    "e2e" { Invoke-Frontend @("exec", "--", "npx", "playwright", "test") }
    "docker-up" { Push-Location $repo; try { & docker compose up --build } finally { Pop-Location } }
    "docker-down" { Push-Location $repo; try { & docker compose down -v } finally { Pop-Location } }
}
