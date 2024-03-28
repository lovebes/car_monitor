
from __future__ import print_function

import sys
import time
import traceback
from os.path import getmtime

try:
    from importlib import reload
except ImportError:
    pass

def load(modname):
    mod = __import__(modname)
    initreload(mod)
    return mod

def initreload(module):
    module.__loadtime = time.time()
    module.__loadfile = module.__file__
    if module.__loadfile.endswith('pyc'):
        module.__loadfile = module.__loadfile[:-1]
    #print('module %s: file=%s' % (module.__name__, module.__loadfile))

def tryreload(module, report_error=True):
    reloaded = False
    try:
        mtime = getmtime(module.__loadfile)
        if  mtime > module.__loadtime:
            if report_error:
                print("Reloading %s (%f, %f)..." % (module.__name__, mtime, module.__loadtime), file=sys.stderr)
            newmod = reload(module)
            newmod.__loadtime = mtime
            module = newmod
            reloaded = True
    except Exception:
        if not report_error:
            raise
        print("Error while reloading %s, old rules still apply:" % module.__name__, file=sys.stderr)
        traceback.print_exc()
    return module, reloaded
