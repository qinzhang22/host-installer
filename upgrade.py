# Copyright (c) 2005-2006 XenSource, Inc. All use and distribution of this 
# copyrighted material is governed by and subject to terms and conditions 
# as licensed by XenSource, Inc. All other rights reserved.
# Xen, XenSource and XenEnterprise are either registered trademarks or 
# trademarks of XenSource Inc. in the United States and/or other countries.

###
# XEN CLEAN INSTALLER
# Upgrade paths
#
# written by Andrew Peace

# This stuff exists to hide ugliness and hacks that are required for upgrades
# from the rest of the installer.

import os
import re
import shutil

import diskutil
import product
from xcp.version import *
from disktools import *
from netinterface import *
import util
import constants
import xelogging
import version
import netutil

def upgradeAvailable(src):
    return __upgraders__.hasUpgrader(src.name, src.version, src.variant)

def getUpgrader(src):
    """ Returns an upgrader instance suitable for src. Propogates a KeyError
    exception if no suitable upgrader is available (caller should have checked
    first by calling upgradeAvailable). """
    return __upgraders__.getUpgrader(src.name, src.version, src.variant)(src)

class Upgrader(object):
    """ Base class for upgraders.  Superclasses should define an
    upgrades_product variable that is the product they upgrade, an 
    upgrades_variants list of Retail install types that they upgrade, and an 
    upgrades_versions that is a list of pairs of version extents they support
    upgrading."""

    requires_backup = False
    optional_backup = True
    repartition = False

    def __init__(self, source):
        """ source is the ExistingInstallation object we're to upgrade. """
        self.source = source
        self.restore_list = []

    def upgrades(cls, product, version, variant):
        return (cls.upgrades_product == product and
                variant in cls.upgrades_variants and
                True in [ _min <= version <= _max for (_min, _max) in cls.upgrades_versions ])

    upgrades = classmethod(upgrades)

    prepTargetStateChanges = []
    prepTargetArgs = []
    def prepareTarget(self, progress_callback):
        """ Modify partition layout prior to installation. """
        return

    doBackupStateChanges = []
    doBackupArgs = []
    def doBackup(self, progress_callback):
        """ Collect configuration etc from installation. """
        return

    prepStateChanges = []
    prepUpgradeArgs = []
    def prepareUpgrade(self, progress_callback):
        """ Collect any state needed from the installation, and return a
        tranformation on the answers dict. """
        return

    def buildRestoreList(self):
        """ Add filenames to self.restore_list which will be copied by
        completeUpgrade(). """
        return

    completeUpgradeArgs = ['mounts', 'primary-disk', 'backup-partnum']
    def completeUpgrade(self, mounts, target_disk, backup_partnum):
        """ Write any data back into the new filesystem as needed to follow
        through the upgrade. """

        def restore_file(src_base, f, d = None):
            if not d: d = f
            src = os.path.join(src_base, f)
            dst = os.path.join(mounts['root'], d)
            if os.path.exists(src):
                xelogging.log("Restoring /%s" % f)
                if os.path.isdir(src):
                    util.runCmd2(['cp', '-rp', src, os.path.dirname(dst)])
                else:
                    util.assertDir(os.path.dirname(dst))
                    util.runCmd2(['cp', '-p', src, dst])
            else:
                xelogging.log("WARNING: /%s did not exist in the backup image." % f)

        backup_volume = partitionDevice(target_disk, backup_partnum)
        tds = util.TempMount(backup_volume, 'upgrade-src-', options = ['ro'])
        try:
            self.buildRestoreList()

            xelogging.log("Restoring preserved files")
            for f in self.restore_list:
                if isinstance(f, str):
                    restore_file(tds.mount_point, f)
                elif isinstance(f, dict):
                    if 'src' in f:
                        assert 'dst' in f
                        restore_file(tds.mount_point, f['src'], f['dst'])
                    elif 'dir' in f:
                        pat = 're' in f and f['re'] or None
                        src_dir = os.path.join(tds.mount_point, f['dir'])
                        if os.path.exists(src_dir):
                            for ff in os.listdir(src_dir):
                                fn = os.path.join(f['dir'], ff)
                                if not pat or pat.match(fn):
                                    restore_file(tds.mount_point, fn)
        finally:
            tds.unmount()


