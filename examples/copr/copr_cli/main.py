#!/usr/bin/python3
# -*- coding: UTF-8 -*-
import os
import re
import subprocess

import argparse
import sys
import datetime
import time
import simplejson
from collections import defaultdict

import logging
try:
    from urllib.parse import urlparse
except ImportError:
    from urlparse import urlparse

if sys.version_info < (2, 7):
    class NullHandler(logging.Handler):
        def emit(self, record):
            pass
else:
    from logging import NullHandler

log = logging.getLogger(__name__)
log.addHandler(NullHandler())

from copr import CoprClient
from copr.client.responses import CoprResponse
import copr.exceptions as copr_exceptions

from .util import ProgressBar
from .build_config import MockProfile

import pkg_resources

ON_OFF_MAP = {
    'on': True,
    'off': False,
    None: None,
}

no_config_warning = """
================= WARNING: =======================
File '{0}' is missing or incorrect.
See documentation: man copr-cli.
Any operation requiring credentials will fail!
==================================================

"""


class Commands(object):
    def __init__(self, config):
        self.config = config
        try:
            self.client = CoprClient.create_from_file_config(config)
        except (copr_exceptions.CoprNoConfException,
                copr_exceptions.CoprConfigException):
            sys.stderr.write(no_config_warning.format(config or "~/.config/copr"))
            self.client = CoprClient(
                copr_url=u"http://copr.fedoraproject.org",
                no_config=True
            )

    def requires_api_auth(func):
        """ Decorator that checks config presence
        """

        def wrapper(self, args):
            if self.client.no_config:
                sys.stderr.write("Error: Operation requires api authentication\n")
                sys.exit(6)

            return func(self, args)

        wrapper.__doc__ = func.__doc__
        wrapper.__name__ = func.__name__
        return wrapper

    def check_username_presence(func):
        """ Decorator that checks if username was provided
        """

        def wrapper(self, args):
            if self.client.no_config and args.username is None:
                sys.stderr.write(
                    "Error: Operation requires username\n"
                    "Pass username to command or create `~/.config/copr`\n")
                sys.exit(6)

            if args.username is None and self.client.username is None:
                sys.stderr.write(
                    "Error: Operation requires username\n"
                    "Pass username to command or add it to `~/.config/copr`\n")
                sys.exit(6)

            return func(self, args)

        wrapper.__doc__ = func.__doc__
        wrapper.__name__ = func.__name__
        return wrapper

    def _watch_builds(self, build_ids):
        """
         :param build_ids: list of build IDs
         """
        print("Watching build(s): (this may be safely interrupted)")

        prevstatus = defaultdict(lambda: None)
        failed_ids = []

        watched = set(build_ids)
        done = set()

        try:
            while watched != done:
                for build_id in watched:
                    if build_id in done:
                        continue

                    build_details = self.client.get_build_details(build_id)

                    if build_details.output != "ok":
                        errmsg = "  Build {1}: Unable to get build status: {0}". \
                            format(build_details.error, build_id)
                        raise copr_exceptions.CoprRequestException(errmsg)

                    now = datetime.datetime.now()
                    if prevstatus[build_id] != build_details.status:
                        prevstatus[build_id] = build_details.status
                        print("  {0} Build {2}: {1}".format(
                            now.strftime("%H:%M:%S"),
                            build_details.status, build_id))

                    if build_details.status in ["failed"]:
                        failed_ids.append(build_id)
                    if build_details.status in ["succeeded", "skipped",
                                                "failed", "canceled"]:
                        done.add(build_id)
                    if build_details.status == "unknown":
                        raise copr_exceptions.CoprBuildException(
                            "Unknown status.")

                if watched == done:
                    break

                time.sleep(30)

            if failed_ids:
                raise copr_exceptions.CoprBuildException(
                    "Build(s) {0} failed.".format(
                        ", ".join(str(x) for x in failed_ids)))

        except KeyboardInterrupt:
            pass

    def action_whoami(self, args):
        """
        Simply print out the current user as defined in copr config.
        """
        print(self.client.username)

    @requires_api_auth
    def action_build(self, args):
        """ Method called when the 'build' action has been selected by the
        user.

        :param args: argparse arguments provided by the user
        """
        self.client.authentication_check()

        bar = None
        progress_callback = None
        builds = []

        for pkg in args.pkgs:
            if os.path.exists(pkg):
                bar = ProgressBar(max=os.path.getsize(pkg))

                # pylint: disable=function-redefined
                def progress_callback(monitor):
                    bar.next(n=8192)

                print('Uploading package {0}'.format(pkg))

            data = {
                "pkgs": [pkg],
                "progress_callback": progress_callback,
            }

            builds.append(self.process_build(args, self.client.create_new_build, data, bar=bar))

        return builds

    @requires_api_auth
    def action_build_pypi(self, args):
        """
        Method called when the 'buildpypi' action has been selected by the user.

        :param args: argparse arguments provided by the user
        """
        username, copr = parse_name(args.copr)

        data = {
            "pypi_package_name": args.packagename,
            "pypi_package_version": args.packageversion,
            "python_versions": args.pythonversions,
        }
        return self.process_build(args, self.client.create_new_build_pypi, data)

    @requires_api_auth
    def action_build_tito(self, args):
        """
        Method called when the 'buildtito' action has been selected by the user.

        :param args: argparse arguments provided by the user
        """
        data = {
            "git_url": args.git_url,
            "git_dir": args.git_dir,
            "git_branch": args.git_branch,
            "tito_test": args.tito_test,
        }
        return self.process_build(args, self.client.create_new_build_tito, data)

    @requires_api_auth
    def action_build_mock(self, args):
        """
        Method called when the 'build-mock' action has been selected by the user.

        :param args: argparse arguments provided by the user
        """
        data = {
            "scm_type": args.scm_type,
            "scm_url": args.scm_url,
            "scm_branch": args.scm_branch,
            "spec": args.spec,
        }
        return self.process_build(args, self.client.create_new_build_mock, data)

    @requires_api_auth
    def action_build_rubygems(self, args):
        """
        Method called when the 'buildgem' action has been selected by the user.

        :param args: argparse arguments provided by the user
        """
        data = {"gem_name": args.gem_name}
        return self.process_build(args, self.client.create_new_build_rubygems, data)

    @requires_api_auth
    def action_build_distgit(self, args):
        """
        Method called when the 'buildfedpkg' action has been selected by the user.

        :param args: argparse arguments provided by the user
        """
        data = {"clone_url": args.clone_url, "branch": args.branch}
        return self.process_build(args, self.client.create_new_build_distgit, data)

    def process_build(self, args, build_function, data, bar=None):
        username, copr = parse_name(args.copr)

        result = build_function(username=username, projectname=copr, chroots=args.chroots, memory=args.memory,
                                timeout=args.timeout, background=args.background, **data)
        if bar:
            bar.finish()

        if result.output != "ok":
            sys.stderr.write(result.error + "\n")
            return
        print(result.message)

        build_ids = [bw.build_id for bw in result.builds_list]
        print("Created builds: {0}".format(" ".join(map(str, build_ids))))

        if not args.nowait:
            self._watch_builds(build_ids)


    @requires_api_auth
    def action_create(self, args):
        """ Method called when the 'create' action has been selected by the
        user.

        :param args: argparse arguments provided by the user

        """
        username, copr = parse_name(args.name)
        result = self.client.create_project(
            username=username, projectname=copr, description=args.description,
            instructions=args.instructions, chroots=args.chroots,
            repos=args.repos, initial_pkgs=args.initial_pkgs,
            disable_createrepo=args.disable_createrepo,
            unlisted_on_hp=ON_OFF_MAP[args.unlisted_on_hp],
            enable_net=ON_OFF_MAP[args.enable_net],
            persistent=args.persistent,
            auto_prune=ON_OFF_MAP[args.auto_prune]
        )
        print(result.message)

    @requires_api_auth
    def action_modify_project(self, args):
        """ Method called when the 'modify' action has been selected by the
        user.

        :param args: argparse arguments provided by the user
        """
        username, copr = parse_name(args.name)
        result = self.client.modify_project(
            username=username, projectname=copr,
            description=args.description, instructions=args.instructions,
            repos=args.repos, disable_createrepo=args.disable_createrepo,
            unlisted_on_hp=ON_OFF_MAP[args.unlisted_on_hp],
            enable_net=ON_OFF_MAP[args.enable_net],
            auto_prune=ON_OFF_MAP[args.auto_prune],
            chroots=args.chroots,
        )

    @requires_api_auth
    def action_delete(self, args):
        """ Method called when the 'delete' action has been selected by the
        user.

        :param args: argparse arguments provided by the user
        """
        username, copr = parse_name(args.copr)
        result = self.client.delete_project(username=username, projectname=copr)
        print(result.message)

    @requires_api_auth
    def action_fork(self, args):
        """ Method called when the 'fork' action has been selected by the
        user.

        :param args: argparse arguments provided by the user
        """
        username, copr = parse_name(args.dst)
        result = self.client.fork_project(source=args.src, username=username, projectname=copr, confirm=args.confirm)
        print(result.message)


    def action_mock_config(self, args):
        """ Method called when the 'list' action has been selected by the
        user.

        :param args: argparse arguments provided by the user

        """
        username = self.client.username
        project = args.project.split("/")
        if len(project) != 2:
            args.project = username + "/" + args.project

        result = self.client.get_build_config(args.project, args.chroot)
        if result.output != "ok":
            sys.stderr.write(result.error + "\n")
            sys.stderr.write("Un-expected data returned, please report this issue\n")

        print(MockProfile(result.build_config))


    @check_username_presence
    def action_list(self, args):
        """ Method called when the 'list' action has been selected by the
        user.

        :param args: argparse arguments provided by the user

        """
        username = args.username or self.client.username
        result = self.client.get_projects_list(username)
        # import ipdb; ipdb.set_trace()
        if result.output != "ok":
            sys.stderr.write(result.error + "\n")
            sys.stderr.write("Un-expected data returned, please report this issue\n")
        elif not result.projects_list:
            sys.stderr.write("No copr retrieved for user: '{0}'\n".format(username))
            return

        for prj in result.projects_list:
            print(prj)

    def action_status(self, args):
        result = self.client.get_build_details(args.build_id)
        print(result.status)

    def action_download_build(self, args):
        result = self.client.get_build_details(args.build_id)
        base_len = len(os.path.split(result.results))

        for chroot, url in result.results_by_chroot.items():
            if args.chroots and chroot not in args.chroots:
                continue

            cmd = "wget -r -nH --no-parent --reject 'index.html*'".split(' ')
            cmd.extend(['-P', os.path.join(args.dest, chroot)])
            cmd.extend(['--cut-dirs', str(base_len + 4)])
            cmd.append(url)
            subprocess.call(cmd)

    @requires_api_auth
    def action_cancel(self, args):
        """ Method called when the 'cancel' action has been selected by the
        user.
        :param args: argparse arguments provided by the user
        """
        result = self.client.cancel_build(args.build_id)
        print(result.status)

    def action_watch_build(self, args):
        self._watch_builds(args.build_id)

    def action_delete_build(self, args):
        result = self.client.delete_build(args.build_id)
        print(result.status)

    #########################################################
    ###                   Chroot actions                  ###
    #########################################################

    @requires_api_auth
    def action_edit_chroot(self, args):
        """ Method called when the 'edit-chroot' action has been selected by the
        user.

        :param args: argparse arguments provided by the user
        """
        owner, copr, chroot = parse_chroot_path(args.coprchroot)
        result = self.client.edit_chroot(
            ownername=owner, projectname=copr, chrootname=chroot,
            upload_comps=args.upload_comps, delete_comps=args.delete_comps,
            packages=args.packages, repos=args.repos
        )
        print(result.message)

    def action_get_chroot(self, args):
        """ Method called when the 'get-chroot' action has been selected by the
        user.

        :param args: argparse arguments provided by the user
        """
        owner, copr, chroot = parse_chroot_path(args.coprchroot)
        result = self.client.get_chroot(
            ownername=owner, projectname=copr, chrootname=chroot
        )
        print(simplejson.dumps(result.chroot, indent=4, sort_keys=True, for_json=True))

    #########################################################
    ###                   Package actions                 ###
    #########################################################

    @requires_api_auth
    def action_add_or_edit_package_tito(self, args):
        ownername, projectname = parse_name(args.copr)
        data = {
            "package_name": args.name,
            "git_url": args.git_url,
            "git_dir": args.git_dir,
            "git_branch": args.git_branch,
            "tito_test": ON_OFF_MAP[args.tito_test],
            "webhook_rebuild": ON_OFF_MAP[args.webhook_rebuild],
        }
        if args.create:
            result = self.client.add_package_tito(ownername=ownername, projectname=projectname, **data)
        else:
            result = self.client.edit_package_tito(ownername=ownername, projectname=projectname, **data)
        print(result.message)

    @requires_api_auth
    def action_add_or_edit_package_pypi(self, args):
        ownername, projectname = parse_name(args.copr)
        data = {
            "package_name": args.name,
            "pypi_package_name": args.packagename,
            "pypi_package_version": args.packageversion,
            "python_versions": args.pythonversions,
            "webhook_rebuild": ON_OFF_MAP[args.webhook_rebuild],
        }
        if args.create:
            result = self.client.add_package_pypi(ownername=ownername, projectname=projectname, **data)
        else:
            result = self.client.edit_package_pypi(ownername=ownername, projectname=projectname, **data)
        print(result.message)

    @requires_api_auth
    def action_add_or_edit_package_mockscm(self, args):
        ownername, projectname = parse_name(args.copr)
        data = {
            "package_name": args.name,
            "scm_type": args.scm_type,
            "scm_url": args.scm_url,
            "scm_branch": args.scm_branch,
            "spec": args.spec,
            "webhook_rebuild": ON_OFF_MAP[args.webhook_rebuild],
        }
        if args.create:
            result = self.client.add_package_mockscm(ownername=ownername, projectname=projectname, **data)
        else:
            result = self.client.edit_package_mockscm(ownername=ownername, projectname=projectname, **data)
        print(result.message)

    @requires_api_auth
    def action_add_or_edit_package_rubygems(self, args):
        ownername, projectname = parse_name(args.copr)
        data = {
            "package_name": args.name,
            "gem_name": args.gem_name,
            "webhook_rebuild": ON_OFF_MAP[args.webhook_rebuild],
        }
        if args.create:
            result = self.client.add_package_rubygems(ownername=ownername, projectname=projectname, **data)
        else:
            result = self.client.edit_package_rubygems(ownername=ownername, projectname=projectname, **data)
        print(result.message)

    def action_list_packages(self, args):
        ownername, projectname = parse_name(args.copr)
        data = {
            "with_latest_build": args.with_latest_build,
            "with_latest_succeeded_build": args.with_latest_succeeded_build,
            "with_all_builds": args.with_all_builds,
        }
        result = self.client.get_packages_list(ownername=ownername, projectname=projectname, **data)
        print(simplejson.dumps(result.packages_list, indent=4, sort_keys=True, for_json=True))

    def action_list_package_names(self, args):
        ownername, projectname = parse_name(args.copr)
        result = self.client.get_packages_list(ownername=ownername, projectname=projectname)
        for package in result.packages_list:
            print(package.name)

    def action_get_package(self, args):
        ownername, projectname = parse_name(args.copr)
        data = {
            "pkg_name": args.name,
            "with_latest_build": args.with_latest_build,
            "with_latest_succeeded_build": args.with_latest_succeeded_build,
            "with_all_builds": args.with_all_builds,
        }
        result = self.client.get_package(ownername=ownername, projectname=projectname, **data)
        print(simplejson.dumps(result.package, indent=4, sort_keys=True, for_json=True))

    def action_delete_package(self, args):
        ownername, projectname = parse_name(args.copr)
        data = { "pkg_name": args.name }
        result = self.client.delete_package(ownername=ownername, projectname=projectname, **data)
        print(result.message)

    def action_reset_package(self, args):
        ownername, projectname = parse_name(args.copr)
        data = { "pkg_name": args.name }
        result = self.client.reset_package(ownername=ownername, projectname=projectname, **data)
        print(result.message)

    def action_build_package(self, args):
        ownername, projectname = parse_name(args.copr)
        data = {
            "pkg_name": args.name,
            "chroots": args.chroots,
            #"memory": args.memory,
            "timeout": args.timeout
        }

        result = self.client.build_package(ownername=ownername, projectname=projectname, **data)

        if result.output != "ok":
            sys.stderr.write(result.error + "\n")
            return
        print(result.message)

        build_ids = [bw.build_id for bw in result.builds_list]
        print("Created builds: {0}".format(" ".join(map(str, build_ids))))

        if not args.nowait:
            self._watch_builds(build_ids)

    def action_build_module(self, args):
        """
        Build module via Copr MBS
        """
        ownername, projectname = parse_name(args.copr or "")
        modulemd = open(args.yaml, "rb") if args.yaml else args.url
        response = self.client.build_module(modulemd, ownername, projectname)
        print(response.message if response.output == "ok" else response.error)

