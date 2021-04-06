import argparse
import logging
import re
import tarfile
import tempfile
import time
import typing
from pathlib import Path
from textwrap import dedent

import requests

from github_handler import upload_asset

logger = logging.getLogger("archive-downloader")

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="%Y-%m-%d-%H:%M:%S",
)

perl_to_py_dict_regex = re.compile(r"(?P<key>\S*) (?P<value>[\s\S][^\n]*)")


def find_mirror() -> str:
    """Find a mirror and lock to it. Or else things could
    go weird."""
    # base_mirror = "http://mirror.ctan.org/systems/texlive/tlnet"
    # con = requests.get(base_mirror)
    # return con.history[-1].url
    # maybe let's try texlive.info
    timenow = time.localtime()
    url = "https://texlive.info/tlnet-archive/%d/%02d/%02d/tlnet/" % (
        timenow.tm_year,
        timenow.tm_mon,
        timenow.tm_mday,
    )
    con = requests.get(url)
    if con.status_code == 404:
        return "https://texlive.info/tlnet-archive/%d/%02d/%02d/tlnet/" % (
            timenow.tm_year,
            timenow.tm_mon,
            timenow.tm_mday - 1,
        )
    return url


def get_file_archive_name(pacakge: str) -> str:
    version = time.strftime("%Y%m%d")
    return f"texlive-core-{version}.tar.xz"


def download_texlive_tlpdb(mirror: str) -> None:
    con = requests.get(mirror + "tlpkg/texlive.tlpdb")
    with open("texlive.tlpdb", "wb") as f:
        f.write(con.content)
    logger.info("Downloaded texlive.tlpdb")


def cleanup():
    logger.info("Cleaning up.")
    Path("texlive.tlpdb").unlink()


def parse_perl(perl_code) -> typing.Dict[str, typing.Union[list, str]]:
    final_dict: typing.Dict[str, typing.Union[list, str]] = {}
    for findings in perl_to_py_dict_regex.finditer(perl_code):
        key = findings.group("key")
        value = findings.group("value")
        if key:
            if key in final_dict:
                exists_value = final_dict[key]
                if isinstance(exists_value, str):
                    exists_value = [final_dict[key], value]
                else:
                    exists_value.append(value)
                final_dict[key] = exists_value
            else:
                final_dict[key] = value
    return final_dict


def get_all_packages() -> typing.Dict[str, typing.Dict[str, typing.Union[list, str]]]:
    with open("texlive.tlpdb", "r", encoding="utf-8") as f:
        lines = f.readlines()
    logger.info("Parsing texlive.tlpdb")
    package_list: typing.Dict[str, typing.Dict[str, typing.Union[list, str]]] = {}
    last_line: int = 0
    for n, line in enumerate(lines):
        if line == "\n":
            tmp = "".join(lines[last_line : n + 1]).strip()
            tmp_dict = parse_perl(tmp)
            name = str(tmp_dict["name"])
            if "." not in name:
                package_list[name] = tmp_dict
            last_line = n
    return package_list


def get_dependencies(
    name: str,
    pkglist: typing.Dict[str, typing.Dict[str, typing.Union[list, str]]],
    collection_list: typing.List[str],
) -> typing.List[str]:
    pkg: typing.Dict[str, typing.Union[list, str]] = pkglist[name]
    deps_list = []
    if "depend" not in pkg:
        return []
    dep_name = pkg["depend"]
    if isinstance(dep_name, str):
        if dep_name not in deps_list:
            deps_list.append(dep_name)
    else:
        for i in pkg["depend"]:
            dep_name = i
            if "collection" in dep_name or "scheme" in dep_name:
                if dep_name not in collection_list:
                    collection_list.append(dep_name)
                    deps_list += get_dependencies(dep_name, pkglist, collection_list)
            else:
                if dep_name not in deps_list:
                    deps_list.append(dep_name)
    return deps_list


def get_needed_packages_with_info(
    scheme: str,
) -> typing.Dict[str, typing.Union[typing.Dict[str, typing.Union[str, list]]]]:
    logger.info("Resolving scheme %s", scheme)
    pkg_list = get_all_packages()
    deps = get_dependencies(scheme, pkg_list, [])
    deps.sort()
    deps_info = {}
    for i in deps:
        if "." not in i:
            deps_info[i] = pkg_list[i]
    return deps_info


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


