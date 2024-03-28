#include <unistd.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <fcntl.h>
#include <linux/fb.h>
#include <sys/mman.h>
#include <sys/ioctl.h>
#include <sys/uio.h>
#include <time.h>

#define MAXBUFFER 256

typedef struct {
    //AUTO START : struct CarData
    uint32_t wrc;
    uint32_t fuel_ctr;
    uint32_t odometer;
    uint32_t scflags;
    uint32_t lat;
    uint32_t lon;
    uint16_t wrc1;
    uint16_t wrc2;
    uint16_t mga_rpm;
    uint16_t mgb_rpm;
    uint16_t speed;
    uint16_t hv_amps;
    uint16_t mga_amps;
    uint16_t mgb_amps;
    uint16_t hv_volts;
    uint16_t mga_volts;
    uint16_t mgb_volts;
    uint16_t steer;
    uint16_t engine_rpm;
    uint16_t ev_range_rem;
    uint16_t ccspeed;
    uint8_t brake;
    uint8_t accel;
    uint8_t climate_power;
    uint8_t climate_mode;
    uint8_t heat_ac;
    uint8_t battery_raw_soc;
    uint8_t battery_soc;
    uint8_t clutch_state;
    uint8_t ccbtn;
    uint8_t radiobtn;
    uint8_t coolant_temp;
    uint8_t intake_temp;
    uint8_t battery_temp;
    uint8_t air_temp1;
    uint8_t air_temp2;
    uint8_t air_pressure;
    uint8_t tire_ft_lf;
    uint8_t tire_rr_lf;
    uint8_t tire_ft_rt;
    uint8_t tire_rr_rt;
    uint8_t oil_life;
    uint8_t fanspeed;
    uint8_t vent;
    uint8_t select_fanspeed;
    uint8_t select_temp;
    uint8_t recirc;
    uint8_t gear;
    uint8_t drive_mode;
    uint8_t rear_defrost;
    //AUTO END
} cardata_t;

typedef struct buffer_t {
    int buffer_len;
    int word_pos;
    int bit_pos;
    int in_frame;
    int last_char;
    unsigned long long last_stx_time;
    uint8_t buffer[MAXBUFFER];
} buffer_t;

static inline uint16_t unpack_15(uint16_t val) {
    uint16_t hb = (((val >> 14) & 3) * 3 + ((val >> 6) & 3)) - 4;
    return (val & 0x3F3F) | ((hb & 4) << 12) | ((hb & 3) << 6);
}

static int _bitstream_send_buffer(buffer_t* self, int fd) {
    char stx = 2;
    char etx = 3;
    struct iovec iov[3];
    iov[0].iov_base = &stx;
    iov[0].iov_len = 1;
    iov[1].iov_base = self->buffer;
    iov[1].iov_len = self->buffer_len;
    iov[2].iov_base = &etx;
    iov[2].iov_len = 1;
    return writev(fd, iov, 3);
}

static void _bitstream_unpack_15(buffer_t* self) {
    int nw = self->buffer_len >> 1;
    uint16_t* cpos = (uint16_t*)self->buffer;

    do {
        *cpos = unpack_15(*cpos);
        cpos++;
    } while (--nw);
}

static int _bitstream_parse_data(buffer_t* self, PyObject *buf, int *ppos, int len) {
    Py_buffer inbuf;

    if (PyObject_GetBuffer(buf, &inbuf, PyBUF_SIMPLE) == -1) {
        return -1;
    }

    int pos = *ppos;

    if (pos < 0 || len < 0 || pos + len > inbuf.len) {
        PyErr_SetString(PyExc_IndexError, "invalid argument to parse_data");
        PyBuffer_Release(&inbuf);
    }

    uint8_t* rbuf = ((uint8_t*)inbuf.buf);
    int last_char = self->last_char;
    while (len) {
        uint8_t c = rbuf[pos++];
        if (c == 2) {
            if (last_char != 2) {
                struct timespec ts;
                /* Be very explicit in making sure signed overflow doesn't occur */
                unsigned long long seconds, millis;
                clock_gettime(CLOCK_MONOTONIC, &ts);
                seconds = ts.tv_sec;
                millis = ts.tv_nsec / 1000000ULL;
                millis += (seconds * 1000ULL);
                self->last_stx_time = millis;
            }
            self->in_frame = 1;
            self->buffer_len = 0;
        } else if (c == 10) {
            if (self->in_frame) {
                self->in_frame = 0;
                self->word_pos = 0;
                self->bit_pos = 0;

                if (self->buffer_len >= 2) {
                    self->last_char = last_char;
                    PyBuffer_Release(&inbuf);
                    *ppos = pos;
                    return 1;
                }
            }
        } else {
            if (self->in_frame) {
                if (self->buffer_len < MAXBUFFER) {
                    ((uint8_t*)self->buffer)[self->buffer_len++] = c;
                }
            }
        }
        last_char = c;
        len--;
    }
    self->last_char = last_char;
    PyBuffer_Release(&inbuf);
    *ppos = pos;
    return 0;
}

// from http://stackoverflow.com/questions/10564491/function-to-calculate-a-crc16-checksum
static uint16_t crc16(const uint8_t* data_p, int length) {
    uint8_t x;
    uint16_t crc = 0xFFFF;

    while (length--){
        x = crc >> 8 ^ *data_p++;
        x ^= x>>4;
        crc = (crc << 8) ^ ((uint16_t)(x << 12)) ^ ((uint16_t)(x <<5)) ^ ((uint16_t)x);
    }
    return crc;
}

