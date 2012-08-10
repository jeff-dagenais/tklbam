#!/usr/bin/python
#
# Copyright (c) 2010-2012 Liraz Siri <liraz@turnkeylinux.org>
#
# This file is part of TKLBAM (TurnKey Linux BAckup and Migration).
#
# TKLBAM is open source software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License as
# published by the Free Software Foundation; either version 3 of
# the License, or (at your option) any later version.
#
"""
Backup the current system

Arguments:
    <override> := -?( /path/to/include/or/exclude | mysql:database[/table] )

    Default overrides read from $CONF_OVERRIDES

Options:
    --resume                  Resume previously interrupted backup session

    --address=TARGET_URL      manual backup target URL
                              default: automatically configured via Hub

    -s --simulate             Simulate operation. Don't actually backup.
                              Useful for inspecting /TKLBAM by hand.

    -q --quiet                Be less verbose
    --logfile=PATH            Path of file to log to
                              default: $LOGFILE

    --debug                   Run $$SHELL before Duplicity

Configurable options:

    --volsize MB              Size of backup volume in MBs
                              default: $CONF_VOLSIZE

    --s3-parallel-uploads=N   Number of parallel volume chunk uploads
                              default: $CONF_S3_PARALLEL_UPLOADS

    --full-backup FREQUENCY   Time frequency of full backup
                              default: $CONF_FULL_BACKUP

                              format := <int>[DWM]

                                e.g.,
                                3D - three days
                                2W - two weeks
                                1M - one month

    --skip-files              Don't backup filesystem
    --skip-database           Don't backup databases
    --skip-packages           Don't backup new packages

Resolution order for configurable options:

  1) comand line (highest precedence)
  2) configuration file ($CONF_PATH)
  3) built-in default (lowest precedence)

Configuration file format ($CONF_PATH):

  <option-name> <value>

"""

from os.path import *

import sys
import getopt

import datetime
from string import Template

from pidlock import PidLock

import hub
import backup
import hooks
from registry import registry
from conf import Conf

from version import get_turnkey_version
from stdtrap import UnitedStdTrap

from utils import is_writeable

PATH_LOGFILE = "/var/log/tklbam-backup"

def usage(e=None):
    if e:
        print >> sys.stderr, "error: " + str(e)

    print >> sys.stderr, "Syntax: %s [ -options ] [ override ... ]" % sys.argv[0]
    tpl = Template(__doc__.strip())
    conf = Conf()
    print >> sys.stderr, tpl.substitute(CONF_PATH=conf.paths.conf,
                                        CONF_OVERRIDES=conf.paths.overrides,
                                        CONF_VOLSIZE=conf.volsize,
                                        CONF_FULL_BACKUP=conf.full_backup,
                                        CONF_S3_PARALLEL_UPLOADS=conf.s3_parallel_uploads,
                                        LOGFILE=PATH_LOGFILE)
    sys.exit(1)

def warn(e):
    print >> sys.stderr, "warning: " + str(e)

def fatal(e):
    print >> sys.stderr, "error: " + str(e)
    sys.exit(1)

from conffile import ConfFile

class ServerConf(ConfFile):
    CONF_FILE="/var/lib/hubclient/server.conf"

def get_server_id():
    try:
        return ServerConf()['serverid']
    except KeyError:
        return None

def get_profile(hb):
    """Get a new profile if we don't have a profile in the registry or the Hub
    has a newer profile for this appliance. If we can't contact the Hub raise
    an error if we don't already have profile."""

    profile_timestamp = registry.profile.timestamp \
                        if registry.profile else None

    turnkey_version = get_turnkey_version()

    try:
        new_profile = hb.get_new_profile(turnkey_version, profile_timestamp)
        if new_profile:
            registry.profile = new_profile
    except hb.Error, e:
        if not registry.profile:
            raise

        warn("using cached profile because of a Hub error: " + str(e))

    return registry.profile

