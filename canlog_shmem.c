#include <sys/types.h>
#include <sys/stat.h>
#include <fcntl.h>
#include <stdlib.h>
#include <errno.h>

#include <stdio.h>
#include <string.h>
#include <termios.h>
#include <unistd.h>
#include <sys/epoll.h>
#include <time.h>
#include <signal.h>
#include <sys/mman.h>
#include <sys/ioctl.h>
#include <zlib.h>


#define CANLOG_NUMRECORDS 65536
#define CANLOG_SHM_SIZE (sizeof(canlog_record) * (CANLOG_NUMRECORDS))

#define BUFLEN 2048

#define D(fmt, ...) if (debug_enable) fprintf(stderr, fmt "\n", ## __VA_ARGS__)
static int debug_enable = 0;
static int debug_text_output = 0;


static uint8_t buf[BUFLEN];

static int frame_count;
static int drop_count;
static int sink_mode = 0;

static int bufpos;

static int got_exit_sig = 0;

static const char file_header[] = "CANLOG1\n";

static const char start_command[] = "\xe7\rU\r";
static const char exit_command[] = "\ru\rBINSERIAL=0\r";

static int devfd = -1;
static struct termios termios_orig, termios_new;

static gzFile output_file;

typedef struct canlog_record {
    uint8_t data[8];
    uint64_t time;
    uint32_t id;
    uint8_t bus_len;
    uint8_t pad[3];
} canlog_record;

typedef struct {
    uint32_t read_pointer;
    uint32_t write_pointer;
} canlog_shmem_hdr;

typedef union {
    canlog_shmem_hdr header;
    canlog_record frames[CANLOG_NUMRECORDS];
} canlog_shmem;

static canlog_shmem* shmdata;
static uint32_t *read_pointer;
static uint32_t *write_pointer;


static int get_write_pointer() {
    return __atomic_load_n(write_pointer, __ATOMIC_ACQUIRE);
}

static int get_read_pointer() {
    return __atomic_load_n(read_pointer, __ATOMIC_ACQUIRE);
}

static void init_write_pointer() {
    uint32_t new_write_pointer = get_write_pointer();
    if (new_write_pointer == 0 || new_write_pointer >= CANLOG_NUMRECORDS) {
        __atomic_store_n(write_pointer, 1, __ATOMIC_SEQ_CST);
    }

    /* Make sure the read pointer is within bounds. If the reader hasn't started yet, the
     * pointer might be 0. Do a compare_exchange to make absolutely sure we don't stomp on
     * a valid reader. */

    uint32_t old_read_pointer = get_read_pointer();
    do {
        if (old_read_pointer != 0 && old_read_pointer < CANLOG_NUMRECORDS)
            return;
    } while ((__atomic_compare_exchange_n(read_pointer, &old_read_pointer, 1, 0, __ATOMIC_SEQ_CST, __ATOMIC_SEQ_CST) == 0));
}
static void init_read_pointer() {
    uint32_t new_read_pointer = get_write_pointer();
    if (new_read_pointer == 0 || new_read_pointer >= CANLOG_NUMRECORDS) {
        new_read_pointer = 1;
    }

    __atomic_store_n(read_pointer, new_read_pointer, __ATOMIC_SEQ_CST);
}

static int advance_write_pointer(uint32_t old_write_pointer) {
    int new_write_pointer;
    new_write_pointer = old_write_pointer + 1;
    if (new_write_pointer >= CANLOG_NUMRECORDS) new_write_pointer = 1;
    if (new_write_pointer == get_read_pointer())
        return 0;

    __atomic_store_n(write_pointer, new_write_pointer, __ATOMIC_RELEASE);

    return 1;
}

static int advance_read_pointer(int new_read_pointer) {
    __atomic_store_n(read_pointer, new_read_pointer, __ATOMIC_RELEASE);
    return 1;
}

static int open_shmem(const char* shmpath) {
    int shmfd = open(shmpath, O_RDWR | O_CREAT, 0644);
    if (shmfd < 0) {
        perror("Could not open or create shared memory");
        return 0;
    }
    off_t pos = lseek(shmfd, 0, SEEK_END);
    if (pos < CANLOG_SHM_SIZE) {
        ftruncate(shmfd, CANLOG_SHM_SIZE);
    }
    shmdata = (canlog_shmem*)mmap(0, CANLOG_SHM_SIZE, PROT_READ | PROT_WRITE, MAP_SHARED, shmfd, 0);
    if (shmdata == MAP_FAILED) {
        perror("Could not map shared memory");
        return 0;
    }
    read_pointer = &shmdata->header.read_pointer;
    write_pointer = &shmdata->header.write_pointer;
    return 1;
}