def download(url, local_filename):
    with requests.get(url, stream=True) as r:
        r.raise_for_status()
        with open(local_filename, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                # If you have chunk encoded response uncomment if
                # and set chunk_size parameter to None.
                # if chunk:
                f.write(chunk)


def download_and_retry(url: str, local_filename: Path):
    for i in range(10):
        logger.info("Downloading %s. Try: %s", url, i)
        try:
            download(url, local_filename)
            break
        except requests.HTTPError:
            pass
    else:
        raise Exception("%s can't be downloaded" % url)
    return True


def get_url_for_package(pkgname: str, mirror_url: str):
    if mirror_url[-1] == "/":
        return mirror_url + "archive/" + pkgname + ".tar.xz"
    return mirror_url + "/archive/" + pkgname + ".tar.xz"


def create_tar_archive(path: Path, output_filename: Path):
    logger.info("Creating tar file.")
    with tarfile.open(output_filename, "w:xz") as tar_handle:
        for f in path.iterdir():
            tar_handle.add(str(f), recursive=False, arcname=f.name)


def download_all_packages(
    scheme: str,
    mirror_url: str,
    final_tar_location: Path,
    needed_pkgs: typing.Dict[
        str, typing.Union[typing.Dict[str, typing.Union[str, list]]]
    ],
):
    logger.info("Starting to Download.")
    with tempfile.TemporaryDirectory() as tmpdir_main:
        logger.info("Using tempdir: %s", tmpdir_main)
        tmpdir = Path(tmpdir_main)

        write_contents_file(mirror_url, needed_pkgs, tmpdir / "CONTENTS")
        for pkg in needed_pkgs:
            logger.info("Downloading %s", needed_pkgs[pkg]["name"])
            url = get_url_for_package(str(needed_pkgs[pkg]["name"]), mirror_url)
            file_name = tmpdir / Path(url).name
            download_and_retry(url, file_name)
        create_tar_archive(path=tmpdir, output_filename=final_tar_location)


def create_fmts(
    pkg_infos: typing.Dict[
        str, typing.Union[typing.Dict[str, typing.Union[str, list]]]
    ],
    filename_save: Path,
) -> Path:
    logger.info("Creating %s file", filename_save)
    key_value_search_regex = re.compile(r"(?P<key>\S*)=(?P<value>[\S]+)")
    quotes_search_regex = re.compile(
        r"((?<![\\])['\"])(?P<options>(?:.(?!(?<![\\])\1))*.?)\1"
    )
    final_file = ""

    def parse_perl_string(temp: str) -> typing.Dict[str, str]:
        t_dict: typing.Dict[str, str] = {}
        for mat in key_value_search_regex.finditer(temp):
            if '"' not in mat.group("value"):
                t_dict[mat.group("key")] = mat.group("value")
        quotes_search = quotes_search_regex.search(temp)
        if quotes_search:
            t_dict["options"] = quotes_search.group("options")
        for i in {"name", "engine", "patterns", "options"}:
            if i not in t_dict:
                t_dict[i] = "-"
        return t_dict

    for pkg in pkg_infos:
        temp_pkg = pkg_infos[pkg]
        if "execute" in temp_pkg:
            temp = temp_pkg["execute"]
            if isinstance(temp, str):
                if "AddFormat" in temp:
                    parsed_dict = parse_perl_string(temp)
                    final_file += "{name} {engine} {patterns} {options}\n".format(
                        **parsed_dict
                    )
            else:
                for each in temp:
                    if "AddFormat" in each:
                        parsed_dict = parse_perl_string(each)
                        final_file += "{name} {engine} {patterns} {options}\n".format(
                            **parsed_dict
                        )
    with filename_save.open("w", encoding="utf-8") as f:
        f.write(final_file)
        logger.info("Wrote %s", filename_save)
    return filename_save


def create_maps(
    pkg_infos: typing.Dict[
        str, typing.Union[typing.Dict[str, typing.Union[str, list]]]
    ],
    filename_save: Path,
) -> Path:
    logger.info("Creating %s file", filename_save)
    final_file = ""

    mixed_map_regex = re.compile(r"add(?P<final>MixedMap[\s\S][^\n]*)")
    map_regex = re.compile(r"add(?P<final>Map[\s\S][^\n]*)")

    def parse_string(temp: str):
        if "addMixedMap" in temp:
            res = mixed_map_regex.search(temp)
            if res:
                return res.group("final")
        elif "addMap" in temp:
            res = map_regex.search(temp)
            if res:
                return res.group("final")

    for pkg in pkg_infos:
        temp_pkg = pkg_infos[pkg]
        if "execute" in temp_pkg:
            temp = temp_pkg["execute"]
            if "addMap" in temp or "addMixedMap" in temp:
                if isinstance(temp, str):
                    final_file += parse_string(temp)
                    final_file += "\n"
                else:
                    for each in temp:
                        final_file += parse_string(each)
                        final_file += "\n"
    # let's sort the line
    temp_lines = final_file.split("\n")
    temp_lines.sort()
    final_file = "\n".join(temp_lines)
    final_file.strip()
    with filename_save.open("w", encoding="utf-8") as f:
        f.write(final_file)
        logger.info("Wrote %s", filename_save)
    return filename_save


def main(scheme: str, directory: Path, package: str):
    mirror = find_mirror()
    logger.info("Using mirror: %s", mirror)
    download_texlive_tlpdb(mirror)
    
    needed_pkgs = get_needed_packages_with_info(scheme)
    archive_name = directory / get_file_archive_name(package)
    
    # arch uses "scheme-medium" for texlive-core
    download_all_packages(scheme, mirror, archive_name, needed_pkgs)
    logger.info("Uploading %s", archive_name)
    upload_asset(archive_name)  # uploads archive
    
    fmts_file = directory / (package + ".fmts")
    create_fmts(needed_pkgs, fmts_file)
    logger.info("Uploading %s", fmts_file)
    upload_asset(fmts_file)

    maps_file = directory / (package + ".maps")
    create_maps(needed_pkgs, maps_file)
    logger.info("Uploading %s", maps_file)
    upload_asset(maps_file)

    cleanup()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Process some integers.")
    parser.add_argument(
        "package", type=str, help="Tha pacakge to build.", choices=["texlive-core"]
    )
    parser.add_argument("directory", type=str, help="The directory to save files.")
    args = parser.parse_args()
    logger.info("Starting...")
    logger.info("Package: %s", args.package)
    logger.info("Directory: %s", args.directory)
    if args.package == "texlive-core":
        main("scheme-medium", Path(args.directory), args.package)
