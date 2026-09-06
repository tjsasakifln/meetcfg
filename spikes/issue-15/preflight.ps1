param(
  [switch]$LaunchPhoneLink
)

$ErrorActionPreference = "Stop"

if ($LaunchPhoneLink) {
  Start-Process "ms-phone:"
  Start-Sleep -Seconds 5
}

$os = Get-CimInstance Win32_OperatingSystem
$phoneLink = Get-AppxPackage Microsoft.YourPhone
$phoneLinkProcess = Get-Process PhoneExperienceHost -ErrorAction SilentlyContinue
$endpoints = @(Get-PnpDevice -Class AudioEndpoint -ErrorAction SilentlyContinue |
  Where-Object Status -eq "OK")

$browserPaths = @{
  chrome = "${env:ProgramFiles}\Google\Chrome\Application\chrome.exe"
  edge = "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe"
}
$browsers = @{}
foreach ($entry in $browserPaths.GetEnumerator()) {
  if (Test-Path $entry.Value) {
    $browsers[$entry.Key] = (Get-Item $entry.Value).VersionInfo.ProductVersion
  }
}

# Emit counts and capability booleans only. Friendly names, device IDs, phone
# numbers, contacts, transcripts and audio are deliberately excluded.
[pscustomobject]@{
  schema = "MEETCFG_PHONE_SPIKE_PREFLIGHT/1.0"
  observed_at = [DateTimeOffset]::Now.ToString("o")
  windows = [pscustomobject]@{
    caption = $os.Caption
    version = $os.Version
    build = [int]$os.BuildNumber
    process_loopback_build_eligible = ([int]$os.BuildNumber -ge 20348)
  }
  phone_link = [pscustomobject]@{
    installed = [bool]$phoneLink
    version = if ($phoneLink) { $phoneLink.Version.ToString() } else { $null }
    running = [bool]$phoneLinkProcess
  }
  browsers = $browsers
  audio = [pscustomobject]@{
    active_endpoint_count = $endpoints.Count
    has_hands_free = [bool]($endpoints.FriendlyName -match "Hands-Free|Mãos Livres")
    has_headset_like_endpoint = [bool]($endpoints.FriendlyName -match "Headset|Headphone|Fone(s)? de ouvido|Hands-Free|Mãos Livres")
  }
  privacy = [pscustomobject]@{
    audio_read = $false
    transcript_read = $false
    content_logged = $false
  }
} | ConvertTo-Json -Depth 6
