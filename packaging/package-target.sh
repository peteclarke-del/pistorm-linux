# The one place the package's file name is built, and the record the package
# keeps of it. Sourced by build-deb.sh and by the tests.
#
# The application is pure Python, so one package serves every architecture:
# "all", as dpkg calls it. The record goes beside the application, at
# /usr/lib/pistorm-imager/package-target, and the update asks the release for
# the file it names.

package_distro=ubuntu24.04

package_file_name() {
    # package_file_name VERSION ARCH
    printf 'PiStorm-Imager_%s_%s_%s.deb' "$1" "${package_distro}" "$2"
}

write_package_target() {
    # write_package_target FILE ARCH
    printf 'distro=%s\narch=%s\npackage=%s\n' \
        "${package_distro}" "$2" "$(package_file_name '{version}' "$2")" > "$1"
}
