import hashlib
import shutil
import time
from pathlib import Path
from textwrap import dedent

from .logger import logger


def get_file_archive_name(package: str) -> str:
    version = time.strftime("%Y%m%d")
    return f"{package}-{version}.tar.xz"


def get_url_for_package(pkgname: str, mirror_url: str):
    if mirror_url[-1] == "/":
        return mirror_url + "archive/" + pkgname + ".tar.xz"
    return mirror_url + "/archive/" + pkgname + ".tar.xz"


def cleanup():
    logger.info("Cleaning up.")
    Path("texlive.tlpdb").unlink()


def write_contents_file(mirror_url: str, pkgs: dict, file: Path):
    template = dedent(
        """\
    # These are the CTAN packages bundled in this package.
    # They were downloaded from {url}archive/
    # The svn revision number (on the TeXLive repository)
    # on which each package is based is given in the 2nd column.

    """
    ).format(url=mirror_url)
    for pkg in pkgs:
        template += f"{pkgs[pkg]['name']} {pkgs[pkg]['revision']}\n"
    with open(file, "w") as f:
        f.write(template)


def check_whether_gpg_exists():
    return shutil.which("gpg") is not None


def find_checksum_from_file(fname: Path, hashtype: str):
    hash = hashlib.new(hashtype)
    with open(fname, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            hash.update(chunk)
    return hash.hexdigest()
