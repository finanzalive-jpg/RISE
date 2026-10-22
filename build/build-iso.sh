#!/usr/bin/env bash
# Rise – costruisce la ISO live (Ubuntu 26.04 LTS + desktop Rise) da zero.
# Uso (come root, su Ubuntu 24.04 o più recente):  sudo ./build/build-iso.sh [versione]
# Risultato: out/rise-<versione>-live-amd64.iso
set -euo pipefail

VERSION="${1:-0.3}"
CODENAME="resolute"            # Ubuntu 26.04 LTS
MIRROR="http://archive.ubuntu.com/ubuntu"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="${WORK:-$ROOT/work}"
OUT="$ROOT/out"
CH="$WORK/chroot"
ISO="$WORK/iso"
NAME="rise-${VERSION}-live-amd64"

[ "$(id -u)" -eq 0 ] || { echo "Lancia lo script come root (sudo)."; exit 1; }
log(){ printf '\n==> %s\n' "$*"; }

cleanup(){
  for m in dev/pts dev sys proc; do mountpoint -q "$CH/$m" 2>/dev/null && umount -l "$CH/$m" || true; done
}
trap cleanup EXIT

log "1/9 Strumenti di costruzione"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq debootstrap squashfs-tools xorriso grub-pc-bin grub-efi-amd64-bin mtools dosfstools nodejs npm >/dev/null
# debootstrap più vecchi non conoscono ancora "resolute": usa lo script generico di Ubuntu
[ -e "/usr/share/debootstrap/scripts/$CODENAME" ] || ln -s gutsy "/usr/share/debootstrap/scripts/$CODENAME"

log "2/9 Sistema Ubuntu di base ($CODENAME)"
rm -rf "$WORK"; mkdir -p "$WORK" "$OUT"
debootstrap --arch=amd64 --variant=minbase --components=main,restricted,universe,multiverse "$CODENAME" "$CH" "$MIRROR"
# Fonti dei pacchetti nel formato moderno (sources.list.d/ubuntu.sources):
# all'avvio dalla chiavetta casper le ignora, così non prova a scaricare da internet e l'avvio è veloce.
: > "$CH/etc/apt/sources.list"
cat > "$CH/etc/apt/sources.list.d/ubuntu.sources" <<EOF
Types: deb
URIs: $MIRROR
Suites: $CODENAME $CODENAME-updates
Components: main restricted universe multiverse
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg

