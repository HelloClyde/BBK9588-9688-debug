# SPDX-License-Identifier: GPL-3.0-only
[CmdletBinding()]
param(
    [ValidateSet("all", "9588", "9688")]
    [string[]]$Models = @("all"),
    [string]$Toolchain,
    [switch]$Verify
)
$ErrorActionPreference = "Stop"
$taskArguments = @((Join-Path $PSScriptRoot "build.py"), "--models") + $Models
if ($Toolchain) { $taskArguments += @("--toolchain", $Toolchain) }
if ($Verify) { $taskArguments += "--verify" }
& python @taskArguments
exit $LASTEXITCODE
