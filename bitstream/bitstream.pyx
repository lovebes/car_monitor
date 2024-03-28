cdef extern from "bitstream.inc":
    struct buffer_t:
        int buffer_len
        int word_pos
        int bit_pos
        int in_frame
        int last_char
        char* buffer
        unsigned long long last_stx_time

    unsigned int _bitstream_read_bits(buffer_t* self, int nbits)
    int _bitstream_read_bits_signed(buffer_t* self, int nbits)
    int _bitstream_parse_data(buffer_t* self, object buf, int* ppos, int len) except -1
    int _bitstream_parse_cardata(buffer_t* self, object cd, object lcd, int expect_seq) except -2
    unsigned short _bitstream_calc_crc(buffer_t* self)
    int _bitstream_send_buffer(buffer_t* self, int fd)
    void _bitstream_unpack_15(buffer_t* self)

cdef extern from "py3k_compat.h":
    int STRING_Check(object obj)
    int STRING_AsStringAndSize(object obj, char **buffer, Py_ssize_t *buffer_len) except -1
    object STRING_FromStringAndSize(char* str, Py_ssize_t len)
    void PyErr_SetFromErrno(object typ) except *

cdef class BitStream:
    cdef buffer_t data

    def __cinit__(self):
        self.data.buffer_len = 0
        self.data.word_pos = 0
        self.data.bit_pos = 0
        self.data.in_frame = 0
        self.data.last_char = 0

    @property
    def stxtime(self):
        return self.data.last_stx_time

    def parse_data(self, buf, pos, len):
        cdef int cpos
        cpos = pos
        rv = _bitstream_parse_data(&self.data, buf, &cpos, len)
        return rv, cpos

    def getbuffer(self):
        return STRING_FromStringAndSize(<char*>self.data.buffer, self.data.buffer_len)

    def unpack_15(self):
        _bitstream_unpack_15(&self.data)

    def send_buffer(self, fd):
        return _bitstream_send_buffer(&self.data, fd)

    def read_bits(self, nbits):
        return _bitstream_read_bits(&self.data, nbits)

    def read_bits_signed(self, nbits):
        return _bitstream_read_bits_signed(&self.data, nbits)

    def reset_write(self):
        self.data.buffer_len = 0

    def reset_read(self):
        self.data.word_pos = 0
        self.data.bit_pos = 0

    def calc_crc(self):
        return _bitstream_calc_crc(&self.data)

    def parse_cardata(self, cd, lcd, expect_seq):
        return _bitstream_parse_cardata(&self.data, cd, lcd, expect_seq);
