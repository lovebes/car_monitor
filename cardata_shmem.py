import os
import ctypes
import mmap
import weakref

class ShareableStructure(ctypes.Structure):
    _fields_ = []

    @classmethod
    def create(cls, path):
        fd = mm = None
        def closemm(self):
            if mm is not None:
                mm.close()
        try:
            size = ctypes.sizeof(cls)
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
            fsize = os.lseek(fd, 0, 2)
            if fsize < size:
                os.ftruncate(fd, size)
            mm = mmap.mmap(fd, size)
            self = cls.from_buffer(mm)
            self._wr = weakref.ref(self, closemm)
            self._mmap = mm
            mm = None
        finally:
            if fd is not None:
                os.close(fd)

        return self

class CarData(ShareableStructure):
    _fields_ = [
        #AUTO START : ctypes CarData fields
        ('wrc3', ctypes.c_uint32),
        ('fuel_ctr', ctypes.c_uint32),
        ('raw_odometer', ctypes.c_uint32),
        ('scflags', ctypes.c_uint32),
        ('lat', ctypes.c_int32),
        ('lon', ctypes.c_int32),
        ('wrc1', ctypes.c_uint16),
        ('wrc2', ctypes.c_uint16),
        ('mga_rpm', ctypes.c_int16),
        ('mgb_rpm', ctypes.c_int16),
        ('rawspeed', ctypes.c_uint16),
        ('hv_amps', ctypes.c_int16),
        ('mga_amps', ctypes.c_int16),
        ('mgb_amps', ctypes.c_int16),
        ('hv_volts', ctypes.c_uint16),
        ('mga_volts', ctypes.c_uint16),
        ('mgb_volts', ctypes.c_uint16),
        ('steer', ctypes.c_int16),
        ('rpm', ctypes.c_uint16),
        ('range', ctypes.c_uint16),
        ('rawccspeed', ctypes.c_uint16),
        ('brake_pct', ctypes.c_uint8),
        ('accel_pct', ctypes.c_uint8),
        ('climate_power', ctypes.c_uint8),
        ('climate_mode', ctypes.c_uint8),
        ('heat_ac', ctypes.c_uint8),
        ('battery_raw_soc', ctypes.c_uint8),
        ('battery_soc', ctypes.c_uint8),
        ('clutch_state', ctypes.c_uint8),
        ('ccbtn', ctypes.c_uint8),
        ('radiobtn', ctypes.c_uint8),
        ('coolant_temp', ctypes.c_uint8),
        ('intake_temp', ctypes.c_uint8),
        ('battery_temp', ctypes.c_uint8),
        ('air_temp1', ctypes.c_uint8),
        ('air_temp2', ctypes.c_uint8),
        ('air_pressure', ctypes.c_uint8),
        ('tire_ft_lf', ctypes.c_uint8),
        ('tire_rr_lf', ctypes.c_uint8),
        ('tire_ft_rt', ctypes.c_uint8),
        ('tire_rr_rt', ctypes.c_uint8),
        ('oil_life', ctypes.c_uint8),
        ('fanspeed', ctypes.c_uint8),
        ('vent', ctypes.c_uint8),
        ('select_fanspeed', ctypes.c_uint8),
        ('select_temp', ctypes.c_uint8),
        ('recirc', ctypes.c_uint8),
        ('gear', ctypes.c_uint8),
        ('drive_mode', ctypes.c_uint8),
        ('rear_defrost', ctypes.c_uint8),
        #AUTO END
        ('motion_state', ctypes.c_uint8),
        ('fw_millis', ctypes.c_uint32),
    ]

    odometer = 0
    odometer_km = 0
    trip_distance = 0
    trip_ev_distance = 0
