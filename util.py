###
# XEN CLEAN INSTALLER
# Utilty functions for the clean installer
#
# written by Andrew Peace
# Copyright XenSource Inc. 2006

import os
import os.path
import xelogging
import commands
import subprocess
import urllib2
import shutil

###
# directory/tree management

def assertDir(dirname):
    # make sure there isn't already a file there:
    assert not (os.path.exists(dirname) and not os.path.isdir(dirname))

    # does the specified directory exist?
    if not os.path.isdir(dirname):
        os.makedirs(dirname)

def assertDirs(*dirnames):
    for d in dirnames:
        assertDir(d)
        
def copyFile(source, dest):
    assert os.path.isfile(source)
    assert os.path.isdir(dest)
    
    assert runCmd("cp -f %s %s/" % (source, dest)) == 0

def copyFilesFromDir(sourcedir, dest):
    assert os.path.isdir(sourcedir)
    assert os.path.isdir(dest)

    files = os.listdir(sourcedir)
    for f in files:
        assert runCmd("cp -a %s/%s %s/" % (sourcedir, f, dest)) == 0

def rmtree(path):
    assert os.path.exists(path)
    if not os.path.isdir(path):
        os.unlink(path)
    else:
        for f in os.listdir(path):
            rmtree(os.path.join(path, f))
        os.rmdir(path)

###
# shell

def runCmd(command):
    (rv, output) = commands.getstatusoutput(command)
    xelogging.logOutput(command, output)
    return rv

def runCmd2(command):
    cmd = subprocess.Popen(command,
                           stdout = subprocess.PIPE,
                           stderr = subprocess.PIPE)
    rv = cmd.wait()

    out = ""
    err = ""

    nextout = cmd.stdout.read()
    while nextout:
        out += nextout
        nextout = cmd.stdout.read()

    nexterr = cmd.stderr.read()
    while nexterr:
        err += nexterr
        nexterr = cmd.stderr.read()

    output = "STANDARD OUT:\n" + out + \
             "STANDARD ERR:\n" + err
    
    xelogging.logOutput(command, output)
    return rv

def runCmdWithOutput(command):
    (rv, output) = commands.getstatusoutput(command)
    xelogging.logOutput(command, output)
    return (rv, output)

###
# mounting/unmounting

class MountFailureException(Exception):
    pass

def mount(dev, mountpoint, options = None, fstype = None):
    cmd = ['/bin/mount']
    if options:
        assert type(options) == list

    if fstype:
        cmd.append('-t')
        cmd.append(fstype)

    if options:
        cmd.append("-o")
        cmd.append(",".join(options))

    cmd.append(dev)
    cmd.append(mountpoint)

    rc = subprocess.Popen(cmd, stdout = subprocess.PIPE,
                          stderr = subprocess.PIPE).wait()
    if rc != 0:
        raise MountFailureException()

def bindMount(source, mountpoint):
    cmd = [ '/bin/mount', '--bind', source, mountpoint]
    rc = subprocess.Popen(cmd, stdout = subprocess.PIPE,
                          stderr = subprocess.PIPE).wait()
    if rc != 0:
        raise MountFailureException()

def umount(mountpoint, force = False):
    if force:
        assert os.path.ismount(mountpoint)
    elif not os.path.ismount(mountpoint):
        return

    rc = subprocess.Popen(['/bin/umount', mountpoint],
                          stdout = subprocess.PIPE,
                          stderr = subprocess.PIPE).wait()
    assert rc == 0

###
# fetching of remote files

class InvalidSource(Exception):
    pass

# source may be
#  http://blah
#  ftp://blah
#  file://blah
#  nfs://server:/path/blah
def fetchFile(source, dest):
    unmount = []
    
    try:
        # if it's NFS, then mount the NFS server then treat like
        # file://:
        if source[:4] == 'nfs:':
            # work out the components:
            [_, server, path] = source.split(':')
            if server[:2] != '//':
                raise InvalidSource("Did not start {ftp,http,file,nfs}://")
            server = server[2:]
            dirpart = os.path.dirname(path)
            if dirpart[0] != '/':
                raise InvalidSource("Directory part of NFS path was not an absolute path.")
            filepart = os.path.basename(path)

            # make sure the mountpoint exists
            if not os.path.exists("/tmp/nfsmount"):
                os.mkdir("/tmp/nfsmount")

            mount('%s:%s' % (server, dirpart), "/tmp/nfsmount")
            source = 'file:///tmp/nfsmount/%s' % filepart

        if source[:5] == 'http:' or \
               source[:5] == 'file:' or \
               source[:4] == 'ftp:':
            # This something that can be fetched using urllib2:
            fd = urllib2.urlopen(source)
            fd_dest = open(dest, 'w')
            shutil.copyfileobj(fd, fd_dest)
            fd_dest.close()
            fd.close()
        else:
            raise InvalidSource("Unknown source type.")

    finally:
        # make sure we unmount anything we mounted:
        for m in unmount:
            umount(m)