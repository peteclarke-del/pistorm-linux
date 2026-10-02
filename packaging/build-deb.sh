#!/usr/bin/env bash
#
# Build the Debian package for PiStorm Imager.
#
#   packaging/build-deb.sh [OUTPUT_DIR]
#
# The application is pure Python, so the package is "all" - one file for every
# architecture. It installs into /usr/lib/pistorm-imager, with a launcher in
# /usr/bin, the desktop entry and icon where the desktop looks, the record of
# which package it is (package-target), and the root-side update installer.

set -euo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
output_dir="$(realpath -m -- "${1:-${project_dir}/dist}")"
# shellcheck source=packaging/package-target.sh
source "${project_dir}/packaging/package-target.sh"

version="$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' \
    "${project_dir}/pistorm_imager/__init__.py")"
if [[ -z "${version}" || "${version}" == *-* ]]; then
    echo "Cannot package version '${version}': it must be X.Y.Z." >&2
    exit 1
fi
architecture=all

stage="$(mktemp -d)"
trap 'rm -rf -- "${stage}"' EXIT
# mktemp's folder is private, and it becomes the package's /.
chmod 0755 "${stage}"
application_lib="${stage}/usr/lib/pistorm-imager"
mkdir -p "${application_lib}" "${stage}/usr/bin" "${stage}/DEBIAN" \
    "${stage}/usr/share/applications" "${stage}/usr/share/doc/pistorm-imager"

python3 -m pip install --quiet --no-deps --no-compile --no-build-isolation \
    --target "${application_lib}" "${project_dir}"
# pip's own launchers are not wanted; the package has its own in /usr/bin.
rm -rf -- "${application_lib}/bin"
mkdir -p "${application_lib}/bin"
install -m 0755 "${project_dir}/packaging/install-update" \
    "${application_lib}/bin/install-update"
write_package_target "${application_lib}/package-target" "${architecture}"
chmod 0644 "${application_lib}/package-target"

install -m 0755 "${project_dir}/packaging/pistorm-imager" "${stage}/usr/bin/pistorm-imager"
data="${project_dir}/pistorm_imager/data"
install -m 0644 "${data}/applications/pistorm-imager.desktop" \
    "${stage}/usr/share/applications/pistorm-imager.desktop"
(cd "${data}/icons" && find . -type f -print0) | while IFS= read -r -d '' icon; do
    install -D -m 0644 "${data}/icons/${icon}" "${stage}/usr/share/icons/${icon#./}"
done
install -m 0644 "${project_dir}/README.md" "${stage}/usr/share/doc/pistorm-imager/README.md"

install -m 0755 "${project_dir}/packaging/postinst" "${stage}/DEBIAN/postinst"
install -m 0755 "${project_dir}/packaging/postrm" "${stage}/DEBIAN/postrm"
chmod -R go-w "${stage}"

installed_size="$(du -sk --exclude=DEBIAN "${stage}" | cut -f 1)"
cat > "${stage}/DEBIAN/control" <<CONTROL
Package: pistorm-imager
Version: ${version}
Section: utils
Priority: optional
Architecture: ${architecture}
Installed-Size: ${installed_size}
Maintainer: Pete Clarke <pete.clarke@gmail.com>
Homepage: https://github.com/peteclarke-del/pistorm-linux
Depends: python3 (>= 3.10), python3-gi, gir1.2-gtk-4.0, gir1.2-adw-1, dosfstools, util-linux, udisks2, pkexec, xz-utils, 7zip | p7zip-full
Recommends: zstd
Suggests: fs-uae
Description: Build Amiga SD cards for PiStorm and Emu68
 PiStorm Imager prepares SD cards and drive images for a PiStorm running
 Emu68: a boot partition with Emu68 and its settings, and Amiga drives with
 AmigaOS installed from floppy images or a CD and the software you choose.
 It also writes prepared images such as PiMiga, rebuilds one drive on an
 existing card, and exports drives as .hdf files.
CONTROL

mkdir -p "${output_dir}"
package="${output_dir}/$(package_file_name "${version}" "${architecture}")"
dpkg-deb --root-owner-group --build "${stage}" "${package}" >/dev/null
echo "${package}"