def version():
    if 'BUILD_MANPAGES_RUNNING' in os.environ:
        # pkg_resources magic doesn't work during 'setup.py build', but it
        # doesn't matter as the version in the manual page is read directly from
        # setuptools.
        return 'fake'
    return pkg_resources.require('copr-cli')[0].version


def setup_parser():
    """
    Set the main arguments.
    """
    #########################################################
    ###                    General options                ###
    #########################################################

    parser = argparse.ArgumentParser(prog="copr", epilog="dummy text")
    # General connection options

    parser.add_argument("--debug", dest="debug", action="store_true",
                        help="Enable debug output")

    parser.add_argument("--config", dest="config",
                        help="Path to an alternative configuration file")

    parser.add_argument("--version", action="version",
                        version="%(prog)s version " + version())

    subparsers = parser.add_subparsers(title="actions")

    #########################################################
    ###                    Project options                ###
    #########################################################

    # TEST: add a subcommand that should not be rendered in the output
    parser_whoami = subparsers.add_parser(
        "hidden",
        help=argparse.SUPPRESS,
    )

    parser_whoami = subparsers.add_parser(
        "whoami",
        help="Print username that the client authenticates with against copr-frontend"
    )
    parser_whoami.set_defaults(func="action_whoami")

    # create the parser for the "list" command
    parser_list = subparsers.add_parser(
        "list",
        help="List all the copr of the "
             "provided "
    )
    parser_list.add_argument(
        "username", metavar="username|@groupname", nargs="?",
        help="The username or @groupname that you would like to "
             "list the coprs of (defaults to current user)"
    )
    parser_list.set_defaults(func="action_list")

    parser_mock_config = subparsers.add_parser(
        "mock-config",
        help="Get the mock profile (similar to koji mock-config)"
    )
    parser_mock_config.add_argument(
        "project",
        help="Expected format is <user>/<project>, <group>/<project> (including '@') or <project> (name of project you own)."
    )
    parser_mock_config.add_argument(
        "chroot",
        help="chroot id, e.g. 'fedora-rawhide-x86_64'"
    )
    parser_mock_config.set_defaults(func="action_mock_config")

    # create the parser for the "create" command
    parser_create = subparsers.add_parser("create", help="Create a new copr")
    parser_create.add_argument("name",
                               help="The name of the copr to create")
    parser_create.add_argument("--chroot", dest="chroots", action="append",
                               help="Chroot to use for this copr")
    parser_create.add_argument("--repo", dest="repos", action="append",
                               help="Repository to add to this copr")
    parser_create.add_argument("--initial-pkgs", dest="initial_pkgs",
                               action="append",
                               help="List of packages URL to build in this "
                                    "new copr")
    parser_create.add_argument("--description",
                               help="Description of the copr")
    parser_create.add_argument("--instructions",
                               help="Instructions for the copr")
    parser_create.add_argument("--disable_createrepo",
                               help="Disable metadata auto generation")
    parser_create.add_argument("--enable-net", choices=["on", "off"], default="off",
                               help="If net should be enabled for builds in this project (default is off)")
    parser_create.add_argument("--unlisted-on-hp", choices=["on", "off"],
                               help="The project will not be shown on COPR home page")
    parser_create.add_argument("--persistent", action="store_true",
                               help="Project and its builds will be undeletable. This option can only be specified by a COPR admin.")
    parser_create.add_argument("--auto-prune", choices=["on", "off"], default="on",
                               help="If auto-deletion of project's obsoleted builds should be enabled (default is on).\
                               This option can only be specified by a COPR admin.")
    parser_create.set_defaults(func="action_create")

    # create the parser for the "modify_project" command
    parser_modify = subparsers.add_parser("modify", help="Modify existing copr")

    parser_modify.add_argument("name", help="The name of the copr to modify")
    parser_modify.add_argument("--chroot", dest="chroots", action="append",
                               help="Chroot to use for this copr")
    parser_modify.add_argument("--description",
                               help="Description of the copr")
    parser_modify.add_argument("--instructions",
                               help="Instructions for the copr")
    parser_modify.add_argument("--repo", dest="repos", action="append",
                               help="Repository to add to this copr")
    parser_modify.add_argument("--disable_createrepo",
                               help="Disable metadata auto generation")
    parser_modify.add_argument("--enable-net", choices=["on", "off"],
                               help="If net should be enabled for builds in this project (default is \"don't change\")")
    parser_modify.add_argument("--unlisted-on-hp", choices=["on", "off"],
                               help="The project will not be shown on COPR home page")
    parser_modify.add_argument("--auto-prune", choices=["on", "off"],
                               help="If auto-deletion of project's obsoleted builds should be enabled.\
                               This option can only be specified by a COPR admin.")
    parser_modify.set_defaults(func="action_modify_project")

    # create the parser for the "delete" command
    parser_delete = subparsers.add_parser("delete", help="Deletes the entire project")
    parser_delete.add_argument("copr", help="Name of your project to be deleted.")
    parser_delete.set_defaults(func="action_delete")

    # create the parser for the "fork" command
    parser_delete = subparsers.add_parser("fork", help="Fork the project and builds in it")
    parser_delete.add_argument("src", help="Which project should be forked")
    parser_delete.add_argument("dst", help="Name of the new project")
    parser_delete.add_argument("--confirm", action="store_true", help="Confirm forking into existing project")
    parser_delete.set_defaults(func="action_fork")

    #########################################################
    ###             Source-type related options           ###
    #########################################################

    parser_tito_args_parent = argparse.ArgumentParser(add_help=False)
    parser_tito_args_parent.add_argument("--git-url", metavar="URL", dest="git_url", required=True,
                                         help="URL to a project managed by Tito")
    parser_tito_args_parent.add_argument("--git-dir", metavar="DIRECTORY", dest="git_dir",
                                         help="Relative path from Git root to directory containing .spec file")
    parser_tito_args_parent.add_argument("--git-branch", metavar="BRANCH", dest="git_branch",
                                         help="Git branch that you want to build from")
    parser_tito_args_parent.add_argument("--test", dest="tito_test", choices=["on", "off"],
                                         help="Build the last commit instead of the last release tag")

    parser_pypi_args_parent = argparse.ArgumentParser(add_help=False)
    parser_pypi_args_parent.add_argument("--pythonversions", nargs="*", type=int, metavar="VERSION", default=[3, 2],
                                         help="For what Python versions to build (by default: 3 2)")
    parser_pypi_args_parent.add_argument("--packageversion", metavar = "PYPIVERSION",
                                         help="Version of the PyPI package to be built (by default latest)")
    parser_pypi_args_parent.add_argument("--packagename", required=True, metavar="PYPINAME",
                                         help="Name of the PyPI package to be built, required.")

    parser_mockscm_args_parent = argparse.ArgumentParser(add_help=False)
    parser_mockscm_args_parent.add_argument("--scm-type", metavar="TYPE", dest="scm_type", choices=["git", "svn"], default="git",
                                            help="specify versioning tool, default is 'git'")
    parser_mockscm_args_parent.add_argument("--scm-url", metavar="URL", dest="scm_url",
                                            help="url to a project versioned by Git or SVN, required")
    parser_mockscm_args_parent.add_argument("--scm-branch", metavar="BRANCH", dest="scm_branch", help="")
    parser_mockscm_args_parent.add_argument("--spec", dest="spec", metavar="FILE",
                                            help="relative path from SCM root to .spec file, required")

    parser_rubygems_args_parent = argparse.ArgumentParser(add_help=False)
    parser_rubygems_args_parent.add_argument("--gem", metavar="GEM", dest="gem_name",
                                             help="Specify gem name")

    parser_distgit_args_parent = argparse.ArgumentParser(add_help=False)
    parser_distgit_args_parent.add_argument("--clone-url", metavar="URL", dest="clone_url", required=True,
                                             help="Specify clone url for the distgit repository")
    parser_distgit_args_parent.add_argument("--branch", metavar="BRANCH", dest="branch",
                                             help="Specify branch to be used")

    #########################################################
    ###                    Build options                  ###
    #########################################################

    # parent parser for the builds commands below
    parser_build_parent = argparse.ArgumentParser(add_help=False)
    parser_build_parent.add_argument("copr",
                                     help="The copr repo to build the package in. Can be just name of project or even in format username/project or @groupname/project.")
    parser_build_parent.add_argument("--memory", dest="memory",
                                     help="")
    parser_build_parent.add_argument("--timeout", dest="timeout",
                                     help="")
    parser_build_parent.add_argument("--nowait", action="store_true", default=False,
                                     help="Don't wait for build")
    parser_build_parent.add_argument("-r", "--chroot", dest="chroots", action="append",
                                     help="If you don't need this build for all the project's chroots. You can use it several times for each chroot you need.")
    parser_build_parent.add_argument("--background", dest="background", action="store_true", default=False,
                                     help="Mark the build as a background job. It will have lesser priority than regular builds.")

    # create the parser for the "build" (url/upload) command
    parser_build = subparsers.add_parser("build", parents=[parser_build_parent],
                                         help="Build packages to a specified copr")
    parser_build.add_argument("pkgs", nargs="+",
                              help="filename of SRPM or URL of packages to build")
    parser_build.set_defaults(func="action_build")

    # create the parser for the "buildpypi" command
    parser_build_pypi = subparsers.add_parser("buildpypi", parents=[parser_pypi_args_parent, parser_build_parent],
                                              help="Build PyPI package to a specified copr")
    parser_build_pypi.set_defaults(func="action_build_pypi")

    # create the parser for the "buildgem" command
    parser_build_rubygems = subparsers.add_parser("buildgem", parents=[parser_rubygems_args_parent, parser_build_parent],
                                                  help="Build gem from rubygems.org to a specified copr")
    parser_build_rubygems.set_defaults(func="action_build_rubygems")

    # create the parser for the "buildfedpkg" command
    parser_build_distgit = subparsers.add_parser("buildfedpkg", parents=[parser_distgit_args_parent, parser_build_parent],
                                                  help="Build package from pkgs.fedoraproject.org")
    parser_build_distgit.set_defaults(func="action_build_distgit")

    # create the parser for the "buildtito" command
    parser_build_tito = subparsers.add_parser("buildtito", parents=[parser_tito_args_parent, parser_build_parent],
                                              help="submit a build from Git repository via Tito to a specified copr")
    parser_build_tito.set_defaults(func="action_build_tito")

    # create the parser for the "buildmock" command
    parser_build_mock = subparsers.add_parser("buildmock", parents=[parser_mockscm_args_parent, parser_build_parent],
                                              help="submit a build from SCM repository via Mock to a specified copr")
    parser_build_mock.set_defaults(func="action_build_mock")

    # create the parser for the "status" command
    parser_status = subparsers.add_parser("status", help="Get build status of build specified by its ID")
    parser_status.add_argument("build_id", help="Build ID", type=int)
    parser_status.set_defaults(func="action_status")

    # create the parser for the "download-build" command
    parser_download_build = subparsers.add_parser("download-build", help="Fetches built packages")
    parser_download_build.add_argument("build_id", help="Build ID")
    parser_download_build.add_argument("-r", "--chroot", dest="chroots", action="append",
                                       help="Select chroots to fetch")
    parser_download_build.add_argument("--dest", "-d", dest="dest",
                                       help="Base directory to store packages", default=".")
    parser_download_build.set_defaults(func="action_download_build")

    # create the parser for the "cancel" command
    parser_cancel = subparsers.add_parser("cancel", help="Cancel build specified by its ID")
    parser_cancel.add_argument("build_id", help="Build ID")
    parser_cancel.set_defaults(func="action_cancel")

    # create the parser for the "watch-build" command
    parser_watch = subparsers.add_parser("watch-build",
                                         help="Watch status and progress of build(s)"
                                              " specified by their ID")
    parser_watch.add_argument("build_id", nargs="+",
                              help="Build ID", type=int)
    parser_watch.set_defaults(func="action_watch_build")

    # create the parser for the "delete-build" command
    parser_delete = subparsers.add_parser("delete-build",
                                         help="Delete build specified by its ID")
    parser_delete.add_argument("build_id", help="Build ID", type=int)
    parser_delete.set_defaults(func="action_delete_build")


    #########################################################
    ###                   Chroot options                  ###
    #########################################################

    parser_edit_chroot = subparsers.add_parser("edit-chroot", help="Edit chroot of a project")
    parser_edit_chroot.add_argument("coprchroot", help="Path to a project chroot as owner/project/chroot or project/chroot")
    parser_edit_chroot_comps_group = parser_edit_chroot.add_mutually_exclusive_group()
    parser_edit_chroot_comps_group.add_argument("--upload-comps", metavar="FILEPATH",
                                                  help="filepath to the comps.xml file to be uploaded")
    parser_edit_chroot_comps_group.add_argument("--delete-comps", action="store_true",
                                                  help="deletes already existing comps.xml for the chroot")

    parser_edit_chroot.add_argument("--packages",
                                      help="space separated string of package names to be added to buildroot")
    parser_edit_chroot.add_argument("--repos",
                                      help="space separated string of additional repo urls for chroot")
    parser_edit_chroot.set_defaults(func="action_edit_chroot")

    parser_get_chroot = subparsers.add_parser("get-chroot", help="Get chroot of a project")
    parser_get_chroot.add_argument("coprchroot", help="Path to a project chroot as owner/project/chroot or project/chroot")
    parser_get_chroot.set_defaults(func="action_get_chroot")

    #########################################################
    ###                   Package options                 ###
    #########################################################

    # package edit/create parent
    parser_add_or_edit_package_parent = argparse.ArgumentParser(add_help=False)
    parser_add_or_edit_package_parent.add_argument("--name",
                                                   help="Name of the package to be edited or created",
                                                   metavar="PKGNAME", required=True)
    parser_add_or_edit_package_parent.add_argument("copr",
                                                   help="The copr repo for the package. Can be just name of project or even in format username/project or @groupname/project.")
    parser_add_or_edit_package_parent.add_argument("--webhook-rebuild",
                                                   choices=["on", "off"], help="Enable auto-rebuilding.")

    # Tito edit/create
    parser_add_package_tito = subparsers.add_parser("add-package-tito",
                                                    help="Creates a new Tito package",
                                                    parents=[parser_tito_args_parent, parser_add_or_edit_package_parent])
    parser_add_package_tito.set_defaults(func="action_add_or_edit_package_tito", create=True)

    parser_edit_package_tito = subparsers.add_parser("edit-package-tito",
                                                     help="Edits an existing Tito package",
                                                     parents=[parser_tito_args_parent, parser_add_or_edit_package_parent])
    parser_edit_package_tito.set_defaults(func="action_add_or_edit_package_tito", create=False)

    # PyPI edit/create
    parser_add_package_pypi = subparsers.add_parser("add-package-pypi",
                                                    help="Creates a new PyPI package",
                                                    parents=[parser_pypi_args_parent, parser_add_or_edit_package_parent])
    parser_add_package_pypi.set_defaults(func="action_add_or_edit_package_pypi", create=True)

    parser_edit_package_pypi = subparsers.add_parser("edit-package-pypi",
                                                     help="Edits an existing PyPI package",
                                                     parents=[parser_pypi_args_parent, parser_add_or_edit_package_parent])
    parser_edit_package_pypi.set_defaults(func="action_add_or_edit_package_pypi", create=False)

    # MockSCM edit/create
    parser_add_package_mockscm = subparsers.add_parser("add-package-mockscm",
                                                       help="Creates a new Mock-SCM package",
                                                       parents=[parser_mockscm_args_parent, parser_add_or_edit_package_parent])
    parser_add_package_mockscm.set_defaults(func="action_add_or_edit_package_mockscm", create=True)

    parser_edit_package_mockscm = subparsers.add_parser("edit-package-mockscm",
                                                        help="Edits an existing Mock-SCM package",
                                                        parents=[parser_mockscm_args_parent, parser_add_or_edit_package_parent])
    parser_edit_package_mockscm.set_defaults(func="action_add_or_edit_package_mockscm", create=False)

    # Rubygems edit/create
    parser_add_package_rubygems = subparsers.add_parser("add-package-rubygems",
                                                        help="Creates a new RubyGems package",
                                                        parents=[parser_rubygems_args_parent, parser_add_or_edit_package_parent])
    parser_add_package_rubygems.set_defaults(func="action_add_or_edit_package_rubygems", create=True)

    parser_edit_package_rubygems = subparsers.add_parser("edit-package-rubygems",
                                                         help="Edits a new RubyGems package",
                                                         parents=[parser_rubygems_args_parent, parser_add_or_edit_package_parent])
    parser_edit_package_rubygems.set_defaults(func="action_add_or_edit_package_rubygems", create=False)

    # package listing
    parser_list_packages = subparsers.add_parser("list-packages",
                                                 help="Returns list of packages in the given copr")
    parser_list_packages.add_argument("copr",
                                      help="The copr repo to list the packages of. Can be just name of project or even in format owner/project.")
    parser_list_packages.add_argument("--with-latest-build", action="store_true",
                                      help="Also display data related to the latest build for the package.")
    parser_list_packages.add_argument("--with-latest-succeeded-build", action="store_true",
                                      help="Also display data related to the latest succeeded build for the package.")
    parser_list_packages.add_argument("--with-all-builds", action="store_true",
                                      help="Also display data related to the builds for the package.")
    parser_list_packages.set_defaults(func="action_list_packages")

    # package names listing
    parser_list_package_names = subparsers.add_parser("list-package-names",
                                                      help="Returns list of package names in the given copr")
    parser_list_package_names.add_argument("copr",
                                           help="The copr repo to list the packages of. Can be just name of project or even in format owner/project.")
    parser_list_package_names.set_defaults(func="action_list_package_names")

    # single package fetching
    parser_get_package = subparsers.add_parser("get-package",
                                               help="Returns package of the given name in the given copr")
    parser_get_package.add_argument("copr",
                                    help="The copr repo to list the packages of. Can be just name of project or even in format owner/project.")
    parser_get_package.add_argument("--name",
                                    help="Name of a single package to be displayed",
                                    metavar="PKGNAME", required=True)
    parser_get_package.add_argument("--with-latest-build", action="store_true",
                                    help="Also display data related to the latest build for each package.")
    parser_get_package.add_argument("--with-latest-succeeded-build", action="store_true",
                                    help="Also display data related to the latest succeeded build for each package.")
    parser_get_package.add_argument("--with-all-builds", action="store_true",
                                    help="Also display data related to the builds for each package.")
    parser_get_package.set_defaults(func="action_get_package")

    # package deletion
    parser_delete_package = subparsers.add_parser("delete-package",
                                                  help="Deletes the specified package")
    parser_delete_package.add_argument("copr",
                                       help="The copr repo to list the packages of. Can be just name of project or even in format owner/project.")
    parser_delete_package.add_argument("--name",
                                       help="Name of a package to be deleted",
                                       metavar="PKGNAME", required=True)
    parser_delete_package.set_defaults(func="action_delete_package")

    # package reseting
    parser_reset_package = subparsers.add_parser("reset-package",
                                                 help="Resets (clears) default source of the specified package")
    parser_reset_package.add_argument("copr",
                                      help="The copr repo to list the packages of. Can be just name of project or even in format owner/project.")
    parser_reset_package.add_argument("--name",
                                      help="Name of a package to be reseted",
                                      metavar="PKGNAME", required=True)
    parser_reset_package.set_defaults(func="action_reset_package")

    # package building
    parser_build_package = subparsers.add_parser("build-package", parents=[parser_build_parent],
                                                 help="Builds the package from its default source")
    parser_build_package.add_argument("--name",
                                      help="Name of a package to be built",
                                      metavar="PKGNAME", required=True)
    parser_build_package.set_defaults(func="action_build_package")

    # module building
    parser_build_module = subparsers.add_parser("build-module", help="Builds a given module in Copr")
    parser_build_module.add_argument("copr", help="The copr repo to list the packages of. Can be just name of project or even in format owner/project.", nargs="?")
    parser_build_module_mmd_source = parser_build_module.add_mutually_exclusive_group(required=True)
    parser_build_module_mmd_source.add_argument("--url", help="SCM with modulemd file in yaml format")
    parser_build_module_mmd_source.add_argument("--yaml", help="Path to modulemd file in yaml format")
    parser_build_module.set_defaults(func="action_build_module")

    return parser