def main():
    try:
        opts, args = getopt.gnu_getopt(sys.argv[1:], 'qsh',
                                       ['help',
                                        'skip-files', 'skip-database', 'skip-packages',
                                        'debug',
                                        'resume',
                                        'logfile=',
                                        'simulate', 'quiet',
                                        'profile=', 'secretfile=', 'address=',
                                        'volsize=', 's3-parallel-uploads=', 'full-backup='])
    except getopt.GetoptError, e:
        usage(e)

    opt_debug = False
    opt_resume = None
    opt_logfile = PATH_LOGFILE

    conf = Conf()
    conf.secretfile = registry.path.secret

    for opt, val in opts:
        if opt in ('-s', '--simulate'):
            conf.simulate = True

        if opt == '--resume':
            opt_resume = True

        elif opt == '--profile':
            conf.profile = val

        elif opt in ('-q', '--quiet'):
            conf.verbose = False

        elif opt == '--secretfile':
            if not exists(val):
                usage("secretfile %s does not exist" % `val`)
            conf.secretfile = val

        elif opt == '--address':
            conf.address = val

        elif opt == '--volsize':
            conf.volsize = val

        elif opt == '--s3-parallel-uploads':
            conf.s3_parallel_uploads = val

        elif opt == '--full-backup':
            conf.full_backup = val

        elif opt == '--logfile':
            if not is_writeable(val):
                fatal("logfile '%s' is not writeable" % val)
            opt_logfile = val

        elif opt == '--debug':
            opt_debug = True

        elif opt == '--skip-files':
            conf.backup_skip_files = True

        elif opt == '--skip-database':
            conf.backup_skip_database = True

        elif opt == '--skip-packages':
            conf.backup_skip_packages = True

        elif opt in ('-h', '--help'):
            usage()

    conf.overrides += args

    lock = PidLock("/var/run/tklbam-backup.pid", nonblock=True)
    try:
        lock.lock()
    except lock.Locked:
        fatal("a previous backup is still in progress")

    if conf.s3_parallel_uploads > 1 and conf.s3_parallel_uploads > (conf.volsize / 5):
        warn("s3-parallel-uploads > volsize / 5 (minimum upload chunk is 5MB)")

    hb = hub.Backups(registry.sub_apikey)

    if not conf.profile:
        conf.profile = get_profile(hb)

    if not conf.address:
        try:
            registry.credentials = hb.get_credentials()
        except hb.Error, e:
            # asking for get_credentials() might fail if the hub is down.
            # But If we already have the credentials we can survive that.

            if isinstance(e, hub.NotSubscribedError):
                fatal(e)

            if not registry.credentials:
                pass

            warn(e)

        conf.credentials = registry.credentials

        if registry.hbr:
            try:
                registry.hbr = hb.get_backup_record(registry.hbr.backup_id)
            except hb.Error, e:
                # if the Hub is down we can hope that the cached address
                # is still valid and warn and try to backup anyway.
                #
                # But if we reach the Hub and it tells us the backup is invalid
                # we must invalidate the cached backup record and start over.

                if isinstance(e, hub.InvalidBackupError):
                    warn("old backup record deleted, creating new ... ")
                    registry.hbr = None
                else:
                    warn(e)

        if not registry.hbr:
            registry.hbr = hb.new_backup_record(registry.key,
                                                get_turnkey_version(),
                                                get_server_id())

        conf.address = registry.hbr.address

    if opt_resume:
        # explicit resume
        if conf.simulate:
            fatal("--resume and --simulate incompatible")

        if registry.backup_resume_conf is None:
            fatal("no previous backup session to resume from")

        conf = registry.backup_resume_conf
    else:
        # implicit resume
        if not conf.simulate and registry.backup_resume_conf == conf:
            opt_resume = True

    if opt_resume:
        print "ATTEMPTING TO RESUME ABORTED SESSION"

    registry.backup_resume_conf = None
    if not conf.simulate:
        registry.backup_resume_conf = conf

    is_hub_address = not conf.simulate and registry.hbr and registry.hbr.address == conf.address
    backup_id = registry.hbr.backup_id

    b = backup.Backup(conf, resume=opt_resume)
    try:
        trap = UnitedStdTrap(transparent=True)
        try:
            hooks.backup.pre()
            if is_hub_address:
                hb.set_backup_inprogress(backup_id, True)

            if opt_debug:
                trap.close()
                trap = None

            b.run(opt_debug)
            hooks.backup.post()
        finally:
            if is_hub_address:
                hb.set_backup_inprogress(backup_id, False)

            if trap:
                trap.close()
                fh = file(opt_logfile, "a")

                timestamp = "### %s ###" % datetime.datetime.now().ctime()
                print >> fh, "#" * len(timestamp)
                print >> fh, timestamp
                print >> fh, "#" * len(timestamp)

                fh.write(trap.std.read())
                fh.close()
    except:
        if not conf.checkpoint_restore:
            b.cleanup()

        # not cleaning up
        raise

    if conf.simulate:
        print "Completed --simulate: Leaving /TKLBAM intact so you can manually inspect it"
    else:
        b.cleanup()

    registry.backup_resume_conf = None

    if not conf.simulate:
        hb.updated_backup(conf.address)

if __name__=="__main__":
    main()
