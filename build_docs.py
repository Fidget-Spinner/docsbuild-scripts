#!/usr/bin/env python3

"""Build the Python docs for various branches and various languages.

Usage:

  build_docs.py [-h] [-d] [-q] [-b 3.7] [-r BUILD_ROOT] [-w WWW_ROOT]
                [--skip-cache-invalidation] [--group GROUP] [--git]
                [--log-directory LOG_DIRECTORY]
                [--languages [fr [fr ...]]]


Without any arguments builds docs for all branches configured in the
global BRANCHES value and all languages configured in LANGUAGES,
ignoring the -d flag as it's given in the BRANCHES configuration.

-q selects "quick build", which means to build only HTML.

-d allow the docs to be built even if the branch is in
development mode (i.e. version contains a, b or c).

Translations are fetched from github repositories according to PEP
545.  --languages allow select translations, use "--languages" to
build all translations (default) or "--languages en" to skip all
translations (as en is the untranslated version)..

This script was originally created and by Georg Brandl in March
2010.
Modified by Benjamin Peterson to do CDN cache invalidation.
Modified by Julien Palard to build translations.

"""

from bisect import bisect_left as bisect
import filecmp
import logging
import os
import pathlib
import re
import shutil
import subprocess
import sys
from datetime import datetime


try:
    import sentry_sdk
except ImportError:
    sentry_sdk = None
else:
    sentry_sdk.init()

VERSION = "19.0"

BRANCHES = [
    # version, git branch, isdev
    ("3.6", "3.6", False),
    ("3.7", "3.7", False),
    ("3.8", "3.8", False),
    ("3.9", "3.9", True),
    ("3.10", "master", True),
]

LANGUAGES = ["en", "fr", "ja", "ko", "pt-br", "zh-cn", "zh-tw", "id"]

SPHINXOPTS = {
    "ja": [
        "-D latex_engine=platex",
        "-D latex_elements.inputenc=",
        "-D latex_elements.fontenc=",
    ],
    "ko": [
        "-D latex_engine=xelatex",
        "-D latex_elements.inputenc=",
        "-D latex_elements.fontenc=",
        r"-D latex_elements.preamble=\\usepackage{kotex}\\setmainhangulfont{UnBatang}\\setsanshangulfont{UnDotum}\\setmonohangulfont{UnTaza}",
    ],
    "pt-br": [
        "-D latex_engine=xelatex",
        "-D latex_elements.inputenc=",
        "-D latex_elements.fontenc=",
    ],
    "fr": [
        "-D latex_engine=xelatex",
        "-D latex_elements.inputenc=",
        r"-D latex_elements.fontenc=\\usepackage{fontspec}",
    ],
    "en": [
        "-D latex_engine=xelatex",
        "-D latex_elements.inputenc=",
        "-D latex_elements.fontenc=",
    ],
    "zh-cn": [
        "-D latex_engine=xelatex",
        "-D latex_elements.inputenc=",
        r"-D latex_elements.fontenc=\\usepackage{xeCJK}",
    ],
    "zh-tw": [
        "-D latex_engine=xelatex",
        "-D latex_elements.inputenc=",
        r"-D latex_elements.fontenc=\\usepackage{xeCJK}",
    ],
    "id": [
        "-D latex_engine=xelatex",
        "-D latex_elements.inputenc=",
        "-D latex_elements.fontenc=",
    ],
}


def shell_out(cmd, shell=False, logfile=None):
    logging.debug("Running command %r", cmd)
    now = str(datetime.now())
    try:
        output = subprocess.check_output(
            cmd,
            shell=shell,
            stdin=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding="utf-8",
            errors="backslashreplace",
        )
        if logfile:
            with open(logfile, "a+") as log:
                log.write("# " + now + "\n")
                log.write("# Command {cmd!r} ran successfully:".format(cmd=cmd))
                log.write(output)
                log.write("\n\n")
        return output
    except subprocess.CalledProcessError as e:
        if sentry_sdk:
            with sentry_sdk.push_scope() as scope:
                scope.fingerprint = ["{{ default }}", str(cmd)]
                sentry_sdk.capture_exception(e)
        if logfile:
            with open(logfile, "a+") as log:
                log.write("# " + now + "\n")
                log.write("# Command {cmd!r} failed:".format(cmd=cmd))
                log.write(e.output)
                log.write("\n\n")
            logging.error("Command failed (see %s at %s)", logfile, now)
        else:
            logging.error("Command failed with output %r", e.output)


