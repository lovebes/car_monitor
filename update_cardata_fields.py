#!/usr/bin/python3
import sys
import re
import time
import os
import argparse
import subprocess
import traceback

UPDATE_FILES = [
    'monitor_hotload.py',
    'cardata_shmem.py',
    'bitstream/bitstream.inc',
]

fields = [
 ('wrc',                   'wrc3',             'int',   25, 0), # 25
 ('wrc1',                  'wrc1',             'int',   13, 0), # 13
 ('wrc2',                  'wrc2',             'int',   13, 0), # 13
 ('mga_rpm',               'mga_rpm',          'int',   15, 1), # 14
 ('mgb_rpm',               'mgb_rpm',          'int',   15, 1), # 14
 ('speed',                 'rawspeed',         'int',   14, 0), # 14
 ('hv_amps',               'hv_amps',          'int',   16, 1), # 16
 ('mga_amps',              'mga_amps',         'int',   16, 1), # 16
 ('mgb_amps',              'mgb_amps',         'int',   16, 1), # 16
 ('hv_volts',              'hv_volts',         'int',   16, 0), # 16
 ('mga_volts',             'mga_volts',        'int',   16, 0), # 16
 ('mgb_volts',             'mgb_volts',        'int',   16, 0), # 16
 ('steer',                 'steer',            'int',   16, 1), # 16
 ('brake',                 'brake_pct',        'int',    8, 0), # 8
 ('accel',                 'accel_pct',        'int',    8, 0), # 8
 ('engine_rpm',            'rpm',              'int',   14, 0), # 14
 ('fuel_ctr',              'fuel_ctr',         'int',   21, 0), # 21
 ('climate_power',         'climate_power',    'short', 7, 0), # 7
 ('climate_mode',          'climate_mode',     'short', 2, 0), # 2
 ('heat_ac',               'heat_ac',          'short', 2, 0), # 2
 ('battery_raw_soc',       'battery_raw_soc',  'int',    8, 0), # 8
 ('battery_soc',           'battery_soc',      'int',    8, 0), # 8
 ('odometer',              'raw_odometer',     'int',   25, 0), # 25
 ('ev_range_rem',          'range',            'int',   16, 0), # 16
 ('scflags',               'scflags',          'long',  24, 0), # 24
 ('clutch_state',          'clutch_state',     'int',   8, 0), # 14
 ('ccspeed',               'rawccspeed',       'int',   13, 0), # 13
 ('ccbtn',                 'ccbtn',            'short', 4, 0), # 4
 ('radiobtn',              'radiobtn',         'short', 4, 0), # 4
 ('coolant_temp',          'coolant_temp',     'int',    8, 0), # 8
 ('intake_temp',           'intake_temp',      'int',    8, 0), # 8
 ('battery_temp',          'battery_temp',     'int',   8, 0), # 8
 ('lat',                   'lat',              'int',   31, 1),
 ('lon',                   'lon',              'int',   31, 1),
 ('air_temp1',             'air_temp1',        'int',    8, 0), # 8
 ('air_temp2',             'air_temp2',        'int',    8, 0), # 8
 ('air_pressure',          'air_pressure',     'int',    8, 0), # 8
 ('tire_ft_lf',            'tire_ft_lf',       'short', 8, 0), # 8
 ('tire_rr_lf',            'tire_rr_lf',       'short', 8, 0), # 8
 ('tire_ft_rt',            'tire_ft_rt',       'short', 8, 0), # 8
 ('tire_rr_rt',            'tire_rr_rt',       'short', 8, 0), # 8
 ('oil_life',              'oil_life',         'short', 8, 0), # 8
 ('fanspeed',              'fanspeed',         'short', 8, 0), # 8
 ('vent',                  'vent',             'short', 3, 0), # 3
 ('select_fanspeed',       'select_fanspeed',  'short', 5, 0), # 5
 ('select_temp',           'select_temp',      'short', 6, 0), # 6
 ('recirc',                'recirc',           'short', 2, 0), # 2
 ('gear',                  'gear',             'int',    3, 0), # 3
 ('drive_mode',            'drive_mode',       'int',    2, 0), # 2
 ('rear_defrost',          'rear_defrost',     'short', 1, 0), # 1
]

