#!/usr/bin/env python3
#
# Copyright (C) 2006, 2014 Red Hat, Inc.
# Copyright (C) 2006 Daniel P. Berrange <berrange@redhat.com>
#
# This work is licensed under the GNU GPLv2 or later.
# See the COPYING file in the top-level directory.

import argparse
import os
import signal
import sys
import traceback

import gi
gi.require_version('LibvirtGLib', '1.0')
from gi.repository import LibvirtGLib

from virtinst import BuildConfig
from virtinst import VirtinstConnection
from virtinst import cli
from virtinst import log

# This is massively heavy handed, but I can't figure out any way to shut
# up the slew of gtk deprecation warnings that clog up our very useful
# stdout --debug output. Of course we could drop use of deprecated APIs,
# but it's a serious quantity of churn
import warnings  # pylint: disable=wrong-import-order
warnings.simplefilter("ignore")

try:
    gi.check_version("3.22.0")
except (ValueError, AttributeError):
    print("pygobject3 3.22.0 or later is required.")
    sys.exit(1)


def _show_startup_error(msg, details):
    log.debug("Error starting virt-manager: %s\n%s", msg, details,
                  exc_info=True)
    from .error import vmmErrorDialog
    err = vmmErrorDialog.get_instance()
    title = _("Error starting Virtual Machine Manager")
    err.show_err(title + ": " + msg,
                 details=details,
                 title=title,
                 modal=True,
                 debug=False)


def _import_gtk(leftovers):
    # The never ending fork+gsettings problems now require us to
    # import Gtk _after_ the fork. This creates a funny race, since we
    # need to parse the command line arguments to know if we need to
    # fork, but need to import Gtk before cli processing so it can
    # handle --g-fatal-args. We strip out our flags first and pass the
    # left overs to gtk
    origargv = sys.argv
    try:
        sys.argv = origargv[:1] + leftovers[:]
        gi.require_version('Gtk', '3.0')
        from gi.repository import Gtk
        leftovers = sys.argv[1:]

        if Gtk.check_version(3, 22, 0):
            print("gtk3 3.22.0 or later is required.")
            sys.exit(1)

        # This will error if Gtk wasn't correctly initialized
        Gtk.init()
        globals()["Gtk"] = Gtk

        # This ensures we can init gsettings correctly
        from . import config
        ignore = config
    except Exception as e:
        # Don't just let the exception raise here. abrt reports bugs
        # when users mess up su/sudo and DISPLAY isn't set. Printing
        # it avoids the issue
        display = os.environ.get("DISPLAY", "")
        msg = str(e)
        if display:
            msg += ": Could not open display: %s" % display
        log.debug("".join(traceback.format_exc()))
        print(msg)
        sys.exit(1)
    finally:
        sys.argv = origargv

    return leftovers


def _setup_gsettings_path(schemadir):
    """
    If running from the virt-manager.git srcdir, compile our gsettings
    schema and use it directly
    """
    import subprocess
    import shutil

    exe = shutil.which("glib-compile-schemas")
    if not exe:  # pragma: no cover
        raise RuntimeError("You must install glib-compile-schemas to run "
            "virt-manager from git.")
    subprocess.check_call([exe, "--strict", schemadir])


def drop_tty():
    # We fork and setsid so that we drop the controlling
    # tty. This prevents libvirt's SSH tunnels from prompting
    # for user input if SSH keys/agent aren't configured.
    if os.fork() != 0:
        os._exit(0)  # pylint: disable=protected-access

    os.setsid()


def drop_stdio():
    # This is part of the fork process described in drop_tty()
    for fd in range(0, 2):
        try:
            os.close(fd)
        except OSError:
            pass

    os.open(os.devnull, os.O_RDWR)
    os.dup2(0, 1)
    os.dup2(0, 2)


def parse_commandline():
    epilog = ("Also accepts standard GTK arguments like --g-fatal-warnings")
    parser = argparse.ArgumentParser(usage="virt-manager [options]",
                                     epilog=epilog)
    parser.add_argument('--version', action='version',
                        version=BuildConfig.version)
    parser.set_defaults(domain=None)

    # Trace every libvirt API call to debug output
    parser.add_argument("--trace-libvirt", choices=["all", "mainloop"],
        help=argparse.SUPPRESS)

    # Don't load any connections on startup to test first run
    # PackageKit integration
    parser.add_argument("--test-first-run",
        help=argparse.SUPPRESS, action="store_true")
    # Force disable use of libvirt object events
    parser.add_argument("--test-no-events",
        help=argparse.SUPPRESS, action="store_true")
    # Enabling this will tell us, at app exit time, which vmmGObjects were not
    # garbage collected. This is caused by circular references to other objects,
    # like a signal that wasn't disconnected. It's not a big deal, but if we
    # have objects that can be created and destroyed a lot over the course of
    # the app lifecycle, every non-garbage collected class is a memory leak.
    # So it's nice to poke at this every now and then and try to track down
    # what we need to add to class _cleanup handling.
    parser.add_argument("--test-leak-debug",
        help=argparse.SUPPRESS, action="store_true")

    # comma separated string of options to tweak app behavior,
    # for manual and automated testing config
    parser.add_argument("--test-options", help=argparse.SUPPRESS)

    parser.add_argument("-c", "--connect", dest="uri",
        help="Connect to hypervisor at URI", metavar="URI")
    parser.add_argument("--debug", action="store_true",
        help="Print debug output to stdout (implies --no-fork)",
        default=False)
    parser.add_argument("--no-fork", action="store_true",
        help="Don't fork into background on startup")

    parser.add_argument("--show-domain-creator", action="store_true",
        help="Show 'New VM' wizard")
    parser.add_argument("--show-domain-editor", metavar="NAME|ID|UUID",
        help="Show domain details window")
    parser.add_argument("--show-domain-performance", metavar="NAME|ID|UUID",
        help="Show domain performance window")
    parser.add_argument("--show-domain-console", metavar="NAME|ID|UUID",
        help="Show domain graphical console window")
    parser.add_argument("--show-domain-delete", metavar="NAME|ID|UUID",
        help="Show domain delete window")
    parser.add_argument("--show-host-summary", action="store_true",
        help="Show connection details window")

    return parser.parse_known_args()