static int writeall(int fd, const char* buf, int len) {
    int cp = 0;

    time_t sttime = time(NULL);
    while (cp < len) {
        int w = write(fd, &buf[cp], len - cp);
        if (w < 0) {
            if ((errno == EAGAIN || errno == EINTR) && time(NULL) < sttime + 5)
                continue;
            return -1;
        }
        cp += w;
    }
    return len;
}

static void cleanup_source() {
    int flags;
    fprintf(stderr, "canlog source: received %d frames, dropped %d\n", frame_count, drop_count);

    fcntl(devfd, F_GETFL, &flags);
    flags &= ~O_NONBLOCK;
    fcntl(devfd, F_SETFL, &flags);

    if (writeall(devfd, exit_command, sizeof(exit_command) - 1) < 0) {
        perror("cleanup write to pipe");
    }
}

static void cleanup_sink() {
    fprintf(stderr, "canlog sink: wrote %d frames\n", frame_count);
    gzclose(output_file);
}

static void setsigs(void* h) {
    signal(SIGINT, h);
    signal(SIGPIPE, h);
    signal(SIGTERM, h);
    signal(SIGHUP, h);
    signal(SIGQUIT, h);
}

static void exitsig(int sig) {
    got_exit_sig = sig;
    fprintf(stderr, "canlog %s: exiting due to signal %d\n", sink_mode ? "sink" : "source", sig);
    setsigs(SIG_DFL);
}

static void check_exit_sig() {
    if (got_exit_sig) {
        if (sink_mode) {
            cleanup_sink();
        } else {
            cleanup_source();
        }
        raise(got_exit_sig);
    }
}

static uint64_t gettime() {
    struct timespec ctime;
    clock_gettime(CLOCK_REALTIME, &ctime);
    return ((uint64_t)ctime.tv_sec * 1000) + (ctime.tv_nsec / 1000000);
}

static int process_frame(uint8_t* buf, int nr) {
    if (nr < 12) {
        return -1;
    }
    int framelength = buf[10] & 0x0F;
    int whichbus = (buf[10] >> 4) & 3;

    if (nr < 12 + framelength) {
        return -1;
    }

    uint64_t millis = gettime();
    uint32_t write_ptr = get_write_pointer();
    canlog_record* cwp = &shmdata->frames[write_ptr];

    cwp->time = millis;
    cwp->bus_len = buf[10];
    cwp->id = (buf[6]) | (buf[7] << 8) | (buf[8] << 16) | (buf[9] << 24);
    memcpy(cwp->data, buf + 11, framelength);

    frame_count += 1;

    if (!advance_write_pointer(write_ptr)) {
        drop_count += 1;
    }

    return 12 + framelength;
}

static void usage(const char* argv0) {
    fprintf(stderr, "usage: %s <shmfile> (source <device> | sink <output>)\n", argv0);
}


static int run_source() {
    int last_fc = 0;
    time_t last_fc_time = 0;

    struct epoll_event ev_usb;
    struct epoll_event rd_event;

    int pollfd = epoll_create(2);
    ev_usb.events = EPOLLIN | EPOLLHUP | EPOLLERR;
    ev_usb.data.u32 = 0;

    epoll_ctl(pollfd, EPOLL_CTL_ADD, devfd, &ev_usb);

    while (1) {
        check_exit_sig();
        time_t ctime = time(NULL);
        if (last_fc != frame_count && ctime != last_fc_time) {
            D("received %d frames, dropped %d", frame_count, drop_count);
            last_fc = frame_count;
            last_fc_time = ctime;
        }
        int numevt = epoll_wait(pollfd, &rd_event, 1, 10000);
        check_exit_sig();
        if (numevt < 0) {
            perror("epoll");
            cleanup_source();
            return 1;
        }
        if (numevt == 0) {
            continue;
        }
        int nr = read(devfd, &buf[bufpos], BUFLEN - bufpos);
        check_exit_sig();
        if (nr <= 0) {
            if (nr < 0) {
                if ((errno == EAGAIN || errno == EINTR))
                    continue;
                perror("read devfd");
                cleanup_source();
                return 0;
            } else {
                fprintf(stderr, "no data read\n");
                continue;
            }
        }
        int cpos = 0;
        nr += bufpos;
        do {
            if (buf[cpos] == 0xF1) {
                int skip = process_frame(buf + cpos, nr - cpos);
                if (skip == -1) {
                    D("incomplete frame, moving");
                    if (cpos != 0) {
                        memmove(&buf[0], &buf[cpos], nr - cpos);
                        bufpos = nr - cpos;
                    }
                    break;
                } else {
                    bufpos = 0;
                    cpos += skip;
                }
            } else {
                bufpos = 0;
                cpos++;
            }
        } while(cpos < nr);
    }
    return 0;
}

