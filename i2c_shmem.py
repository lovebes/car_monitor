import ctypes
from cardata_shmem import ShareableStructure

PIN_HUB = 0
PIN_DC1 = 1
PIN_DC2 = 3
PIN_DISPLAY = 2

PATH = '/dev/shm/i2c_shmem'

class I2CData(ShareableStructure):
    _fields_ = [
        #AUTO START : ctypes CarData fields
        ('volts', ctypes.c_uint32),
        ('current', ctypes.c_uint32),
        ('pin_enable', ctypes.c_uint32 * 8),
        ('pin_input', ctypes.c_uint32),
    ]

    def enable_hub(self, value):
        self.pin_enable[PIN_HUB] = 1 if value else 0

    def enable_dc1(self, value):
        self.pin_enable[PIN_DC1] = 1 if value else 0

    def enable_dc2(self, value):
        self.pin_enable[PIN_DC2] = 1 if value else 0

    def enable_display(self, value):
        # Display power is inverted
        self.pin_enable[PIN_DISPLAY] = 0 if value else 1

    @property
    def power_button_pressed(self):
        return not (self.pin_input & 0x40)

    @property
    def display_override(self):
        return bool(self.pin_input & 0x20)
