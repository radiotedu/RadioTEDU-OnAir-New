param(
    [string]$OutputDir = (Join-Path $PSScriptRoot "assets")
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Drawing

function New-BrandBrush {
    param([string]$Hex)
    $value = $Hex.TrimStart("#")
    [System.Drawing.SolidBrush]::new(
        [System.Drawing.Color]::FromArgb(
            [Convert]::ToInt32($value.Substring(0, 2), 16),
            [Convert]::ToInt32($value.Substring(2, 2), 16),
            [Convert]::ToInt32($value.Substring(4, 2), 16)
        )
    )
}

function Save-WizardLargeImage {
    param([string]$Path, [string]$LogoPath)

    $bitmap = [System.Drawing.Bitmap]::new(164, 314)
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    $graphics.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::HighQuality
    $graphics.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
    $graphics.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::AntiAliasGridFit
    $gradient = [System.Drawing.Drawing2D.LinearGradientBrush]::new(
        [System.Drawing.Rectangle]::new(0, 0, 164, 314),
        [System.Drawing.Color]::FromArgb(7, 9, 13),
        [System.Drawing.Color]::FromArgb(72, 8, 18),
        [System.Drawing.Drawing2D.LinearGradientMode]::ForwardDiagonal
    )
    $graphics.FillRectangle($gradient, 0, 0, 164, 314)

    $white = New-BrandBrush "#FFFFFF"
    $red = New-BrandBrush "#ED1B2F"
    $muted = New-BrandBrush "#D7D9DF"
    $graphics.FillRectangle($white, 28, 18, 108, 108)
    $logo = [System.Drawing.Image]::FromFile($LogoPath)
    $graphics.DrawImage($logo, 34, 24, 96, 96)
    $logo.Dispose()

    $graphics.FillRectangle($red, 0, 136, 164, 8)
    $titleFont = [System.Drawing.Font]::new("Segoe UI Semibold", 18, [System.Drawing.FontStyle]::Bold)
    $onAirFont = [System.Drawing.Font]::new("Segoe UI Semibold", 25, [System.Drawing.FontStyle]::Bold)
    $bodyFont = [System.Drawing.Font]::new("Segoe UI", 8, [System.Drawing.FontStyle]::Regular)
    $smallFont = [System.Drawing.Font]::new("Segoe UI Semibold", 8, [System.Drawing.FontStyle]::Bold)
    $graphics.DrawString("RadioTEDU", $titleFont, $white, [System.Drawing.PointF]::new(13, 153))
    $graphics.DrawString("OnAir", $onAirFont, $red, [System.Drawing.PointF]::new(11, 179))
    $graphics.DrawString(
        "Deterministic broadcast.`nOperator control.`nPlaylist continuity.",
        $bodyFont,
        $muted,
        [System.Drawing.RectangleF]::new(14, 224, 138, 45)
    )
    $graphics.DrawString("RadioTEDU OnAir", $smallFont, $white, [System.Drawing.PointF]::new(14, 286))

    $graphics.Dispose()
    $bitmap.Save($Path, [System.Drawing.Imaging.ImageFormat]::Bmp)
    $bitmap.Dispose()
}

function Save-WizardSmallImage {
    param([string]$Path, [string]$LogoPath)
    $bitmap = [System.Drawing.Bitmap]::new(55, 55)
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    $graphics.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
    $graphics.Clear([System.Drawing.Color]::White)
    $logo = [System.Drawing.Image]::FromFile($LogoPath)
    $graphics.DrawImage($logo, 2, 2, 51, 51)
    $logo.Dispose()
    $graphics.Dispose()
    $bitmap.Save($Path, [System.Drawing.Imaging.ImageFormat]::Bmp)
    $bitmap.Dispose()
}

New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
$logoPath = Join-Path $OutputDir "radiotedu-onair-logo.png"
if (-not (Test-Path -LiteralPath $logoPath)) {
    throw "RadioTEDU OnAir logo is missing: $logoPath"
}
Save-WizardLargeImage -Path (Join-Path $OutputDir "wizard-large.bmp") -LogoPath $logoPath
Save-WizardSmallImage -Path (Join-Path $OutputDir "wizard-small.bmp") -LogoPath $logoPath
Write-Output "Generated RadioTEDU OnAir installer artwork in $OutputDir"