static int run_sink() {
    uint32_t read_pointer = get_read_pointer();
    uint64_t last_write_millis = 0;
    uint64_t delta;

    int outbuflen;
    uint8_t outbuf[32];

    char dbgout[256];

    uint64_t next_flush = 0;
    uint64_t bytes_written = 8;

    while(1) {
        uint32_t write_pointer = get_write_pointer();
        uint64_t millis = gettime();
        check_exit_sig();
        if (next_flush != 0 && millis >= next_flush) {
            D("flush output, frames = %d, bytes = %lld", frame_count, bytes_written);
            gzflush(output_file, Z_PARTIAL_FLUSH);
            next_flush = 0;
        }

        if (write_pointer == read_pointer || write_pointer < 1 || write_pointer > CANLOG_NUMRECORDS) {
            usleep(100000);
            continue;
        }

        while (read_pointer != write_pointer) {
            canlog_record* cwp = &shmdata->frames[read_pointer];
            uint64_t time = cwp->time;
            int whichbus = (cwp->bus_len >> 4) & 3;
            int framelen = (cwp->bus_len) & 15;
            int extended = cwp->id >> 31;
            int frameid = cwp->id & 0x7FFFFFFF;

            /* realtime clock sometimes moves backward due to naive time sync algorithm */
            if (time < last_write_millis) {
                time = last_write_millis;
            }

            outbuflen = 0;
            delta = time - last_write_millis;
            last_write_millis = time;

            uint64_t wdelta = delta;
            do {
                outbuf[outbuflen++] = 0x80 | (wdelta & 0x7F);
                wdelta >>= 7;
            } while (wdelta);
            outbuf[outbuflen - 1] &= 0x7F;

            outbuf[outbuflen++] = (extended << 7) | (whichbus << 4) | framelen;
            outbuf[outbuflen++] = frameid & 0xFF;
            outbuf[outbuflen++] = (frameid >> 8) & 0xFF;
            if (extended) {
                outbuf[outbuflen++] = (frameid >> 16) & 0xFF;
                outbuf[outbuflen++] = (frameid >> 24) & 0xFF;
            }


            memcpy(&outbuf[outbuflen], cwp->data, framelen);
            outbuflen += framelen;
            if (debug_text_output) {
                int j;
                int pos = snprintf(dbgout, 256, "t=%lld dt=%4lld id=%08x ln=%d b=%d e=%d:", time, delta, frameid, framelen, whichbus, extended);
                for (j = 0; j < outbuflen; j++) {
                    pos += snprintf(dbgout + pos, 256 - pos, " %02X", outbuf[j]);
                }
                dbgout[pos++] = '\n';

                bytes_written += gzwrite(output_file, dbgout, pos);
            } else {
                bytes_written += gzwrite(output_file, outbuf, outbuflen);
            }

            check_exit_sig();

            if (next_flush == 0) {
                D("schedule next flush");
                next_flush = millis + 5000;
            }

            frame_count += 1;

            read_pointer++;
            if (read_pointer >= CANLOG_NUMRECORDS)
                read_pointer = 1;
            advance_read_pointer(read_pointer);
        }
    }
    return 0;
}

int main(int argc, char** argv) {
    if (argc < 3) {
        usage(argv[0]);
        return 1;
    }

    if (strcmp(argv[2], "source") == 0) {
        sink_mode = 0;
    } else if (strcmp(argv[2], "sink") == 0) {
        sink_mode = 1;
    } else {
        usage(argv[0]);
        return 1;
    }

    char* dbgenv = getenv("CANLOG_DEBUG");
    if (dbgenv && atoi(dbgenv) == 1) {
        debug_enable = 1;
        D("debug enabled");
    }

    dbgenv = getenv("CANLOG_TEXT");
    if (dbgenv && atoi(dbgenv) == 1) {
        debug_text_output = 1;
        D("debug text output enabled");
    }

    char* shmpath = argv[1];
    if (!open_shmem(shmpath)) {
        return 1;
    }


    setsigs(exitsig);

    if (sink_mode) {
        char* output = argv[3];

        output_file = gzopen(output, "wb9");
        gzwrite(output_file, file_header, 8);

        init_read_pointer();
        return run_sink();
    } else {
        char* dev = argv[3];

        devfd = open(dev, O_RDWR | O_NONBLOCK);
        if (devfd < 0) {
            perror("could not open device");
            return 1;
        }

        tcgetattr(devfd, &termios_orig);
        memcpy(&termios_new, &termios_orig, sizeof(struct termios));

        termios_new.c_iflag = IGNBRK | IGNPAR;
        termios_new.c_oflag = 0;
        termios_new.c_cflag = CS8 | CLOCAL | CREAD | HUPCL;
        termios_new.c_lflag = 0;
        termios_new.c_cc[VTIME] = 10;
        termios_new.c_cc[VMIN] = 1;
        cfsetospeed(&termios_new, B230400);
        cfsetispeed(&termios_new, B230400);
        tcsetattr(devfd, TCSANOW, &termios_new);
        writeall(devfd, start_command, sizeof(start_command) - 1);

        init_write_pointer();
        return run_source();
    }





}