def changed_files(left, right):
    """Compute a list of different files between left and right, recursively.
    Resulting paths are relative to left.
    """
    changed = []

    def traverse(dircmp_result):
        base = pathlib.Path(dircmp_result.left).relative_to(left)
        changed.extend(str(base / file) for file in dircmp_result.diff_files)
        for dircomp in dircmp_result.subdirs.values():
            traverse(dircomp)

    traverse(filecmp.dircmp(left, right))
    return changed


def git_clone(repository, directory, branch=None):
    """Clone or update the given repository in the given directory.
    Optionally checking out a branch.
    """
    logging.info("Updating repository %s in %s", repository, directory)
    try:
        if not os.path.isdir(os.path.join(directory, ".git")):
            raise AssertionError("Not a git repository.")
        shell_out(["git", "-C", directory, "fetch"])
        if branch:
            shell_out(["git", "-C", directory, "checkout", branch])
            shell_out(["git", "-C", directory, "reset", "--hard", "origin/" + branch])
    except (subprocess.CalledProcessError, AssertionError):
        if os.path.exists(directory):
            shutil.rmtree(directory)
        logging.info("Cloning %s into %s", repository, directory)
        os.makedirs(directory, mode=0o775)
        shell_out(
            ["git", "clone", "--depth=1", "--no-single-branch", repository, directory]
        )
        if branch:
            shell_out(["git", "-C", directory, "checkout", branch])


def pep_545_tag_to_gettext_tag(tag):
    """Transforms PEP 545 language tags like "pt-br" to gettext language
    tags like "pt_BR". (Note that none of those are IETF language tags
    like "pt-BR").
    """
    if "-" not in tag:
        return tag
    language, region = tag.split("-")
    return language + "_" + region.upper()


def locate_nearest_version(available_versions, target_version):
    """Look for the nearest version of target_version in available_versions.
    Versions are to be given as tuples, like (3, 7) for 3.7.

    >>> locate_nearest_version(["2.7", "3.6", "3.7", "3.8"], "3.9")
    '3.8'
    >>> locate_nearest_version(["2.7", "3.6", "3.7", "3.8"], "3.5")
    '3.6'
    >>> locate_nearest_version(["2.7", "3.6", "3.7", "3.8"], "2.6")
    '2.7'
    >>> locate_nearest_version(["2.7", "3.6", "3.7", "3.8"], "3.10")
    '3.8'
    >>> locate_nearest_version(["2.7", "3.6", "3.7", "3.8"], "3.7")
    '3.7'
    """

    def version_to_tuple(version):
        return tuple(int(part) for part in version.split("."))

    def tuple_to_version(version_tuple):
        return ".".join(str(part) for part in version_tuple)

    available_versions_tuples = [
        version_to_tuple(available_version) for available_version in available_versions
    ]
    target_version_tuple = version_to_tuple(target_version)
    try:
        found = available_versions_tuples[
            bisect(available_versions_tuples, target_version_tuple)
        ]
    except IndexError:
        found = available_versions_tuples[-1]
    return tuple_to_version(found)


def translation_branch(locale_repo, locale_clone_dir, needed_version):
    """Some cpython versions may be untranslated, being either too old or
    too new.

    This function looks for remote branches on the given repo, and
    returns the name of the nearest existing branch.
    """
    git_clone(locale_repo, locale_clone_dir)
    remote_branches = shell_out(["git", "-C", locale_clone_dir, "branch", "-r"])
    branches = []
    for branch in remote_branches.split("\n"):
        if re.match(r".*/[0-9]+\.[0-9]+$", branch):
            branches.append(branch.split("/")[-1])
    return locate_nearest_version(branches, needed_version)


