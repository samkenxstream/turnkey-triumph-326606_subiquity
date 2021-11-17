# Copyright 2021 Canonical, Ltd.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import datetime
import os
import shutil
import tempfile

from curtin.util import write_file

import yaml

from subiquitycore.lsb_release import lsb_release
from subiquitycore.utils import arun_command

from subiquity.server.curtin import run_curtin_command


class AptConfigurer:

    def __init__(self, app, source):
        self.app = app
        self.source = source
        self.configured = None
        self._mounts = []
        self._tdirs = []

    def tdir(self):
        d = tempfile.mkdtemp()
        self._tdirs.append(d)
        return d

    async def mount(self, device, mountpoint, options=None, type=None):
        opts = []
        if options is not None:
            opts.extend(['-o', options])
        if type is not None:
            opts.extend(['-t', type])
        await self.app.command_runner.run(
            ['mount'] + opts + [device, mountpoint])
        self._mounts.append(mountpoint)

    async def unmount(self, mountpoint):
        await self.app.command_runner.run(['umount', mountpoint])

    async def setup_overlay(self, source, target):
        tdir = self.tdir()
        w = f'{tdir}/work'
        u = f'{tdir}/upper'
        for d in w, u:
            os.mkdir(d)
        await self.mount(
            'overlay', target, type='overlay',
            options=f'lowerdir={source},upperdir={u},workdir={w}')
        return u

    async def configure(self, context):
        # Configure apt so that installs from the pool on the cdrom are
        # preferred during installation but not in the installed system.
        #
        # First we create an overlay ('configured') over the installation
        # source and configure that overlay as we want the target system to
        # end up by running curtin's apt-config subcommand.
        #
        # Then we create a fresh overlay ('for_install') over the first one
        # and configure it for the installation. This means:
        #
        # 1. Bind-mounting /cdrom into this new overlay.
        #
        # 2. When the network is expected to be working, copying the original
        #    /etc/apt/sources.list to /etc/apt/sources.list.d/original.list.
        #
        # 3. writing "deb file:///cdrom $(lsb_release -sc) main restricted"
        #    to /etc/apt/sources.list.
        #
        # 4. running "apt-get update" in the new overlay.
        #
        # When the install is done we try to make the installed system's apt
        # state look as if the pool had never been configured. So this means:
        #
        # 1. Removing /cdrom from the installed system.
        #
        # 2. Copying /etc/apt from the 'configured' overlay to the installed
        #    system.
        #
        # 3. If the network is working, run apt-get update in the installed
        #    system, or if it is not, just copy /var/lib/apt/lists from the
        #    'configured' overlay.

        self.configured = self.tdir()

        config_upper = await self.setup_overlay(self.source, self.configured)

        config = {
            'apt': self.app.base_model.mirror.config,
            }
        config_location = os.path.join(
            self.app.root, 'var/log/installer/subiquity-curtin-apt.conf')

        datestr = '# Autogenerated by Subiquity: {} UTC\n'.format(
            str(datetime.datetime.utcnow()))
        write_file(config_location, datestr + yaml.dump(config))

        self.app.note_data_for_apport("CurtinAptConfig", config_location)

        await run_curtin_command(
            self.app, context, 'apt-config', '-t', self.configured,
            config=config_location)

        for_install = self.tdir()
        await self.setup_overlay(config_upper + ':' + self.source, for_install)

        os.mkdir(f'{for_install}/cdrom')
        await self.mount('/cdrom', f'{for_install}/cdrom', options='bind')

        if self.app.base_model.network.has_network:
            os.rename(
                f'{for_install}/etc/apt/sources.list',
                f'{for_install}/etc/apt/sources.list.d/original.list')
        else:
            proxy_path = f'{for_install}/etc/apt/apt.conf.d/90curtin-aptproxy'
            if os.path.exists(proxy_path):
                os.unlink(proxy_path)

        codename = lsb_release()['codename']

        write_file(
            f'{for_install}/etc/apt/sources.list',
            f'deb [check-date=no] file:///cdrom {codename} main restricted\n')

        await run_curtin_command(
            self.app, context, "in-target", "-t", for_install,
            "--", "apt-get", "update")

        return for_install

    async def deconfigure(self, context, target):
        await self.unmount(f'{target}/cdrom')
        os.rmdir(f'{target}/cdrom')

        restore_dirs = ['etc/apt']
        if not self.app.base_model.network.has_network:
            restore_dirs.append('var/lib/apt/lists')
        for dir in restore_dirs:
            shutil.rmtree(f'{target}/{dir}')
            await self.app.command_runner.run([
                'cp', '-aT', f'{self.configured}/{dir}', f'{target}/{dir}',
                ])

        if self.app.base_model.network.has_network:
            await run_curtin_command(
                self.app, context, "in-target", "-t", target,
                "--", "apt-get", "update")

        for m in reversed(self._mounts):
            await self.unmount(m)
        for d in self._tdirs:
            shutil.rmtree(d)
        if self.app.base_model.network.has_network:
            await run_curtin_command(
                self.app, context, "in-target", "-t", target,
                "--", "apt-get", "update")


class DryRunAptConfigurer(AptConfigurer):

    async def setup_overlay(self, source, target):
        if source.startswith('u+'):
            # Please excuse the obscure way the path is transmitted
            # from the first invocation of this method to the second :/
            source = source.split(':')[0][2:]
        os.mkdir(f'{target}/etc')
        await arun_command([
            'cp', '-aT', f'{source}/etc/apt', f'{target}/etc/apt',
            ], check=True)
        if os.path.isdir(f'{target}/etc/apt/sources.list.d'):
            shutil.rmtree(f'{target}/etc/apt/sources.list.d')
        os.mkdir(f'{target}/etc/apt/sources.list.d')
        return 'u+' + target

    async def deconfigure(self, context, target):
        return


def get_apt_configurer(app, source):
    if app.opts.dry_run:
        return DryRunAptConfigurer(app, source)
    else:
        return AptConfigurer(app, source)
