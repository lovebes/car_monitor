#!/usr/bin/python3
import sys
import re
import time
import os
import stat
import struct
import argparse
import subprocess
import traceback
import functools
import json
import socket
import select
import fcntl
import threading
import copy

import urllib.request
import urllib.error

from utils import load_config, CONFIG, getmtime

from os.path import dirname, basename, join, exists, expanduser, splitext

TIMEOUT = 10
FAIL_TIMEOUT = 90
BLOCK_SIZE = 1024 * 1024

uint64 = struct.Struct('>Q')
uint32 = struct.Struct('>I')
uint16 = struct.Struct('>H')

RX_DIGIT = re.compile(r'\d+')

def version_compare(s):
    return RX_DIGIT.sub(lambda m: m.group(0).rjust(15, '0'), s)

def read_http_status(s):
    statusline = None
    rbuf = b''
    while True:
        r = s.recv(256)
        if not r:
            return ''
        rbuf += r
        nlines = rbuf.split(b'\n')
        rbuf = nlines.pop()
        for line in nlines:
            line = line.rstrip(b'\r\n')
            if statusline is None:
                statusline = line
            if not line:
                return statusline

def _put_data(srcaddr, name, dstfn, cpos, data, totalsize, modtime):
    path = CONFIG['upload_path'].format(copyname=name, dstfn=dstfn, start=cpos, size=len(data), totalsize=totalsize, modtime=modtime, key=CONFIG['key'])
    host = CONFIG['upload_host']
    header = 'PUT %s HTTP/1.1\r\nHost: %s\r\nConnection: close\r\nContent-type: application/octet-stream\r\nContent-length: %d\r\n\r\n' % (path, host, len(data))
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(TIMEOUT)
        if srcaddr:
            s.bind((srcaddr, 0))

        s.connect((host, CONFIG['upload_port']))
        s.sendall(header.encode('utf8'))
        s.sendall(data)

        statusline = read_http_status(s)

        if not statusline:
            raise urllib.error.HTTPError('http://%s%s' % (host, path), 500, 'no response received', {}, None)

        resp = statusline.decode('utf8', 'ignore').strip().split(' ', 2)
        try:
            vers, code, msg = resp
            code = int(code)
            if code != 200:
                raise urllib.error.HTTPError('http://%s%s' % (host, path), code, msg, {}, None)
        except ValueError as v:
            raise urllib.error.HTTPError('http://%s%s' % (host, path), 500, 'malformed response received', {}, None)

def put_data(srcaddr_file, name, dstfn, cpos, data, totalsize, modtime):
    try:
        with open(srcaddr_file) as fp:
            srcaddr = fp.read().strip()
    except FileNotFoundError:
        srcaddr = None

    try:
        _put_data(srcaddr, name, dstfn, cpos, data, totalsize, modtime)
    except socket.error as e:
        print('[%r] IO error: %s' % (srcaddr, e))
        traceback.print_exc()
        raise

def post_data(url, data, timeout):
    u = urllib.request.urlopen(url, data, timeout)
    resp = u.read()
    return resp

rxcomma=re.compile(r'(\d\d\d)(?=\d)')
def addcomma(n):
    n = str(n)
    return rxcomma.sub(r'\1,', n[::-1])[::-1]

def hms(fsecs):
    secs = int(fsecs)
    mins = secs / 60
    secs %= 60
    hrs = mins / 60
    mins %= 60
    return '%02d:%02d:%02d' % (hrs, mins, secs)

