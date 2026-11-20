# Dux Icons

<div align="center">
  <a href="https://duxlabs.dev">
    <picture>
      <source media="(prefers-color-scheme: light)" srcset="assets/dux-logo-light.svg">
      <img src="assets/dux-logo-dark.svg" alt="Dux logo" width="140">
    </picture>
  </a>

### An icon theme that follows your colors

Built for [DMS](https://github.com/duxlabs-dev/dux-shell), works on any freedesktop desktop

<picture>
  <source media="(prefers-color-scheme: light)" srcset="assets/hero-light.png">
  <img src="assets/hero-dark.png" alt="Dux Icons folder and file type icons in the seven badge shapes, each row in a different wallpaper color, the last three rows shaded" width="880">
</picture>

</div>

Dux Icons is a generated icon theme. It puts each app's own icon on a Material badge, draws folders and file types itself, and paints all of it from your current Material colors. Change the wallpaper and the icons change with it.

It is a generator, not a hand-drawn app set. Your apps keep the artwork their authors shipped; Dux Icons supplies the frame and the color.

## Features

**Your apps, one shape**
Every installed app with a `.desktop` entry gets its own icon framed on the same badge. Nothing is redrawn, so a new app looks right the day you install it.

**Follows your theme**
Badge, folders and file types take Material `primary` and `on_primary`. With DMS they repaint on every wallpaper or theme change. Without DMS you pass the colors yourself.

**Folders and file types**
Folders, the home and XDG user folders, trash, network places and the common file types are drawn for the theme and use the same colors.

**Monochrome mode**
Paint app icons in tones of your theme colors instead of keeping their own.

**A normal icon theme**
The output is a standard freedesktop theme of scalable SVGs. GTK, Qt and KDE apps can all use it, and any name it does not provide falls back to `hicolor`.

## Installation

Dux Icons is packaged as `dux-icons` for Arch (AUR), Fedora and CentOS (Copr), Debian and openSUSE (OBS), Ubuntu (PPA) and Void. Add the [Dux Labs repository](https://duxlabs.dev/docs/repo/) for your distribution and install it:

```bash
paru -S dux-icons                # Arch
sudo dnf install dux-icons       # Fedora, after enabling the duxlabs/dux-repo Copr
sudo apt install dux-icons       # Debian and Ubuntu
sudo zypper install dux-icons    # openSUSE
sudo xbps-install -S dux-icons   # Void
```

From source, you need Python 3.9 or newer and nothing else:

```bash
git clone https://github.com/duxlabs-dev/dux-icons.git
cd dux-icons
make install
```

## Usage

With DMS, pick **Dux Icons** under Settings → Appearance → Icons. DMS rebuilds the theme on every wallpaper or theme change.

Without DMS, give it your two colors. The theme is built into `~/.local/share/icons/dux-icons`:

```bash
dux-icons --badge '#9ecaff' --glyph '#003258'
```

Options, badge shapes, the build recipe and drawing your own icons are covered in the [Dux Icons documentation](https://duxlabs.dev/docs/dicons/).

## Development

```bash
python3 -m unittest
```

## Credits

Type marks on folders and file types are adapted from [Material Symbols](https://github.com/google/material-design-icons), Apache-2.0. See [NOTICE](NOTICE).

## License

[MIT](LICENSE)