#define CURWORD() (self->word_pos < self->buffer_len ? *(uint16_t*)&self->buffer[self->word_pos] : 0)

static uint32_t _bitstream_read_bits(buffer_t* self, int nbits) {
    uint32_t mask = nbits == 32 ? 0xFFFFFFFF : (1 << nbits) - 1;
    uint32_t rv = CURWORD() >> self->bit_pos;
    int bits_copied = 15 - self->bit_pos;
    self->bit_pos += nbits;
    if (self->bit_pos < 15)
        return rv & mask;

    nbits -= bits_copied;

    while (nbits > 0) {
        self->word_pos += 2;
        rv |= CURWORD() << bits_copied;
        bits_copied += 15;
        nbits -= 15;
    }
    if (nbits == 0) {
        self->bit_pos = 0;
        self->word_pos += 2;
    } else {
        self->bit_pos = nbits + 15;
    }
    return rv & mask;
}

static int32_t _bitstream_read_bits_signed(buffer_t* self, int nbits) {
    int32_t rv = _bitstream_read_bits(self, nbits);
    if ((rv & (1L << (nbits - 1))) != 0) {
        rv -= 1L << nbits;
    }
    return rv;
}

uint16_t _bitstream_calc_crc(buffer_t* self) {
    return crc16(&self->buffer[2], self->buffer_len - 2) & 0x7FFF;
}

int _bitstream_parse_cardata(buffer_t* self, PyObject* cdo, PyObject* lcdo, int expect_frameseq) {
    Py_buffer cdv, lcdv;

    if (PyObject_GetBuffer(cdo, &cdv, PyBUF_WRITEABLE | PyBUF_SIMPLE) == -1) {
        return -2;
    }
    if (cdv.len < sizeof(cardata_t)) {
        PyErr_SetString(PyExc_ValueError, "cd buffer size too small");
        PyBuffer_Release(&cdv);
        return -2;
    }

    if (PyObject_GetBuffer(lcdo, &lcdv, PyBUF_WRITEABLE | PyBUF_SIMPLE) == -1) {
        PyBuffer_Release(&cdv);
        return -2;
    }

    if (lcdv.len < sizeof(cardata_t)) {
        PyErr_SetString(PyExc_ValueError, "lcd buffer size too small");
        PyBuffer_Release(&cdv);
        PyBuffer_Release(&lcdv);
        return -2;
    }

    cardata_t* cd = (cardata_t*)cdv.buf;
    cardata_t* lcd = (cardata_t*)lcdv.buf;

    int full_update = _bitstream_read_bits(self, 1);
    if (full_update) {
        expect_frameseq = 0;
    } else {
        int frameseq = _bitstream_read_bits(self, 4);
        if (frameseq != expect_frameseq) {
            PyBuffer_Release(&cdv);
            PyBuffer_Release(&lcdv);
            return -1;
        }
        expect_frameseq = (frameseq + 1) & 0xF;
    }

#define _PV(bits, field, m)                             \
    if (full_update) {                                  \
        cd->field = m(self, bits);                      \
    } else {                                            \
        int updated = _bitstream_read_bits(self, 1);    \
        if (updated) {                                  \
            cd->field = m(self, bits);                  \
        } else {                                        \
            cd->field = lcd->field;                     \
        }                                               \
    }


#define PV(bits, field) _PV(bits, field, _bitstream_read_bits)
#define PVS(bits, field) _PV(bits, field, _bitstream_read_bits_signed)

    //AUTO START : _bitstream_parse_cardata
    PV(25, wrc);
    PV(13, wrc1);
    PV(13, wrc2);
    PVS(15, mga_rpm);
    PVS(15, mgb_rpm);
    PV(14, speed);
    PVS(16, hv_amps);
    PVS(16, mga_amps);
    PVS(16, mgb_amps);
    PV(16, hv_volts);
    PV(16, mga_volts);
    PV(16, mgb_volts);
    PVS(16, steer);
    PV(8, brake);
    PV(8, accel);
    PV(14, engine_rpm);
    PV(21, fuel_ctr);
    PV(7, climate_power);
    PV(2, climate_mode);
    PV(2, heat_ac);
    PV(8, battery_raw_soc);
    PV(8, battery_soc);
    PV(25, odometer);
    PV(16, ev_range_rem);
    PV(24, scflags);
    PV(8, clutch_state);
    PV(13, ccspeed);
    PV(4, ccbtn);
    PV(4, radiobtn);
    PV(8, coolant_temp);
    PV(8, intake_temp);
    PV(8, battery_temp);
    PVS(31, lat);
    PVS(31, lon);
    PV(8, air_temp1);
    PV(8, air_temp2);
    PV(8, air_pressure);
    PV(8, tire_ft_lf);
    PV(8, tire_rr_lf);
    PV(8, tire_ft_rt);
    PV(8, tire_rr_rt);
    PV(8, oil_life);
    PV(8, fanspeed);
    PV(3, vent);
    PV(5, select_fanspeed);
    PV(6, select_temp);
    PV(2, recirc);
    PV(3, gear);
    PV(2, drive_mode);
    PV(1, rear_defrost);
    //AUTO END

    PyBuffer_Release(&cdv);
    PyBuffer_Release(&lcdv);
    return expect_frameseq;
}