def build_one(
    version,
    git_branch,
    isdev,
    quick,
    venv,
    build_root,
    group="docs",
    log_directory="/var/log/docsbuild/",
    language=None,
):
    if not language:
        language = "en"
    if sentry_sdk:
        with sentry_sdk.configure_scope() as scope:
            scope.set_tag("version", version)
            scope.set_tag("language", language)
    checkout = os.path.join(build_root, version, "cpython-{lang}".format(lang=language))
    logging.info("Build start for version: %s, language: %s", version, language)
    sphinxopts = SPHINXOPTS[language].copy()
    sphinxopts.extend(["-q"])
    if language != "en":
        gettext_language_tag = pep_545_tag_to_gettext_tag(language)
        locale_dirs = os.path.join(build_root, version, "locale")
        locale_clone_dir = os.path.join(
            locale_dirs, gettext_language_tag, "LC_MESSAGES"
        )
        locale_repo = "https://github.com/python/python-docs-{}.git".format(language)
        git_clone(
            locale_repo,
            locale_clone_dir,
            translation_branch(locale_repo, locale_clone_dir, version),
        )
        sphinxopts.extend(
            (
                "-D locale_dirs={}".format(locale_dirs),
                "-D language={}".format(gettext_language_tag),
                "-D gettext_compact=0",
            )
        )
    git_clone("https://github.com/python/cpython.git", checkout, git_branch)
    maketarget = (
        "autobuild-" + ("dev" if isdev else "stable") + ("-html" if quick else "")
    )
    logging.info("Running make %s", maketarget)
    logname = "cpython-{lang}-{version}.log".format(lang=language, version=version)
    python = os.path.join(venv, "bin/python")
    sphinxbuild = os.path.join(venv, "bin/sphinx-build")
    blurb = os.path.join(venv, "bin/blurb")
    shell_out(
        [
            "make",
            "-C",
            os.path.join(checkout, "Doc"),
            "PYTHON=" + python,
            "SPHINXBUILD=" + sphinxbuild,
            "BLURB=" + blurb,
            "VENVDIR=" + venv,
            "SPHINXOPTS=" + " ".join(sphinxopts),
            "SPHINXERRORHANDLING=",
            maketarget,
        ],
        logfile=os.path.join(log_directory, logname),
    )
    shell_out(["chgrp", "-R", group, log_directory])
    logging.info("Build done for version: %s, language: %s", version, language)


def copy_build_to_webroot(
    build_root, version, language, group, quick, skip_cache_invalidation, www_root
):
    """Copy a given build to the appropriate webroot with appropriate rights.
    """
    logging.info("Publishing start for version: %s, language: %s", version, language)
    checkout = os.path.join(build_root, version, "cpython-{lang}".format(lang=language))
    if language == "en":
        target = os.path.join(www_root, version)
    else:
        language_dir = os.path.join(www_root, language)
        os.makedirs(language_dir, exist_ok=True)
        try:
            shell_out(["chgrp", "-R", group, language_dir])
        except subprocess.CalledProcessError as err:
            logging.warning("Can't change group of %s: %s", language_dir, str(err))
        os.chmod(language_dir, 0o775)
        target = os.path.join(language_dir, version)

    os.makedirs(target, exist_ok=True)
    try:
        os.chmod(target, 0o775)
    except PermissionError as err:
        logging.warning("Can't change mod of %s: %s", target, str(err))
    try:
        shell_out(["chgrp", "-R", group, target])
    except subprocess.CalledProcessError as err:
        logging.warning("Can't change group of %s: %s", target, str(err))

    changed = changed_files(os.path.join(checkout, "Doc/build/html"), target)
    logging.info("Copying HTML files to %s", target)
    shell_out(["chown", "-R", ":" + group, os.path.join(checkout, "Doc/build/html/")])
    shell_out(["chmod", "-R", "o+r", os.path.join(checkout, "Doc/build/html/")])
    shell_out(
        [
            "find",
            os.path.join(checkout, "Doc/build/html/"),
            "-type",
            "d",
            "-exec",
            "chmod",
            "o+x",
            "{}",
            ";",
        ]
    )
    if quick:
        shell_out(["rsync", "-a", os.path.join(checkout, "Doc/build/html/"), target])
    else:
        shell_out(
            [
                "rsync",
                "-a",
                "--delete-delay",
                "--filter",
                "P archives/",
                os.path.join(checkout, "Doc/build/html/"),
                target,
            ]
        )
    if not quick:
        logging.debug("Copying dist files")
        shell_out(["chown", "-R", ":" + group, os.path.join(checkout, "Doc/dist/")])
        shell_out(
            ["chmod", "-R", "o+r", os.path.join(checkout, os.path.join("Doc/dist/"))]
        )
        shell_out(["mkdir", "-m", "o+rx", "-p", os.path.join(target, "archives")])
        shell_out(["chown", ":" + group, os.path.join(target, "archives")])
        shell_out(
            "cp -a {src} {dst}".format(
                src=os.path.join(checkout, "Doc/dist/*"),
                dst=os.path.join(target, "archives"),
            ),
            shell=True,
        )
        changed.append("archives/")
        for fn in os.listdir(os.path.join(target, "archives")):
            changed.append("archives/" + fn)

    logging.info("%s files changed", len(changed))
    if changed and not skip_cache_invalidation:
        targets_dir = www_root
        prefixes = shell_out(["find", "-L", targets_dir, "-samefile", target])
        prefixes = prefixes.replace(targets_dir + "/", "")
        prefixes = [prefix + "/" for prefix in prefixes.split("\n") if prefix]
        to_purge = prefixes[:]
        for prefix in prefixes:
            to_purge.extend(prefix + p for p in changed)
        logging.info("Running CDN purge")
        shell_out(
            ["curl", "-XPURGE", "https://docs.python.org/{%s}" % ",".join(to_purge)]
        )
    logging.info("Publishing done for version: %s, language: %s", version, language)


