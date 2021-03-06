#!/usr/bin/env python
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import argparse
import subprocess
import logging
import multiprocessing
import os
import sys
import yum

log = logging.getLogger()
log.setLevel(logging.DEBUG)
ch = logging.StreamHandler(sys.stdout)
ch.setLevel(logging.DEBUG)
formatter = logging.Formatter('%(asctime)s %(levelname)s: %(message)s')
ch.setFormatter(formatter)
log.addHandler(ch)


def parse_opts(argv):
    parser = argparse.ArgumentParser("Tool to let you know what packages need"
                                     "updating in a list of containers")
    parser.add_argument('-c', '--containers',
                        help="""File containing a list of containers to inspect.""",
                        default='container_list')
    parser.add_argument('-p', '--process-count',
                        help="""Number of processes to use in the pool when running docker containers.""",
                        default=multiprocessing.cpu_count())
    parser.add_argument('-r', '--rpm-list',
                        help="""File containing a list of the latest available rpms.""",
                        default="rpm_list")
    parser.add_argument('-u', '--update',
                        action='store_true',
                        help="""Run yum update in any containers that need updating.""",
                        default=False)
    opts = parser.parse_args(argv[1:])

    return opts


def rm_container(name):
    log.info('Removing container: %s' % name)
    subproc = subprocess.Popen(['/usr/bin/docker', 'rm', name],
                               stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE)
    cmd_stdout, cmd_stderr = subproc.communicate()
    if cmd_stdout:
        log.debug(cmd_stdout)
    if cmd_stderr and \
           cmd_stderr != 'Error response from daemon: ' \
           'No such container: {}\n'.format(name):
        log.debug(cmd_stderr)


def populate_container_rpms_list((container)):

    dcmd = ['/usr/bin/docker', 'run',
            '--user', 'root',
            '--rm',
            container]

    dcmd.extend(['rpm', '-qa'])

    log.info('Running docker command: %s' % ' '.join(dcmd))

    subproc = subprocess.Popen(dcmd, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE)
    cmd_stdout, cmd_stderr = subproc.communicate()
    if subproc.returncode != 0:
        log.error('Failed running rpm -qa for %s' % container)
        log.error(cmd_stderr)

    rpms = cmd_stdout.split("\n")

    return (subproc.returncode, container, rpms)


def yum_update_container((container, name)):

    container_name = 'yum-update-%s' % name

    rm_container(container_name)

    dcmd = ['/usr/bin/docker', 'run',
            '--user', 'root',
            '--net', 'host',
            '--volume', os.getcwd() + '/etc/yum.repos.d:/etc/yum.repos.d',
            '--name', container_name,
            container]

    dcmd.extend(['yum', '-y', 'update'])

    log.info('Running docker command: %s' % ' '.join(dcmd))

    subproc = subprocess.Popen(dcmd, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE)
    cmd_stdout, cmd_stderr = subproc.communicate()
    if subproc.returncode != 0:
        log.error('Failed running yum update for %s' % container)
        log.error(cmd_stderr)
        rm_container(container_name)
        return (subproc.returncode, container)

    dcmd = ['/usr/bin/docker', 'commit',
            '-m', 'automatic yum update',
            container_name,
            container]

    log.info('Running docker command: %s' % ' '.join(dcmd))

    subproc = subprocess.Popen(dcmd, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE)
    cmd_stdout, cmd_stderr = subproc.communicate()
    if subproc.returncode != 0:
        log.error('Failed running docker commit in %s' % container)
        log.error(cmd_stderr)

    rm_container(container_name)

    return (subproc.returncode, container)


def get_available_rpms():
    available_rpms = {}
    yb = yum.YumBase()
    yb.doConfigSetup(fn='etc/yum.conf', root='./',
            init_plugins=True, debuglevel=4, errorlevel=None)
    yb.setCacheDir()
    pkglist = yb.doPackageLists(pkgnarrow='all')
    for pkg in pkglist.available:
        # This gives us a string the same as rpm -qa
        available_rpms[pkg.name + '-' + pkg.vra] = 1
    return available_rpms


if __name__ == '__main__':
    opts = parse_opts(sys.argv)

    # Load up available rpms as a hash containing the latest versions of rpms.
    available_rpms = get_available_rpms()

    # Get a list of all the docker containers we need to inspect.
    docker_containers = [line.rstrip('\n') for line in open(opts.containers)]

    # Holds all the information for each process to consume.
    # Instead of starting them all linearly we run them using a process
    # pool.
    process_map = []
    for container in docker_containers:
        process_map.append(container)

    # This is what we're after here, a hash keyed by containers, each entry
    # containing a list of rpms in that container.
    container_rpms = {}
    success = True
    # Fire off processes to perform each rpm list.
    p = multiprocessing.Pool(int(opts.process_count))
    ret = list(p.map(populate_container_rpms_list, process_map))
    for returncode, container, rpms in ret:
        container_rpms[container] = rpms
        if returncode != 0:
            log.error('ERROR running rpm query in container: %s' % container)
            success = False

    if not success:
        sys.exit(1)

    container_update_list = {}

    for container in container_rpms:
        for rpm in container_rpms[container]:
            if len(rpm) > 0 and not rpm in available_rpms:
                if container not in container_update_list:
                    container_update_list[container] = []
                container_update_list[container].append(rpm)

    for container in container_update_list:
        log.info("Container needs updating: %s" % container)
        for rpm in container_update_list[container]:
            log.info("  rpm: %s" % rpm)

    # And finally update the containers if required
    if opts.update:
        process_map = []
        name = 0
        for container in container_update_list:
            process_map.append([container, str(name)])
            name += 1

        ret = list(p.map(yum_update_container, process_map))
        for returncode, container in ret:
            if returncode != 0:
                log.error('ERROR running yum update in container %s' % container)
                success = False
        if not success:
            sys.exit(1)
