#!/usr/bin/python3
import sys
import re
import time
import os
import argparse
import subprocess
import datetime
import traceback
import stat
from os.path import dirname, basename, join, exists, expanduser

PY3 = sys.version_info >= (3, 0)

if PY3:
    strtypes = str
    def utf8open(f, m):
        return open(f, m, encoding='utf-8')
else:
    strtypes = str, unicode
    utf8open = open

class Log(object):
    def __init__(self, pat='', stdout=False, timestdout=False, links=False):
        self.fil = None
        self.pat = pat
        self.curfn = None
        self.stdout = stdout
        self.timestdout = timestdout
        self.bydate = pat and '@@' in pat
        if links is True:
            links = 'today', 'yesterday'

        self.links = links
        if pat and not self.bydate:
            self.fil = utf8open(pat, 'a')

    def _get_file_name(self, cdate):
        return self.pat.replace('@@', cdate)

    def _file_for_utime(self, utime):
        localtime = time.localtime(utime)
        cdate = time.strftime("%Y-%m-%d", localtime)
        return self._get_file_name(cdate)

    def log(self, lines):
        if isinstance(lines, strtypes):
            lines = [lines]
        ctime = time.time()
        localtime = time.localtime(ctime)
        if self.bydate:
            cdate = time.strftime("%Y-%m-%d", localtime)

            cfn = self._get_file_name(cdate)
            if cfn != self.curfn:
                if self.fil:
                    self.fil.close()
                self.curfn = cfn
                self.fil = utf8open(cfn, 'a')
                if self.links:
                    d = dirname(self.curfn)
                    lfn = None
                    utime = ctime
                    for l in self.links:
                        # Ugly hack
                        while cfn == lfn:
                            utime -= 22*3600
                            cfn = self._file_for_utime(utime)
                        lfn = cfn
                        if exists(cfn):
                            lnk = join(d, l)
                            try:
                                os.unlink(lnk)
                            except OSError:
                                pass
                            try:
                                os.symlink(basename(cfn), lnk)
                            except OSError:
                                pass

        stime = time.strftime("%Y-%m-%d %H:%M:%S ", localtime)

        for l in lines:
            if self.stdout:
                if self.timestdout:
                    print(stime + l)
                else:
                    print(l)

        if self.fil:
            for l in lines:
                if not PY3 and isinstance(l, unicode):
                    l = l.encode('utf-8')
                self.fil.write(stime + l + '\n')
            self.fil.flush()


def main():
    p = argparse.ArgumentParser(description='')
    p.add_argument('pipepath', help='path to pipe')
    p.add_argument('logpath', help='log file pattern')
    p.add_argument('-l', '--links', nargs=2, help='link paths for "today" and "yesterday"')
    p.add_argument('-t', '--tee', action='store_true', help='write data to stdout in addition to log file')
    #p.add_argument(help='')
    args = p.parse_args()

    mgr = Log(args.logpath, timestdout=True, stdout=args.tee, links=args.links)

    try:
        st = os.stat(args.pipepath)
        if not stat.S_ISFIFO(st.st_mode):
            print('error: %s exists but is not pipe', file=sys.stderr)
            return
    except FileNotFoundError:
        os.mkfifo(args.pipepath)

    # Open for both reading and writing so the pipe doesn't get EOF when other processes close
    pipefd = os.open(args.pipepath, os.O_RDWR)
    dbuf = b''
    while True:
        data = os.read(pipefd, 256)
        if not data:
            break
        dbuf += data
        lines = dbuf.split(b'\n')
        dbuf = lines.pop()
        mgr.log([line.decode('utf8', 'replace') for line in lines])

if __name__ == '__main__':
    main()