def head(lines, n=10):
    return "\n".join(lines.split("\n")[:n])


def version_info():
    platex_version = head(
        subprocess.check_output(["platex", "--version"], universal_newlines=True), n=3
    )

    xelatex_version = head(
        subprocess.check_output(["xelatex", "--version"], universal_newlines=True), n=2
    )
    print(
        f"""build_docs: {VERSION}

# platex

{platex_version}


# xelatex

{xelatex_version}
    """
    )


def parse_args():
    from argparse import ArgumentParser

    parser = ArgumentParser(
        description="Runs a build of the Python docs for various branches."
    )
    parser.add_argument(
        "-d",
        "--devel",
        action="store_true",
        help="Use make autobuild-dev instead of autobuild-stable",
    )
    parser.add_argument(
        "-q",
        "--quick",
        action="store_true",
        help="Make HTML files only (Makefile rules suffixed with -html).",
    )
    parser.add_argument(
        "-b",
        "--branch",
        metavar="3.6",
        help="Version to build (defaults to all maintained branches).",
    )
    parser.add_argument(
        "-r",
        "--build-root",
        help="Path to a directory containing a checkout per branch.",
        default="/srv/docsbuild",
    )
    parser.add_argument(
        "-w",
        "--www-root",
        help="Path where generated files will be copied.",
        default="/srv/docs.python.org",
    )
    parser.add_argument(
        "--skip-cache-invalidation",
        help="Skip fastly cache invalidation.",
        action="store_true",
    )
    parser.add_argument(
        "--group",
        help="Group files on targets and www-root file should get.",
        default="docs",
    )
    parser.add_argument(
        "--git",
        default=True,
        help="Deprecated: Use git instead of mercurial. "
        "Defaults to True for compatibility.",
        action="store_true",
    )
    parser.add_argument(
        "--log-directory",
        help="Directory used to store logs.",
        default="/var/log/docsbuild/",
    )
    parser.add_argument(
        "--languages",
        nargs="*",
        default=LANGUAGES,
        help="Language translation, as a PEP 545 language tag like" " 'fr' or 'pt-br'.",
        metavar="fr",
    )
    parser.add_argument(
        "--jobs",
        "-j",
        type=int,
        default=4,
        help="Specifies the number of jobs (languages, versions) "
        "to run simultaneously.",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="Get build_docs and dependencies version info",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    if args.version:
        version_info()
        exit(0)
    if args.log_directory:
        args.log_directory = os.path.abspath(args.log_directory)
    if args.build_root:
        args.build_root = os.path.abspath(args.build_root)
    if args.www_root:
        args.www_root = os.path.abspath(args.www_root)
    if sys.stderr.isatty():
        logging.basicConfig(format="%(levelname)s:%(message)s", stream=sys.stderr)
    else:
        logging.basicConfig(
            format="%(levelname)s:%(asctime)s:%(message)s",
            filename=os.path.join(args.log_directory, "docsbuild.log"),
        )
    logging.root.setLevel(logging.DEBUG)
    venv = os.path.join(args.build_root, "venv")
    if args.branch:
        branches_to_do = [
            branch
            for branch in BRANCHES
            if str(branch[0]) == args.branch or branch[1] == args.branch
        ]
    else:
        branches_to_do = BRANCHES
    if not args.languages:
        # Allow "--languages" to build all languages (as if not given)
        # instead of none.  "--languages en" builds *no* translation,
        # as "en" is the untranslated one.
        args.languages = LANGUAGES
    for version, git_branch, devel in branches_to_do:
        for language in args.languages:
            if sentry_sdk:
                with sentry_sdk.configure_scope() as scope:
                    scope.set_tag("version", version)
                    scope.set_tag("language", language if language else "en")
            try:
                build_one(
                    version,
                    git_branch,
                    devel,
                    args.quick,
                    venv,
                    args.build_root,
                    args.group,
                    args.log_directory,
                    language,
                )
                copy_build_to_webroot(
                    args.build_root,
                    version,
                    language,
                    args.group,
                    args.quick,
                    args.skip_cache_invalidation,
                    args.www_root,
                )
            except Exception as err:
                logging.error(
                    "Exception while building %s version %s: %s",
                    language,
                    version,
                    err,
                )
                if sentry_sdk:
                    sentry_sdk.capture_exception(err)


if __name__ == "__main__":
    main()
