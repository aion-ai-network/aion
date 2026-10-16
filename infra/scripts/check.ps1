$ErrorActionPreference = 'Stop'
$repository = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
Push-Location $repository
try {
  uv run --locked --project backend ruff check backend/src backend/tests
  if ($LASTEXITCODE -ne 0) { throw 'Backend lint failed' }
  uv run --locked --project backend mypy --config-file backend/pyproject.toml backend/src/aion
  if ($LASTEXITCODE -ne 0) { throw 'Backend type checking failed' }
  uv run --locked --project backend pytest backend/tests
  if ($LASTEXITCODE -ne 0) { throw 'Backend tests failed' }
  pnpm.cmd --dir frontend build
  if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed' }
}
finally {
  Pop-Location
}