def parse_name(name):
    m = re.match(r"([^/]+)/(.*)", name)
    if m:
        owner = m.group(1)
        name = m.group(2)
    else:
        owner = None
    return owner, name

def parse_chroot_path(path):
    m = re.match(r"(([^/]+)/)?([^/]+)/(.*)", path)
    if m:
        return m.group(2), m.group(3), m.group(4)
    return None


def enable_debug():
    logging.basicConfig(
        level=logging.DEBUG,
        format='[%(asctime)s] {%(pathname)s:%(lineno)d} %(levelname)s - %(message)s',
        datefmt='%H:%M:%S'
    )
    log.debug("#  Debug log enabled  #")


def main(argv=sys.argv[1:]):
    try:
        # Set up parser for global args
        parser = setup_parser()
        # Parse the commandline
        arg = parser.parse_args(argv)
        if arg.debug:
            enable_debug()

        if not "func" in arg:
            parser.print_help()
            return

        commands = Commands(arg.config)
        getattr(commands, arg.func)(arg)

    except KeyboardInterrupt:
        sys.stderr.write("\nInterrupted by user.")
        sys.exit(1)
    except copr_exceptions.CoprBuildException as e:
        sys.stderr.write("\nBuild error: {0}\n".format(e))
        sys.exit(4)
    except copr_exceptions.CoprUnknownResponseException as e:
        sys.stderr.write("\nError: {0}\n".format(e))
        sys.exit(5)
    except copr_exceptions.CoprRequestException as e:
        sys.stderr.write("\nSomething went wrong:")
        sys.stderr.write("\nError: {0}\n".format(e))
        sys.exit(1)
    except argparse.ArgumentTypeError as e:
        sys.stderr.write("\nError: {0}".format(e))
        sys.exit(2)
    except copr_exceptions.CoprException as e:
        sys.stderr.write("\nError: {0}\n".format(e))
        sys.exit(3)

        # except Exception as e:
        # print "Error: {0}".format(e)
        # sys.exit(100)


if __name__ == "__main__":
    main()