class ThirdGenUpgrader(Upgrader):
    """ Upgrader class for series 5 Retail products. """
    upgrades_product = "xenenterprise"
    upgrades_versions = [ (product.XENSERVER_6_0_0, product.THIS_PRODUCT_VERSION) ]
    upgrades_variants = [ 'Retail' ]
    requires_backup = True
    optional_backup = False
    
    def __init__(self, source):
        Upgrader.__init__(self, source)
        primary_fs = util.TempMount(self.source.root_device, 'primary-', options = ['ro'])
        safe2upgrade_path = os.path.join(primary_fs.mount_point, "var/preserve/safe2upgrade")
        if os.path.isfile(safe2upgrade_path):
            self.safe2upgrade = True
        else:
            self.safe2upgrade = False
        self.vgs_output = None
        primary_fs.unmount()

    prepTargetStateChanges = []
    prepTargetArgs = ['primary-disk', 'target-boot-mode', 'boot-partnum', 'primary-partnum', 'logs-partnum', 'swap-partnum', 'storage-partnum', 'partition-table-type']
    def prepareTarget(self, progress_callback, primary_disk, target_boot_mode, boot_partnum, primary_partnum, logs_partnum, swap_partnum, storage_partnum, partition_table_type):
        """ Modify partition layout prior to installation. """

        if partition_table_type == constants.PARTITION_GPT:
            tool = PartitionTool(primary_disk, partition_table_type)

            # Create the new partition layout (5,2,1,4,6,3) after the backup
            # 1 - dom0 partition
            # 2 - backup partition
            # 3 - LVM partition
            # 4 - UEFI partition
            # 5 - logs partition
            # 6 - swap partition

            if self.safe2upgrade:
                # Rename old dom0 and UEFI (if any) partitions (10 and 11 are temporary number which let us create
                # dom0 and UEFI partitions using the same numbers)
                tool.renamePartition(srcNumber = primary_partnum, destNumber = 10, overwrite = False)
                boot_part = tool.getPartition(boot_partnum)
                if boot_part:
                    tool.renamePartition(srcNumber = boot_partnum, destNumber = 11, overwrite = False)
                # Create new bigger dom0 partition
                tool.createPartition(tool.ID_LINUX, sizeBytes = constants.root_size * 2**20, number = primary_partnum)
                # Create UEFI partition
                if target_boot_mode == constants.TARGET_BOOT_MODE_UEFI:
                    tool.createPartition(tool.ID_EFI_BOOT, sizeBytes = constants.boot_size * 2**20, number = boot_partnum)
                else:
                    tool.createPartition(tool.ID_BIOS_BOOT, sizeBytes = constants.boot_size * 2**20, number = boot_partnum)
                # Create swap partition
                tool.createPartition(tool.ID_LINUX_SWAP, sizeBytes = constants.swap_size * 2**20, number = swap_partnum)
                # Create storage LVM partition
                if storage_partnum > 0:
                    tool.createPartition(tool.ID_LINUX_LVM, number = storage_partnum)
                # Create logs partition using the old dom0 + UEFI (if any) partitions
                tool.deletePartition(10)
                if boot_part:
                    tool.deletePartition(11)
                tool.createPartition(tool.ID_LINUX, sizeBytes = constants.logs_size * 2**20, startBytes = 1024*1024, number = logs_partnum)

            else:

                # If the boot partition already, exists, no partition updates are
                # necessary.
                part = tool.getPartition(boot_partnum)
                if part:
                    return

                # Otherwise, replace the root partition with a boot partition and
                # a smaller root partition.
                part = tool.getPartition(primary_partnum)
                tool.deletePartition(primary_partnum)

                boot_size = constants.boot_size * 2**20
                root_size = part['size'] * tool.sectorSize - boot_size
                if target_boot_mode == constants.TARGET_BOOT_MODE_UEFI:
                    tool.createPartition(tool.ID_EFI_BOOT, sizeBytes = boot_size, startBytes = part['start'] * tool.sectorSize, number = boot_partnum)
                else:
                    tool.createPartition(tool.ID_BIOS_BOOT, sizeBytes = boot_size, startBytes = part['start'] * tool.sectorSize, number = boot_partnum)

                tool.createPartition(part['id'], sizeBytes = root_size, number = primary_partnum, order = primary_partnum + 1)

            tool.commit(log = True)

            storage_part = partitionDevice(primary_disk, storage_partnum)
            rc, self.vgs_output_wrong  = util.runCmd2(['vgs', '--noheadings', '-o', 'vg_name'], with_stdout = True)
            self.vgs_output_wrong = self.vgs_output_wrong.strip()
            util.runCmd2(['vgremove', '-f', self.vgs_output_wrong])
            util.runCmd2(['vgcreate', self.vgs_output, storage_part])

    doBackupArgs = ['primary-disk', 'backup-partnum', 'boot-partnum', 'storage-partnum', 'partition-table-type']
    doBackupStateChanges = []
    def doBackup(self, progress_callback, target_disk, backup_partnum, boot_partnum, storage_partnum, partition_table_type):

        tool = PartitionTool(target_disk)
        boot_part = tool.getPartition(boot_partnum)
        boot_device = partitionDevice(target_disk, boot_partnum) if boot_part else None

        # Check if possible to create new partition layout, increasing the size, using plugin result
        if self.safe2upgrade:
            # Get current Volume Group
            rc, self.vgs_output  = util.runCmd2(['vgs', '--noheadings', '-o', 'vg_name'], with_stdout = True)
            self.vgs_output = self.vgs_output.strip()
            # Remove current Volume Group
            util.runCmd2(['vgremove', '-f', self.vgs_output])
            # Remove LVM Phisical Volume
            storage_part = partitionDevice(target_disk, storage_partnum)
            util.runCmd2(['pvremove', storage_part])
            # Delete LVM partition
            tool.deletePartition(storage_partnum)
            # Resize backup partition
            tool.resizePartition(number = backup_partnum, sizeBytes = constants.backup_size * 2**20)
            # Write partition table
            tool.commit(log = True)
        
        # format the backup partition:
        backup_partition = partitionDevice(target_disk, backup_partnum)
        if util.runCmd2(['mkfs.ext3', backup_partition]) != 0:
            raise RuntimeError, "Backup: Failed to format filesystem on %s" % backup_partition
        progress_callback(10)

        # copy the files across:
        primary_fs = util.TempMount(self.source.root_device, 'primary-', options = ['ro'], boot_device = boot_device)
        try:
            backup_fs = util.TempMount(backup_partition, 'backup-')
            try:
                just_dirs = ['dev', 'proc', 'lost+found', 'sys']
                top_dirs = os.listdir(primary_fs.mount_point)
                val = 10
                for x in top_dirs:
                    if x in just_dirs:
                        path = os.path.join(backup_fs.mount_point, x)
                        if not os.path.exists(path):
                            os.mkdir(path, 0755)
                    else:
                        cmd = ['cp', '-a'] + \
                              [ os.path.join(primary_fs.mount_point, x) ] + \
                              ['%s/' % backup_fs.mount_point]
                        if util.runCmd2(cmd) != 0:
                            raise RuntimeError, "Backup of %s directory failed" % x
                    val += 90 / len(top_dirs)
                    progress_callback(val)

                if partition_table_type == constants.PARTITION_GPT:
                    # save the GPT table
                    rc, err = util.runCmd2(["sgdisk", "-b", os.path.join(backup_fs.mount_point, '.xen-gpt.bin'), target_disk], with_stderr = True)
                    if rc != 0:
                        raise RuntimeError, "Failed to save partition layout: %s" % err
            finally:
                # replace rolling pool upgrade bootloader config
                def replace_config(config_file, destination):
                    src = os.path.join(backup_fs.mount_point, constants.ROLLING_POOL_DIR, config_file)
                    if os.path.exists(src):
                        util.runCmd2(['cp', '-f', src, os.path.join(backup_fs.mount_point, destination)])

                map(replace_config, ('efi-grub.cfg', 'grub.cfg', 'menu.lst', 'extlinux.conf'),
                                    ('boot/efi/EFI/xenserver/grub.cfg', 'boot/grub',
                                     'boot/grub', 'boot'))

                fh = open(os.path.join(backup_fs.mount_point, '.xen-backup-partition'), 'w')
                fh.close()
                backup_fs.unmount()
        finally:
            primary_fs.unmount()

    prepUpgradeArgs = ['installation-uuid', 'control-domain-uuid']
    prepStateChanges = ['installation-uuid', 'control-domain-uuid']
    def prepareUpgrade(self, progress_callback, installID, controlID):
        """ Try to preserve the installation and control-domain UUIDs from
        xensource-inventory."""
        try:
            installID = self.source.getInventoryValue("INSTALLATION_UUID")
            controlID = self.source.getInventoryValue("CONTROL_DOMAIN_UUID")
        except KeyError:
            raise RuntimeError, "Required information (INSTALLATION_UUID, CONTROL_DOMAIN_UUID) was missing from your xensource-inventory file.  Aborting installation; please replace these keys and try again."

        return installID, controlID

    def buildRestoreList(self):
        self.restore_list += ['etc/xensource/ptoken', 'etc/xensource/pool.conf', 
                              'etc/xensource/xapi-ssl.pem']
        self.restore_list.append({'dir': 'etc/ssh', 're': re.compile(r'.*/ssh_host_.+')})

        self.restore_list += [ 'etc/sysconfig/network', constants.DBCACHE ]
	self.restore_list.append({'src': constants.OLD_DBCACHE, 'dst': constants.DBCACHE})
        self.restore_list.append({'dir': 'etc/sysconfig/network-scripts', 're': re.compile(r'.*/ifcfg-[a-z0-9.]+')})

        self.restore_list += ['var/lib/xcp/state.db', 'etc/xensource/license']
	self.restore_list.append({'src': 'var/xapi/state.db', 'dst': 'var/lib/xcp/state.db'})
        self.restore_list.append({'dir': constants.FIRSTBOOT_DATA_DIR, 're': re.compile(r'.*.conf')})

        self.restore_list += ['etc/xensource/syslog.conf']

        self.restore_list.append({'src': 'etc/xensource-inventory', 'dst': 'var/tmp/.previousInventory'})

        # CP-1508: preserve AD config
        self.restore_list += [ 'etc/resolv.conf', 'etc/nsswitch.conf', 'etc/krb5.conf', 'etc/krb5.keytab', 'etc/pam.d/sshd' ]
        self.restore_list.append({'dir': 'var/lib/likewise'})

        # CA-47142: preserve v6 cache
        self.restore_list += [{'src': 'var/xapi/lpe-cache', 'dst': 'var/lib/xcp/lpe-cache'}]

        # CP-2056: preserve RRDs etc
        self.restore_list += [{'src': 'var/xapi/blobs', 'dst': 'var/lib/xcp/blobs'}]

        self.restore_list.append('etc/sysconfig/mkinitrd.latches')

        # EA-1069: Udev network device naming
        self.restore_list += [{'dir': 'etc/sysconfig/network-scripts/interface-rename-data'}]
        self.restore_list += [{'dir': 'etc/sysconfig/network-scripts/interface-rename-data/.from_install'}]

        # CA-67890: preserve root's ssh state
        self.restore_list += [{'dir': 'root/.ssh'}]

        # CA-82709: preserve networkd.db for Tampa upgrades
        self.restore_list.append({'src': constants.OLD_NETWORK_DB, 'dst': constants.NETWORK_DB})
	self.restore_list.append(constants.NETWORK_DB)

        # CP-9653: preserve Oracle 5 blacklist
        self.restore_list += ['etc/pygrub/rules.d/oracle-5.6']

        # CA-150889: backup multipath config
        self.restore_list.append({'src': 'etc/multipath.conf', 'dst': 'etc/multipath.conf.bak'})

        self.restore_list += ['etc/locale.conf', 'etc/machine-id', 'etc/vconsole.conf']

    completeUpgradeArgs = ['mounts', 'installation-to-overwrite', 'primary-disk', 'backup-partnum', 'net-admin-interface', 'net-admin-bridge', 'net-admin-configuration']
    def completeUpgrade(self, mounts, prev_install, target_disk, backup_partnum, admin_iface, admin_bridge, admin_config):

        util.assertDir(os.path.join(mounts['root'], "var/lib/xcp"))
        util.assertDir(os.path.join(mounts['root'], "etc/xensource"))

        Upgrader.completeUpgrade(self, mounts, target_disk, backup_partnum)

        v = Version(prev_install.version.ver)
        f = open(os.path.join(mounts['root'], 'var/tmp/.previousVersion'), 'w')
        f.write("PRODUCT_VERSION='%s'\n" % v)
        f.close()

        state = open(os.path.join(mounts['root'], constants.FIRSTBOOT_DATA_DIR, 'host.conf'), 'w')
        print >>state, "UPGRADE=true"
        state.close()

        # CP-1508: preserve AD service state
        ad_on = False
        try:
            fh = open(os.path.join(mounts['root'], 'etc/nsswitch.conf'), 'r')
            for line in fh:
                if line.startswith('passwd:') and 'lsass' in line:
                    ad_on = True
                    break
            fh.close()
        except:
            pass

        if ad_on:
            for service in ['dcerpd', 'eventlogd', 'netlogond', 'npcmuxd', 'lsassd']:
                util.runCmd2(['chroot', mounts['root'], 'chkconfig', '--add', service])

        # CA-147442: fix up library paths in AD registry
        try:
            os.mkdir(os.path.join(mounts['root'], constants.FIX_AD_WORK_DIR))
            shutil.copy(os.path.join(constants.INSTALLER_DIR, constants.FIX_AD_REG_PATHS_SCRIPT),
                        os.path.join(mounts['root'], constants.FIX_AD_WORK_DIR))
            util.runCmd2(['chroot', mounts['root'], os.path.join(constants.FIX_AD_WORK_DIR, constants.FIX_AD_REG_PATHS_SCRIPT)])
        except:
            pass
        shutil.rmtree(os.path.join(mounts['root'], constants.FIX_AD_WORK_DIR), ignore_errors = True)

        # The existence of the static-rules.conf is used to detect upgrade from Boston or newer
        if os.path.exists(os.path.join(mounts['root'], 'etc/sysconfig/network-scripts/interface-rename-data/static-rules.conf')):
            # CA-82901 - convert any old style ppn referenced to new style ppn references
            util.runCmd2(['sed', r's/pci\([0-9]\+p[0-9]\+\)/p\1/g', '-i',
                          os.path.join(mounts['root'], 'etc/sysconfig/network-scripts/interface-rename-data/static-rules.conf')])

        # EA-1069: create interface-rename state from old xapi database if it doesnt currently exist (static-rules.conf)
        else:
            if not os.path.exists(os.path.join(mounts['root'], 'etc/sysconfig/network-scripts/interface-rename-data/.from_install/')):
                os.makedirs(os.path.join(mounts['root'], 'etc/sysconfig/network-scripts/interface-rename-data/.from_install/'), 0775)

            from xcp.net.ifrename.static import StaticRules
            sr = StaticRules()
            sr.path = os.path.join(mounts['root'], 'etc/sysconfig/network-scripts/interface-rename-data/static-rules.conf')
            sr.save()
            sr.path = os.path.join(mounts['root'], 'etc/sysconfig/network-scripts/interface-rename-data/.from_install/static-rules.conf')
            sr.save()

            from xcp.net.biosdevname import all_devices_all_names
            from xcp.net.ifrename.dynamic import DynamicRules

            devices = all_devices_all_names()
            dr = DynamicRules()

            # this is a dirty hack but I cant think of much better
            backup_volume = partitionDevice(target_disk, backup_partnum)
            tds = util.TempMount(backup_volume, 'upgrade-src-', options = ['ro'])
            try:
                dbcache_path = constants.DBCACHE
                if not os.path.exists(os.path.join(tds.mount_point, dbcache_path)):
                    dbcache_path = constants.OLD_DBCACHE
                dbcache = open(os.path.join(tds.mount_point, dbcache_path), "r")
                mac_next = False
                eth_next = False

                for line in ( x.strip() for x in dbcache ):

                    if mac_next:
                        dr.lastboot.append([line.upper()])
                        mac_next = False
                        continue

                    if eth_next:
                        # CA-77436 - Only pull real eth devices from network.dbcache, not bonds or other constructs
                        for bdev in devices.values():
                            if line.startswith("eth") and bdev.get('Assigned MAC', None) == dr.lastboot[-1][0] and 'Bus Info' in bdev:
                                dr.lastboot[-1].extend([bdev['Bus Info'], line])
                                break
                        else:
                            del dr.lastboot[-1]
                        eth_next = False
                        continue

                    if line == "<MAC>":
                        mac_next = True
                        continue

                    if line == "<device>":
                        eth_next = True

                dbcache.close()
            finally:
                tds.unmount()

            dr.path = os.path.join(mounts['root'], 'etc/sysconfig/network-scripts/interface-rename-data/dynamic-rules.json')
            dr.save()
            dr.path = os.path.join(mounts['root'], 'etc/sysconfig/network-scripts/interface-rename-data/.from_install/dynamic-rules.json')
            dr.save()

        net_dict = util.readKeyValueFile(os.path.join(mounts['root'], 'etc/sysconfig/network'))
        if 'NETWORKING_IPV6' not in net_dict:
            nfd = open(os.path.join(mounts['root'], 'etc/sysconfig/network'), 'a')
            nfd.write("NETWORKING_IPV6=no\n")
            nfd.close()
            netutil.disable_ipv6_module(mounts["root"])

        # handle the conversion of HP Gen6 controllers from cciss to scsi
        primary_disk = self.source.getInventoryValue("PRIMARY_DISK")
        target_link = diskutil.idFromPartition(target_disk)
        if 'cciss' in primary_disk and 'scsi' in target_link:
            util.runCmd2(['sed', '-i', '-e', "s#%s#%s#g" % (primary_disk, target_link),
                          os.path.join(mounts['root'], 'etc/firstboot.d/data/default-storage.conf')])
            util.runCmd2(['sed', '-i', '-e', "s#%s#%s#g" % (primary_disk, target_link),
                          os.path.join(mounts['root'], 'var/lib/xcp/state.db')])

class XCPUpgrader(ThirdGenUpgrader):
    """ Upgrader class for XCP products. """
    upgrades_product = "XCP"
    upgrades_versions = [ (product.XCP_1_6_0, product.THIS_PLATFORM_VERSION) ]


################################################################################

# Upgraders provided here, in preference order:
class UpgraderList(list):
    def getUpgrader(self, product, version, variant):
        for x in self:
            if x.upgrades(product, version, variant):
                return x
        raise KeyError, "No upgrader found for %s" % version

    def hasUpgrader(self, product, version, variant):
        for x in self:
            if x.upgrades(product, version, variant):
                return True
        return False
    
__upgraders__ = UpgraderList([ ThirdGenUpgrader, XCPUpgrader ])

def filter_for_upgradeable_products(installed_products):
    upgradeable_products = filter(lambda p: p.isUpgradeable() and upgradeAvailable(p),
        installed_products)
    return upgradeable_products
