# Melted-skull logo

The Postmortem logo (2026-09-18): a striped skull in red `#C4331E` on
cream `#F3E3C3`, melting downward, with a sunburst ring on the full lock-up.

| file | use |
|---|---|
| `svg/mark.svg`, `png/mark-1000.png` | the skull alone, transparent background |
| `svg/mark-small.svg` | the same skull with fewer, thicker stripes for sizes under ~40 px (site header, app title bar) |
| `svg/mark-reversed.svg`, `png/mark-reversed-1000.png` | cream skull for red or dark grounds |
| `svg/logo-full.svg`, `png/logo-full-2000.png` | skull inside the sunburst ring |
| `svg/app-icon.svg`, `png/app-icon-*.png` | square cream tile with the skull: app icons, PWA icons, CurseForge avatar |
| `svg/app-icon-small.svg`, `png/favicon-*.png`, `favicon.ico` | favicon sizes |

Where it is used from here (regenerate these when the art changes):

- `build/postmortem.icns` and `build/postmortem.ico` -- the desktop app's
  icon, from `png/app-icon-1024.png` (`sips` + `iconutil` on macOS,
  `magick -define icon:auto-resize=256,128,64,48,32,16` for the .ico).
- `addon/Postmortem/media/icon-64.tga` / `icon-32.tga` (+ .png) -- the
  addon's AddOns-list icon and minimap button, from the same PNG
  (Pillow, uncompressed 32-bit TGA).
- `src/postmortem/desktop/shell/mark.svg` -- a copy of `svg/mark-small.svg`
  for the app's title bar.
- The site's header mark, favicon and PWA icons live in the private site
  repo and are copies of `mark-small.svg` / `favicon-32.png` /
  `app-icon-192.png` / `app-icon-512.png` / `apple-touch-icon-180.png`.