class CLITestOptionsClass:
    """
    Helper class for parsing and tracking --test-* options
    """
    def __init__(self, test_options_str):
        opts = []
        if test_options_str:
            opts = test_options_str.split(",")

        def _get(optname):
            if optname not in opts:
                return False
            opts.remove(optname)
            return True

        self.first_run = _get("first-run")
        self.leak_debug = _get("leak-debug")
        self.no_events = _get("no-events")
        self.xmleditor_enabled = _get("xmleditor-enabled")

        if opts:
            print("Unknown --test-options keys: %s" % opts)
            sys.exit(1)


def main():
    (options, leftovers) = parse_commandline()

    cli.setupLogging("virt-manager", options.debug, False, False)

    log.debug("virt-manager version: %s", BuildConfig.version)
    log.debug("virtManager import: %s", os.path.dirname(__file__))

    if BuildConfig.running_from_srcdir:
        _setup_gsettings_path(BuildConfig.gsettings_dir)

    if options.trace_libvirt:
        log.debug("Libvirt tracing requested")
        from .lib import module_trace
        import libvirt
        module_trace.wrap_module(libvirt,
                mainloop=(options.trace_libvirt == "mainloop"),
                regex=None)

    CLITestOptions = CLITestOptionsClass(options.test_options)
    if options.test_first_run:
        CLITestOptions.first_run = True
    if options.test_leak_debug:
        CLITestOptions.leak_debug = True
    if options.test_no_events:
        CLITestOptions.no_events = True

    # With F27 gnome+wayland we need to set these before GTK import
    os.environ["GSETTINGS_SCHEMA_DIR"] = BuildConfig.gsettings_dir
    if CLITestOptions.first_run:
        os.environ["GSETTINGS_BACKEND"] = "memory"

    # Now we've got basic environment up & running we can fork
    do_drop_stdio = False
    if not options.no_fork and not options.debug:
        drop_tty()
        do_drop_stdio = True

        # Ignore SIGHUP, otherwise a serial console closing drops the whole app
        signal.signal(signal.SIGHUP, signal.SIG_IGN)

    leftovers = _import_gtk(leftovers)
    Gtk = globals()["Gtk"]

    # Do this after the Gtk import so the user has a chance of seeing any error
    if do_drop_stdio:
        drop_stdio()

    if leftovers:
        raise RuntimeError("Unhandled command line options '%s'" % leftovers)

    log.debug("PyGObject version: %d.%d.%d",
                  gi.version_info[0],
                  gi.version_info[1],
                  gi.version_info[2])
    log.debug("GTK version: %d.%d.%d",
                  Gtk.get_major_version(),
                  Gtk.get_minor_version(),
                  Gtk.get_micro_version())

    if not VirtinstConnection.libvirt_new_enough_for_virtmanager(6000):
        # We need this version for threaded virConnect access
        _show_startup_error(
                _("virt-manager requires libvirt 0.6.0 or later."), "")
        return

    # Prime the vmmConfig cache
    from . import config
    config.vmmConfig.get_instance(BuildConfig, CLITestOptions)

    # Add our icon dir to icon theme
    icon_theme = Gtk.IconTheme.get_default()
    icon_theme.prepend_search_path(BuildConfig.icon_dir)

    from .engine import vmmEngine
    Gtk.Window.set_default_icon_name("virt-manager")

    show_window = None
    domain = None
    if options.show_domain_creator:
        show_window = vmmEngine.CLI_SHOW_DOMAIN_CREATOR
    elif options.show_host_summary:
        show_window = vmmEngine.CLI_SHOW_HOST_SUMMARY
    elif options.show_domain_editor:
        show_window = vmmEngine.CLI_SHOW_DOMAIN_EDITOR
        domain = options.show_domain_editor
    elif options.show_domain_performance:
        show_window = vmmEngine.CLI_SHOW_DOMAIN_PERFORMANCE
        domain = options.show_domain_performance
    elif options.show_domain_console:
        show_window = vmmEngine.CLI_SHOW_DOMAIN_CONSOLE
        domain = options.show_domain_console
    elif options.show_domain_delete:
        show_window = vmmEngine.CLI_SHOW_DOMAIN_DELETE
        domain = options.show_domain_delete

    if show_window and options.uri is None:
        raise RuntimeError("can't use --show-* options without --connect")

    skip_autostart = False
    if show_window:
        skip_autostart = True

    # Hook libvirt events into glib main loop
    LibvirtGLib.init(None)
    LibvirtGLib.event_register()

    engine = vmmEngine.get_instance()

    # Actually exit when we receive ctrl-c
    from gi.repository import GLib
    def _sigint_handler(user_data):
        ignore = user_data
        log.debug("Received KeyboardInterrupt. Exiting application.")
        engine.exit_app()
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT,
                         _sigint_handler, None)

    engine.start(options.uri, show_window, domain, skip_autostart)


def runcli():
    try:
        main()
    except KeyboardInterrupt:
        log.debug("Received KeyboardInterrupt. Exiting application.")
    except Exception as run_e:
        if "Gtk" not in globals():
            raise
        _show_startup_error(str(run_e), "".join(traceback.format_exc()))