def run_ffprobe(fil):
    p = subprocess.Popen(['ffprobe', fil], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    out, err = p.communicate()
    return out.decode('iso8859')

def get_atom_info(eight_bytes):
    try:
        atom_size, atom_type = struct.unpack('>I4s', eight_bytes)
    except struct.error:
        return 0, ''
    return int(atom_size), atom_type.decode('latin1')

recursive_atoms = "moov", "trak", "mdia", "minf", "stbl", "dinf"

def find_atom(fp, startpos, atype):
    while True:
        try:
            fp.seek(startpos)
            atom_size, atom_type = get_atom_info(fp.read(8))
        except OSError:
            return None, None

        if atom_size < 8:
            return None, None

        if atom_type == atype:
            return startpos, atom_size

        startpos += atom_size

def decode_mvhd(fp):
    moov_pos, moov_size = find_atom(fp, 0, 'moov')
    if moov_pos is None:
        raise ValueError('MOOV not found')

    mvhd_pos, mvhd_size = find_atom(fp, moov_pos + 8, 'mvhd')
    if mvhd_pos is None:
        raise ValueError('MVHD not found')

    mvhd = fp.read(mvhd_size - 8)
    if len(mvhd) < 20:
        raise ValueError('MVHD invalid')

    vers = mvhd[0]
    if vers == 1:
        ctime = uint64.unpack_from(mvhd, 4)[0]
        mtime = uint64.unpack_from(mvhd, 12)[0]
        timescale = uint32.unpack_from(mvhd, 20)[0]
        duration = uint64.unpack_from(mvhd, 24)[0]
    else:
        ctime = uint32.unpack_from(mvhd, 4)[0]
        mtime = uint32.unpack_from(mvhd, 8)[0]
        timescale = uint32.unpack_from(mvhd, 12)[0]
        duration = uint32.unpack_from(mvhd, 16)[0]

    if ctime > 2082844800:
        ctime -= 2082844800
    duration = duration / timescale
    return ctime, duration


def strtime(ut):
    lt = time.localtime(ut)
    if lt.tm_isdst:
        ofs = time.altzone
    else:
        ofs = time.timezone
    ofs = ofs // 60
    if ofs < 0:
        sgn = '+'
        ofs = -ofs
    else:
        sgn = '-'
    return '%s%s%02d%02d' % (time.strftime('%F__%H-%M-%S', lt), sgn, ofs // 60, ofs % 60)

rxcreation = re.compile(r'creation_time\s*:\s*(\d{4})-(\d\d)-(\d\d)[ T]+(\d\d):(\d\d):(\d\d)', re.I)

# Attempt to guess what the correct universal time is for the given local time
def local_to_ut(yy, mm, dd, h, m, s):
    ut1 = time.mktime((yy, mm, dd, h, m, s, 0, 0, 0))
    lt = time.localtime(ut1)
    if lt.tm_isdst:
        return time.mktime((yy, mm, dd, h, m, s, 0, 0, 1))
    else:
        return ut1

class VideoFile:
    SEQUENTIAL_MERGE = False
    forcetz = None

    def __init__(self, path, metapath, st):
        self.path = path
        self.metapath = metapath
        self.stat = st

        try:
            with open(metapath, 'r') as fp:
                mdata = json.load(fp)
        except (ValueError, FileNotFoundError):
            mdata = {}

        self.orig_data = copy.deepcopy(mdata)
        self.data = mdata
        self.mergefiles = None

        self.data['origfn'] = basename(path)

        self.data['size'] = st.st_size
        self.data['mtime'] = st.st_mtime

        self.sequence = None
        if self.SEQUENTIAL_MERGE:
            m = re.search('_(\d\d\d)\.', basename(path))
            if m:
                self.sequence = int(m.group(1))
            self.data['sequence'] = self.sequence

        self.duration = self.data.get('duration', 0)
        self.begintime = self.data.get('begintime', 0)
        self.endtime = self.begintime + self.duration
        self.merged = False

    def set_newfn(self, btime, seq, tot):
        strtot = str(tot)
        strseq = str(seq).rjust(len(strtot), '0')
        self.data['newfn'] = ('%s_f%s,%s.mov' % (strtime(btime), strseq, strtot)).lower()

    def save_meta(self, force=False):
        if not force and self.orig_data == self.data:
            return

        with open(self.metapath + '~', 'w') as fp:
            json.dump(self.data, fp)
            os.fsync(fp.fileno())
        os.rename(self.metapath + '~', self.metapath)
        self.orig_data = copy.deepcopy(self.data)

    def fill_meta(self):
        if self.data.get('error'):
            return False

        if self.begintime and self.duration:
            return True

        try:
            with open(self.path, 'rb') as fp:
                ctime, duration = decode_mvhd(fp)
        except Exception as e:
            print('error decoding %s: %s' % (self.path, e))
            self.data['duration'] = 0
            self.data['begintime'] = 0
            self.data['endtime'] = 0
            self.data['error'] = str(e)
            self.save_meta()
            return False
        if self.forcetz is not None:
            ctime -= self.forcetz * 3600
            lt = time.localtime(ctime)
        else:
            lt = time.gmtime(ctime)
            ctime = local_to_ut(*lt[:6])
        print('%s: %7.3fs %s %r' % (self.path, duration, time.strftime('%F %T', lt), self.sequence))
        self.data['duration'] = self.duration = duration
        self.data['endtime'] = self.endtime = ctime
        self.data['begintime'] = self.begintime = ctime - duration

        self.save_meta()
        return True

    def should_merge(self, nextop):
        if 'merge_from' in self.data or 'merge_from' in nextop.data:
            return False

        if self.SEQUENTIAL_MERGE:
            if self.sequence is not None and nextop.sequence is not None and nextop.sequence == self.sequence + 1:
                return True
        else:
            end = self.endtime + 30
            nextbt = nextop.begintime
            return nextbt <= end


    def trymerge(self, nextop):
        if self.should_merge(nextop):
            nextop.merged = True
            if self.mergefiles is None:
                self.mergefiles = [self]
            self.sequence = nextop.sequence
            self.endtime = nextop.endtime
            self.mergefiles.append(nextop)
            return self
        else:
            return nextop

    def domerge(self):
        if 'merge_from' in self.data:
            return

        if not self.mergefiles:
            self.set_newfn(self.begintime, 1, 1)
            return

        merge_data = {}
        #for field in ('timestamp', 'newfn'):
        #    merge_data[field] = self.data[field]

        mergedfrom = merge_data['merge_from'] = []
        nfiles = len(self.mergefiles)

        total_duration = 0
        for i, op in enumerate(self.mergefiles):
            op.data['merge_sequence'] = i
            if i != len(self.mergefiles) - 1:
                # assume exactly 1 second of overlap
                op.data['outpoint'] = op.data['duration'] - 1

            origdata = dict(op.data)
            origdata.pop('merge_from', None)
            origdata.pop('timestamp', None)
            origdata.pop('newfn', None)
            origdata.pop('merge_duration', None)
            origdata.pop('merge_begintime', None)
            origdata.pop('merge_endtime', None)
            duration = origdata.get('duration', 0)
            total_duration += duration

        self.begintime = self.data['begintime'] = self.endtime - total_duration

        for i, op in enumerate(self.mergefiles):
            op.set_newfn(self.begintime, i + 1, nfiles)
            mergedfrom.append(origdata)

        merge_data['merge_duration'] = total_duration
        merge_data['merge_begintime'] = self.begintime
        merge_data['merge_endtime'] = self.begintime + total_duration

        for op in self.mergefiles:
            op.data.update(merge_data)
            op.save_meta()


def generate_meta(srcdir, metadir):
    try:
        os.makedirs(metadir)
    except OSError:
        pass

    seen_meta = set()

    all_files = []

    for fn in os.listdir(srcdir):
        ext = splitext(fn)[1].lower()
        if ext == '.mov' or ext == '.mkv' or ext == '.mp4':
            path = join(srcdir, fn)
            try:
                st = os.lstat(path)
            except IOError:
                continue

            if not stat.S_ISREG(st.st_mode):
                continue

            metafn = fn.lower() + '.json'
            seen_meta.add(metafn)
            metapath = join(metadir, metafn)
            if st.st_size < 4*1048576:
                continue

            vf = VideoFile(join(srcdir, fn), metapath, st)
            if vf.fill_meta():
                all_files.append(vf)
                vf.save_meta()

    last_op = None
    all_files.sort(key=lambda vf: vf.begintime)
    for cur_op in all_files:
        if last_op:
            last_op = last_op.trymerge(cur_op)
        else:
            last_op = cur_op

    for cur_op in all_files:
        if not cur_op.merged:
            cur_op.domerge()

    for fn in os.listdir(metadir):
        if fn.endswith('.json') and fn not in seen_meta:
            os.unlink(join(metadir, fn))

    return all_files


class FileReader(threading.Thread):
    def __init__(self, filename, startpos):
        super().__init__()
        self.filename = filename
        self.startpos = startpos
        self.lock = threading.Lock()
        self.have_data = threading.Condition(self.lock)
        self.got_data = threading.Condition(self.lock)
        self.data = None
        self.error = None

    def run(self):
        try:
            with open(self.filename, 'rb') as fp:
                fp.seek(self.startpos)
                while True:
                    cpos = fp.tell()
                    data = fp.read(BLOCK_SIZE)

                    with self.lock:
                        while self.data is not None and self.filename is not None:
                            self.got_data.wait(1000)
                        if self.filename is None:
                            return
                        self.data = cpos, data
                        self.have_data.notify()
                    if not data:
                        break
        except Exception as e:
            traceback.print_exc()
            with self.lock:
                self.error = e
                self.have_data.notify()

    def get_data(self):
        with self.lock:
            while self.data is None and self.error is None:
                self.have_data.wait(1000)
            if self.error is not None:
                raise self.error
            rv = self.data
            self.data = None
            self.got_data.notify()
        return rv

    def stop(self):
        with self.lock:
            self.filename = None
            self.got_data.notify()


def timeout_retry(nsec, f, *a, **kw):
    etime = getmtime() + nsec
    while True:
        try:
            return f(*a, **kw)
        except urllib.error.HTTPError as e:
            print('HTTP error: %s' % e)
            raise
        except IOError as e:
            rtime = etime - getmtime()
            if rtime < 0:
                raise
            time.sleep(min(rtime, 0.5))

def notify(status, which):
    u = urllib.request.urlopen(CONFIG['upload_notify_url'].format(status=status, copyname=which, key=CONFIG['key']), timeout=3)
    u.read()


def local_query(basepath, do_delete, jdata):
    if jdata is None:
        raise ValueError('no input data supplied')

    srcfiles = jdata['files']
    dstfiles = {}
    for fn in os.listdir(basepath):
        path = join(basepath, fn)
        try:
            st = os.lstat(path)
        except IOError:
            continue
        if stat.S_ISREG(st.st_mode):
            dstfiles[fn] = st.st_size

    total_size = 0

    need_files = {}
    to_delete = []
    all_files = srcfiles.keys() | dstfiles.keys()
    for fn in all_files:
        ssrc = srcfiles.get(fn, 0)
        sdst = dstfiles.get(fn, 0)
        if ssrc > sdst:
            need_files[fn] = sdst
            total_size += ssrc - sdst

        if ssrc == 0 and sdst != 0:
            print('will delete: %s' % join(basepath, fn))
            to_delete.append(fn)
            if do_delete:
                try:
                    os.unlink(join(basepath, fn))
                except FileNotFoundError:
                    print("WTF????? %s" % join(basepath, fn))

    jdata['need'] = need_files
    jdata['delete'] = to_delete
    jdata['total_size'] = total_size

    return jdata



def main():
    print(sys.argv)
    p = argparse.ArgumentParser(description='')
    p.add_argument('srcpath')
    p.add_argument('copyname')
    p.add_argument('--srcaddr-file', default='wifi-addr', help='')
    p.add_argument('-l', '--localpath', help='')
    p.add_argument('-t', '--forcetz', type=int, help='')
    p.add_argument('-s', '--sequential', action='store_true', help='')
    p.add_argument('-n', '--noaction', action='store_true', help='')
    p.add_argument('-N', '--nonotify', action='store_true', help='')
    p.add_argument('-D', '--nodelete', action='store_true', help='')
    p.add_argument('-d', '--cardata', action='store_true', help='')
    args = p.parse_args()

    do_delete = not args.nodelete

    if args.localpath:
        args.localpath = join(args.localpath, args.copyname)

    VideoFile.forcetz = args.forcetz
    VideoFile.SEQUENTIAL_MERGE = args.sequential

    load_config()

    srcfiles = {}
    true_fn = {}

    print('scan %s...' % args.srcpath)
    if args.cardata:
        for fn in os.listdir(args.srcpath):
            if not fn.endswith('.txt.gz'):
                continue

            path = join(args.srcpath, fn)
            try:
                st = os.lstat(path)
            except IOError:
                continue

            if not stat.S_ISREG(st.st_mode):
                continue

            true_fn[fn] = fn
            srcfiles[fn] = st.st_size
    else:
        all_ops = generate_meta(args.srcpath, join(dirname(__file__), 'cvmeta', args.copyname))

        for op in all_ops:
            dstfn = op.data['newfn']
            true_fn[dstfn] = op.data['origfn']
            srcfiles[dstfn] = op.data['size']
            #print('mv %s %s' % (op.data['origfn'].lower(), dstfn))
            #print('mv meta.%s.json meta.%s.json' % (op.data['origfn'].lower(), dstfn))

    if args.localpath:
        response = local_query(args.localpath, False, {'files': srcfiles})
    else:
        print('query server...')
        url = CONFIG['upload_query_url'].format(copyname=args.copyname, key=CONFIG['key'])
        if not args.noaction:
            url += '&delete=%d&save=autocopy' % do_delete

        r = timeout_retry(FAIL_TIMEOUT, post_data, url, json.dumps({'files': srcfiles}).encode('utf8'), timeout=TIMEOUT)
        response = json.loads(r.decode('utf8', 'ignore'))

    need = response['need']

    total_need = 0
    need = sorted(need.items(), key=lambda v: version_compare(v[0]))
    for fn, startpos in need:
        need_amt = max(0, srcfiles.get(fn) - startpos)
        total_need += need_amt
        print('%-30s: %s' % (fn, addcomma(need_amt)))

    print()
    print('%-30s: %s' % ('total', addcomma(total_need)))
    print()
    if args.noaction:
        return

    if not need:
        if not args.nonotify:
            notify('novideo', args.copyname)
        return

    if not args.nonotify:
        notify('start', args.copyname)

    for dstfn, startpos in need:
        fn = true_fn[dstfn]
        path = join(args.srcpath, fn)
        st = os.lstat(path)
        totalsize = st.st_size
        modtime = int(st.st_mtime * 1000)

        reader = FileReader(path, startpos)

        if args.localpath:
            output_file_path = join(args.localpath, dstfn)
            output_fd = os.open(output_file_path, os.O_CREAT | os.O_WRONLY, 0o644)

        try:
            reader.start()
            begin_time = getmtime()
            last_time = getmtime()
            last_delta = 0
            last_size = 0
            total_copied = 0
            while True:
                cpos, data = reader.get_data()
                if last_size:
                    mbps = last_size / last_delta / 1000000.0
                else:
                    mbps = 0
                print('%-30s: %12s / %12s %6.2f MB/s' % (dstfn, addcomma(cpos), addcomma(totalsize), mbps))

                last_size = len(data)
                total_copied += last_size
                if not data:
                    break
                if args.localpath:
                    os.lseek(output_fd, cpos, 0)
                    os.write(output_fd, data)
                else:
                    #url = (BASEURL_COPY % args.copyname) + '%s?start=%d&append=%d&totalsize=%d&modtime=%d' % (dstfn, cpos, len(data), totalsize, modtime)
                    timeout_retry(FAIL_TIMEOUT, put_data, args.srcaddr_file, args.copyname, dstfn, cpos, data, totalsize, modtime)
                ctime = getmtime()
                last_delta = ctime - last_time
                last_time = ctime

            end_time = getmtime()
            mbps = total_copied / (end_time - begin_time) / 1000000.0
            print('%-30s: copied %s in %s, %.2f MB/s' % (dstfn, addcomma(total_copied), hms(end_time - begin_time), mbps))
            print()
        finally:
            if args.localpath:
                os.close(output_fd)
            reader.stop()

    if not args.nonotify:
        notify('finish', args.copyname)

if __name__ == '__main__':
    main()