log_fields = [
 'wrc3',
 'wrc2',
 'wrc1',
 'hv_amps',
 'hv_volts',
 'mga_rpm',
 'mga_amps',
 'mga_volts',
 'mgb_rpm',
 'mgb_amps',
 'mgb_volts',
 'rawspeed',
 'steer',
 'brake_pct',
 'accel_pct',
 'range',
 'rpm',
 'fuel_ctr',
 'battery_soc',
 'battery_raw_soc',
 'motion_state',
 'gear',
 'scflags',
 'clutch_state',
 'raw_odometer',
 'coolant_temp',
 'intake_temp',
 'battery_temp',
 'lat',
 'lon',
 'air_temp1',
 'air_temp2',
 'air_pressure',
 'vent',
 'select_fanspeed',
 'select_temp',
 'recirc',
 'climate_mode',
 'climate_power',
 'tire_ft_lf',
 'tire_rr_lf',
 'tire_ft_rt',
 'tire_rr_rt',
 'heat_ac',
 'rear_defrost',
 'fanspeed',
 'rawccspeed',
 'ccbtn',
 'radiobtn',
 'drive_mode',
 'oil_life'
]

bigfirst = []
for mcname, logname, dtype, bits, signed in fields:
    if bits > 16:
        typbits = 32
    elif bits > 8:
        typbits = 16
    else:
        typbits = 8
    bigfirst.append((typbits, mcname, logname, dtype, bits, signed))

    bigfirst.sort(key=lambda v: -v[0])

field_by_name = {v[1]:v for v in fields}

rxautostart = re.compile(r'^(\s*)(//|#)AUTO START : (.*)$')
rxautoend = re.compile(r'^(\s*)(//|#)AUTO END')

def modify_file(path, all_changes, all_seen_changes):
    with open(path, 'r') as fp:
        orig_data = list(fp)

    new_data = []

    seen_changes = []

    current_data = None
    current_indent = ''
    for line in orig_data:
        if current_data:
            m = rxautoend.match(line)
            if m:
                new_data.extend(current_indent + v + '\n' for v in current_data)
                new_data.append(line)
                current_data = None
        else:
            new_data.append(line)
            m = rxautostart.match(line)
            if m:
                current_indent = m.group(1)
                current_hdr = m.group(3).strip()
                current_data = all_changes.get(current_hdr)
                seen_changes.append(current_hdr)
                all_seen_changes.add(current_hdr)
                if not current_data:
                    print('%s: WARNING: unknown section %s' % (path, current_hdr))

    if current_data:
        print('%s: ERROR: unterminated section %s' % (path, current_hdr))
        return

    if new_data != orig_data:
        print('updating %s [%s]...' % (path, ', '.join(seen_changes)))
        with open(path + '~', 'w') as fp:
            for line in new_data:
                fp.write(line)
        os.rename(path + '~', path)
    else:
        print('no updates for %s' % path)

def add_struct_changes(changes):
    cc = changes['struct CarData'] = []
    for typbits, mcname, logname, dtype, bits, signed in bigfirst:
        cc.append('uint%d_t %s;' % (typbits, mcname))

def add_m2ret_changes(changes):
    cc = changes['build_data_frame'] = []
    for mcname, logname, dtype, bits, signed in fields:
        cc.append('PV(%d, %s);' % (bits, mcname))

def add_monitor_changes(changes):
    cc = changes['ctypes CarData fields'] = []
    for typbits, mcname, logname, dtype, bits, signed in bigfirst:
        cc.append('(%r, ctypes.c_%sint%d),' % (logname, ('' if signed else 'u'), typbits))

    cc = changes['monitor_hotload CarDataLogger row_order'] = []
    for logname in log_fields:
        cc.append('%r,' % (logname))

    cc = changes['_bitstream_parse_cardata'] = []
    for mcname, logname, dtype, bits, signed in fields:
        cc.append('PV%s(%d, %s);' % ('S' if signed else '', bits, mcname))

    cc = changes['custom_monitor handle_data_frame'] = []
    for mcname, logname, dtype, bits, signed in fields:
        cc.append('update_cd(bs, %r, %d, %r, cd, lcd, full)' % (logname, bits, bool(signed)))

def do_updates(files, changes):
    all_seen_changes = set()
    for f in files:
        modify_file(f, changes, all_seen_changes)

    unused = changes.keys() - all_seen_changes
    if unused:
        print('UNUSED SECTIONS: %s' % ', '.join(unused))

def main():
    changes = {}

    add_struct_changes(changes)
    add_monitor_changes(changes)

    do_updates(UPDATE_FILES, changes)


if __name__ == '__main__':
    main()