Types: deb
URIs: http://security.ubuntu.com/ubuntu
Suites: $CODENAME-security
Components: main restricted universe multiverse
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg
EOF
for m in proc sys dev dev/pts; do mount --bind "/$m" "$CH/$m"; done
# ISO più leggera: niente manuali e documentazione, traduzioni solo in italiano (e inglese di riserva),
# niente sfondi GNOME e varianti di icone che Rise non usa
mkdir -p "$CH/etc/dpkg/dpkg.cfg.d"
cat > "$CH/etc/dpkg/dpkg.cfg.d/rise-leggera" <<'EOF'
path-exclude=/usr/share/doc/*
path-include=/usr/share/doc/*/copyright
path-exclude=/usr/share/man/*
path-exclude=/usr/share/info/*
path-exclude=/usr/share/locale/*
path-include=/usr/share/locale/it/*
path-include=/usr/share/locale/en/*
path-include=/usr/share/locale/locale.alias
path-exclude=/usr/share/help/*
path-include=/usr/share/help/it/*
path-include=/usr/share/help/C/*
path-exclude=/usr/share/backgrounds/gnome/*
path-exclude=/usr/share/icons/Papirus-Dark/*
path-exclude=/usr/share/icons/Papirus-Light/*
path-exclude=/usr/share/icons/ePapirus/*
path-exclude=/usr/share/icons/ePapirus-Dark/*
EOF
cp -L /etc/resolv.conf "$CH/etc/resolv.conf"
printf '#!/bin/sh\nexit 101\n' > "$CH/usr/sbin/policy-rc.d"; chmod +x "$CH/usr/sbin/policy-rc.d"

log "3/9 Kernel, driver, firmware e componenti di Rise"
chroot "$CH" apt-get update -qq
# shellcheck disable=SC2046
chroot "$CH" env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends $(cat "$ROOT/build/packages.txt")

log "3a0/9 Pacchetti facoltativi (stampanti, archivi, Bluetooth)"
for pkg in $(grep -v '^#' "$ROOT/build/packages-optional.txt"); do
  chroot "$CH" env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends "$pkg" >/dev/null 2>&1 \
    && echo "  $pkg: ok" || echo "  ATTENZIONE: $pkg non disponibile"
done
chroot "$CH" systemctl enable cups cups-browsed >/dev/null 2>&1 || true

log "3a/9 Installatore: avvio con Secure Boot e dual boot con Windows (GRUB firmato, shim, os-prober)"
# grub-install non deve partire adesso (siamo nella cartella di costruzione): lo si disattiva e poi lo si rimette
chroot "$CH" dpkg-divert --local --rename --add /usr/sbin/grub-install >/dev/null
ln -sf /bin/true "$CH/usr/sbin/grub-install"
chroot "$CH" env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    grub-efi-amd64-signed shim-signed grub-efi-amd64-bin grub-pc-bin grub2-common efibootmgr os-prober \
    parted gdisk squashfs-tools mokutil dosfstools e2fsprogs || echo "ATTENZIONE: pacchetti dell'installatore mancanti"
rm -f "$CH/usr/sbin/grub-install"
chroot "$CH" dpkg-divert --local --rename --remove /usr/sbin/grub-install >/dev/null
for t in grub-install grub-mkconfig efibootmgr os-prober parted unsquashfs; do echo "$t: $(chroot "$CH" sh -c "command -v $t" || echo MANCANTE)"; done
ls "$CH/usr/lib/shim/" 2>/dev/null | head -5

log "3b/9 Suite Office: WordCraft, GridCraft, DeckCraft (ultima versione ufficiale)"
mkdir -p "$CH/tmp/craft"
for app in wordcraft gridcraft deckcraft; do
  url="$(curl -fsSL ${GITHUB_TOKEN:+-H "Authorization: Bearer $GITHUB_TOKEN"} "https://api.github.com/repos/storytold/$app/releases/latest" 2>/dev/null \
    | python3 -c 'import json,sys; d=json.load(sys.stdin); print(next((a["browser_download_url"] for a in d.get("assets", []) if a["name"].endswith("-linux-x86_64.deb")), ""))' 2>/dev/null || true)"
  if [ -z "$url" ]; then
    tag="$(git ls-remote --tags --sort=-v:refname "https://github.com/storytold/$app" 'v*' 2>/dev/null | grep -v '\^{}' | head -1 | sed 's#.*refs/tags/v##')"
    [ -n "$tag" ] && url="https://github.com/storytold/$app/releases/download/v$tag/$app-$tag-linux-x86_64.deb"
  fi
  echo "$app: $url"
  if [ -n "$url" ] && curl -fsSL -o "$CH/tmp/craft/$app.deb" "$url"; then
    chroot "$CH" env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "/tmp/craft/$app.deb" || echo "ATTENZIONE: $app non installato"
    # Rise avvia il programma col nome semplice (wordcraft, gridcraft, deckcraft): se il pacchetto
    # mette l'eseguibile altrove o con un altro nome, si crea il collegamento.
    if ! chroot "$CH" sh -c "command -v $app" >/dev/null 2>&1; then
      exe="$(chroot "$CH" dpkg -L "$app" 2>/dev/null | grep -iE "/(usr/)?(bin|opt/[^/]+)/[^/]*$app[^/]*$" | head -1)"
      [ -z "$exe" ] && exe="$(chroot "$CH" sh -c "dpkg -L $app 2>/dev/null | while read f; do [ -f \"\$f\" ] && [ -x \"\$f\" ] && echo \"\$f\"; done" | grep -i "$app" | head -1)"
      [ -n "$exe" ] && ln -sf "$exe" "$CH/usr/local/bin/$app"
    fi
    echo "$app: $(chroot "$CH" sh -c "command -v $app" || echo 'NON TROVATO')"
  else
    echo "ATTENZIONE: non trovo il pacchetto di $app, continuo senza"
  fi
done
rm -rf "$CH/tmp/craft"

log "3c/9 (saltato) Rise leggero: nessun modello IA, nessuna voce da scaricare"

log "4/9 Desktop di Rise (offline: caratteri e lettori di file inclusi)"
D="$CH/opt/rise"; mkdir -p "$D/fonts" "$D/lib"
NPM="$WORK/npm"
# NPM_CACHE: cartella con i pacchetti già scaricati (facoltativa, accelera le prove locali)
"$ROOT/build/npm-libs.sh" "$NPM"
"$ROOT/build/install-system.sh" "$CH" "$NPM/x"
chroot "$CH" /usr/local/bin/rise-cinnamon-setup || echo "ATTENZIONE: impostazioni del desktop Cinnamon non applicate"
# versione installata: serve agli aggiornamenti dal web per sapere se c'è qualcosa di nuovo
BUILD_ID="$(git -C "$ROOT" log -1 --format=%ct 2>/dev/null || date +%s)"
printf '{"build": %s, "version": "%s", "commit": "%s"}\n' "$BUILD_ID" "$VERSION" "$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo ?)" > "$CH/etc/rise-build"

log "5/9 Configurazione di sistema"
for d in Documenti Immagini Musica Download Video Scrivania; do mkdir -p "$CH/etc/skel/$d"; done
# Negozio: catalogo Flathub già configurato
chroot "$CH" flatpak remote-add --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo || echo "ATTENZIONE: Flathub non configurato"
echo rise > "$CH/etc/hostname"; printf '127.0.0.1 localhost\n127.0.1.1 rise\n' > "$CH/etc/hosts"
grep -q XKB_DEFAULT_LAYOUT "$CH/etc/environment" || echo 'XKB_DEFAULT_LAYOUT=it' >> "$CH/etc/environment"
chroot "$CH" sh -c '
  sed -i "s/^# *it_IT.UTF-8/it_IT.UTF-8/" /etc/locale.gen; locale-gen >/dev/null; grep -q "^LANG=" /etc/default/locale 2>/dev/null || echo LANG=it_IT.UTF-8 > /etc/default/locale
  ln -sf /usr/share/zoneinfo/Europe/Rome /etc/localtime; echo Europe/Rome > /etc/timezone
  systemctl enable greetd NetworkManager >/dev/null 2>&1
  systemctl disable getty@tty1.service >/dev/null 2>&1 || true
  systemctl mask systemd-networkd-wait-online.service >/dev/null 2>&1 || true
  systemctl enable smbd nmbd rise-wsdd avahi-daemon >/dev/null 2>&1 || true
  # lavori pesanti in background che dalla chiavetta bloccherebbero il computer per minuti
  for t in plocate-updatedb.timer man-db.timer apt-daily.timer apt-daily-upgrade.timer fwupd-refresh.timer motd-news.timer e2scrub_all.timer; do
    systemctl mask "$t" >/dev/null 2>&1 || true
  done
  systemctl set-default graphical.target >/dev/null 2>&1'
# condivisione cartelle (Samba): le cartelle scelte in Esplora file finiscono in rise-shares.conf
if [ -f "$CH/etc/samba/smb.conf" ] && ! grep -q rise-shares "$CH/etc/samba/smb.conf"; then
  sed -i '/^\[global\]/a\   include = /etc/samba/rise-shares.conf' "$CH/etc/samba/smb.conf"
  grep -q '^ *map to guest' "$CH/etc/samba/smb.conf" || sed -i '/^\[global\]/a\   map to guest = bad user' "$CH/etc/samba/smb.conf"
  sed -i "s/^\( *\)workgroup = .*/\1workgroup = WORKGROUP/" "$CH/etc/samba/smb.conf"
