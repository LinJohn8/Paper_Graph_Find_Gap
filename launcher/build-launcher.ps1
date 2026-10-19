$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path $compiler)) { throw '.NET Framework 4 C# compiler is required.' }
$outputFile = '/out:' + (Join-Path $root 'PaperGraph.exe')
& $compiler /nologo /target:winexe /codepage:65001 /reference:System.Windows.Forms.dll /reference:System.Drawing.dll $outputFile (Join-Path $PSScriptRoot 'PaperGraphLauncher.cs')
if ($LASTEXITCODE -ne 0) { throw 'Launcher compilation failed.' }
Write-Output (Join-Path $root 'PaperGraph.exe')
