from distutils.core import Extension, setup

bsmod = Extension('bitstream',
                     libraries=[],
                     #library_dirs=[''],
                     include_dirs=[],
                     sources=['bitstream.c'],
                     depends=['bitstream.inc', 'setup.py'],
                     )

setup(name="BitStream",
      version='1.0',
      author="Katie Stafford",
      author_email="katie@ktpanda.org",
      ext_modules=[bsmod])