fi
touch "$CH/etc/samba/rise-shares.conf"
cat > "$CH/etc/os-release.rise" <<EOF
PRETTY_NAME="Rise $VERSION"
NAME="Rise"
VERSION="$VERSION"
ID=rise
ID_LIKE=ubuntu
HOME_URL="https://www.iuppiter.ch"
SUPPORT_URL="https://github.com/finanzalive-jpg/RISE/issues"
EOF

log "6/9 Schermata di avvio e avvio live (initramfs)"
if [ -f "$CH/usr/share/plymouth/themes/rise/rise.plymouth" ]; then
  chroot "$CH" update-alternatives --install /usr/share/plymouth/themes/default.plymouth default.plymouth /usr/share/plymouth/themes/rise/rise.plymouth 200
  chroot "$CH" update-alternatives --set default.plymouth /usr/share/plymouth/themes/rise/rise.plymouth
fi
chroot "$CH" update-initramfs -c -k all
KVER="$(ls "$CH/lib/modules" | sort -V | tail -1)"

log "7/9 Pulizia"
chroot "$CH" apt-get clean
rm -f "$CH/usr/sbin/policy-rc.d"
rm -rf "$CH/var/lib/apt/lists/"* "$CH/tmp/"* "$CH/var/log/"*.log
printf 'nameserver 127.0.0.53\n' > "$CH/etc/resolv.conf"
: > "$CH/etc/machine-id"
cleanup

log "8/9 Immagine compressa del sistema"
mkdir -p "$ISO/casper" "$ISO/boot/grub" "$ISO/.disk"
cp "$CH/boot/vmlinuz-$KVER" "$ISO/casper/vmlinuz"
cp "$CH/boot/initrd.img-$KVER" "$ISO/casper/initrd"
cp "$CH/boot/config-$KVER" "$ISO/casper/config" 2>/dev/null || true   # serve all'installatore
chmod 644 "$ISO/casper/vmlinuz" "$ISO/casper/initrd"
chroot "$CH" dpkg-query -W --showformat='${Package} ${Version}\n' > "$ISO/casper/filesystem.manifest"
# Le cartelle /proc /sys /dev /run /tmp /boot devono esistere (vuote) o l'avvio si blocca
mksquashfs "$CH" "$ISO/casper/filesystem.squashfs" -comp zstd -Xcompression-level 19 -b 1M -noappend -no-progress \
  -wildcards -e 'proc/*' 'sys/*' 'dev/*' 'run/*' 'tmp/*' 'boot/*'
printf '%s' "$(du -sx --block-size=1 "$CH" | cut -f1)" > "$ISO/casper/filesystem.size"
echo "Rise $VERSION - live amd64 $(date +%Y%m%d)" > "$ISO/.disk/info"
cp "$ROOT/build/grub.cfg" "$ISO/boot/grub/grub.cfg"
mkdir -p "$ISO/boot/grub/themes/rise" && cp "$ROOT/system/share/grub-theme/"* "$ISO/boot/grub/themes/rise/"
( cd "$ISO" && find . -type f ! -name md5sum.txt -print0 | xargs -0 md5sum > md5sum.txt )

log "9/9 ISO avviabile (BIOS + UEFI)"
grub-mkrescue -o "$OUT/$NAME.iso" "$ISO" -- -volid "RISE_${VERSION//./}"
( cd "$OUT" && sha256sum "$NAME.iso" > "$NAME.iso.sha256" )
cp "$ISO/casper/filesystem.manifest" "$OUT/$NAME.packages.txt"
ls -lh "$OUT"
echo "Fatto: $OUT/$NAME.iso"
